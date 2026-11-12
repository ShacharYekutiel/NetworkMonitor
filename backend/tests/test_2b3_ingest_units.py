"""[2b-3] Pure units of the ingestion side: syslog parsing and framing (data-model vectors),
OID names, trap decoding / rendering, rules, flapping tracker, RFC 5424 forwarding, attribution,
LAG resolution and inheritance, cursors."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pyasn1.codec.ber import encoder
from pysnmp.proto import api

from netmon.ingest import attribution, oid_names, query, rules
from netmon.ingest import trap_decode as td
from netmon.ingest.flapping import FlapTracker
from netmon.ingest.forward import Target, frame, render_5424
from netmon.ingest.limits import RateLimiter, TokenBucket
from netmon.ingest.syslog_parse import FrameTooLarge, TcpFramer, parse_syslog
from netmon.ports import view
from netmon.ports.collect import lag_sources, parse_if_stack, parse_lacp, resolve_lag

RECV = datetime(2026, 11, 8, 10, 0, 5, tzinfo=UTC)
TZ = "Asia/Jerusalem"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ------------------------------------------------------------------ syslog vectors S1-S11
def test_s1_rfc5424_full() -> None:
    r = parse_syslog(
        b"<165>1 2026-11-08T10:00:00.003Z sw1.mnidom.net evntslog 1234 ID47 "
        b'[exampleSDID@32473 iut="3"] \xef\xbb\xbfAn application event',
        RECV,
        TZ,
    )
    assert (r.facility, r.severity, r.format) == (20, 5, "rfc5424")
    assert r.device_time == _dt("2026-11-08T10:00:00.003Z")
    assert (r.hostname, r.program, r.pid, r.msgid) == ("sw1.mnidom.net", "evntslog", "1234", "ID47")
    assert r.structured_data == '[exampleSDID@32473 iut="3"]'
    assert r.message == "An application event"


def test_s2_rfc5424_nil() -> None:
    r = parse_syslog(b"<34>1 - - - - - - plain", RECV, TZ)
    assert (r.facility, r.severity, r.format) == (4, 2, "rfc5424")
    assert r.device_time is None and r.structured_data is None
    assert (r.hostname, r.program, r.pid, r.msgid, r.message) == ("", "", "", "", "plain")


def test_s3_rfc3164_year_rule() -> None:
    r = parse_syslog(
        b"<34>Oct 11 22:14:15 mymachine su: 'su root' failed for lonvick on /dev/pts/8", RECV, TZ
    )
    assert (r.facility, r.severity, r.format) == (4, 2, "rfc3164")
    assert (r.hostname, r.program) == ("mymachine", "su")
    assert r.message == "'su root' failed for lonvick on /dev/pts/8"
    # contract deviation (reported): data-model S3 says 2025, but its own rule ("the current
    # year unless that lands more than 1 day in the future") gives 2026: Oct 11 < Nov 8
    assert r.device_time == _dt("2026-10-11T19:14:15Z")
    # a date really more than 1 day ahead goes to the previous year
    r2 = parse_syslog(b"<34>Nov 10 10:00:00 h su: x", RECV, TZ)
    assert r2.device_time is not None and r2.device_time.year == 2025


def test_s4_tag_pid() -> None:
    r = parse_syslog(b"<30>Nov  8 09:05:01 gw-hq sshd[2211]: Accepted password for admin", RECV, TZ)
    assert (r.facility, r.severity) == (3, 6)
    assert (r.hostname, r.program, r.pid) == ("gw-hq", "sshd", "2211")
    assert r.device_time == _dt("2026-11-08T07:05:01Z")
    assert r.message == "Accepted password for admin"


def test_s5_comware() -> None:
    r = parse_syslog(
        b"<188>Nov  8 12:00:00 2026 FF5945-IRF %%10IFNET/4/PHY_UPDOWN: Physical state on the "
        b"interface Ten-GigabitEthernet1/0/49 changed to down.",
        RECV,
        TZ,
    )
    assert (r.facility, r.severity) == (23, 4)
    assert (r.hostname, r.program, r.msgid) == ("FF5945-IRF", "IFNET", "PHY_UPDOWN")
    assert r.message.startswith("Physical state on the interface Ten-GigabitEthernet1/0/49")
    assert r.device_time == _dt("2026-11-08T10:00:00Z")


def test_s6_arubaos() -> None:
    r = parse_syslog(b"<190>Nov  8 12:00:00 10.0.0.2 00076 ports: port 5 is now on-line", RECV, TZ)
    assert (r.facility, r.severity) == (23, 6)
    assert (r.hostname, r.msgid, r.program, r.message) == (
        "10.0.0.2",
        "00076",
        "ports",
        "port 5 is now on-line",
    )


def test_s7_s8_s9_s10() -> None:
    r = parse_syslog(b"<13>Nov  8 12:00:00 su: test", RECV, TZ)
    assert (r.facility, r.severity, r.hostname, r.program, r.message) == (1, 5, "", "su", "test")
    r = parse_syslog(b"<13>hello world", RECV, TZ)
    assert (r.format, r.device_time, r.hostname, r.program, r.message) == (
        "rfc3164",
        None,
        "",
        "",
        "hello world",
    )
    r = parse_syslog(b"just text", RECV, TZ)
    assert (r.facility, r.severity, r.format, r.message) == (1, 5, "raw", "just text")
    r = parse_syslog(b"<999>x", RECV, TZ)
    assert (r.facility, r.severity, r.format, r.message) == (1, 5, "raw", "<999>x")


def test_s11_nul_and_truncation() -> None:
    r = parse_syslog(b"<14>Nov  8 12:00:00 host app: a\x00b" + b"x" * 9000, RECV, TZ)
    assert (r.facility, r.severity) == (1, 6)
    assert r.message.startswith("a�b")
    assert len(r.message.encode()) <= 8192 and r.truncated is True


def test_parser_never_raises() -> None:
    for data in (b"", b"<", b"<1>1 ", b"<1>1 bad", b"\xff\xfe\x00", b"<13>Feb 30 12:00:00 h a: x"):
        parse_syslog(data, RECV, TZ)
    parse_syslog(b'<13>1 2026-11-08T10:00:00+02:00 h a p m [x y="a\\]b"] msg', RECV, "Nope/Zone")


def test_tcp_framing_vectors() -> None:
    f = TcpFramer()
    out = f.feed(b"27 <13>1 - h a - - - octet one")
    out += f.feed(b"<13>lf one\n<13>lf ")
    out += f.feed(b"two\r\n")
    assert out == [b"<13>1 - h a - - - octet one", b"<13>lf one", b"<13>lf two"]
    with pytest.raises(FrameTooLarge):
        TcpFramer().feed(b"99999999 x")
    with pytest.raises(FrameTooLarge):
        TcpFramer().feed(b"x" * 70000)
    assert TcpFramer().feed(b"5 ab") == []  # incomplete


# ------------------------------------------------------------------ OID names, rendering
def test_oid_names() -> None:
    assert oid_names.name("1.3.6.1.6.3.1.1.5.3") == "linkDown"
    assert oid_names.name("1.3.6.1.2.1.2.2.1.1.5") == "ifIndex.5"
    assert oid_names.name("1.3.6.1.4.1.25506.2.6.2.0.1") == "h3c.2.6.2.0.1"
    assert oid_names.name("1.3.6.1.4.1.999999.1") is None
    # owner's switch: syslog-as-trap and the Comware heartbeat
    assert oid_names.name("1.3.6.1.2.1.192.0.1") == "syslogMsgNotification"
    assert oid_names.name("1.3.6.1.4.1.25506.2.38.1.6.3.0.1") == "hh3cPeriodicalTrap"
    assert oid_names.name("1.3.6.1.4.1.25506.2.22.1.3.0.3") == "hh3cSSHUserLogin"
    assert oid_names.name("1.3.6.1.4.1.25506.2.2.1.1.3.0.1") == "hh3cLogIn"
    assert oid_names.name("1.3.6.1.2.1.192.1.2.1.11.7") == "syslogMsgMib.1.2.1.11.7"
    assert oid_names.enum_display("1.3.6.1.2.1.2.2.1.8.5", "2") == "down(2)"
    assert oid_names.enum_display("1.3.6.1.2.1.2.2.1.8.5", "9") == "9"


def test_octets_rendering() -> None:
    assert td.octets_text(b"GigabitEthernet1/0/5") == "GigabitEthernet1/0/5"
    assert td.octets_text(b"\x00\x11\x22\x33\x44\x55") == "00:11:22:33:44:55"
    assert td.octets_text(b"\x00\x01\x02") == "0x000102"
    assert td.octets_text("שלום".encode()) == "שלום"


def _v1_message() -> bytes:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_1]
    pdu = p.TrapPDU()
    p.apiTrapPDU.set_defaults(pdu)
    p.apiTrapPDU.set_enterprise(pdu, (1, 3, 6, 1, 4, 1, 25506, 2))
    p.apiTrapPDU.set_agent_address(pdu, "10.0.0.7")
    p.apiTrapPDU.set_generic_trap(pdu, 2)
    p.apiTrapPDU.set_timestamp(pdu, 1234)
    p.apiTrapPDU.set_varbinds(
        pdu,
        [
            (p.ObjectIdentifier("1.3.6.1.2.1.2.2.1.1.5"), p.Integer(5)),
            (p.ObjectIdentifier("1.3.6.1.2.1.2.2.1.7.5"), p.Integer(1)),
            (p.ObjectIdentifier("1.3.6.1.2.1.2.2.1.8.5"), p.Integer(2)),
        ],
    )
    msg = p.Message()
    p.apiMessage.set_defaults(msg)
    p.apiMessage.set_community(msg, "public")
    p.apiMessage.set_pdu(msg, pdu)
    return bytes(encoder.encode(msg))


def test_v1_conversion_and_summary() -> None:
    r = td.finish(td.decode_v1v2c(_v1_message()), "GigabitEthernet1/0/5")
    assert (r.version, r.pdu, r.trap_oid, r.agent_addr, r.uptime_ticks) == (
        "v1",
        "trap",
        "1.3.6.1.6.3.1.1.5.3",
        "10.0.0.7",
        1234,
    )
    assert r.varbinds[-1]["oid"] == "1.3.6.1.6.3.1.1.4.3.0"
    assert r.varbinds[-1]["value"] == "1.3.6.1.4.1.25506.2"
    assert r.summary.startswith(
        "linkDown ifIndex=5 ifAdminStatus=up(1) ifOperStatus=down(2)"
    ) and r.summary.endswith("port=GigabitEthernet1/0/5")
    assert td.v1_trap_oid("1.3.6.1.4.1.9", 6, 17) == "1.3.6.1.4.1.9.0.17"
    assert td.v1_trap_oid("1.3.6.1.4.1.9", 0, 0) == "1.3.6.1.6.3.1.1.5.1"


def test_v2c_inform_response_and_errors() -> None:
    p = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    pdu = p.InformRequestPDU()
    p.apiPDU.set_defaults(pdu)
    p.apiPDU.set_varbinds(
        pdu,
        [
            (p.ObjectIdentifier("1.3.6.1.2.1.1.3.0"), p.TimeTicks(9)),
            (
                p.ObjectIdentifier("1.3.6.1.6.3.1.1.4.1.0"),
                p.ObjectIdentifier("1.3.6.1.6.3.1.1.5.4"),
            ),
            (p.ObjectIdentifier("1.3.6.1.2.1.2.2.1.2.5"), p.OctetString(b"a\x00b")),
        ],
    )
    msg = p.Message()
    p.apiMessage.set_defaults(msg)
    p.apiMessage.set_community(msg, "c1")
    p.apiMessage.set_pdu(msg, pdu)
    r = td.decode_v1v2c(bytes(encoder.encode(msg)))
    assert (r.version, r.pdu, r.trap_oid, r.uptime_ticks, r.community) == (
        "v2c",
        "inform",
        "1.3.6.1.6.3.1.1.5.4",
        9,
        "c1",
    )
    assert r.response and len(r.varbinds) == 1  # sysUpTime / snmpTrapOID taken out
    assert "\x00" not in r.varbinds[0]["value"]
    with pytest.raises(td.TrapDecodeError) as e:
        td.decode_v1v2c(b"\x30\x03\x02\x01\x07")
    assert e.value.reason in ("parse_error", "unsupported")
    with pytest.raises(td.TrapDecodeError):
        td.decode_v1v2c(b"garbage")


# ------------------------------------------------------------------ rules
LINK_DOWN = rules.Rule(
    id=1,
    name="Link down",
    kind="trap",
    priority=1000,
    scope="global",
    group_id=None,
    device_id=None,
    match={
        "trap_oids": ["1.3.6.1.6.3.1.1.5.3"],
        "varbinds": [{"oid": "1.3.6.1.2.1.2.2.1.7", "op": "ne", "value": "2"}],
        "ports": "important",
    },
    action="warning",
    key_varbind_oid="1.3.6.1.2.1.2.2.1.1",
    clear_match={"trap_oids": ["1.3.6.1.6.3.1.1.5.4"], "varbinds": [], "ports": "any"},
    clear_after_min=1440,
)


def _trap(oid: str, idx: int | None = 5, admin: str = "1") -> dict:
    vbs = []
    if idx is not None:
        vbs.append({"oid": f"1.3.6.1.2.1.2.2.1.1.{idx}", "value": str(idx)})
        vbs.append({"oid": f"1.3.6.1.2.1.2.2.1.7.{idx}", "value": admin})
    return {"trap_oid": oid, "varbinds": vbs}


def test_rule_raise_clear_key_important() -> None:
    ctx = rules.Context(important=lambda i: i == 5)
    out = rules.evaluate(_trap("1.3.6.1.6.3.1.1.5.3"), [LINK_DOWN], ctx)
    assert out.raised == (LINK_DOWN, "5") and out.matched_rule_ids == [1]
    out = rules.evaluate(_trap("1.3.6.1.6.3.1.1.5.3", 7), [LINK_DOWN], ctx)
    assert out.raised is None and out.matched_rule_ids == []  # unimportant port
    out = rules.evaluate(_trap("1.3.6.1.6.3.1.1.5.3", admin="2"), [LINK_DOWN], ctx)
    assert out.raised is None  # admin down
    out = rules.evaluate(_trap("1.3.6.1.6.3.1.1.5.3", None), [LINK_DOWN], ctx)
    assert out.raised == (LINK_DOWN, "")  # no ifIndex -> important
    out = rules.evaluate(_trap("1.3.6.1.6.3.1.1.5.4"), [LINK_DOWN], ctx)
    assert out.clears == [(LINK_DOWN, "5")] and out.raised is None


def test_rule_flapping_suppressed_and_ignore_order() -> None:
    ctx = rules.Context(flapping=lambda i: i == 5)
    out = rules.evaluate(_trap("1.3.6.1.6.3.1.1.5.3"), [LINK_DOWN], ctx)
    assert out.raised is None and out.suppressed == (LINK_DOWN, "5")
    assert out.matched_rule_ids == [1]
    ignore = rules.Rule(2, "ign", "trap", 1, "global", None, None, {"trap_oids": []}, "ignore")
    out = rules.evaluate(_trap("1.3.6.1.6.3.1.1.5.3"), [ignore, LINK_DOWN], rules.Context())
    assert out.ignored is ignore and out.raised is None and out.matched_rule_ids == [2]


def test_varbind_ops() -> None:
    r = rules.Rule(1, "x", "trap", 1, "global", None, None, {}, "warning")
    vbs = [{"oid": "1.3.6.1.2.1.31.1.1.1.1.5", "value": "Gi1/0/5"}]
    c = rules.varbind_condition
    assert c({"oid": "1.3.6.1.2.1.31.1.1.1.1", "op": "eq", "value": "Gi1/0/5"}, vbs, r)
    assert c({"oid": "1.3.6.1.2.1.31.1.1.1.1", "op": "contains", "value": "gi1"}, vbs, r)
    assert c({"oid": "1.3.6.1.2.1.31.1.1.1.1", "op": "regex", "value": r"/0/\d$"}, vbs, r)
    assert c({"oid": "1.3.6.1.2.1.31.1.1.1.1", "op": "exists", "value": None}, vbs, r)
    assert not c({"oid": "1.3.6.1.2.1.31.1.1.1.1", "op": "absent", "value": None}, vbs, r)
    assert c({"oid": "1.3.6.1.9", "op": "ne", "value": "x"}, vbs, r)  # missing: ne true
    assert c({"oid": "1.3.6.1.9", "op": "absent", "value": None}, vbs, r)
    assert not c({"oid": "1.3.6.1.9", "op": "eq", "value": "x"}, vbs, r)


def test_syslog_rule_and_regex_key() -> None:
    r = rules.Rule(
        3,
        "if",
        "syslog",
        10,
        "global",
        None,
        None,
        {"severity_max": 4, "facilities": [23], "programs": ["IFNET"], "text": "down",
         "text_regex": False},
        "down",
        key_regex=r"interface (\S+)",
        clear_after_min=5,
    )  # fmt: skip
    rec = {"severity": 4, "facility": 23, "program": "ifnet",
           "message": "Physical state on the interface Ten-Gi1/0/49 changed to down."}  # fmt: skip
    out = rules.evaluate(rec, [r], rules.Context())
    assert out.raised == (r, "Ten-Gi1/0/49")
    assert rules.evaluate({**rec, "severity": 5}, [r], rules.Context()).raised is None
    assert rules.evaluate({**rec, "program": "x"}, [r], rules.Context()).raised is None


def test_scope() -> None:
    g = rules.Rule(1, "g", "trap", 1, "group", 7, None, {}, "warning")
    d = rules.Rule(2, "d", "trap", 1, "device", None, 3, {}, "warning")
    assert rules.in_scope(g, 3, [9, 7, 1]) and not rules.in_scope(g, 3, [9])
    assert rules.in_scope(d, 3, []) and not rules.in_scope(d, 4, [])


def test_validate_rule_errors() -> None:
    from netmon.api.errors import ApiError

    ok = rules.validate_rule(
        {"name": "n", "kind": "syslog", "match": {"severity_max": 3}, "action": "warning",
         "clear_after_min": 30, "key_regex": "(x)"}
    )  # fmt: skip
    assert ok["match"]["facilities"] == [] and ok["key_varbind_oid"] is None
    bad = [
        {"name": "n", "kind": "trap", "match": {}, "action": "warning"},  # no clearing
        {"name": "n", "kind": "trap", "match": {}, "action": "ignore", "clear_after_min": 5},
        {"name": "n", "kind": "syslog", "match": {"text": "(", "text_regex": True},
         "action": "ignore"},
        {"name": "n", "kind": "syslog", "match": {}, "action": "down", "clear_after_min": 5,
         "key_regex": "no group"},
        {"name": "n", "kind": "trap", "match": {"trap_oids": ["abc"]}, "action": "ignore"},
        {"name": "n", "kind": "trap", "match": {}, "action": "ignore", "scope": "group"},
    ]  # fmt: skip
    for b in bad:
        with pytest.raises(ApiError) as e:
            rules.validate_rule(b)
        assert e.value.status == 422


# ------------------------------------------------------------------ flapping vectors
def test_flap_tracker_vectors() -> None:
    f, p = FlapTracker(5, 10, 15), (1, 5)
    for t, d in ((0, "d"), (1, "u"), (2, "d"), (3, "u")):
        assert f.add(p, d, t * 60) is None
    c = f.add(p, "d", 4 * 60)
    assert c is not None and c.kind == "start" and c.count == 5
    assert f.add(p, "d", 4.5 * 60) is None  # duplicate
    for t, d in ((5, "u"), (6, "d"), (7, "u"), (8, "d"), (9, "u")):
        assert f.add(p, d, t * 60).kind == "update"  # type: ignore[union-attr]
    assert f.expire(23.9 * 60) == []
    ends = f.expire(24 * 60)
    assert [e.kind for e in ends] == ["end"] and not f.is_flapping(p)
    g = FlapTracker(5, 10, 15)
    assert all(
        g.add(p, d, t * 60) is None for t, d in ((0, "d"), (3, "u"), (6, "d"), (9, "u"), (12, "d"))
    )


# ------------------------------------------------------------------ forwarding
def test_render_5424_syslog_s5() -> None:
    rec = parse_syslog(
        b"<188>Nov  8 12:00:00 2026 FF5945-IRF %%10IFNET/4/PHY_UPDOWN: Physical state on the "
        b"interface Ten-GigabitEthernet1/0/49 changed to down.",
        RECV,
        TZ,
    )
    line = render_5424(
        {"kind": "syslog", "time": RECV, "source_ip": "10.0.0.12", **rec.__dict__}, "1.1.0"
    )
    assert line == (
        '<188>1 2026-11-08T10:00:00Z FF5945-IRF IFNET - PHY_UPDOWN [origin ip="10.0.0.12" '
        'software="NetworkMonitor" swVersion="1.1.0"] Physical state on the interface '
        "Ten-GigabitEthernet1/0/49 changed to down."
    )
    assert frame(line, "tcp").startswith(str(len(line.encode())).encode() + b" <188>1")
    assert len(frame("x" * 9000, "udp")) == 8192


def test_render_5424_trap_and_filters() -> None:
    rec = {"kind": "trap", "time": RECV, "source_ip": "10.0.0.5", "device_host": "sw1",
           "trap_name": "linkDown", "summary": "linkDown ifIndex=5", "raised_status": "down",
           "device_id": 1}  # fmt: skip
    line = render_5424(rec, "1.0")
    assert line.startswith("<130>1 2026-11-08T10:00:05Z sw1 snmptrap - linkDown [origin")
    t = Target.from_dict({"id": "a", "host": "h", "port": 1, "kinds": ["trap"], "min_severity": 4})
    assert t.accepts(rec)
    assert not t.accepts({**rec, "raised_status": None})  # severity 5 > 4
    assert not t.accepts({**rec, "device_id": None})  # unknown sources excluded
    t2 = Target.from_dict({"id": "b", "host": "h", "port": 1, "sources": ["10.1.0.0/16"]})
    assert not t2.accepts(rec)


# ------------------------------------------------------------------ attribution
def test_attribution_precedence() -> None:
    devices = [
        {"id": 1, "name": "sw1.mnidom.net", "host": "10.0.0.1", "enabled": True},
        {"id": 2, "name": "sw2", "host": "sw2.local", "enabled": True,
         "source_addresses": ["10.0.0.1"]},
        {"id": 3, "name": "fw", "host": "10.0.0.9", "enabled": False},
        {"id": 4, "name": "fw-b", "host": "10.0.0.9", "enabled": True},
    ]  # fmt: skip
    smap = attribution.build(devices, {"sw2.local": ["10.0.0.2"]})
    assert attribution.attribute(smap, "10.0.0.1") == 2  # explicit source address wins
    assert attribution.attribute(smap, "10.0.0.2") == 2  # resolved hostname
    assert attribution.attribute(smap, "10.0.0.9") == 4  # enabled before disabled
    assert attribution.attribute(smap, "::ffff:10.0.0.9") == 4  # mapped IPv6
    assert attribution.attribute(smap, "10.9.9.9", agent_addr="10.0.0.2") == 2
    assert attribution.attribute(smap, "10.9.9.9", trap_address="10.0.0.9") == 4
    assert attribution.attribute(smap, "10.9.9.9", hostname="SW1") == 1  # name first label
    assert attribution.attribute(smap, "10.9.9.9", hostname="fw") == 3
    assert attribution.attribute(smap, "10.9.9.9", hostname="nope") is None


def test_nat_suspect() -> None:
    assert attribution.nat_suspect([("172.17.0.1", False)] * 190 + [("10.0.0.1", True)] * 10) == (
        "172.17.0.1"
    )
    assert attribution.nat_suspect([("10.5.0.1", False)] * 200) is None
    assert (
        attribution.nat_suspect([("172.17.0.1", False)] * 100 + [("10.0.0.1", True)] * 100) is None
    )


def test_rate_limiter() -> None:
    b = TokenBucket(1.0, 2.0, now=0.0)
    assert b.take(0.0) and b.take(0.0) and not b.take(0.0) and b.take(1.0)
    rl = RateLimiter((200.0, 1000.0), (2000.0, 10000.0))
    ok = sum(1 for _ in range(3000) if rl.allow("1.2.3.4", now=0.0))
    assert ok == 1000


# ------------------------------------------------------------------ LAG
PORTS = [{"if_index": i, "kind": "physical"} for i in (1, 2, 3, 4)] + [
    {"if_index": 11, "kind": "lag"}
]


def test_resolve_lag_vectors() -> None:
    assert resolve_lag(PORTS, {3: 11, 4: 11, 1: 0}, {}) == {3: (11, True), 4: (11, True)}
    assert resolve_lag(PORTS, {3: 11, 4: 0}, {(11, 3): 1, (11, 4): 1}) == {
        3: (11, True),
        4: (11, False),
    }
    assert resolve_lag(PORTS, {}, {(11, 4): 1, (0, 11): 1, (4, 0): 1}) == {4: (11, None)}
    assert resolve_lag(PORTS, {3: 99}, {}) == {}
    assert resolve_lag(PORTS, {}, {(11, 3): 2}) == {}
    two = [*PORTS, {"if_index": 12, "kind": "lag"}]
    assert resolve_lag(two, {}, {(12, 3): 1, (11, 3): 1}) == {3: (11, None)}
    assert lag_sources({3: (11, True)}, {3: 11}, None) == ("ok", "ieee8023")
    assert lag_sources({3: (11, None)}, None, {(11, 3): 1}) == ("ok", "if_stack")
    assert lag_sources({}, {}, {}) == ("empty", None)
    assert lag_sources({}, None, None) == ("error", None)
    assert parse_lacp({"3": 11, "0": 5, "4": 0}) == {3: 11, 4: 0}
    assert parse_if_stack({"600.49": 1, "0.600": 1}) == {(600, 49): 1, (0, 600): 1}


def test_lag_inheritance_vectors() -> None:
    lag = {"if_index": 11, "kind": "lag", "bridge_port": 11, "pvid": 1, "mode": "trunk",
           "untagged_vlans": [1], "tagged_vlans": [10, 20]}  # fmt: skip
    m3 = {"if_index": 3, "kind": "physical", "lag_if_index": 11, "bridge_port": None,
          "pvid": None, "mode": None, "untagged_vlans": [], "tagged_vlans": []}  # fmt: skip
    vals, inherited = view.member_vlans(m3, lag)
    assert inherited and vals["untagged_vlans"] == [1] and vals["tagged_vlans"] == [10, 20]
    own = {**m3, "bridge_port": 3, "untagged_vlans": [5]}
    vals, inherited = view.member_vlans(own, lag)
    assert not inherited and vals["untagged_vlans"] == [5]
    m4 = {**m3, "if_index": 4}
    members, lag_of = view.lag_groups([lag, m3, m4, {"if_index": 1, "kind": "physical"}])
    assert members == {11: [3, 4]} and lag_of == {3: 11, 4: 11}
    a = {"link_id": 2, "kind": "device"}
    b = {"link_id": 1, "kind": "neighbor"}
    assert [n["link_id"] for n in view.merge_neighbors([b], [a, b])] == [2, 1]


# ------------------------------------------------------------------ cursors / buckets
def test_cursor_and_buckets() -> None:
    c = query.encode_cursor(RECV, 42)
    assert query.decode_cursor(c, 2) == [RECV, 42]
    from netmon.api.errors import ApiError

    with pytest.raises(ApiError) as e:
        query.decode_cursor("!!!", 2)
    assert e.value.code == "invalid_cursor"
    assert query.pick_bucket(3600) == 60
    assert query.pick_bucket(86400) == 900
    assert query.pick_bucket(31 * 86400) in (21600, 86400)


def test_bind_reason_reads_the_os_message() -> None:
    """§12.2.1: a held port reports the OS reason even when asyncio's message has none."""
    import socket

    from netmon.ingest.bind_error import bind_reason

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as held:
        held.bind(("127.0.0.1", 0))
        port = held.getsockname()[1]
        text = bind_reason(OSError("could not bind to local_addr"), "127.0.0.1", port, udp=True)
    assert text.startswith("could not bind to local_addr (")
    assert "in use" in text.lower() or "10048" in text
    assert bind_reason(OSError(98, "Address already in use"), "127.0.0.1", 1, udp=True) == (
        "[Errno 98] Address already in use"
    )


def test_cursor_keeps_sub_second_precision() -> None:
    """E2E 39/47: a cursor rounded to the second skipped rows from the same second."""
    from datetime import UTC, datetime

    from netmon.ingest.query import decode_cursor, encode_cursor

    t = datetime(2026, 11, 8, 10, 0, 0, 123456, tzinfo=UTC)
    assert decode_cursor(encode_cursor(t, 7), 2) == [t, 7]
