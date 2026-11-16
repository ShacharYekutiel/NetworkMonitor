"""[4] The chain Test of a connection (+ optional hop) (architecture § 15.1.7).

Steps: `config` (hop null) → `credentials` (hop 0) → `identity` (hop 0, `GetCallerIdentity`)
→ [`hop` (hop 1: `AssumeRole` or a second `CreateSession`) → `hop_identity` (hop 1)]. A failed
step ends the run and carries `hints` (pure `errors.hints_for`). With access `direct` an STS
network error at `identity` is only the warning `sts_unreachable`. Fresh providers and HTTP
clients are used (never the live cache); nothing is stored here."""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from netmon.aws import connections as conns
from netmon.aws import endpoints
from netmon.aws import transport as tp
from netmon.aws.credentials import CachingProvider, Credentials
from netmon.aws.errors import AssistantError, hint, hints_for, make
from netmon.aws.sts import StsClient, account_of, role_arn_of_session, session_name

TEST_BUDGET_S = 45.0


def step(
    name: str, hop: int | None, ok: bool, detail: str, t0: float, code: str | None = None,
    hints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:  # fmt: skip
    return {
        "step": name,
        "hop": hop,
        "ok": ok,
        "detail": detail[:500],
        "ms": int((time.monotonic() - t0) * 1000),
        "error_code": code,
        "hints": hints or [],
    }


class FixedProvider:
    """Credentials obtained by a Test, served to the consumer steps of the same Test."""

    def __init__(self, creds: Credentials) -> None:
        self.creds = creds
        self.source = creds.source

    async def fetch(self) -> Credentials:
        return self.creds

    async def aclose(self) -> None:
        return None


@dataclass
class ChainRun:
    steps: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    creds: Credentials | None = None  # the final credentials (base or hop)
    identity: dict[str, Any] | None = None  # base GetCallerIdentity (account, arn)
    hop_identity: dict[str, Any] | None = None
    ctx: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.creds is not None

    def fail(self, st: dict[str, Any], err: AssistantError) -> None:
        st["ok"] = False
        st["error_code"] = err.code
        st["detail"] = (err.message or err.code)[:500]
        self.steps.append(st)
        self.error = {"code": err.code, "message": err.message}


def _ctx(v: dict[str, Any], secret_key: str) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "trust_anchor_arn": v.get("ra_trust_anchor_arn"),
        "profile_arn": v.get("ra_profile_arn"),
        "role_arn": v.get("ra_role_arn"),
        "base_kind": v.get("auth_mode"),
        "region": v.get("region"),
        "base_role_arn": v.get("ra_role_arn") if v.get("auth_mode") == "roles_anywhere" else None,
    }
    ctx.update(conns.cert_values(v, secret_key))
    return ctx


def static_warnings(v: dict[str, Any], secret_key: str) -> list[str]:
    out: list[str] = []
    if v.get("auth_mode") == "roles_anywhere":
        if conns.arn_account_warning(v):
            out.append("arn_accounts_differ")
        mat = conns.safe_material(v, secret_key)
        if mat is not None:
            from datetime import UTC, datetime

            from netmon.aws import certs

            out += certs.cn_warnings(certs.subject_cn(mat.cert))
            if certs.expiring(mat.cert, datetime.now(UTC)):
                out.append("cert_expiring")
    if v.get("auth_mode") == "access_key" and v.get("session_token_enc"):
        out.append("temporary_credentials")
    for svc in ("sts", "rolesanywhere"):
        url = v.get(f"{'ra' if svc == 'rolesanywhere' else 'sts'}_endpoint_url")
        if url and tp.is_vpce(url) and tp.via(url, v) != "direct":
            out.append("vpce_via_proxy")
    return list(dict.fromkeys(out))


