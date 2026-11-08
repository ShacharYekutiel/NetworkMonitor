"""[2b-3] Pure units: LDAP helpers (data-model vectors), settings crypto, audit redaction,
Teams card, API token helpers, the PEM trust bundle. No database."""

from __future__ import annotations

import base64
import json
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from netmon.alerts import teams
from netmon.auth import ldap_config, ldap_pure, tokens
from netmon.services import audit, crypto

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- ldap_pure: names
@pytest.mark.parametrize(
    ("text", "sam", "upn"),
    [
        ("jdoe", "jdoe", None),
        ("MNIDOM\\jdoe", "jdoe", None),
        ("mnidom\\jdoe", "jdoe", None),  # NetBIOS domain is case-insensitive
        ("John.Doe@MNIDOM.NET", "John.Doe", "John.Doe@MNIDOM.NET"),  # fallback sam
        ("x@other.com", None, "x@other.com"),  # UPN only
    ],
)
def test_parse_login_vectors(text: str, sam: str | None, upn: str | None) -> None:
    name = ldap_pure.parse_login(text, "mnidom.net", "MNIDOM")
    assert name is not None
    assert name.sam == sam
    assert name.upn == upn


@pytest.mark.parametrize(
    "text",
    ["OTHER\\jdoe", "\\jdoe", "MNIDOM\\", "", "a\x00b", "x" * 257, "bad/name", "a*b", "@x"],
)
def test_parse_login_rejects(text: str) -> None:
    assert ldap_pure.parse_login(text, "mnidom.net", "MNIDOM") is None


def test_parse_login_domain_first_label() -> None:
    # DOMAIN may also be the domain's first label when no NetBIOS name is set
    assert ldap_pure.parse_login("mnidom\\jdoe", "mnidom.net", "").sam == "jdoe"  # type: ignore[union-attr]


def test_escape_vectors() -> None:
    assert ldap_pure.escape("a*)(uid=*") == "a\\2a\\29\\28uid=\\2a"
    assert ldap_pure.escape("CN=A\\,B") == "CN=A\\5c,B"
    assert ldap_pure.escape("nul\x00") == "nul\\00"


def test_filters_escape_injection() -> None:
    f = ldap_pure.user_filter_sam("*)(uid=*")
    assert f == "(&(objectCategory=person)(objectClass=user)(sAMAccountName=\\2a\\29\\28uid=\\2a))"
    assert "(uid=*" not in f
    g = ldap_pure.group_filter("CN=A\\,B,OU=G,DC=x")
    assert g == "(memberOf:1.2.840.113556.1.4.1941:=CN=A\\5c,B,OU=G,DC=x)"
    assert ldap_pure.user_filter_upn("a@b*").endswith("(userPrincipalName=a@b\\2a))")


@pytest.mark.parametrize(
    ("uac", "computed", "expires", "reason"),
    [
        (512, 0, 0, None),
        ("512", "0", "9223372036854775807", None),
        (514, 0, 0, "account_disabled"),
        (512, 0x10, 0, "account_locked"),
        (512, 0x800000, 0, "password_expired"),
        (512, 0, 1, "account_expired"),  # 1601-01-01 + 100 ns
        (512, None, None, None),
    ],
)
def test_account_state(uac: object, computed: object, expires: object, reason: str | None) -> None:
    assert ldap_pure.account_state(uac, computed, expires, NOW) == reason


