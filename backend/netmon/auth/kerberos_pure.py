"""[3] Kerberos SSO, pure parts (architecture § 13.5.1, data-model § Users, SSO).

- `token_mech(token)`: which mechanism a browser's `Authorization: Negotiate` token carries
  (`kerberos` | `ntlm` | `invalid`) — NTLM is refused before GSSAPI ever sees the token.
- `split_principal("jdoe@MNIDOM.NET")` → ("jdoe", "MNIDOM.NET"), service principals refused.
- `parse_keytab(bytes)`: MIT keytab file format 0x0502 → principals, kvno, enctypes (key
  material is read past, never returned).
- `krb5_conf(realm)`: the generated krb5.conf of the acceptor.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------- OIDs
OID_SPNEGO = "1.3.6.1.5.5.2"
OID_KRB5 = "1.2.840.113554.1.2.2"
OID_MS_KRB5 = "1.2.840.48018.1.2.2"
OID_KRB5_U2U = "1.2.840.113554.1.2.2.3"
OID_NTLM = "1.3.6.1.4.1.311.2.2.10"
KERBEROS_OIDS = frozenset({OID_KRB5, OID_MS_KRB5, OID_KRB5_U2U})
NTLM_SIGNATURE = b"NTLMSSP\x00"

MAX_TOKEN_BYTES = 64 * 1024


class _DerError(ValueError):
    pass


def _tlv(buf: bytes, pos: int) -> tuple[int, int, int]:
    """(tag, value_start, value_end) of the DER element at `pos` (definite lengths only)."""
    if pos + 2 > len(buf):
        raise _DerError("truncated")
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        if n == 0 or n > 4 or pos + n > len(buf):
            raise _DerError("bad length")
        length = int.from_bytes(buf[pos : pos + n], "big")
        pos += n
    end = pos + length
    if end > len(buf):
        raise _DerError("truncated value")
    return tag, pos, end


def _children(buf: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    out = []
    pos = start
    while pos < end:
        tag, vs, ve = _tlv(buf, pos)
        if ve > end:
            raise _DerError("child overflows")
        out.append((tag, vs, ve))
        pos = ve
    return out


def decode_oid(raw: bytes) -> str:
    if not raw:
        raise _DerError("empty oid")
    first = raw[0]
    parts = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
    value = 0
    for b in raw[1:]:
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(value)
            value = 0
    if raw[-1] & 0x80:
        raise _DerError("truncated oid")
    return ".".join(str(p) for p in parts)


def encode_oid(oid: str) -> bytes:
    """DER OID element (tag 0x06) — used by tests to build tokens."""
    nums = [int(x) for x in oid.split(".")]
    body = bytearray([40 * nums[0] + nums[1]])
    for n in nums[2:]:
        chunk = [n & 0x7F]
        n >>= 7
        while n:
            chunk.append(0x80 | (n & 0x7F))
            n >>= 7
        body.extend(reversed(chunk))
    return der(0x06, bytes(body))


def der(tag: int, value: bytes) -> bytes:
    n = len(value)
    if n < 0x80:
        head = bytes([tag, n])
    else:
        lb = n.to_bytes((n.bit_length() + 7) // 8, "big")
        head = bytes([tag, 0x80 | len(lb)]) + lb
    return head + value


def spnego_init(mech_oids: list[str], mech_token: bytes | None = None) -> bytes:
    """Build a SPNEGO NegTokenInit (tests and the simulators)."""
    mech_list = der(0x30, b"".join(encode_oid(o) for o in mech_oids))
    fields = der(0xA0, mech_list)
    if mech_token is not None:
        fields += der(0xA2, der(0x04, mech_token))
    neg = der(0xA0, der(0x30, fields))
    return der(0x60, encode_oid(OID_SPNEGO) + neg)


def token_mech(token: bytes) -> str:
    """`kerberos` | `ntlm` | `invalid` (data-model § NTLM detection vectors)."""
    if not token or len(token) > MAX_TOKEN_BYTES:
        return "invalid"
    if token.startswith(NTLM_SIGNATURE):
        return "ntlm"
    try:
        tag, vs, ve = _tlv(token, 0)
        if tag != 0x60:
            return "invalid"
        otag, os_, oe = _tlv(token, vs)
        if otag != 0x06:
            return "invalid"
        oid = decode_oid(token[os_:oe])
        if oid in KERBEROS_OIDS:  # a raw GSS Kerberos token (no SPNEGO wrapper)
            return "kerberos"
        if oid != OID_SPNEGO:
            return "invalid"
        ntag, ns, ne = _tlv(token, oe)
        if ntag != 0xA0:  # NegTokenInit; a NegTokenResp (0xA1) is never a first leg
            return "invalid"
        stag, ss, se = _tlv(token, ns)
        if stag != 0x30:
            return "invalid"
        mechs: list[str] = []
        mech_token: bytes | None = None
        for ftag, fs, _fe in _children(token, ss, se):
            if ftag == 0xA0:
                ltag, ls, le = _tlv(token, fs)
                if ltag != 0x30:
                    return "invalid"
                for mtag, ms, me in _children(token, ls, le):
                    if mtag != 0x06:
                        return "invalid"
                    mechs.append(decode_oid(token[ms:me]))
            elif ftag == 0xA2:
                otag2, o2s, o2e = _tlv(token, fs)
                if otag2 != 0x04:
                    return "invalid"
                mech_token = token[o2s:o2e]
    except (_DerError, IndexError):
        return "invalid"
    if mech_token is not None and mech_token.startswith(NTLM_SIGNATURE):
        return "ntlm"
    if not mechs:
        return "invalid"
    if mechs[0] == OID_NTLM:  # the browser prefers NTLM (no Kerberos ticket)
        return "ntlm"
    if any(m in KERBEROS_OIDS for m in mechs):
        return "kerberos"
    if OID_NTLM in mechs:
        return "ntlm"
    return "invalid"


# ---------------------------------------------------------------- principals
def split_principal(principal: str) -> tuple[str, str] | None:
    """`jdoe@MNIDOM.NET` → ("jdoe", "MNIDOM.NET"); realm upper-cased; service principals
    (`HTTP/x@R`), several `@`, empty parts → None."""
    if not isinstance(principal, str) or principal.count("@") != 1:
        return None
    name, _, realm = principal.partition("@")
    if not name or not realm or "/" in name or "\\" in name:
        return None
    if any(ord(c) < 32 for c in principal):
        return None
    return name, realm.upper()


def default_realms(ldap_domain: str | None) -> list[str]:
    d = (ldap_domain or "").strip().upper()
    return [d] if d else []


# ---------------------------------------------------------------- keytab
ENCTYPE_NAMES = {
    1: "des-cbc-crc",
    3: "des-cbc-md5",
    16: "des3-cbc-sha1",
    17: "aes128-cts-hmac-sha1-96",
    18: "aes256-cts-hmac-sha1-96",
    19: "aes128-cts-hmac-sha256-128",
    20: "aes256-cts-hmac-sha384-192",
    23: "arcfour-hmac",
    24: "arcfour-hmac-exp",
    25: "camellia128-cts-cmac",
    26: "camellia256-cts-cmac",
}
MAX_KEYTAB_BYTES = 64 * 1024


class KeytabError(ValueError):
    pass


@dataclass(frozen=True)
class KeytabEntry:
    principal: str
    kvno: int
    enctype: int

    def out(self) -> dict[str, Any]:
        return {
            "principal": self.principal,
            "kvno": self.kvno,
            "enctype": self.enctype,
            "enctype_name": ENCTYPE_NAMES.get(self.enctype, f"etype-{self.enctype}"),
        }


class _Reader:
    def __init__(self, data: bytes, pos: int, end: int) -> None:
        self.data, self.pos, self.end = data, pos, end

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > self.end:
            raise KeytabError("The keytab file is truncated")
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int(struct.unpack(">H", self.take(2))[0])

    def u32(self) -> int:
        return int(struct.unpack(">I", self.take(4))[0])

    def counted(self) -> bytes:
        return self.take(self.u16())


def parse_keytab(data: bytes) -> list[KeytabEntry]:
    """MIT keytab (file format 0x0502, big-endian). Raises KeytabError (→ 422
    `invalid_keytab`) on another version, truncation or no entries."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 2:
        raise KeytabError("Not a keytab file")
    if len(data) > MAX_KEYTAB_BYTES:
        raise KeytabError("The keytab file is larger than 64 KiB")
    if data[0] != 0x05:
        raise KeytabError("Not a keytab file")
    if data[1] != 0x02:
        raise KeytabError(f"Unsupported keytab version 0x05{data[1]:02x} (only 0x0502)")
    entries: list[KeytabEntry] = []
    pos = 2
    n = len(data)
    while pos < n:
        if pos + 4 > n:
            raise KeytabError("The keytab file is truncated")
        size = struct.unpack(">i", data[pos : pos + 4])[0]
        pos += 4
        if size == 0:
            break  # a zero-size slot ends the used part of the file
        if size < 0:  # deleted entry (hole)
            if pos + (-size) > n:
                raise KeytabError("The keytab file is truncated")
            pos += -size
            continue
        end = pos + size
        if end > n:
            raise KeytabError("The keytab file is truncated")
        r = _Reader(bytes(data), pos, end)
        count = r.u16()
        realm = r.counted().decode("utf-8", "replace")
        comps = [r.counted().decode("utf-8", "replace") for _ in range(count)]
        r.u32()  # name type
        r.u32()  # timestamp
        kvno = r.u8()
        enctype = r.u16()
        r.counted()  # key material: skipped, never kept
        if r.end - r.pos >= 4:
            kvno32 = r.u32()
            if kvno32:
                kvno = kvno32
        if not comps or not realm:
            raise KeytabError("The keytab file has an entry without a principal")
        entries.append(KeytabEntry("/".join(comps) + "@" + realm, kvno, enctype))
        pos = end
    if not entries:
        raise KeytabError("The keytab file contains no keys")
    return entries


def keytab_warnings(entries: list[KeytabEntry]) -> list[str]:
    out: list[str] = []
    if entries and all(e.enctype in (23, 24) for e in entries):
        out.append("rc4_only")
    if not any(e.principal.upper().startswith("HTTP/") for e in entries):
        out.append("no_http_principal")
    return out


def krb5_conf(default_realm: str | None) -> str:
    lines = ["[libdefaults]"]
    if default_realm:
        lines.append(f"    default_realm = {default_realm}")
    lines += [
        "    dns_canonicalize_hostname = false",
        "    rdns = false",
        "    ignore_acceptor_hostname = true",
        "    dns_lookup_kdc = false",
        "    dns_lookup_realm = false",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- Negotiate header
def negotiate_token(header: str | None) -> bytes | None:
    """The decoded token of `Authorization: Negotiate <b64>`; None when absent; b"" when the
    header is `Negotiate` with an undecodable value."""
    import base64
    import binascii

    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "negotiate":
        return None
    value = value.strip()
    if not value:
        return b""
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return b""
