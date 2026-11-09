#!/usr/bin/env python3
"""Fake Active Directory: an asyncio LDAPv3 server built on ldap3's own ASN.1 definitions
(`ldap3.protocol.rfc4511` + pyasn1 BER), docs/architecture.md §12.14.

Supports simple Bind (DN, UPN or DOMAIN\\user; empty password = anonymous success, like AD), rootDSE,
Search (base/one/sub; and/or/not/equality/present/substrings/>=/<=, extensibleMatch
1.2.840.113556.1.4.1941 (transitive) on memberOf/member, 1.2.840.113556.1.4.803/804 bit matches; attribute
selection incl. `1.1`; sizeLimit), StartTLS (extended op 1.3.6.1.4.1.1466.20037), an LDAPS listener,
Unbind/Abandon, AD-style bind errors (`80090308: LdapErr: DSID-0C09044E, comment: AcceptSecurityContext
error, data 52e, v4563`), and a hang mode (connections are accepted and never answered).

CLI (E2E, tests/e2e/run.sh):

    python tests/sim/ldap/fake_ad.py --data tests/sim/ldap/mnidom.yaml --certs DIR \
        --ldaps-port 16360 --ldap-port 13890 --control-port 18389 [--hang] [--wrong-cert] [--record binds.jsonl]

  --certs DIR    output of make_certs.py (created when missing); --wrong-cert serves wrong.pem (CN wrong.test)
  --control-port HTTP control API on 127.0.0.1:
                 GET /health, GET /binds (JSON list), DELETE /binds, GET /state,
                 POST /hang/on, POST /hang/off, POST /stop (close the LDAP listeners), POST /start
  Prints "READY ldaps=<port> ldap=<port>" once listening.

In-process (backend pytest):

    from fake_ad import FakeAD, load_directory          # sys.path += tests/sim/ldap
    ad = FakeAD(load_directory(path), certs=make_certs(tmp))
    await ad.start()                 # free ports: ad.ldaps_port, ad.ldap_port
    ...; ad.binds                    # list of BindRecord dicts; ad.hang = True
    await ad.stop()
  or `with FakeADThread(FakeAD(...)) as ad:` (own event loop in a daemon thread).

Bind records: {"time", "listener": "ldaps"|"ldap", "tls": bool, "name", "password_empty": bool,
"result": 0|49, "data": "52e"|None, "principal": dn|None}. Passwords are never recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from ldap3.protocol import rfc4511 as r
from pyasn1.codec.ber import decoder, encoder
from pyasn1.error import PyAsn1Error

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from make_certs import CertPaths, make_certs  # noqa: E402

STARTTLS_OID = "1.3.6.1.4.1.1466.20037"
WHOAMI_OID = "1.3.6.1.4.1.4203.1.11.3"
IN_CHAIN = "1.2.840.113556.1.4.1941"
BIT_AND = "1.2.840.113556.1.4.803"
BIT_OR = "1.2.840.113556.1.4.804"
AD_CAPABILITY = "1.2.840.113556.1.4.800"

# LDAP result codes
SUCCESS, OPERATIONS_ERROR, PROTOCOL_ERROR, SIZE_LIMIT, AUTH_UNKNOWN = 0, 1, 2, 4, 7
NO_SUCH_OBJECT, INVALID_CREDENTIALS, UNAVAILABLE, UNWILLING = 32, 49, 52, 53

CONSTRUCTED = {"msds-user-account-control-computed"}
BINARY = {"objectguid", "objectsid"}


def ad_bind_error(data: str) -> str:
    return f"80090308: LdapErr: DSID-0C09044E, comment: AcceptSecurityContext error, data {data}, v4563"


NOT_BOUND = (
    "000004DC: LdapErr: DSID-0C090A5C, comment: In order to perform this operation a successful bind "
    "must be completed on the connection., data 0, v4563"
)


def norm_dn(dn: str) -> str:
    """Case-insensitive DN comparison key (spaces around , and = removed; escapes kept)."""
    out, parts, cur, esc = [], [], [], False
    for ch in dn:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            cur.append(ch)
            esc = True
        elif ch == ",":
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    for p in parts:
        if "=" in p:
            a, v = p.split("=", 1)
            out.append(f"{a.strip().lower()}={v.strip().lower()}")
        elif p.strip():
            out.append(p.strip().lower())
    return ",".join(out)


def parent_dn(ndn: str) -> str:
    # ndn is normalised; split on the first unescaped comma
    i, esc = 0, False
    for i, ch in enumerate(ndn):
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ",":
            return ndn[i + 1 :]
    return ""


@dataclass
class Entry:
    dn: str
    attrs: dict[str, tuple[str, list[Any]]] = field(default_factory=dict)  # lower name -> (Name, values)
    password: str | None = None
    bind_error: str | None = None
    bind_error_always: bool = False

    @property
    def ndn(self) -> str:
        return norm_dn(self.dn)

    def set(self, name: str, values: list[Any]) -> None:
        self.attrs[name.lower()] = (name, list(values))

    def get(self, name: str) -> list[Any]:
        return self.attrs.get(name.lower(), (name, []))[1]

    def first(self, name: str) -> Any:
        v = self.get(name)
        return v[0] if v else None


@dataclass
class Directory:
    domain: str
    netbios: str
    base_dn: str
    entries: dict[str, Entry]  # ndn -> entry

    def by_sam(self, sam: str) -> Entry | None:
        for e in self.entries.values():
            if str(e.first("sAMAccountName") or "").lower() == sam.lower():
                return e
        return None

    def by_upn(self, upn: str) -> Entry | None:
        for e in self.entries.values():
            if str(e.first("userPrincipalName") or "").lower() == upn.lower():
                return e
        return None

    def resolve_principal(self, name: str) -> Entry | None:
        if not name:
            return None
        if "\\" in name:
            dom, sam = name.split("\\", 1)
            if dom.lower() in (self.netbios.lower(), self.domain.split(".")[0].lower(), self.domain.lower()):
                return self.by_sam(sam)
            return None
        if "=" in name:
            return self.entries.get(norm_dn(name))
        if "@" in name:
            return self.by_upn(name)
        return self.by_sam(name)

    def member_of_closure(self, ndn: str) -> set[str]:
        """Normalised DNs of every group `ndn` belongs to, transitively."""
        seen: set[str] = set()
        todo = [ndn]
        while todo:
            cur = self.entries.get(todo.pop())
            if not cur:
                continue
            for g in cur.get("memberOf"):
                ng = norm_dn(g)
                if ng not in seen:
                    seen.add(ng)
                    todo.append(ng)
        return seen

    def member_closure(self, ndn: str) -> set[str]:
        seen: set[str] = set()
        todo = [ndn]
        while todo:
            cur = self.entries.get(todo.pop())
            if not cur:
                continue
            for m in cur.get("member"):
                nm = norm_dn(m)
                if nm not in seen:
                    seen.add(nm)
                    todo.append(nm)
        return seen


def _guid(seed: str) -> bytes:
    return hashlib.md5(seed.encode()).digest()  # noqa: S324 - deterministic test GUID


def load_directory(path: str | Path | dict) -> Directory:
    data = path if isinstance(path, dict) else yaml.safe_load(Path(path).read_text())
    domain = data["domain"]
    base = data.get("base_dn") or ",".join(f"DC={p}" for p in domain.split("."))
    netbios = data.get("netbios_domain") or domain.split(".")[0].upper()
    users_ou = data.get("users_ou", "OU=Users")
    groups_ou = data.get("groups_ou", "OU=Groups")
    default_pw = data.get("default_password", "Passw0rd!")
    entries: dict[str, Entry] = {}

    def add(e: Entry) -> Entry:
        entries[e.ndn] = e
        return e

    # domain root + containers
    root = add(Entry(base))
    root.set("objectClass", ["top", "domain", "domainDNS"])
    root.set("distinguishedName", [base])
    root.set("dc", [domain.split(".")[0]])
    containers: set[str] = set()

    def ensure_ous(ou: str) -> None:
        rdns = [x.strip() for x in ou.split(",") if x.strip()]
        for i in range(len(rdns) - 1, -1, -1):
            dn = ",".join(rdns[i:] + [base])
            if norm_dn(dn) in containers:
                continue
            containers.add(norm_dn(dn))
            e = add(Entry(dn))
            e.set("objectClass", ["top", "organizationalUnit"])
            e.set("distinguishedName", [dn])
            e.set("ou", [rdns[i].split("=", 1)[1]])

    category_person = f"CN=Person,CN=Schema,CN=Configuration,{base}"
    category_group = f"CN=Group,CN=Schema,CN=Configuration,{base}"
    group_dn: dict[str, str] = {}
    for g in data.get("groups", []):
        ou = g.get("ou", groups_ou)
        ensure_ous(ou)
        dn = f"CN={g['name']},{ou},{base}"
        group_dn[g["name"].lower()] = dn
        e = add(Entry(dn))
        e.set("objectClass", ["top", "group"])
        e.set("objectCategory", [category_group])
        e.set("cn", [g["name"]])
        e.set("name", [g["name"]])
        e.set("sAMAccountName", [g.get("sam", g["name"])])
        e.set("distinguishedName", [dn])
        e.set("objectGUID", [_guid("group:" + g["name"])])
        e.set("groupType", ["-2147483646"])
        if g.get("description"):
            e.set("description", [g["description"]])

    def link(member: Entry, groups: list[str]) -> None:
        for gname in groups or []:
            gdn = group_dn[gname.lower()]
            ge = entries[norm_dn(gdn)]
            ge.set("member", ge.get("member") + [member.dn])
            member.set("memberOf", member.get("memberOf") + [gdn])

    for g in data.get("groups", []):
        link(entries[norm_dn(group_dn[g["name"].lower()])], g.get("groups", []))

    for u in data.get("users", []):
        ou = u.get("ou", users_ou)
        ensure_ous(ou)
        cn = u.get("cn", u["sam"])
        dn = f"CN={cn.replace(',', chr(92) + ',')},{ou},{base}"
        e = add(Entry(dn, password=str(u.get("password", default_pw))))
        e.bind_error = str(u["bind_error"]) if u.get("bind_error") else None
        e.bind_error_always = bool(u.get("bind_error_always"))
        e.set("objectClass", ["top", "person", "organizationalPerson", "user"])
        e.set("objectCategory", [category_person])
        e.set("cn", [cn])
        e.set("name", [cn])
        e.set("distinguishedName", [dn])
        e.set("sAMAccountName", [u["sam"]])
        e.set("userPrincipalName", [u.get("upn", f"{u['sam']}@{domain}")])
        if u.get("display_name"):
            e.set("displayName", [u["display_name"]])
        if u.get("mail"):
            e.set("mail", [u["mail"]])
        e.set("objectGUID", [_guid("user:" + u["sam"].lower())])
        e.set("userAccountControl", [str(int(u.get("uac", 512)))])
        e.set("msDS-User-Account-Control-Computed", [str(int(u.get("computed", 0)))])
        e.set("accountExpires", [str(int(u.get("account_expires", 9223372036854775807)))])
        e.set("primaryGroupID", ["513"])
        link(e, u.get("groups", []))
    return Directory(domain=domain, netbios=netbios, base_dn=base, entries=entries)


# ---------------------------------------------------------------------------------------------- filters
def _s(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _octets(v: Any) -> bytes:
    return bytes(v) if not isinstance(v, str) else v.encode()


def _tlv(buf: bytes, i: int) -> tuple[int, bytes, int]:
    """(tag byte, value, next index) of the BER TLV at buf[i]. Single-byte tags only (all LDAP needs)."""
    tag = buf[i]
    n = buf[i + 1]
    j = i + 2
    if n >= 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[j : j + k], "big")
        j += k
    return tag, buf[j : j + n], j + n


def _tlvs(buf: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i < len(buf):
        t, v, i = _tlv(buf, i)
        out.append((t, v))
    return out


def _int(v: bytes) -> int:
    return int.from_bytes(v, "big", signed=True) if v else 0


def parse_filter(tag: int, v: bytes) -> tuple:
    """RFC 4511 Filter (BER) -> tuple tree. Hand-written: ldap3's recursive pyasn1 spec cannot decode `not`."""
    num = tag & 0x1F
    if num == 0:
        return ("and", [parse_filter(t, x) for t, x in _tlvs(v)])
    if num == 1:
        return ("or", [parse_filter(t, x) for t, x in _tlvs(v)])
    if num == 2:
        (t, x), = _tlvs(v)
        return ("not", parse_filter(t, x))
    if num in (3, 5, 6, 8):
        (_, a), (_, val) = _tlvs(v)
        return ({3: "eq", 5: "ge", 6: "le", 8: "approx"}[num], a.decode("utf-8", "replace"), val)
    if num == 4:
        (_, a), (_, subs) = _tlvs(v)
        return ("sub", a.decode("utf-8", "replace"),
                [({0: "initial", 1: "any", 2: "final"}[t & 0x1F], x) for t, x in _tlvs(subs)])
    if num == 7:
        return ("present", v.decode("utf-8", "replace"))
    if num == 9:
        parts = {t & 0x1F: x for t, x in _tlvs(v)}
        return ("ext", parts.get(1, b"").decode(), parts.get(2, b"").decode(), parts.get(3, b""),
                bool(parts.get(4, b"\x00")[0]))
    raise ValueError(f"unknown filter tag {tag:#x}")


