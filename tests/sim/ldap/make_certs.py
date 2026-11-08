#!/usr/bin/env python3
"""Test PKI for the 2b-3 simulators (docs/architecture.md §12.14): a CA, a server certificate for
`localhost` / `127.0.0.1` / `dc1.test` / `dc2.test`, and a wrong-name certificate (`wrong.test` only).

    python tests/sim/ldap/make_certs.py OUT_DIR

Writes OUT_DIR/{ca.pem, ca.key, server.pem, server.key, wrong.pem, wrong.key} (idempotent: existing files
are kept). The backend trusts the CA via NETMON_EXTRA_CA_FILE=OUT_DIR/ca.pem. Used by the fake AD
(fake_ad.py), the Teams mock (tests/sim/teams_mock.py) and the SIEM sink TLS listener.
Importable: `make_certs(out_dir) -> CertPaths`.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import sys
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SERVER_NAMES = ["localhost", "dc1.test", "dc2.test"]
SERVER_IPS = ["127.0.0.1"]
WRONG_NAMES = ["wrong.test"]


@dataclass(frozen=True)
class CertPaths:
    dir: Path
    ca: Path
    ca_key: Path
    server: Path
    server_key: Path
    wrong: Path
    wrong_key: Path


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _write_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _name(cn: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NetworkMonitor QA"),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ]
    )


def _leaf(ca_key, ca_cert, cn: str, dns: list[str], ips: list[str]):
    key = _key()
    now = dt.datetime.now(dt.UTC)
    san = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def make_certs(out_dir: str | Path) -> CertPaths:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = CertPaths(
        dir=d,
        ca=d / "ca.pem",
        ca_key=d / "ca.key",
        server=d / "server.pem",
        server_key=d / "server.key",
        wrong=d / "wrong.pem",
        wrong_key=d / "wrong.key",
    )
    if all(f.exists() for f in (p.ca, p.ca_key, p.server, p.server_key, p.wrong, p.wrong_key)):
        return p
    now = dt.datetime.now(dt.UTC)
    ca_key = _key()
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("NetworkMonitor QA Test CA"))
        .issuer_name(_name("NetworkMonitor QA Test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(p.ca_key, ca_key)
    p.ca.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    for (cert_path, key_path, cn, dns, ips) in (
        (p.server, p.server_key, "localhost", SERVER_NAMES, SERVER_IPS),
        (p.wrong, p.wrong_key, "wrong.test", WRONG_NAMES, []),
    ):
        k, c = _leaf(ca_key, ca_cert, cn, dns, ips)
        _write_key(key_path, k)
        cert_path.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return p


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    paths = make_certs(sys.argv[1])
    print(f"certs in {paths.dir}: ca.pem server.pem/.key wrong.pem/.key")