async def run_chain(
    v: dict[str, Any],
    accesses: list[dict[str, Any]],
    *,
    secret_key: str,
    test_mode: bool,
    consumer: str = "test",
    run: ChainRun | None = None,
) -> ChainRun:
    """Steps 1–5 for the connection view `v` and each hop access in `accesses` (empty: base
    only). Never raises AssistantError (failures are steps)."""
    run = run or ChainRun()
    run.ctx = _ctx(v, secret_key)
    run.warnings += static_warnings(v, secret_key)
    hop_accesses = [conns.normalize_access(a) for a in accesses]
    hop_accesses = [a for a in hop_accesses if a["mode"] != "direct"]
    # 1. config
    t0 = time.monotonic()
    errs = conns.field_errors(v, test_mode=test_mode)
    has_cert, has_key = conns.has_cert_and_key(v, secret_key)
    errs += conns.required_errors(v, secret_key, has_cert=has_cert, has_key=has_key)
    for a in hop_accesses:
        errs += conns.access_errors(a, v)
    if errs:
        detail = "; ".join(f"{e['loc'][-1]}: {e['msg']}" for e in errs)
        run.steps.append(step("config", None, False, detail, t0, "validation_error"))
        run.error = {"code": "validation_error", "message": f"Settings incomplete: {detail}"}
        return run
    run.steps.append(step("config", None, True, f"auth {v['auth_mode']}, region {v['region']}", t0))
    http = tp.make_client(v)
    try:
        await _steps(v, hop_accesses, run, http, secret_key, consumer)
    finally:
        with contextlib.suppress(Exception):
            await http.aclose()
    run.warnings = list(dict.fromkeys(run.warnings))
    return run


async def _steps(
    v: dict[str, Any],
    hop_accesses: list[dict[str, Any]],
    run: ChainRun,
    http: Any,
    secret_key: str,
    consumer: str,
) -> None:
    from netmon.services.serialize import iso

    ctx = run.ctx
    region = str(v["region"])
    sts_url = endpoints.sts_endpoint(v)
    sts = StsClient(http, region, sts_url, via=tp.via(sts_url, v))
    # 2. credentials
    t0 = time.monotonic()
    try:
        provider = conns.make_provider(v, http, secret_key)
        base = CachingProvider(provider)
        creds = await base.get()
    except AssistantError as err:
        st = step("credentials", 0, False, "", t0)
        st["hints"] = hints_for(
            "credentials", err, {**ctx, "host": _ra_host(v), "service": "rolesanywhere"}
        )
        run.fail(st, err)
        return
    parts = [f"source {creds.source}"]
    if creds.expiration:
        parts.append(f"expires {iso(creds.expiration)}")
    d = creds.detail or {}
    if d.get("subject_arn"):
        parts.append(f"subject {d['subject_arn']}")
    if d.get("role_arn"):
        parts.append(f"role {d['role_arn']}")
    if d.get("role"):
        parts.append(f"role {d['role']}")
    if d.get("source_identity"):
        parts.append(f"source identity {d['source_identity']}")
        ctx["source_identity"] = d["source_identity"]
    run.steps.append(step("credentials", 0, True, ", ".join(parts), t0))
    if d.get("role_arn"):
        ctx["from_arn"] = d["role_arn"]
    # 3. identity
    t0 = time.monotonic()
    try:
        ident = await sts.get_caller_identity(creds)
    except AssistantError as err:
        if err.code == "aws_network" and not hop_accesses:
            run.warnings.append("sts_unreachable")
            run.steps.append(
                step(
                    "identity", 0, True,
                    f"warning: STS unreachable ({err.message}); not needed for direct access",
                    t0, None,
                    hints_for(
                        "identity", err, {**ctx, "host": tp.host_of(sts_url), "service": "sts"}
                    ),
                )
            )  # fmt: skip
        else:
            st = step("identity", 0, False, "", t0)
            st["hints"] = hints_for(
                "identity", err, {**ctx, "host": tp.host_of(sts_url), "service": "sts"}
            )
            run.fail(st, err)
            return
    else:
        run.identity = ident
        ctx["account"] = ident.get("account")
        ctx["from_arn"] = ident.get("arn") or ctx.get("from_arn")
        if ctx.get("base_role_arn") is None:
            ctx["base_role_arn"] = role_arn_of_session(ident.get("arn"))
        used = v.get("ra_role_arn") if v.get("auth_mode") == "roles_anywhere" else None
        if used and account_of(used) and ident.get("account") != account_of(used):
            run.warnings.append("account_mismatch")
        run.steps.append(
            step("identity", 0, True, f"account {ident.get('account')}, arn {ident.get('arn')}", t0)
        )
    run.creds = creds
    # 4./5. hop(s)
    for access in hop_accesses:
        if not await _hop(v, access, run, http, sts, base, secret_key, consumer):
            run.creds = None
            return