def filter_str(f: tuple) -> str:
    k = f[0]
    if k in ("and", "or"):
        return "(" + ("&" if k == "and" else "|") + "".join(filter_str(x) for x in f[1]) + ")"
    if k == "not":
        return "(!" + filter_str(f[1]) + ")"
    if k == "present":
        return f"({f[1]}=*)"
    if k == "sub":
        return f"({f[1]}=~" + "|".join(f"{a}:{_s(b)}" for a, b in f[2]) + ")"
    if k == "ext":
        return f"({f[2]}:{f[1]}:={_s(f[3])})"
    op = {"eq": "=", "ge": ">=", "le": "<=", "approx": "~="}[k]
    return f"({f[1]}{op}{_s(f[2])})"


@dataclass
class SearchReq:
    base: str
    scope: int
    size_limit: int
    types_only: bool
    filter: tuple
    attributes: list[str]


def parse_search_request(v: bytes) -> SearchReq:
    items = _tlvs(v)
    base, scope, _deref, size, _time, types_only = (x[1] for x in items[:6])
    ftag, fval = items[6]
    attrs = [a.decode("utf-8", "replace") for _, a in _tlvs(items[7][1])] if len(items) > 7 else []
    return SearchReq(base.decode("utf-8", "replace"), _int(scope), _int(size), bool(types_only and types_only[0]),
                     parse_filter(ftag, fval), attrs)


