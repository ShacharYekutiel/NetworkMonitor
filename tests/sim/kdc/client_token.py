#!/usr/bin/env python3
"""Kerberos/SPNEGO and NTLM client tokens for the phase-3 SSO tests (architecture.md §13.5.1, §13.11).

    KRB5_CONFIG=$KDC_DIR/krb5.conf backend/.venv/bin/python tests/sim/kdc/client_token.py spnego --user jdoe \
        --kdc-dir $KDC_DIR --service HTTP@localhost            # prints the base64 SPNEGO token (Negotiate header)
    ... spnego --raw-krb5 ...                                  # a raw Kerberos AP-REQ token (no SPNEGO wrapper)
    ... ntlm raw | ntlm spnego | ntlm spnego-mechlist-krb5-ntlm-token | garbage     # refusal fixtures
    ... big --size 49152                                       # a valid-looking Negotiate header of ~48 KB
    ... call --base http://127.0.0.1:8080 --user jdoe --kdc-dir DIR     # GET /api/auth/sso/kerberos with it

The NTLM fixtures are deterministic byte strings (an NTLMSSP NEGOTIATE_MESSAGE, raw and wrapped in a SPNEGO
NegTokenInit whose mechTypes offer only NTLM, and a NegTokenInit offering Kerberos but carrying an NTLMSSP
mechToken) matching the data-model NTLM detection vectors. Importable: spnego_token(), ntlm_raw(),
ntlm_spnego(), ntlm_in_krb5_list().
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import subprocess
import sys
import urllib.request

OID_SPNEGO = "1.3.6.1.5.5.2"
OID_KRB5 = "1.2.840.113554.1.2.2"
OID_MS_KRB5 = "1.2.840.48018.1.2.2"
OID_NTLM = "1.3.6.1.4.1.311.2.2.10"


def der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + der_len(len(body)) + body


def oid(s: str) -> bytes:
    parts = [int(x) for x in s.split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        enc = [p & 0x7F]
        p >>= 7
        while p:
            enc.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(enc))
    return tlv(0x06, bytes(out))


def neg_token_init(mechs: list[str], mech_token: bytes | None) -> bytes:
    body = tlv(0xA0, tlv(0x30, b"".join(oid(m) for m in mechs)))
    if mech_token is not None:
        body += tlv(0xA2, tlv(0x04, mech_token))
    inner = tlv(0xA0, tlv(0x30, body))
    return tlv(0x60, oid(OID_SPNEGO) + inner)


def ntlm_raw() -> bytes:
    flags = 0xE2088297
    return b"NTLMSSP\x00" + struct.pack("<II", 1, flags) + struct.pack("<HHI", 0, 0, 0) * 2 + \
        bytes([10, 0, 0x61, 0x4A, 0, 0, 0, 15])


def ntlm_spnego() -> bytes:
    return neg_token_init([OID_NTLM], ntlm_raw())


def ntlm_in_krb5_list() -> bytes:
    return neg_token_init([OID_MS_KRB5, OID_KRB5, OID_NTLM], ntlm_raw())


def spnego_token(user: str, kdc_dir: str, service: str = "HTTP@localhost", raw_krb5: bool = False) -> bytes:
    import gssapi  # noqa: PLC0415

    cc = f"FILE:{kdc_dir}/ccache_{user}"
    pw = dict(x.split() for x in open(f"{kdc_dir}/passwords").read().splitlines())[user]
    env = dict(os.environ, KRB5_CONFIG=f"{kdc_dir}/krb5.conf")
    subprocess.run(["kinit", "-c", cc, user], input=pw + "\n", text=True, env=env, check=True,
                   capture_output=True)
    os.environ["KRB5_CONFIG"] = f"{kdc_dir}/krb5.conf"
    creds = gssapi.Credentials(usage="initiate", store={"ccache": cc})
    name = gssapi.Name(service, gssapi.NameType.hostbased_service)
    mech = gssapi.Mechanism.from_sasl_name("GS2-KRB5") if raw_krb5 else gssapi.Mechanism.from_sasl_name("SPNEGO")
    ctx = gssapi.SecurityContext(name=name, creds=creds, usage="initiate", mech=mech,
                                 flags=gssapi.RequirementFlag.mutual_authentication)
    return ctx.step()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("spnego")
    s.add_argument("--user", default="jdoe")
    s.add_argument("--kdc-dir", default=os.environ.get("KDC_DIR", ""))
    s.add_argument("--service", default="HTTP@localhost")
    s.add_argument("--raw-krb5", action="store_true")
    n = sub.add_parser("ntlm")
    n.add_argument("kind", choices=["raw", "spnego", "spnego-mechlist-krb5-ntlm-token"])
    sub.add_parser("garbage")
    b = sub.add_parser("big")
    b.add_argument("--size", type=int, default=48 * 1024)
    c = sub.add_parser("call")
    c.add_argument("--base", required=True)
    c.add_argument("--user", default="jdoe")
    c.add_argument("--kdc-dir", default=os.environ.get("KDC_DIR", ""))
    c.add_argument("--service", default="HTTP@localhost")
    a = ap.parse_args()
    if a.cmd == "spnego":
        tok = spnego_token(a.user, a.kdc_dir, a.service, a.raw_krb5)
    elif a.cmd == "ntlm":
        tok = {"raw": ntlm_raw, "spnego": ntlm_spnego, "spnego-mechlist-krb5-ntlm-token": ntlm_in_krb5_list}[a.kind]()
    elif a.cmd == "garbage":
        tok = b"\x01\x02garbage-not-a-token"
    elif a.cmd == "big":
        # a SPNEGO-shaped blob padded to the size (the server must accept the header size; the token is invalid)
        pad = os.urandom(max(0, a.size * 3 // 4 - 64))
        tok = neg_token_init([OID_MS_KRB5, OID_KRB5], b"\x6e\x82" + pad)
    else:
        tok = spnego_token(a.user, a.kdc_dir, a.service)
        req = urllib.request.Request(a.base + "/api/auth/sso/kerberos",
                                     headers={"Authorization": "Negotiate " + base64.b64encode(tok).decode()})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                print(json.dumps({"status": r.status, "headers": dict(r.headers), "body": json.loads(r.read())}))
        except urllib.error.HTTPError as e:
            print(json.dumps({"status": e.code, "headers": dict(e.headers), "body": e.read().decode()[:500]}))
        return 0
    print(base64.b64encode(tok).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
