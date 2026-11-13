"""[1.4.2] Static review "low" items: krb5.conf realm injection, one-line SIEM records, the
user template path check, LIKE wildcards in the new-devices search, httpx URL logging."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from netmon.api.errors import ApiError
from netmon.assistant.features.templates import _inside
from netmon.auth import kerberos_pure
from netmon.ingest.forward import render_5424
from tests.conftest import requires_db


def test_krb5_conf_realm_cannot_inject_lines() -> None:
    conf = kerberos_pure.krb5_conf("CORP.LOCAL\n[realms]\n EVIL = {")
    assert "[realms]" not in conf and "EVIL" not in conf and "default_realm" not in conf
    assert "default_realm = CORP.LOCAL" in kerberos_pure.krb5_conf("corp.local")
    assert kerberos_pure.default_realms("bad domain\n") == []
    assert kerberos_pure.default_realms("corp.example") == ["CORP.EXAMPLE"]


def test_siem_record_is_one_line() -> None:
    rec = {
        "kind": "syslog",
        "source_ip": "192.0.2.1",
        "facility": 1,
        "severity": 5,
        "time": __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC),
        "hostname": "sw1",
        "program": "x",
        "message": "first\r\n<13>1 2026-01-01T00:00:00Z forged - - - - injected\x00",
    }
    line = render_5424(rec, "1.4.2")
    assert "\n" not in line and "\r" not in line and "\x00" not in line
    assert "first" in line and "injected" in line


def test_user_template_path_stays_inside(tmp_path: Path) -> None:
    assert _inside(tmp_path, "cisco/x.yaml") == str((tmp_path / "cisco" / "x.yaml").resolve())
    for bad in ("../x.yaml", "/etc/passwd", "a/../../x.yaml"):
        with pytest.raises(ApiError):
            _inside(tmp_path, bad)
    (tmp_path / "link").symlink_to("/tmp")
    with pytest.raises(ApiError):
        _inside(tmp_path, "link/x.yaml")


def test_httpx_request_urls_are_not_logged() -> None:
    """Webhook / Teams / Telegram URLs carry secrets; httpx logs request URLs at INFO."""
    from netmon.cli import _setup_logging

    _setup_logging("debug")
    try:
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    finally:
        logging.getLogger().setLevel(logging.WARNING)


@requires_db
async def test_newdev_search_wildcards_are_literal(rt: Any, admin: Any) -> None:
    r = await admin.put("/api/settings", json={"feature_new_device_alerts_enabled": True})
    assert r.status_code == 200, r.text
    async with rt.sessionmaker() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "INSERT INTO known_macs (mac, vendor, first_seen, last_seen, state) VALUES "
                "('aa:bb:cc:00:00:01', 'Acme', now(), now(), 'new'), "
                "('aa:bb:cc:00:00:02', 'Ac_e 100%', now(), now(), 'new')"
            )
        )
        await session.commit()
    r = await admin.get("/api/new-devices", params={"q": "%"})
    assert r.status_code == 200, r.text
    assert [i["vendor"] for i in r.json()["items"]] == ["Ac_e 100%"]
    r = await admin.get("/api/new-devices", params={"q": "ac_e"})
    assert [i["vendor"] for i in r.json()["items"]] == ["Ac_e 100%"]


def test_hsts_include_subdomains_option() -> None:
    from netmon.services import security

    assert security.hsts_value(False) == "max-age=31536000"
    assert security.hsts_value(True) == "max-age=31536000; includeSubDomains"
    pol = security.DEFAULT_POLICY
    h = security.security_headers(pol, csp="x", https=True, hsts=security.hsts_value(True))
    assert h["Strict-Transport-Security"].endswith("includeSubDomains")
    assert "Strict-Transport-Security" not in security.security_headers(pol, csp="x", https=False)