def test_account_expires() -> None:
    assert ldap_pure.account_expires_at(0) is None
    assert ldap_pure.account_expires_at(9223372036854775807) is None
    future = (
        int((NOW + timedelta(days=1) - datetime(1601, 1, 1, tzinfo=UTC)).total_seconds()) * 10**7
    )
    assert ldap_pure.account_state(512, 0, future, NOW) is None
    assert ldap_pure.account_state(512, 0, str(future), NOW + timedelta(days=2)) == (
        "account_expired"
    )


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ("52e", "bad_password"),
        ("775", "account_locked"),
        ("532", "password_expired"),
        ("533", "account_disabled"),
        ("701", "account_expired"),
        ("773", "password_must_change"),
        ("530", "not_permitted"),
        ("531", "not_permitted"),
    ],
)
def test_map_bind_error(data: str, reason: str) -> None:
    msg = (
        "80090308: LdapErr: DSID-0C09044E, comment: AcceptSecurityContext error, "
        f"data {data}, v4563"
    )
    assert ldap_pure.map_bind_error(msg) == reason


def test_map_bind_error_without_data() -> None:
    assert ldap_pure.map_bind_error("invalidCredentials") == "bad_password"
    assert ldap_pure.map_bind_error(None) == "bad_password"


def test_parse_server_and_base_dn() -> None:
    assert ldap_pure.parse_server("dc1.mnidom.net", "ldaps") == ("dc1.mnidom.net", 636)
    assert ldap_pure.parse_server("dc1.mnidom.net", "starttls") == ("dc1.mnidom.net", 389)
    assert ldap_pure.parse_server("dc2.mnidom.net:3269", "ldaps") == ("dc2.mnidom.net", 3269)
    assert ldap_pure.parse_server("[fd00::1]:636", "ldaps") == ("fd00::1", 636)
    assert ldap_pure.parse_server("[fd00::1]", "starttls") == ("fd00::1", 389)
    with pytest.raises(ValueError):
        ldap_pure.parse_server("dc1:99999", "ldaps")
    assert ldap_pure.server_label("fd00::1", 636) == "[fd00::1]:636"
    assert ldap_pure.base_dn_from_domain("mnidom.net") == "DC=mnidom,DC=net"
    assert ldap_pure.base_dn_from_domain("") == ""


def test_guid_and_sam() -> None:
    raw = bytes(range(16))
    assert ldap_pure.guid_hex(raw) == raw.hex()
    assert ldap_pure.guid_hex([raw]) == raw.hex()
    assert ldap_pure.guid_hex("{00010203-0405-0607-0809-0A0B0C0D0E0F}") == raw.hex()
    assert ldap_pure.guid_hex(b"short") is None
    assert ldap_pure.valid_sam("jdoe") and ldap_pure.valid_sam("star*user") is False
    assert not ldap_pure.valid_sam("x" * 65)


# ---------------------------------------------------------------- crypto + ldap settings
def test_crypto_roundtrip_and_wrong_key() -> None:
    enc = crypto.encrypt("key-one", "Passw0rd!", crypto.AAD_LDAP_BIND_PASSWORD)
    assert enc.startswith("v1:") and "Passw0rd" not in enc
    raw = base64.urlsafe_b64decode(enc[3:] + "=" * (-len(enc[3:]) % 4))
    assert len(raw) == 12 + len("Passw0rd!") + 16
    assert crypto.decrypt("key-one", enc, crypto.AAD_LDAP_BIND_PASSWORD) == "Passw0rd!"
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt("key-two", enc, crypto.AAD_LDAP_BIND_PASSWORD)
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt("key-one", enc, b"other.aad")
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt("key-one", "plain", crypto.AAD_LDAP_BIND_PASSWORD)
    # a fresh nonce every time
    assert crypto.encrypt("k", "p", b"a") != crypto.encrypt("k", "p", b"a")