async def _hop(
    v: dict[str, Any],
    access: dict[str, Any],
    run: ChainRun,
    http: Any,
    sts: StsClient,
    base: CachingProvider,
    secret_key: str,
    consumer: str,
) -> bool:
    from netmon.services.serialize import iso

    ctx = {**run.ctx, "hop_role_arn": access["role_arn"]}
    t0 = time.monotonic()
    try:
        if access["mode"] == "assume_role":
            src = await base.get()
            creds = await sts.assume_role(
                src, access["role_arn"], session_name(consumer), access.get("external_id"),
                from_arn=ctx.get("from_arn"),
            )  # fmt: skip
        else:
            prov = conns.make_provider(v, http, secret_key, role_arn=access["role_arn"])
            creds = await prov.fetch()
    except AssistantError as err:
        if err.code == "rolesanywhere_failed":
            ctx = {**ctx, "role_arn": access["role_arn"]}
        sts_hop = access["mode"] == "assume_role"
        host = tp.host_of(sts.endpoint) if sts_hop else _ra_host(v)
        st = step("hop", 1, False, "", t0)
        st["hints"] = hints_for(
            "hop", err, {**ctx, "host": host, "service": "sts" if sts_hop else "rolesanywhere"}
        )
        run.fail(st, err)
        return False
    d = creds.detail or {}
    parts = [f"role {access['role_arn']}"]
    if creds.expiration:
        secs = int((creds.expiration - _now()).total_seconds())
        parts.append(f"expires {iso(creds.expiration)} ({max(secs, 0) // 60} min)")
    if d.get("source_identity"):
        parts.append(f"source identity {d['source_identity']}")
    run.steps.append(step("hop", 1, True, ", ".join(parts), t0))
    t0 = time.monotonic()
    try:
        ident = await sts.get_caller_identity(creds)
    except AssistantError as err:
        st = step("hop_identity", 1, False, "", t0)
        st["hints"] = hints_for(
            "hop_identity", err, {**ctx, "host": tp.host_of(sts.endpoint), "service": "sts"}
        )
        run.fail(st, err)
        return False
    run.hop_identity = ident
    want = account_of(access["role_arn"])
    if want and ident.get("account") != want:
        run.warnings.append("account_mismatch")
    run.steps.append(
        step("hop_identity", 1, True, f"account {ident.get('account')}, arn {ident.get('arn')}", t0)
    )
    run.creds = creds
    return True


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _ra_host(v: dict[str, Any]) -> str:
    return tp.host_of(endpoints.ra_endpoint(v)) if v.get("region") else ""


async def run_chain_test(
    v: dict[str, Any],
    accesses: list[dict[str, Any]],
    *,
    secret_key: str,
    test_mode: bool,
) -> ChainRun:
    """`run_chain` under the 45 s budget."""
    run = ChainRun()
    try:
        await asyncio.wait_for(
            run_chain(v, accesses, secret_key=secret_key, test_mode=test_mode, run=run),
            TEST_BUDGET_S,
        )
    except TimeoutError:
        err = make("assistant_timeout")
        run.error = {"code": err.code, "message": "The test took longer than 45 s."}
        run.creds = None
    return run


def result(run: ChainRun) -> dict[str, Any]:
    """`TestResult` (api.md "[4] Type changes"), without the `test_id`."""
    return {
        "ok": run.ok,
        "test_id": None,
        "expires_at": None,
        "steps": run.steps,
        "warnings": list(dict.fromkeys(run.warnings)),
        "error": run.error,
        "identity": run.identity,
        "hop_identity": run.hop_identity,
    }


__all__ = ["ChainRun", "FixedProvider", "hint", "result", "run_chain", "run_chain_test", "step"]
