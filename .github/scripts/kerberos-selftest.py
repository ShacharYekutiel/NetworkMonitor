#!/usr/bin/env python3
"""[1.4.2] CI: the Kerberos SSO acceptor works inside the hardened app container.

Run inside the netmon container (`docker compose exec -T netmon python - < this file`).
Does what the app does when Kerberos SSO is turned on (netmon.auth.kerberos): writes a
krb5.conf and a keytab under ${NETMON_DATA_DIR}/run (the only writable place besides /tmp),
points KRB5_CONFIG / KRB5RCACHEDIR there, then runs the app's real acceptor
(`gss_accept`: MIT krb5 through python-gssapi) on a malformed token. Expected: a clean
`KerberosFailure` (the keytab was read and the token refused), not a crash or a permission
error from the read-only root filesystem or the dropped capabilities.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

from netmon.auth import kerberos, kerberos_pure


def _counted(b: bytes) -> bytes:
    return struct.pack(">H", len(b)) + b


def keytab(principal: str) -> bytes:
    name, realm = principal.rsplit("@", 1)
    comps = name.split("/")
    body = struct.pack(">H", len(comps)) + _counted(realm.encode())
    body += b"".join(_counted(c.encode()) for c in comps)
    body += struct.pack(">IIB", 1, 1700000000, 2)
    body += struct.pack(">H", 18) + _counted(os.urandom(32))  # aes256-cts-hmac-sha1-96
    return bytes([0x05, 0x02]) + struct.pack(">i", len(body)) + body


def main() -> int:
    run = Path(os.environ.get("NETMON_DATA_DIR", "/data")) / "run" / "ci-kerberos"
    run.mkdir(parents=True, exist_ok=True)
    conf = run / "krb5.conf"
    conf.write_text(kerberos_pure.krb5_conf("CI.LOCAL"), encoding="utf-8")
    kt = run / "http.keytab"
    kt.write_bytes(keytab("HTTP/netmonitor.ci.local@CI.LOCAL"))
    os.environ["KRB5_CONFIG"] = str(conf)
    os.environ["KRB5RCACHEDIR"] = str(run)
    ok, why = kerberos.gssapi_available()
    print("gssapi available:", ok, why or "")
    if not ok:
        return 1
    # a krb5 mechanism token (OID 1.2.840.113554.1.2.2, AP-REQ id) with a garbage body
    oid = bytes.fromhex("06092a864886f712010202")
    inner = oid + b"\x01\x00" + b"\x6e\x03\x02\x01\x05"
    token = b"\x60" + bytes([len(inner)]) + inner
    try:
        kerberos.gss_accept(str(kt), token)
    except kerberos.KerberosFailure as exc:
        print("acceptor refused the malformed token as expected:", str(exc)[:120])
        for p in run.iterdir():
            p.unlink()
        run.rmdir()
        return 0
    print("unexpected: the malformed token was accepted")
    return 1


if __name__ == "__main__":
    sys.exit(main())