class FilterEval:
    def __init__(self, d: Directory):
        self.d = d

    def eq(self, e: Entry, attr: str, val: bytes) -> bool:
        a = attr.lower()
        if a in BINARY:
            return any(_octets(x) == val for x in e.get(attr))
        sval = _s(val).lower()
        for x in e.get(attr):
            xs = _s(x).lower()
            if xs == sval:
                return True
            if a == "objectcategory" and xs.startswith("cn=") and xs.split(",", 1)[0][3:] == sval:
                return True
            if a in ("member", "memberof", "distinguishedname") and norm_dn(xs) == norm_dn(sval):
                return True
        return False

    def match(self, e: Entry, f: tuple) -> bool:
        k = f[0]
        if k == "and":
            return all(self.match(e, x) for x in f[1])
        if k == "or":
            return any(self.match(e, x) for x in f[1])
        if k == "not":
            return not self.match(e, f[1])
        if k in ("eq", "approx"):
            return self.eq(e, f[1], f[2])
        if k == "present":
            return f[1].lower() == "objectclass" or bool(e.get(f[1]))
        if k in ("ge", "le"):
            v = _s(f[2])
            for x in e.get(f[1]):
                try:
                    xi, vi = int(_s(x)), int(v)
                    if (xi >= vi) if k == "ge" else (xi <= vi):
                        return True
                except ValueError:
                    xs = _s(x).lower()
                    if (xs >= v.lower()) if k == "ge" else (xs <= v.lower()):
                        return True
            return False
        if k == "sub":
            parts = [(kind, _s(b).lower()) for kind, b in f[2]]
            for x in e.get(f[1]):
                xs, pos, ok = _s(x).lower(), 0, True
                for kind, sub in parts:
                    if kind == "initial":
                        ok = xs.startswith(sub)
                        pos = len(sub)
                    elif kind == "any":
                        i = xs.find(sub, pos)
                        ok = i >= 0
                        pos = i + len(sub) if ok else pos
                    else:
                        ok = xs.endswith(sub) and len(xs) - len(sub) >= pos
                    if not ok:
                        break
                if ok:
                    return True
            return False
        if k == "ext":
            _, rule, attr, raw, _dn = f
            val = _s(raw)
            if rule == IN_CHAIN:
                if attr.lower() == "memberof":
                    return norm_dn(val) in self.d.member_of_closure(e.ndn)
                if attr.lower() == "member":
                    return norm_dn(val) in self.d.member_closure(e.ndn)
                return self.eq(e, attr, raw)
            if rule in (BIT_AND, BIT_OR):
                want = int(val)
                for x in e.get(attr):
                    xi = int(_s(x)) & 0xFFFFFFFF
                    if ((xi & want) == want) if rule == BIT_AND else ((xi & want) != 0):
                        return True
                return False
            return self.eq(e, attr, raw) if attr else False
        return False