def test_ldap_settings_merge_and_out() -> None:
    stored = ldap_config.merge(
        None,
        ldap_config.LdapSettingsIn(
            domain="mnidom.net", bind_dn="svc@mnidom.net", bind_password="pw"
        ),
        "sk",
    )
    assert stored["bind_password_enc"].startswith("v1:")
    out = ldap_config.ldap_out(stored, "sk")
    assert out["bind_password"] is None and out["bind_password_set"] is True
    assert out["bind_password_unreadable"] is False
    assert "bind_password_enc" not in out
    # omitted/null keep, "" clears
    kept = ldap_config.merge(stored, ldap_config.LdapSettingsIn(timeout_s=7), "sk")
    assert kept["bind_password_enc"] == stored["bind_password_enc"] and kept["timeout_s"] == 7
    cleared = ldap_config.merge(stored, ldap_config.LdapSettingsIn(bind_password=""), "sk")
    assert cleared["bind_password_enc"] is None
    # another secret.key: unreadable, reported, treated as unset
    other = ldap_config.ldap_out(stored, "another-key")
    assert other["bind_password_unreadable"] is True and other["bind_password_set"] is False
    cfg = ldap_config.to_config(stored, "another-key")
    assert cfg.bind_password is None and cfg.bind_password_unreadable


def test_ldap_settings_validation() -> None:
    errs = ldap_config.validation_errors(ldap_config.DEFAULT_LDAP, "sk")
    fields = {e["loc"][-1] for e in errs}
    assert {"bind_dn", "bind_password", "servers", "base_dn", "admin_group_dn"} <= fields
    ok = ldap_config.merge(
        None,
        ldap_config.LdapSettingsIn(
            domain="mnidom.net",
            bind_dn="svc-netmon@mnidom.net",
            bind_password="x",
            viewer_group_dn="CN=NetMon-Viewers,OU=Groups,DC=mnidom,DC=net",
        ),
        "sk",
    )
    assert ldap_config.validation_errors(ok, "sk") == []
    with pytest.raises(ValueError):
        ldap_config.LdapSettingsIn(admin_group_dn="not a dn")
    with pytest.raises(ValueError):
        ldap_config.LdapSettingsIn(servers=["a b"])
    with pytest.raises(ValueError):
        ldap_config.LdapSettingsIn(timeout_s=31)
    assert ldap_config.to_config(ok, "sk").budget_s == 17  # min(3*5+2, 30)


# ---------------------------------------------------------------- audit redaction
def test_redact_secrets_never_values() -> None:
    changes = audit.redact(
        {
            "smtp.password": ["old-secret", "new-secret"],
            "ldap.bind_password_enc": [None, "v1:abc"],
            "snmp_credentials.community": ["public", None],
            "trap_v3_users": [
                [],
                [{"username": "u", "auth_key": "k1", "priv_key": "k2", "auth_protocol": "SHA"}],
            ],
            "config.url": ["https://a/x?sig=1", "https://a/y?sig=2"],
            "name": ["a", "b"],
        },
        secret_keys=("config.url",),
    )
    text = json.dumps(changes)
    for secret in ("old-secret", "new-secret", "v1:abc", "public", "k1", "k2", "sig="):
        assert secret not in text
    assert changes["smtp.password"] == ["***", "***"]
    assert changes["ldap.bind_password_enc"] == [None, "***"]
    assert changes["snmp_credentials.community"] == ["***", None]
    assert changes["trap_v3_users"][1][0]["auth_key"] == "***"
    assert changes["trap_v3_users"][1][0]["auth_protocol"] == "SHA"
    assert changes["name"] == ["a", "b"]


def test_diff_created_deleted() -> None:
    old = {"name": "a", "snmp_credentials": {"community": "c1", "version": "v2c"}, "x": 1}
    new = {"name": "b", "snmp_credentials": {"community": "c2", "version": "v3"}, "x": 1}
    d = audit.diff(old, new)
    assert d == {
        "name": ["a", "b"],
        "snmp_credentials.community": ["***", "***"],
        "snmp_credentials.version": ["v2c", "v3"],
    }
    assert audit.diff(old, old) == {}
    c = audit.created({"name": "n", "password": "pw", "when": NOW})
    assert c == {
        "name": [None, "n"],
        "password": [None, "***"],
        "when": [None, "2026-11-08T12:00:00Z"],
    }
    dl = audit.deleted(
        {"name": "n", "password": "pw", "config": {"bot_token": "t", "chat_id": "1"}}
    )
    assert dl == {"name": ["n", None], "config.chat_id": ["1", None]}


