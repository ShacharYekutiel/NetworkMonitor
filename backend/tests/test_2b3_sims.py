"""[2b-3] Self-tests of the QA simulators (docs/architecture.md §12.14), so a broken fixture is
caught before it confuses a product test: the fake AD (tests/sim/ldap/fake_ad.py) driven by a real
ldap3 client through the
§12.7 login sequence, and the trap / syslog senders (tests/sim/ingest) against local sockets.

Backend tests can use the fake AD the same way:
    sys.path.insert(0, str(SIM / "ldap")); from fake_ad import FakeAD, FakeADThread, load_directory
"""

from __future__ import annotations

import socket
import ssl
import sys
import time
from pathlib import Path

import pytest

ldap3 = pytest.importorskip("ldap3")

SIM = Path(__file__).resolve().parents[2] / "tests" / "sim"
sys.path.insert(0, str(SIM / "ldap"))
sys.path.insert(0, str(SIM / "ingest"))

from fake_ad import FakeAD, FakeADThread, load_directory  # noqa: E402
from make_certs import make_certs  # noqa: E402

ADMINS = "CN=NetMon-Admins,OU=Groups,DC=mnidom,DC=net"
VIEWERS = "CN=NetMon-Viewers,OU=Groups,DC=mnidom,DC=net"
ATTRS = [
    "sAMAccountName",
    "userPrincipalName",
    "displayName",
    "mail",
    "objectGUID",
    "userAccountControl",
    "msDS-User-Account-Control-Computed",
    "accountExpires",
    "distinguishedName",
]


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    return make_certs(tmp_path_factory.mktemp("certs"))


@pytest.fixture(scope="module")
def ad(certs):
    with FakeADThread(FakeAD(load_directory(SIM / "ldap" / "mnidom.yaml"), certs)) as server:
        yield server


def _conn(ad, certs, user, pw, starttls=False):
    tls = ldap3.Tls(ca_certs_file=str(certs.ca), validate=ssl.CERT_REQUIRED)
    if starttls:
        s = ldap3.Server("localhost", port=ad.ldap_port, tls=tls, connect_timeout=3)
        c = ldap3.Connection(s, user, pw, receive_timeout=3, raise_exceptions=False)
        c.open()
        assert c.start_tls()
    else:
        s = ldap3.Server(
            "localhost",
            port=ad.ldaps_port,
            use_ssl=True,
            tls=tls,
            connect_timeout=3,
            get_info="ALL",
        )
        c = ldap3.Connection(s, user, pw, receive_timeout=3, raise_exceptions=False)
    c.bind()
    return c


def _role(svc, dn):
    from ldap3.utils.conv import escape_filter_chars

    for group, role in ((ADMINS, "admin"), (VIEWERS, "viewer")):
        svc.search(
            dn,
            f"(memberOf:1.2.840.113556.1.4.1941:={escape_filter_chars(group)})",
            ldap3.BASE,
            attributes=["1.1"],
        )
        if svc.entries:
            return role
    return None


@pytest.mark.parametrize(
    ("sam", "role", "bind_data"),
    [
        ("jdoe", "viewer", None),
        ("asmith", "admin", None),
        ("bhelp", "admin", None),
        ("nogroup", None, None),
        ("olduser", "viewer", "533"),
        ("lockeduser", "viewer", "775"),
        ("expired", "viewer", "532"),
    ],
)
def test_fake_ad_login_sequence(ad, certs, sam, role, bind_data):
    from ldap3.utils.conv import escape_filter_chars

    svc = _conn(ad, certs, "svc-netmon@mnidom.net", "Passw0rd!")
    assert svc.bound
    assert "1.2.840.113556.1.4.800" in [x[0] for x in svc.server.info.supported_features]
    svc.search(
        "DC=mnidom,DC=net",
        f"(&(objectCategory=person)(objectClass=user)(sAMAccountName={escape_filter_chars(sam)}))",
        ldap3.SUBTREE,
        attributes=ATTRS,
        size_limit=2,
    )
    assert len(svc.entries) == 1
    dn = svc.entries[0].entry_dn
    assert len(svc.entries[0].objectGUID.raw_values[0]) == 16
    assert _role(svc, dn) == role
    u = _conn(ad, certs, dn, "Passw0rd!")
    assert u.bound is (bind_data is None)
    if bind_data:
        assert f"data {bind_data}," in u.result["message"]