# ---------------------------------------------------------------------------------------------- server
def _encode(mid: int, op_name: str, op: Any) -> bytes:
    m = r.LDAPMessage()
    m["messageID"] = mid
    po = r.ProtocolOp()
    po.setComponentByName(op_name, op)
    m["protocolOp"] = po
    return encoder.encode(m)


def _result(cls: type, code: int, msg: str = "", matched: str = "") -> Any:
    res = cls()
    res["resultCode"] = code
    res["matchedDN"] = matched
    res["diagnosticMessage"] = msg
    return res


def _frame_len(buf: bytes) -> int | None:
    """Total length of the first BER TLV in buf, or None when more bytes are needed."""
    if len(buf) < 2:
        return None
    first = buf[1]
    if first < 0x80:
        return 2 + first
    n = first & 0x7F
    if n == 0 or n > 4:
        raise ValueError("unsupported BER length")
    if len(buf) < 2 + n:
        return None
    return 2 + n + int.from_bytes(buf[2 : 2 + n], "big")


class FakeAD:
    def __init__(
        self,
        directory: Directory,
        certs: CertPaths | None = None,
        *,
        wrong_cert: bool = False,
        hang: bool = False,
        record_path: str | Path | None = None,
        host: str = "127.0.0.1",
    ):
        self.d = directory
        self.certs = certs
        self.wrong_cert = wrong_cert
        self.hang = hang
        self.host = host
        self.record_path = Path(record_path) if record_path else None
        self.binds: list[dict] = []
        self.searches: list[dict] = []
        self.connections = 0
        self.ldaps_port = 0
        self.ldap_port = 0
        self._servers: list[asyncio.base_events.Server] = []
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task] = set()
        self._want = (0, 0)
        self.f = FilterEval(directory)

    # -------------------------------------------------------------------- lifecycle
    def ssl_context(self) -> ssl.SSLContext:
        if not self.certs:
            raise RuntimeError("no certificates configured")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if self.wrong_cert:
            ctx.load_cert_chain(self.certs.wrong, self.certs.wrong_key)
        else:
            ctx.load_cert_chain(self.certs.server, self.certs.server_key)
        return ctx

    async def start(self, ldaps_port: int | None = 0, ldap_port: int | None = 0) -> None:
        """Ports 0 = free port; None = that listener is not started."""
        self._want = (ldaps_port, ldap_port)
        if ldaps_port is not None and self.certs:
            srv = await asyncio.start_server(
                lambda rd, wr: self._conn(rd, wr, "ldaps"), self.host, ldaps_port, ssl=self.ssl_context()
            )
            self.ldaps_port = srv.sockets[0].getsockname()[1]
            self._servers.append(srv)
        if ldap_port is not None:
            srv = await asyncio.start_server(lambda rd, wr: self._conn(rd, wr, "ldap"), self.host, ldap_port)
            self.ldap_port = srv.sockets[0].getsockname()[1]
            self._servers.append(srv)

    async def stop(self) -> None:
        for s in self._servers:
            s.close()
        for w in list(self._writers):
            with contextlib.suppress(Exception):
                w.close()
        for t in list(self._tasks):
            t.cancel()
        for s in self._servers:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(s.wait_closed(), 2)
        self._servers = []

    async def restart(self) -> None:
        """Re-open the listeners on the same ports (after stop())."""
        a, b = self.ldaps_port or self._want[0], self.ldap_port or self._want[1]
        await self.start(a if self._want[0] is not None else None, b if self._want[1] is not None else None)

    @property
    def running(self) -> bool:
        return bool(self._servers)

    # -------------------------------------------------------------------- connection
    def _record(self, rec: dict) -> None:
        self.binds.append(rec)
        if self.record_path:
            with self.record_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")

    async def _conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, listener: str) -> None:
        self.connections += 1
        self._writers.add(writer)
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        state = {"bound": None, "tls": listener == "ldaps"}  # bound: principal DN ("" = anonymous)
        buf = b""
        try:
            if self.hang:
                await asyncio.sleep(3600)
                return
            while True:
                n = _frame_len(buf)
                while n is None or len(buf) < n:
                    chunk = await reader.read(65536)
                    if not chunk:
                        return
                    buf += chunk
                    n = _frame_len(buf)
                frame, buf = buf[:n], buf[n:]
                if self.hang:
                    await asyncio.sleep(3600)
                    return
                try:
                    _, body, _ = _tlv(frame, 0)
                    (_, mid_raw, k) = _tlv(body, 0)
                    if body[k] == 0x63:  # [APPLICATION 3] SearchRequest: parsed by hand (see parse_filter)
                        _, sv, _ = _tlv(body, k)
                        for chunk_out in self._search(_int(mid_raw), parse_search_request(sv), state):
                            writer.write(chunk_out)
                        await writer.drain()
                        continue
                    msg, _ = decoder.decode(frame, asn1Spec=r.LDAPMessage())
                except (PyAsn1Error, ValueError, IndexError) as exc:
                    print(f"fake_ad: undecodable request ({exc}); closing", flush=True)
                    return
                mid = int(msg["messageID"])
                op = msg["protocolOp"]
                name = op.getName()
                comp = op.getComponent()
                if name == "unbindRequest":
                    return
                if name == "abandonRequest":
                    continue
                if name == "bindRequest":
                    writer.write(self._bind(mid, comp, state, listener))
                elif name == "extendedReq":
                    oid = _s(comp["requestName"])
                    if oid == STARTTLS_OID:
                        if state["tls"]:
                            writer.write(_encode(mid, "extendedResp", _result(r.ExtendedResponse, OPERATIONS_ERROR, "TLS already active")))
                            continue
                        resp = _result(r.ExtendedResponse, SUCCESS)
                        resp["responseName"] = STARTTLS_OID
                        writer.write(_encode(mid, "extendedResp", resp))
                        await writer.drain()
                        await writer.start_tls(self.ssl_context())
                        state["tls"] = True
                        continue
                    if oid == WHOAMI_OID:
                        resp = _result(r.ExtendedResponse, SUCCESS)
                        resp["responseValue"] = f"u:{self.d.netbios}\\{state['bound'] or ''}".encode()
                        writer.write(_encode(mid, "extendedResp", resp))
                    else:
                        writer.write(_encode(mid, "extendedResp", _result(r.ExtendedResponse, PROTOCOL_ERROR, "unsupported extended operation")))
                else:
                    resp_name = {
                        "modifyRequest": ("modifyResponse", r.ModifyResponse),
                        "addRequest": ("addResponse", r.AddResponse),
                        "delRequest": ("delResponse", r.DelResponse),
                        "modDNRequest": ("modDNResponse", r.ModifyDNResponse),
                        "compareRequest": ("compareResponse", r.CompareResponse),
                    }.get(name)
                    if resp_name:
                        writer.write(_encode(mid, resp_name[0], _result(resp_name[1], UNWILLING, "read-only fake AD")))
                await writer.drain()
        except (ConnectionError, ssl.SSLError, asyncio.IncompleteReadError, OSError):
            return
        except Exception as exc:  # noqa: BLE001 - a fixture bug must be visible, not a silent disconnect
            import traceback

            print(f"fake_ad: handler error: {exc!r}", flush=True)
            traceback.print_exc()
            return
        except asyncio.CancelledError:
            raise
        finally:
            self._writers.discard(writer)
            if task:
                self._tasks.discard(task)
            with contextlib.suppress(Exception):
                writer.close()

    def _bind(self, mid: int, req: Any, state: dict, listener: str) -> bytes:
        name = _s(req["name"])
        auth = req["authentication"]
        rec: dict[str, Any] = {"time": time.time(), "listener": listener, "tls": state["tls"], "name": name}
        if auth.getName() != "simple":
            rec.update(password_empty=None, result=AUTH_UNKNOWN, data=None, principal=None)
            self._record(rec)
            return _encode(mid, "bindResponse", _result(r.BindResponse, AUTH_UNKNOWN, "only simple bind is supported"))
        pw = bytes(auth.getComponent()).decode("utf-8", "replace")
        rec["password_empty"] = pw == ""
        if pw == "":
            # AD: an unauthenticated simple bind (name, empty password) succeeds as anonymous
            state["bound"] = ""
            rec.update(result=SUCCESS, data=None, principal=None)
            self._record(rec)
            return _encode(mid, "bindResponse", _result(r.BindResponse, SUCCESS))
        e = self.d.resolve_principal(name)
        if e is None or e.password is None:
            data = "52e"
        elif e.bind_error_always and e.bind_error:
            data = e.bind_error
        elif pw != e.password:
            data = "52e"
        elif e.bind_error:
            data = e.bind_error
        else:
            data = None
        if data:
            state["bound"] = None
            rec.update(result=INVALID_CREDENTIALS, data=data, principal=e.dn if e else None)
            self._record(rec)
            return _encode(mid, "bindResponse", _result(r.BindResponse, INVALID_CREDENTIALS, ad_bind_error(data)))
        state["bound"] = e.dn
        rec.update(result=SUCCESS, data=None, principal=e.dn)
        self._record(rec)
        return _encode(mid, "bindResponse", _result(r.BindResponse, SUCCESS))

    def _root_dse(self) -> Entry:
        e = Entry("")
        b = self.d.base_dn
        e.set("defaultNamingContext", [b])
        e.set("rootDomainNamingContext", [b])
        e.set("configurationNamingContext", [f"CN=Configuration,{b}"])
        e.set("schemaNamingContext", [f"CN=Schema,CN=Configuration,{b}"])
        e.set("namingContexts", [b, f"CN=Configuration,{b}"])
        e.set("supportedLDAPVersion", ["3", "2"])
        e.set("supportedCapabilities", [AD_CAPABILITY, "1.2.840.113556.1.4.1670", "1.2.840.113556.1.4.1791"])
        e.set("supportedExtension", [STARTTLS_OID, WHOAMI_OID])
        e.set("supportedControl", ["1.2.840.113556.1.4.319"])
        e.set("supportedSASLMechanisms", ["GSSAPI", "GSS-SPNEGO", "EXTERNAL", "DIGEST-MD5"])
        e.set("dnsHostName", ["dc1.test"])
        e.set("serverName", [f"CN=DC1,CN=Servers,CN=Default-First-Site-Name,CN=Sites,CN=Configuration,{b}"])
        e.set("domainFunctionality", ["7"])
        e.set("forestFunctionality", ["7"])
        e.set("isSynchronized", ["TRUE"])
        return e

    def _entry_msg(self, mid: int, e: Entry, attrs: list[str], types_only: bool) -> bytes:
        res = r.SearchResultEntry()
        res["object"] = e.dn
        pal = res["attributes"]
        want = [a.lower() for a in attrs]
        if "1.1" in want and len(want) == 1:
            names: list[str] = []
        elif not want or "*" in want:
            names = [k for k in e.attrs if k not in CONSTRUCTED] + [a for a in want if a in e.attrs and a in CONSTRUCTED]
        else:
            names = [a for a in want if a in e.attrs]
        for i, a in enumerate(dict.fromkeys(names)):
            disp, vals = e.attrs[a]
            pa = r.PartialAttribute()
            pa["type"] = disp
            if not types_only:
                for j, v in enumerate(vals):
                    pa["vals"].setComponentByPosition(j, _octets(v) if isinstance(v, bytes) else str(v).encode())
            else:
                pa["vals"]  # noqa: B018 - initialise the empty SetOf
            pal.setComponentByPosition(i, pa)
        if not names:
            pal  # noqa: B018
        return _encode(mid, "searchResEntry", res)

    def _search(self, mid: int, req: SearchReq, state: dict) -> list[bytes]:
        base, scope, size_limit, attrs, types_only, flt = (
            req.base, req.scope, req.size_limit, req.attributes, req.types_only, req.filter)
        self.searches.append({"time": time.time(), "base": base, "scope": scope, "bound": state["bound"],
                              "filter": filter_str(flt)[:2000], "attributes": attrs, "size_limit": size_limit})
        if base == "" and scope == 0:
            e = self._root_dse()
            out = [self._entry_msg(mid, e, attrs, types_only)] if self.f.match(e, flt) else []
            return out + [_encode(mid, "searchResDone", _result(r.SearchResultDone, SUCCESS))]
        if not state["bound"]:
            return [_encode(mid, "searchResDone", _result(r.SearchResultDone, OPERATIONS_ERROR, NOT_BOUND))]
        nbase = norm_dn(base)
        if nbase not in self.d.entries:
            return [_encode(mid, "searchResDone", _result(
                r.SearchResultDone, NO_SUCH_OBJECT,
                f"0000208D: NameErr: DSID-03100241, problem 2001 (NO_OBJECT), data 0, best match of:\n\t'{self.d.base_dn}'\n",
                self.d.base_dn))]
        hits: list[Entry] = []
        for ndn, e in self.d.entries.items():
            if scope == 0:
                inscope = ndn == nbase
            elif scope == 1:
                inscope = parent_dn(ndn) == nbase
            else:
                inscope = ndn == nbase or ndn.endswith("," + nbase)
            if inscope and self.f.match(e, flt):
                hits.append(e)
        out: list[bytes] = []
        for i, e in enumerate(hits):
            if size_limit and i >= size_limit:
                out.append(_encode(mid, "searchResDone", _result(r.SearchResultDone, SIZE_LIMIT, "Size limit exceeded")))
                return out
            out.append(self._entry_msg(mid, e, attrs, types_only))
        out.append(_encode(mid, "searchResDone", _result(r.SearchResultDone, SUCCESS)))
        return out