def test_csv_line_and_like_escape() -> None:
    assert audit.csv_line(["a,b", None, 1, "=cmd"]) == '"a,b",,1,\'=cmd\r\n'
    assert audit.like_escape("50%_\\") == "50\\%\\_\\\\"


def test_audit_filter_where() -> None:
    where, p = audit.AuditFilter(action="device.", actor="ad_m").where()
    assert "action LIKE :action" in where and p["action"] == "device.%"
    assert p["actor"] == "%ad\\_m%"
    where, p = audit.AuditFilter(action="auth.login").where()
    assert "action = :action" in where


# ---------------------------------------------------------------- Teams card
def _payload(**kw: object) -> dict:
    base = {
        "event": "sensor.status_changed",
        "stage": "problem",
        "after_maintenance": False,
        "problem_since": "2026-11-08T10:00:00Z",
        "time": "2026-11-08T10:00:00Z",
        "old_status": "up",
        "new_status": "down",
        "message": "Host unreachable",
        "sensor": {"id": 7, "name": "Ping", "type": "ping"},
        "device": {"id": 2, "name": "core-sw", "host": "10.0.0.2"},
        "group": {"id": 1, "name": "Default"},
        "rule": {"id": 1, "name": "All down"},
        "url": "https://netmon.mnidom.net/sensors/7",
    }
    base.update(kw)
    return base


def test_teams_card_problem_exact() -> None:
    from netmon.alerts import templates

    token = templates.DISPLAY_TZ.set("Asia/Jerusalem")
    try:
        body = teams.build_card(_payload(), "en")
    finally:
        templates.DISPLAY_TZ.reset(token)
    assert body == {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "msteams": {"width": "Full"},
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "DOWN: core-sw / Ping",
                            "weight": "Bolder",
                            "size": "Medium",
                            "color": "Attention",
                            "wrap": True,
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Status", "value": "up → down"},
                                {"title": "Device", "value": "core-sw (10.0.0.2)"},
                                {"title": "Sensor", "value": "Ping"},
                                {"title": "Message", "value": "Host unreachable"},
                                {"title": "Since", "value": "2026-11-08 12:00 (Asia/Jerusalem)"},
                                {"title": "Rule", "value": "All down"},
                            ],
                        },
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "Open in NetworkMonitor",
                            "url": "https://netmon.mnidom.net/sensors/7",
                        }
                    ],
                },
            }
        ],
    }


def test_teams_card_stages_and_lang() -> None:
    card = lambda b: b["attachments"][0]["content"]  # noqa: E731
    rec = card(
        teams.build_card(_payload(stage="recovery", old_status="down", new_status="up"), "en")
    )
    assert rec["body"][0]["color"] == "Good"
    assert rec["body"][0]["text"] == "RECOVERED: core-sw / Ping"
    esc = card(teams.build_card(_payload(stage="escalation"), "en"))
    assert esc["body"][0]["color"] == "Warning"
    assert esc["body"][0]["text"].startswith("ESCALATION: DOWN")
    assert {"title": "Stage", "value": "Escalation"} in esc["body"][1]["facts"]
    rem = card(teams.build_card(_payload(stage="reminder", new_status="warning"), "en"))
    assert rem["body"][0]["text"].startswith("REMINDER: WARNING")
    he = card(teams.build_card(_payload(), "he"))
    assert he["version"] == "1.5" and he["rtl"] is True
    assert he["body"][0]["text"].startswith("תקלה")
    no_url = card(teams.build_card(_payload(url=None), "en"))
    assert "actions" not in no_url
    test = card(teams.build_card({"event": "test", "url": None}, "en"))
    assert test["body"] == [
        {
            "type": "TextBlock",
            "text": "NetworkMonitor test message",
            "weight": "Bolder",
            "size": "Medium",
            "color": "Good",
            "wrap": True,
        }
    ]


