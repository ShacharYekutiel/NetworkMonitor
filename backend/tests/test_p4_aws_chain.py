"""[4] PR 4-0a credential chain against the STS mock: `AssumeRoleProvider` success and the
refusals (ExternalId, principal, SetSourceIdentity, TagSession, region), refresh 5 minutes
before the 1-hour expiry on a test clock, a valid hop with an expired base, a forced refresh of
hop and base, single flight under 50 concurrent callers, and the `rt.aws` cache keys."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from netmon.aws.credentials import (
    AssumeRoleProvider,
    CachingProvider,
    ChainCachingProvider,
    Credentials,
    StaticProvider,
)
from netmon.aws.errors import AssistantError
from netmon.aws.sts import StsClient
from tests.conftest import requires_db
from tests.p4_aws import AKID, BEDROCK_ROLE, EXT_ID, SECRET, STATIC_ARN, start_mock, trust_model


@pytest.fixture(scope="module")
def mock(tmp_path_factory: pytest.TempPathFactory) -> Any:
    m = start_mock(tmp_path_factory)
    yield m
    m.stop()


class Clock:
    def __init__(self) -> None:
        self.t = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += timedelta(minutes=minutes)


class CountingBase:
    """A base provider with a chosen lifetime (counts its fetches)."""

    source = "access_key"

    def __init__(self, clock: Clock, minutes: float | None) -> None:
        self.clock, self.minutes, self.fetches = clock, minutes, 0

    async def fetch(self) -> Credentials:
        self.fetches += 1
        exp = self.clock() + timedelta(minutes=self.minutes) if self.minutes else None
        return Credentials(AKID, SECRET, None, exp, "access_key", {"arn": STATIC_ARN})


def chain(mock: Any, http: httpx.AsyncClient, clock: Clock, base: Any, ext: str | None = EXT_ID):
    sts = StsClient(http, "us-east-1", mock.url)
    base_cache = CachingProvider(base, now=clock)
    hop = ChainCachingProvider(
        AssumeRoleProvider(base_cache, BEDROCK_ROLE, ext, "netmon-test", sts, now=clock),
        base_cache,
        now=clock,
    )
    return base_cache, hop


def assume_calls(mock: Any) -> int:
    return len(mock.requests("sts_assume_role"))


async def test_assume_role_success_and_refusals(mock: Any) -> None:
    mock.reset()
    clock = Clock()
    async with httpx.AsyncClient(trust_env=False) as http:
        trust_model(mock, caller=STATIC_ARN, require_source_identity=None)
        _, hop = chain(mock, http, clock, StaticProvider(AKID, SECRET))
        creds = await hop.get()
        assert creds.source == "assume_role" and creds.session_token
        assert creds.detail["assumed_role_arn"].endswith(":assumed-role/netmon-bedrock/netmon-test")
        assert creds.expiration is not None
        assert timedelta(minutes=59) < creds.expiration - datetime.now(UTC) <= timedelta(hours=1)
        # the hop credentials work against the service (signature verified by the mock)
        ident = await StsClient(http, "us-east-1", mock.url).get_caller_identity(creds)
        assert ident["account"] == "111122223333"

        async def refused(ext: str | None = EXT_ID) -> AssistantError:
            _, h = chain(mock, http, clock, StaticProvider(AKID, SECRET), ext)
            with pytest.raises(AssistantError) as e:
                await h.get()
            return e.value

        err = await refused("wrong-external-id")
        assert err.code == "aws_assume_role_denied"
        assert err.details[0] == {
            "hop": 1,
            "from_arn": STATIC_ARN,
            "role_arn": BEDROCK_ROLE,
            "action": "sts:AssumeRole",
        }
        trust_model(mock, caller="arn:aws:iam::111122223333:role/someone-else")
        assert (await refused()).details[0]["action"] == "sts:AssumeRole"
        trust_model(mock, caller=STATIC_ARN, require_source_identity=None, require_tag_session=True)
        assert (await refused()).details[0]["action"] == "sts:TagSession"
        trust_model(mock, caller=STATIC_ARN, require_source_identity=None)
        mock.sts_config(disabled_regions=["us-east-1"])
        err = await refused()
        assert err.code == "aws_assume_role_invalid" and err.aws_type == "RegionDisabledException"


async def test_refresh_before_expiry_and_base_expired_hop_valid(mock: Any) -> None:
    mock.reset()
    trust_model(mock, caller=STATIC_ARN, require_source_identity=None)
    clock = Clock()
    base = CountingBase(clock, minutes=10)  # a short-lived base
    async with httpx.AsyncClient(trust_env=False) as http:
        base_cache, hop = chain(mock, http, clock, base)
        n0 = assume_calls(mock)
        first = await hop.get()
        assert assume_calls(mock) == n0 + 1 and base.fetches == 1
        clock.advance(20)  # base expired, hop still valid: no refresh at all
        assert (await hop.get()) is first
        assert base.fetches == 1 and assume_calls(mock) == n0 + 1
        clock.advance(34)  # 54 min: still more than 5 min left
        assert (await hop.get()) is first and assume_calls(mock) == n0 + 1
        # the hop's expiry is the mock's real clock; move the test clock past "expiry - 5 min"
        assert first.expiration is not None
        clock.t = first.expiration - timedelta(minutes=4)
        second = await hop.get()
        assert second is not first and assume_calls(mock) == n0 + 2
        assert base.fetches == 2  # the expired base was refreshed lazily by the hop's fetch
        # an authentication error of a service: one forced refresh of hop AND base
        third = await hop.get(force=True)
        assert third is not second and base.fetches == 3 and assume_calls(mock) == n0 + 3
        assert base_cache.refreshes == 3


async def test_single_flight_under_concurrency(mock: Any) -> None:
    mock.reset()
    trust_model(mock, caller=STATIC_ARN, require_source_identity=None)
    clock = Clock()
    async with httpx.AsyncClient(trust_env=False) as http:
        _, hop = chain(mock, http, clock, StaticProvider(AKID, SECRET))
        n0 = assume_calls(mock)
        out = await asyncio.gather(*(hop.get() for _ in range(50)))
        assert len({id(c) for c in out}) == 1 and assume_calls(mock) == n0 + 1


async def test_sts_expired_base_token_forces_one_base_refresh(mock: Any) -> None:
    """STS answers `ExpiredToken` for the base credentials: the hop refreshes its base once and
    retries (the base cache thought its credentials were still valid)."""
    mock.reset()
    trust_model(mock, caller=STATIC_ARN, require_source_identity=None)
    clock = Clock()
    base = CountingBase(clock, minutes=None)
    async with httpx.AsyncClient(trust_env=False) as http:
        _, hop = chain(mock, http, clock, base)
        mock.mode("expired_token", "sts", times=1)
        creds = await hop.get()
        assert creds.source == "assume_role" and base.fetches == 2


@requires_db
async def test_credential_cache_keys_and_invalidation(rt: Any) -> None:
    """`rt.aws.chain`: one base per connection hash, chains keyed by the access; a save (new
    hash) builds new chains; `invalidate` drops them."""
    from netmon.aws import connections as conns

    row = conns.new_row(
        {
            "name": "k",
            "auth_mode": "access_key",
            "region": "us-east-1",
            "access_key_id": AKID,
            "secret_access_key": SECRET,
        },
        rt.secret_key,
    )
    row["id"] = 4242
    v = conns.view(row)
    cache = rt.aws.cache
    rt.aws.invalidate(4242)
    d1 = rt.aws.chain("assistant", v, {"mode": "direct"})
    assert rt.aws.chain("assistant", v, {"mode": "direct"}) is d1
    h1 = rt.aws.chain(
        "assistant", v, {"mode": "assume_role", "role_arn": BEDROCK_ROLE, "external_id": "a1"}
    )
    h2 = rt.aws.chain(
        "assistant", v, {"mode": "assume_role", "role_arn": BEDROCK_ROLE, "external_id": "a2"}
    )
    assert h1 is not h2 and h1.base is d1 and h2.base is d1  # type: ignore[attr-defined]
    v2 = conns.view(conns.merge(row, {"secret_access_key": "x" * 40}, rt.secret_key))
    assert rt.aws.chain("assistant", v2, {"mode": "direct"}) is not d1
    assert cache.status_for(4242)[0]["state"] == "idle"
    rt.aws.invalidate(4242)
    assert cache.status_for(4242) == [] and not [k for k in cache.chains if k[0] == 4242]