class FakeADThread:
    """Runs a FakeAD on its own event loop in a daemon thread (for sync tests)."""

    def __init__(self, ad: FakeAD, ldaps_port: int | None = 0, ldap_port: int | None = 0):
        self.ad = ad
        self.ports = (ldaps_port, ldap_port)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="fake-ad")

    def call(self, coro: Any, timeout: float = 10) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def __enter__(self) -> FakeAD:
        self.thread.start()
        self.call(self.ad.start(*self.ports))
        return self.ad

    def __exit__(self, *exc: object) -> None:
        with contextlib.suppress(Exception):
            self.call(self.ad.stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


# ---------------------------------------------------------------------------------------------- control
async def _control(ad: FakeAD, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = (await reader.readline()).decode("latin-1").strip()
        while (await reader.readline()).strip():
            pass
        method, path = (line.split(" ") + ["", ""])[:2]
        code, body = 200, {"ok": True}
        if method == "GET" and path.startswith("/health"):
            pass
        elif method == "GET" and path.startswith("/binds"):
            body = ad.binds
        elif method == "DELETE" and path.startswith("/binds"):
            ad.binds.clear()
            ad.searches.clear()
            if ad.record_path:
                ad.record_path.write_text("")
        elif method == "GET" and path.startswith("/searches"):
            body = ad.searches
        elif method == "GET" and path.startswith("/state"):
            body = {"hang": ad.hang, "running": ad.running, "ldaps_port": ad.ldaps_port,
                    "ldap_port": ad.ldap_port, "connections": ad.connections}
        elif method == "POST" and path.startswith("/hang/on"):
            ad.hang = True
        elif method == "POST" and path.startswith("/hang/off"):
            ad.hang = False
            for w in list(ad._writers):  # drop the hung connections
                with contextlib.suppress(Exception):
                    w.close()
        elif method == "POST" and path.startswith("/stop"):
            await ad.stop()
        elif method == "POST" and path.startswith("/start"):
            if not ad.running:
                await ad.restart()
        else:
            code, body = 404, {"error": "not found"}
        data = json.dumps(body).encode()
        writer.write(
            f"HTTP/1.1 {code} {'OK' if code == 200 else 'Not Found'}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
        )
        await writer.drain()
    finally:
        writer.close()


async def _main(a: argparse.Namespace) -> None:
    certs = make_certs(a.certs) if a.certs else None
    ad = FakeAD(load_directory(a.data), certs, wrong_cert=a.wrong_cert, hang=a.hang, record_path=a.record,
                host=a.host)
    if ad.record_path:
        ad.record_path.write_text("")
    await ad.start(a.ldaps_port if a.ldaps_port >= 0 else None, a.ldap_port if a.ldap_port >= 0 else None)
    if a.control_port:
        await asyncio.start_server(lambda rd, wr: _control(ad, rd, wr), "127.0.0.1", a.control_port)
    print(f"fake_ad: base {ad.d.base_dn}, {len(ad.d.entries)} entries, hang={ad.hang}, "
          f"wrong_cert={ad.wrong_cert}", flush=True)
    print(f"READY ldaps={ad.ldaps_port} ldap={ad.ldap_port} control={a.control_port}", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    p = argparse.ArgumentParser(description="Fake Active Directory (LDAPv3) for NetworkMonitor tests")
    p.add_argument("--data", default=str(HERE / "mnidom.yaml"))
    p.add_argument("--certs", default="", help="directory for make_certs.py output (needed for LDAPS/StartTLS)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--ldaps-port", type=int, default=0, help="0 = free port, -1 = no LDAPS listener")
    p.add_argument("--ldap-port", type=int, default=0, help="0 = free port, -1 = no plain/StartTLS listener")
    p.add_argument("--control-port", type=int, default=0)
    p.add_argument("--hang", action="store_true", help="accept connections, never answer")
    p.add_argument("--wrong-cert", action="store_true", help="serve the wrong-name certificate")
    p.add_argument("--record", default="", help="append bind records (JSON lines) to this file")
    a = p.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main(a))


if __name__ == "__main__":
    main()
