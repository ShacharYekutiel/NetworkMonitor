"""[security] GET /api/health is public and says only whether the service is up; the runtime
details (version, uptime, scheduler, receivers, Kerberos, assistant, test mode) moved to
GET /api/health/details, which needs a signed-in user or an API token."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from netmon.api import health as health_mod
from tests.conftest import ADMIN, requires_db

pytestmark = requires_db

# what the UI (Settings page subtitle) and the ops/E2E consumers read from the details
DETAIL_KEYS = {
    "status",
    "version",
    "database",
    "timescale",
    "poller",
    "sensors_scheduled",
    "uptime_s",
    "test_mode",
    "trgm",
    "ingest",
    "flows_rollup",
    "kerberos",
    "assistant",
}


async def test_public_health_has_only_status(client: httpx.AsyncClient) -> None:
    # before setup (exempt from the setup guard) ...
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    # ... and after setup, without credentials
    assert (await client.post("/api/setup", json=ADMIN)).status_code == 201
    client.cookies.clear()
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert set(r.json()) == {"status"}


async def test_public_health_stays_minimal_when_signed_in(admin: httpx.AsyncClient) -> None:
    r = await admin.get("/api/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_public_health_fail_is_503(
    admin: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(_rt: Any) -> bool:
        return False

    monkeypatch.setattr(health_mod, "_db_ok", down)
    r = await admin.get("/api/health")
    assert r.status_code == 503 and r.json() == {"status": "fail"}


async def test_details_require_auth(app: Any, admin: httpx.AsyncClient) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anon:
        r = await anon.get("/api/health/details")
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthenticated"
        r = await anon.get("/api/health/details", headers={"Authorization": "Bearer nm_bogus"})
        assert r.status_code == 401
        assert "version" not in r.text


async def test_details_for_admin_viewer_and_token(
    app: Any, rt: Any, admin: httpx.AsyncClient, viewer: httpx.AsyncClient
) -> None:
    for c in (admin, viewer):
        r = await c.get("/api/health/details")
        assert r.status_code == 200, r.text
        h = r.json()
        assert set(h) == DETAIL_KEYS
        assert h["status"] == "ok" and h["database"] == "ok"
        assert h["version"] == rt.version
        assert h["poller"] in ("running", "stopped")
        assert isinstance(h["sensors_scheduled"], int)
        assert h["test_mode"] is False
        assert set(h["ingest"]) >= {"trap", "syslog_udp", "syslog_tcp", "netflow", "ipfix"}
    tok = (await admin.post("/api/tokens", json={"name": "ops", "role": "viewer"})).json()["token"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {tok}"},
    ) as c:
        r = await c.get("/api/health/details")
        assert r.status_code == 200 and set(r.json()) == DETAIL_KEYS