def test_fake_ad_name_forms_injection_and_size_limit(ad, certs):
    assert _conn(ad, certs, "MNIDOM\\jdoe", "Passw0rd!").bound
    assert _conn(ad, certs, "John.Doe@MNIDOM.NET", "Passw0rd!").bound
    bad = _conn(ad, certs, "jdoe@mnidom.net", "nope")
    assert not bad.bound and "data 52e," in bad.result["message"]
    svc = _conn(ad, certs, "svc-netmon@mnidom.net", "Passw0rd!", starttls=True)
    assert svc.bound
    svc.search(
        "DC=mnidom,DC=net",
        "(sAMAccountName=\\2a\\29\\28uid=\\2a)",
        ldap3.SUBTREE,
        attributes=["1.1"],
    )
    assert svc.entries == []
    svc.search(
        "DC=mnidom,DC=net",
        "(&(objectClass=user)(!(sAMAccountName=jdoe)))",
        ldap3.SUBTREE,
        attributes=["1.1"],
        size_limit=2,
    )
    assert svc.result["description"] == "sizeLimitExceeded"


def test_fake_ad_records_binds_and_rejects_unbound_search(ad, certs):
    ad.binds.clear()
    anon = ldap3.Connection(
        ldap3.Server(
            "localhost",
            port=ad.ldaps_port,
            use_ssl=True,
            tls=ldap3.Tls(ca_certs_file=str(certs.ca), validate=ssl.CERT_REQUIRED),
        ),
        authentication=ldap3.ANONYMOUS,
        raise_exceptions=False,
    )
    assert anon.bind()
    anon.search("DC=mnidom,DC=net", "(objectClass=*)", ldap3.SUBTREE)
    assert anon.result["description"] == "operationsError"
    assert ad.binds and ad.binds[-1]["password_empty"] is True


def test_fake_ad_wrong_cert_and_hang(certs):
    d = load_directory(SIM / "ldap" / "mnidom.yaml")
    with FakeADThread(FakeAD(d, certs, wrong_cert=True)) as wrong:
        s = ldap3.Server(
            "localhost",
            port=wrong.ldaps_port,
            use_ssl=True,
            connect_timeout=2,
            tls=ldap3.Tls(ca_certs_file=str(certs.ca), validate=ssl.CERT_REQUIRED),
        )
        with pytest.raises(ldap3.core.exceptions.LDAPSocketOpenError):
            ldap3.Connection(s, "svc-netmon@mnidom.net", "Passw0rd!").bind()
    with FakeADThread(FakeAD(d, certs, hang=True)) as hung:
        s = ldap3.Server(
            "localhost",
            port=hung.ldaps_port,
            use_ssl=True,
            connect_timeout=1,
            tls=ldap3.Tls(ca_certs_file=str(certs.ca), validate=ssl.CERT_REQUIRED),
        )
        t0 = time.monotonic()
        with pytest.raises(ldap3.core.exceptions.LDAPSocketReceiveError):
            ldap3.Connection(s, "svc-netmon@mnidom.net", "Passw0rd!", receive_timeout=1).bind()
        assert time.monotonic() - t0 < 4


def test_send_trap_v1_v2c_and_syslog_vectors():
    import send_syslog
    import send_trap
    from pyasn1.codec.ber import decoder
    from pysnmp.proto import api

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(3)
    port = rx.getsockname()[1]
    vbs = send_trap.parse_varbinds(
        ["1.3.6.1.2.1.2.2.1.1.5=i:5", "1.3.6.1.2.1.2.2.1.6.5=x:001122334455"]
    )
    send_trap.send_udp(
        send_trap.build_v1("public", "1.3.6.1.6.3.1.1.5.3", vbs, "10.0.0.5"),
        ("127.0.0.1", port),
        "127.0.0.81",
    )
    data, peer = rx.recvfrom(65535)
    assert peer[0] == "127.0.0.81"
    p1 = api.PROTOCOL_MODULES[api.SNMP_VERSION_1]
    pdu = p1.apiMessage.get_pdu(decoder.decode(data, asn1Spec=p1.Message())[0])
    assert int(p1.apiTrapPDU.get_generic_trap(pdu)) == 2
    assert p1.apiTrapPDU.get_agent_address(pdu).prettyPrint() == "10.0.0.5"
    raw, rid = send_trap.build_v2c("public", "1.3.6.1.6.3.1.1.5.4", vbs)
    send_trap.send_udp(raw, ("127.0.0.1", port), "127.0.0.82")
    data, peer = rx.recvfrom(65535)
    assert peer[0] == "127.0.0.82"
    p2 = api.PROTOCOL_MODULES[api.SNMP_VERSION_2C]
    pdu = p2.apiMessage.get_pdu(decoder.decode(data, asn1Spec=p2.Message())[0])
    assert int(p2.apiPDU.get_request_id(pdu)) == rid
    vec = send_syslog.load_vectors()
    assert vec["S1"].startswith(b"<165>1 ") and b"\xef\xbb\xbf" in vec["S1"]
    assert b"\x00" in vec["S11"] and len(vec["S11"]) > 9000
    send_syslog.send([vec["S5"]], ("127.0.0.1", port), "127.0.0.83")
    data, peer = rx.recvfrom(65535)
    assert peer[0] == "127.0.0.83" and data == vec["S5"]
    rx.close()
