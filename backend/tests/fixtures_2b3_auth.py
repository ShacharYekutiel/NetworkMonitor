"""[2b-3] Shared fixtures for the auth / audit / tokens / Teams / AD tests (import them into a
test module: `from tests.fixtures_2b3_auth import feats, fake_ad, ...  # noqa: F401`)."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
LDAP_SIM = REPO / "tests" / "sim" / "ldap"


@pytest.fixture
async def feats(rt: Any, clean: None) -> AsyncIterator[Callable[..., None]]:
    """Turn features on/off directly on the runtime's FeatureManager (no settings write);
    everything is OFF again afterwards."""
    from netmon.services.features import FEATURES, FeatureManager

    if getattr(rt, "features", None) is None:  # before the runtime wires one
        rt.features = FeatureManager(rt)
    rt.features.values = dict.fromkeys(FEATURES, False)

    def set_(**kw: bool) -> None:
        for k, v in kw.items():
            assert k in FEATURES, k
            rt.features.values[k] = bool(v)

    yield set_
    rt.features.values = dict.fromkeys(FEATURES, False)


def _sim_modules() -> tuple[Any, Any]:
    if str(LDAP_SIM) not in sys.path:
        sys.path.insert(0, str(LDAP_SIM))
    import fake_ad  # type: ignore[import-not-found]
    import make_certs  # type: ignore[import-not-found]

    return fake_ad, make_certs


@pytest.fixture(scope="session")
def ad_certs(tmp_path_factory: pytest.TempPathFactory) -> Any:
    _, mc = _sim_modules()
    return mc.make_certs(tmp_path_factory.mktemp("adcerts"))


@pytest.fixture
def trust_test_ca(ad_certs: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from netmon import tlsutil

    monkeypatch.setenv("NETMON_EXTRA_CA_FILE", str(ad_certs.ca))
    tlsutil.trust_bundle_pem.cache_clear()
    tlsutil.client_ssl_context.cache_clear()
    yield
    monkeypatch.delenv("NETMON_EXTRA_CA_FILE", raising=False)
    tlsutil.trust_bundle_pem.cache_clear()
    tlsutil.client_ssl_context.cache_clear()


@pytest.fixture
async def fake_ad(ad_certs: Any, trust_test_ca: None) -> AsyncIterator[Any]:
    fa, _ = _sim_modules()
    ad = fa.FakeAD(fa.load_directory(LDAP_SIM / "mnidom.yaml"), certs=ad_certs)
    await ad.start()
    yield ad
    await ad.stop()


def ldap_settings(ad: Any, *, security: str = "ldaps", **kw: Any) -> dict[str, Any]:
    port = ad.ldaps_port if security == "ldaps" else ad.ldap_port
    out = {
        "servers": [f"localhost:{port}"],
        "security": security,
        "tls_verify": True,
        "domain": "mnidom.net",
        "netbios_domain": "MNIDOM",
        "base_dn": "DC=mnidom,DC=net",
        "bind_dn": "svc-netmon@mnidom.net",
        "bind_password": "Passw0rd!",
        "admin_group_dn": "CN=NetMon-Admins,OU=Groups,DC=mnidom,DC=net",
        "viewer_group_dn": "CN=NetMon-Viewers,OU=Groups,DC=mnidom,DC=net",
        "timeout_s": 2,
    }
    out.update(kw)
    return out