def test_teams_card_size_cap() -> None:
    body = teams.build_card(_payload(message="x" * 60000), "en")
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw) <= teams.MAX_CARD_BYTES
    facts = body["attachments"][0]["content"]["body"][1]["facts"]
    msg = next(f for f in facts if f["title"] == "Message")
    assert msg["value"].endswith("…") and len(msg["value"]) > 1000


def test_teams_url_helpers() -> None:
    url = (
        "https://prod-12.westeurope.logic.azure.com/workflows/x/triggers/manual/paths/invoke?sig=S"
    )
    assert teams.config_out({"url": url}) == {
        "url": None,
        "url_set": True,
        "url_host": "prod-12.westeurope.logic.azure.com",
        "legacy": False,
        "ack_link": True,  # [3] chat_ack
    }
    assert teams.is_legacy("https://mnidom.webhook.office.com/webhookb2/x")
    assert teams.is_legacy("https://outlook.office.com/webhook/x")
    assert not teams.is_legacy("https://x.environment.api.powerplatform.com/x")
    assert "sig=S" not in teams._scrub(f"error for {url}", url)  # noqa: SLF001


# ---------------------------------------------------------------- tokens
def test_token_generation_and_helpers() -> None:
    text, prefix, digest = tokens.generate()
    assert text.startswith("nmt_") and len(text) == 4 + 43
    assert prefix == text[:12] and len(prefix) == 12
    assert digest == tokens.hash_token(text) and len(digest) == 64
    assert tokens.bearer_from_header("Bearer nmt_x") == "nmt_x"
    assert tokens.bearer_from_header("bearer   nmt_x ") == "nmt_x"
    assert tokens.bearer_from_header("Basic abc") is None
    assert tokens.bearer_from_header(None) is None
    assert tokens.effective_role("admin", "viewer") == "viewer"
    assert tokens.effective_role("viewer", "admin") == "viewer"
    assert tokens.effective_role("admin", "admin") == "admin"
    for path in ("/api/auth/me", "/api/users", "/api/users/3", "/api/tokens", "/api/tokens/1"):
        assert tokens.is_barred(path)
    assert not tokens.is_barred("/api/devices")
    tok = SimpleNamespace(revoked_at=None, expires_at=NOW + timedelta(days=1))
    assert tokens.state(tok, NOW) == "active"
    assert tokens.state(SimpleNamespace(revoked_at=NOW, expires_at=NOW), NOW) == "revoked"
    assert tokens.state(SimpleNamespace(revoked_at=None, expires_at=NOW), NOW) == "expired"


def test_invalid_token_limiter() -> None:
    lim = tokens.InvalidTokenLimiter(max_failures=3, window_s=60)
    for _ in range(3):
        assert not lim.blocked("1.2.3.4")
        lim.fail("1.2.3.4")
    assert lim.blocked("1.2.3.4") and not lim.blocked("1.2.3.5")


# ---------------------------------------------------------------- tlsutil bundle
def test_trust_bundle_includes_extra_ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from netmon import tlsutil

    certs = _make_ca(tmp_path)
    monkeypatch.setenv("NETMON_EXTRA_CA_FILE", str(certs))
    monkeypatch.setenv("NETMON_EXTRA_CA_DIR", str(tmp_path / "none"))
    tlsutil.trust_bundle_pem.cache_clear()
    try:
        bundle = tlsutil.trust_bundle_pem()
        ours = certs.read_text().strip()
        assert ours in bundle
        assert bundle.count(ours) == 1  # de-duplicated
        ctx = ssl.create_default_context(cadata=bundle)
        assert len(ctx.get_ca_certs()) >= 2  # extra CA + certifi/OS store
    finally:
        tlsutil.trust_bundle_pem.cache_clear()


def _make_ca(tmp_path: Path) -> Path:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "netmon test CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "ca.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path
