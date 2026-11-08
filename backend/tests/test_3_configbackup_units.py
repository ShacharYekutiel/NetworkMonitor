"""[3] Pure config-backup units: driver validation and matching, normalisation vectors,
diff, retention selection, host-key decision, credentials merge, eligibility, scheduling."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from netmon.api.errors import ApiError
from netmon.configbackup import credentials, drivers, normalize, service
from netmon.configbackup.runner import next_daily, stagger
from netmon.ingest import rules

LIB = drivers.library()
D = LIB.drivers


def test_builtin_drivers_load() -> None:
    assert LIB.errors == []
    assert sorted(D) == [
        "aruba_cx",
        "checkpoint_gaia",
        "cisco_ios",
        "fortigate",
        "hpe_arubaos_switch",
        "hpe_comware",
        "mikrotik_routeros",
    ]
    assert D["hpe_comware"].mode == "shell" and D["checkpoint_gaia"].mode == "exec"
    assert D["checkpoint_gaia"].commands[0].fallback == 'clish -c "show configuration"'
    assert D["cisco_ios"].enable is not None
    assert D["hpe_arubaos_switch"].pre_prompt == (("Press any key to continue", "\n"),)
    out = D["hpe_comware"].out()
    assert out["commands"] == ["display current-configuration"]
    assert set(out) == {
        "id", "name", "version", "confidence", "mode", "commands", "match_templates", "notes"
    }  # fmt: skip


BASE = yaml.safe_load((drivers.resources.files("netmon.configbackup") / "drivers" /
                       "hpe_comware.yaml").read_text())  # fmt: skip


def _with(**kw: object) -> dict:
    d = copy.deepcopy(BASE)
    d.update(kw)
    return d


@pytest.mark.parametrize(
    "bad",
    [
        {"commands": [{"command": "write memory"}]},
        {"commands": [{"command": "save force"}]},
        {"commands": [{"command": "display current; reboot"}]},
        {"commands": [{"command": "show run | include x"}]},
        {"commands": [{"command": "show run && reload"}]},
        {"commands": [{"command": "show run > flash:x"}]},
        {"commands": [{"command": "show $(id)"}]},
        {"commands": [{"command": "show run\nreload"}]},
        {"commands": [{"command": "configure terminal"}]},
        {"commands": [{"command": "system-view"}]},
        {"commands": [{"command": "/system reboot"}]},
        {"commands": [{"command": "set system x"}]},
        {"commands": [{"command": 'clish -c "set user x"'}]},
        {"commands": []},
        {"commands": [{"command": "show a"}] * 5},
        {"session_setup": ["undo screen-length"]},
        {"session_setup": ["copy run start"]},
        {"session_setup": ["terminal monitor"]},
        {"id": "Bad-Id"},
        {"id": "other_name"},
        {"mode": "telnet"},
        {"prompt": "("},
        {"secrets": ["no group"]},
        {"pre_prompt": [{"pattern": "x", "send": "reload\n"}]},
        {"enable": {"when": ">", "command": "configure", "password_prompt": "P"}},
        {"unknown_key": 1},
        {"confidence": "maybe"},
    ],
)
def test_validate_driver_rejects(bad: dict) -> None:
    with pytest.raises(drivers.DriverError):
        drivers.validate_driver(_with(**bad), "hpe_comware")


def test_validate_driver_accepts_allowlisted() -> None:
    d = drivers.validate_driver(
        _with(session_setup=["Screen-Length  Disable", "set clienv rows 0"]), "hpe_comware"
    )
    assert d.session_setup[1] == "set clienv rows 0"
    exec_d = drivers.validate_driver(
        {**_with(mode="exec", session_setup=[], prompt=None),
         "commands": [{"command": "/export"}]}, "hpe_comware",
    )  # fmt: skip
    assert exec_d.commands[0].command == "/export"
    with pytest.raises(drivers.DriverError):  # fallback only in exec mode
        drivers.validate_driver(
            _with(commands=[{"command": "show x", "fallback": "show y"}]), "hpe_comware"
        )


def test_load_texts_skips_invalid() -> None:
    lib = drivers.load_texts([("a_ok.yaml", yaml.safe_dump(_with(id="a_ok"))),
                              ("b.yaml", "id: b\n"), ("c.yaml", ": : :")])  # fmt: skip
    assert list(lib.drivers) == ["a_ok"]
    assert [e["file"] for e in lib.errors] == ["b.yaml", "c.yaml"]


@pytest.mark.parametrize(
    ("templates", "oid", "override", "want"),
    [
        (["hpe_comware"], None, None, ("hpe_comware", "template")),
        (["hpe_arubaos_switch"], "1.3.6.1.4.1.11.2.3.7.11.181", None,
         ("hpe_arubaos_switch", "template")),
        ([], "1.3.6.1.4.1.25506.11.1.240", None, ("hpe_comware", "sys_object_id")),
        ([], "1.3.6.1.4.1.11.2.3.7.11.181", None, ("hpe_comware", "sys_object_id")),
        ([], "1.3.6.1.4.1.2620.1.6.123", None, ("checkpoint_gaia", "sys_object_id")),
        ([], "1.3.6.1.4.1.8072.3.2.10", None, (None, None)),
        (["hpe_comware"], None, "cisco_ios", ("cisco_ios", "override")),
        ([], None, "nope", (None, None)),
        (["fortinet_fortigate"], None, None, ("fortigate", "template")),
        (["mikrotik_routeros"], None, None, ("mikrotik_routeros", "template")),
    ],
)  # fmt: skip
def test_match_driver(templates: list, oid: str | None, override: str | None, want: tuple) -> None:
    assert drivers.match_driver(D, templates, oid, override) == want


# ------------------------------------------------------------------ normalisation vectors
CISCO_RAW = (
    "show running-config\r\nBuilding configuration...\r\n\r\nCurrent configuration : 1234 "
    "bytes\r\n!\r\n! Last configuration change at 10:00:00 UTC Sun Nov 8 2026\r\nhostname "
    "r1\r\nenable secret 9 $9$abc\r\nr1#"
)
COMWARE_RAW = (
    "display current-configuration\n#\n version 7.1.070\n#\n snmp-agent community read cipher "
    "$c$3$xyz\n#\nreturn\n<sw1>"
)


def test_normalize_vectors() -> None:
    n = normalize.normalize([("show running-config", CISCO_RAW)], D["cisco_ios"])
    assert n.text == "!\nhostname r1\nenable secret 9 <secret removed>\n"
    cmd = "display current-configuration"
    n = normalize.normalize([(cmd, COMWARE_RAW)], D["hpe_comware"])
    assert n.text == (
        "#\n version 7.1.070\n#\n snmp-agent community read cipher <secret removed>\n#\nreturn\n"
    )
    plain = normalize.normalize([(cmd, COMWARE_RAW)], D["hpe_comware"], mask=False)
    assert "$c$3$xyz" in plain.text
    # ANSI cleanup (ArubaOS)
    raw = "show running-config\r\n\x1b[24;1H\x1b[2Khostname sw2\r\n\x1b[2Ksw2# "
    n = normalize.normalize([("show running-config", raw)], D["hpe_arubaos_switch"])
    assert n.text == "hostname sw2\n"
    # ignored line -> same hash
    a = CISCO_RAW
    b = CISCO_RAW.replace("10:00:00", "11:11:11")
    ha = normalize.normalize([("show running-config", a)], D["cisco_ios"]).sha256
    hb = normalize.normalize([("show running-config", b)], D["cisco_ios"]).sha256
    assert ha == hb
    # MikroTik timestamp header
    mt = "# 2026-11-08 02:47:03 by RouterOS 7.16\r\n# software id = X\r\n/ip address\r\n"
    assert normalize.normalize([("/export", mt)], D["mikrotik_routeros"]).text == "/ip address\n"
    # two commands joined with headers
    two = normalize.normalize([("show a", "x\n"), ("show b", "y\n")], D["checkpoint_gaia"])
    assert two.text == "# ==== show a ====\nx\n# ==== show b ====\ny\n"


def test_clean_terminal() -> None:
    assert normalize.clean_terminal("ab\x08c\r\r\nd\re") == "ac\nd\ne"
    assert normalize.mask_line("key cipher abc", [__import__("re").compile(r"cipher (\S+)"),
                               __import__("re").compile(r"key \S+ (\S+)")]) == (
        "key cipher <secret removed>"
    )  # fmt: skip


def test_diff_and_changes() -> None:
    old, new = "a\nb\nc\n", "a\nB\nc\nd\n"
    text_, added, removed, truncated = normalize.diff(old, new)
    assert (added, removed, truncated) == (2, 1, False)
    assert "+B" in text_ and "-b" in text_
    assert normalize.line_changes(old, new) == (2, 1)
    big_old = "\n".join(str(i) for i in range(30000))
    _, a2, r2, tr = normalize.diff(big_old, "", max_lines=100)
    assert tr is True and r2 == 30000 and a2 == 0


def test_select_expired_vectors() -> None:
    now = datetime(2026, 11, 8, tzinfo=UTC)
    vs = [(i, now - timedelta(hours=i)) for i in range(1, 8)]
    assert sorted(normalize.select_expired(vs, now, 5, 30, 2)) == [6, 7]
    vs = [(1, now), (2, now - timedelta(days=40)), (3, now - timedelta(days=50))]
    assert normalize.select_expired(vs, now, 5, 30, 2) == [3]
    assert normalize.select_expired([(1, now - timedelta(days=400))], now, 5, 30, 2) == []


def test_host_key_decision() -> None:
    assert normalize.host_key_decision(None, "SHA256:a") == "pin"
    row = {"state": "pinned", "fingerprint": "SHA256:a"}
    assert normalize.host_key_decision(row, "SHA256:a") == "accept"
    assert normalize.host_key_decision(row, "SHA256:b") == "reject_changed"
    changed = {"state": "changed", "fingerprint": "SHA256:a"}
    assert normalize.host_key_decision(changed, "SHA256:a") == "reject_changed"


# ------------------------------------------------------------------ credentials
KEY = "k" * 40


def test_credentials_merge_rules() -> None:
    In = credentials.SshCredentialsIn
    with pytest.raises(ApiError):  # no username
        credentials.merge(In(password="x"), None, KEY)
    with pytest.raises(ApiError):  # no secret
        credentials.merge(In(username="u"), None, KEY)
    s = credentials.merge(In(username="u", password="pw", enable_password="en"), None, KEY)
    assert s["port"] == 22 and s["password_enc"].startswith("v1:")
    assert "pw" not in str(s)
    out = credentials.ssh_out(s, KEY)
    assert out["password"] is None and out["password_set"] and out["enable_password_set"]
    assert not out["private_key_set"] and not out["credentials_unreadable"]
    s2 = credentials.merge(In(port=2222), s, KEY)  # omitted keep
    assert s2["password_enc"] == s["password_enc"] and s2["port"] == 2222
    s3 = credentials.merge(In(enable_password=""), s2, KEY)  # "" clears
    assert s3["enable_password_enc"] is None
    with pytest.raises(ApiError):  # clearing the only secret
        credentials.merge(In(password=""), s3, KEY)
    c = credentials.decrypt(s3, KEY)
    assert c.password == "pw" and c.username == "u" and "pw" not in repr(c)
    assert credentials.ssh_out(s3, "another-key")["credentials_unreadable"] is True
    with pytest.raises(credentials.CredentialsUnreadable):
        credentials.decrypt(s3, "another-key")


def test_private_key_validation() -> None:
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    pem = key.export_private_key("openssh", passphrase="pp").decode()
    In = credentials.SshCredentialsIn
    s = credentials.merge(In(username="u", private_key=pem, passphrase="pp"), None, KEY)
    assert s["private_key_enc"] and s["passphrase_enc"]
    with pytest.raises(ApiError) as exc:
        credentials.merge(In(username="u", private_key=pem, passphrase="wrong"), None, KEY)
    assert exc.value.code == "invalid_private_key" and "OPENSSH" not in exc.value.message
    with pytest.raises(ApiError):
        credentials.merge(In(username="u", private_key="garbage"), None, KEY)


def test_resolve_and_eligibility() -> None:
    g = {"username": "g"}
    assert credentials.resolve(None, [(2, None), (1, g)]) == (g, "group", 1)
    assert credentials.resolve({"username": "d"}, [(1, g)])[1] == "device"
    assert credentials.resolve(None, [(1, None)]) == (None, "none", None)
    e = service.eligibility
    assert e(False, "auto", True, "x", None) == (False, "disabled_device")
    assert e(True, "off", True, "x", None) == (False, "mode_off")
    assert e(True, "auto", False, "x", None) == (False, "no_credentials")
    assert e(True, "on", True, None, None) == (False, "no_driver")
    assert e(True, "auto", True, "x", "changed") == (False, "host_key_changed")
    assert e(True, "auto", True, "x", "pinned") == (True, "ok")


def test_schedule_helpers() -> None:
    now = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)  # 12:00 in Jerusalem
    nxt = next_daily(now, "02:47", "Asia/Jerusalem")
    assert nxt == datetime(2026, 11, 9, 0, 47, tzinfo=UTC)
    assert next_daily(now, "13:00", "Asia/Jerusalem") == datetime(2026, 11, 8, 11, 0, tzinfo=UTC)
    st = stagger([5, 1, 3], now, 60)
    assert st == {1: now, 3: now + timedelta(minutes=20), 5: now + timedelta(minutes=40)}
    assert set(stagger([1, 2], now, 0).values()) == {now}


def test_backup_trigger_rules() -> None:
    def rule(i: int, action: str, match: dict) -> rules.Rule:
        return rules.Rule(i, "r", "syslog", 100, "global", None, None, match, action)

    cfg = rule(1, "config_backup", {"severity_max": None, "facilities": [], "programs": [],
                                    "text": "%SYS-5-CONFIG_I", "text_regex": False})  # fmt: skip
    warn = rule(2, "warning", {"severity_max": 7, "facilities": [], "programs": [],
                               "text": None, "text_regex": False}, )  # fmt: skip
    rec = {"severity": 5, "facility": 23, "program": "", "message": "%SYS-5-CONFIG_I: x"}
    assert rules.backup_triggers(rec, [cfg, warn]) == [1]
    out = rules.evaluate(rec, [cfg, warn], rules.Context())
    assert out.raised is not None and out.raised[0].id == 2  # config_backup skipped
    assert rules.backup_triggers({**rec, "message": "other"}, [cfg]) == []
    v = rules.validate_rule({"kind": "syslog", "name": "x", "action": "config_backup",
                             "match": cfg.match})  # fmt: skip
    assert v["action"] == "config_backup" and v["clear_after_min"] is None
    with pytest.raises(ApiError):
        rules.validate_rule({"kind": "syslog", "name": "x", "action": "config_backup",
                             "match": cfg.match, "clear_after_min": 5})  # fmt: skip
