#!/usr/bin/env python3
"""Test PKI for IAM Roles Anywhere and the HTTPS Bedrock mock (docs/architecture.md §14.11, §14.2.3).

    python tests/sim/bedrock/pki.py OUT_DIR          # writes the files below + OUT_DIR/manifest.json

Hierarchy (mirrors the owner's AD CS): `QA PKI-ROOT-CA` -> `QA Example Issuing CA` (issuing) -> leaves.
The mock's default trust anchor is the **issuing CA** (owner recommendation, no chain upload needed);
`--anchor root` on the mock requires the chain (`X-Amz-X509-Chain`).

Files (PEM unless noted; keys PKCS#8 unencrypted unless noted):
    root.pem root.key  issuing.pem issuing.key  other-ca.pem other-ca.key (unrelated CA, "wrong CA")
    chain.pem                       issuing CA only (what the admin uploads as the chain)
    leaf-rsa.pem leaf-rsa.key       RSA 2048, CN=netmonitor.example.net, CA:FALSE, KU digitalSignature,
                                    EKU clientAuth, serial 0x1234 (decimal 4660), valid 1 year
    leaf-rsa.pkcs1.key              same key, PKCS#1 ("BEGIN RSA PRIVATE KEY")
    leaf-rsa.enc.key                same key, encrypted PKCS#8, password `Key-pass-1`
    leaf-rsa-bundle.pem             leaf + issuing CA in one file (cert[+chain] upload)
    leaf-rsa.pfx                    PKCS#12 (leaf + key + issuing CA), password `Pfx-pass-1` (AD CS export)
    leaf-ec.pem leaf-ec.key         ECDSA P-256 (serial 0x5678), leaf-ec.sec1.key (SEC1), leaf-ec.pfx
    leaf-ec384.pem leaf-ec384.key   ECDSA P-384
    leaf-expired.pem/.key           expired 10 days ago                  -> 422 invalid_certificate (upload)
    leaf-expiring.pem/.key          expires in 10 days                   -> warning cert_expiring
    leaf-wrongca.pem/.key           issued by other-ca                   -> CreateSession 403 "Untrusted certificate"
    leaf-catrue.pem/.key            CA:TRUE                              -> 422 invalid_certificate
    leaf-nodigsig.pem/.key          KU keyEncipherment only              -> 422 invalid_certificate
    leaf-rsa1024.pem/.key           RSA 1024                             -> 422 invalid_certificate (unsupported key)
    leaf-rsa-root.pem/.key          leaf issued directly by the root (needs anchor root, no chain)
    server.pem server.key           TLS server cert for localhost/127.0.0.1 (+ *.amazonaws.com names) by issuing CA
    server-bundle.pem               server + issuing CA (what a TLS server must present)
    trust.pem                       root + issuing (append to NETMON_EXTRA_CA_FILE so the backend trusts the mock)
    [4] leaf-rsa.p7b / leaf-rsa.p7b.der   PKCS#7 bundle (leaf + issuing CA), PEM and DER (AD CS / certreq output)
    [4] leaf-cn-space.pem/.key      CN "NetMonitor Server" (a space)    -> warning cn_not_source_identity_safe

Importable: `make_pki(out_dir) -> dict` (the manifest; idempotent - existing manifest is reused).

CSR flow (owner decision: the server generates the key, the PKI admin issues the certificate):
    python tests/sim/bedrock/pki.py sign-csr OUT_DIR request.csr issued.pem [--days 365] [--ca issuing|other]
signs a PKCS#10 request (PEM or DER) with the issuing CA (or the unrelated CA) as a Roles Anywhere client cert
(CA:FALSE, digitalSignature, clientAuth, subject taken from the CSR). Importable: `sign_csr(dir, csr_bytes)`.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs7, pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

LEAF_CN = "netmonitor.example.net"
PFX_PASSWORD = "Pfx-pass-1"
KEY_PASSWORD = "Key-pass-1"
NOW = dt.datetime.now(dt.timezone.utc)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "IL"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NetworkMonitor QA"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _rsa(bits: int = 2048):
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def _ku(digsig: bool = True, ca: bool = False) -> x509.KeyUsage:
    return x509.KeyUsage(digital_signature=digsig, content_commitment=False, key_encipherment=not digsig or not ca,
                         data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                         encipher_only=False, decipher_only=False)


def _cert(subject_key, cn: str, issuer_cert, issuer_key, *, ca: bool, serial: int | None = None,
          not_before: dt.datetime | None = None, not_after: dt.datetime | None = None, digsig: bool = True,
          eku_client: bool = True, san_dns: list[str] | None = None, san_ip: list[str] | None = None,
          eku_server: bool = False) -> x509.Certificate:
    subject = _name(cn)
    b = (x509.CertificateBuilder()
         .subject_name(subject)
         .issuer_name(issuer_cert.subject if issuer_cert is not None else subject)
         .public_key(subject_key.public_key())
         .serial_number(serial or x509.random_serial_number())
         .not_valid_before(not_before or NOW - dt.timedelta(days=1))
         .not_valid_after(not_after or NOW + dt.timedelta(days=365))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
         .add_extension(_ku(digsig, ca), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()), critical=False))
    if issuer_cert is not None:
        b = b.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
    ekus = []
    if eku_client and not ca:
        ekus.append(ExtendedKeyUsageOID.CLIENT_AUTH)
    if eku_server:
        ekus.append(ExtendedKeyUsageOID.SERVER_AUTH)
    if ekus:
        b = b.add_extension(x509.ExtendedKeyUsage(ekus), critical=False)
    if san_dns or san_ip:
        names: list[x509.GeneralName] = [x509.DNSName(d) for d in (san_dns or [])]
        names += [x509.IPAddress(ipaddress.ip_address(i)) for i in (san_ip or [])]
        b = b.add_extension(x509.SubjectAlternativeName(names), critical=False)
    return b.sign(issuer_key if issuer_key is not None else subject_key, hashes.SHA256())


def _pem(c: x509.Certificate) -> bytes:
    return c.public_bytes(serialization.Encoding.PEM)


def _key_pem(k, fmt=serialization.PrivateFormat.PKCS8, password: str | None = None) -> bytes:
    enc = serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    return k.private_bytes(serialization.Encoding.PEM, fmt, enc)


def make_pki(out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())

    files: dict[str, str] = {}

    def w(name: str, data: bytes) -> str:
        p = out / name
        p.write_bytes(data)
        p.chmod(0o600 if name.endswith((".key", ".pfx")) else 0o644)
        files[name] = str(p)
        return str(p)

    root_k = _rsa(3072)
    root = _cert(root_k, "QA PKI-ROOT-CA", None, None, ca=True, not_after=NOW + dt.timedelta(days=3650))
    iss_k = _rsa(2048)
    iss = _cert(iss_k, "QA Example Issuing CA", root, root_k, ca=True, not_after=NOW + dt.timedelta(days=1825))
    other_k = ec.generate_private_key(ec.SECP256R1())
    other = _cert(other_k, "QA Other-CA", None, None, ca=True, not_after=NOW + dt.timedelta(days=3650))
    w("root.pem", _pem(root)); w("root.key", _key_pem(root_k))
    w("issuing.pem", _pem(iss)); w("issuing.key", _key_pem(iss_k))
    w("other-ca.pem", _pem(other)); w("other-ca.key", _key_pem(other_k))
    w("chain.pem", _pem(iss))
    w("trust.pem", _pem(root) + _pem(iss))

    leaves: dict[str, dict] = {}

    def leaf(name: str, key, issuer=iss, issuer_key=iss_k, **kw) -> x509.Certificate:
        c = _cert(key, kw.pop("cn", LEAF_CN), issuer, issuer_key, ca=kw.pop("ca", False), **kw)
        w(f"{name}.pem", _pem(c))
        w(f"{name}.key", _key_pem(key))
        leaves[name] = {
            "cert": files[f"{name}.pem"], "key": files[f"{name}.key"], "serial_decimal": str(c.serial_number),
            "serial_hex": format(c.serial_number, "x"), "subject": c.subject.rfc4514_string(),
            "issuer": c.issuer.rfc4514_string(), "not_after": c.not_valid_after_utc.isoformat(),
            "sha256": c.fingerprint(hashes.SHA256()).hex(),
            "key_type": "rsa" if isinstance(key, rsa.RSAPrivateKey) else "ec", "key_bits": key.key_size,
        }
        return c

    k_rsa = _rsa(2048)
    c_rsa = leaf("leaf-rsa", k_rsa, serial=0x1234)
    w("leaf-rsa.pkcs1.key", _key_pem(k_rsa, serialization.PrivateFormat.TraditionalOpenSSL))
    w("leaf-rsa.enc.key", _key_pem(k_rsa, password=KEY_PASSWORD))
    w("leaf-rsa-bundle.pem", _pem(c_rsa) + _pem(iss))
    w("leaf-rsa.pfx", pkcs12.serialize_key_and_certificates(
        b"netmon-bedrock", k_rsa, c_rsa, [iss], serialization.BestAvailableEncryption(PFX_PASSWORD.encode())))

    # [4] PKCS#7 bundles (PEM and DER) of the RSA leaf + issuing CA
    w("leaf-rsa.p7b", pkcs7.serialize_certificates([c_rsa, iss], serialization.Encoding.PEM))
    w("leaf-rsa.p7b.der", pkcs7.serialize_certificates([c_rsa, iss], serialization.Encoding.DER))
    leaf("leaf-cn-space", _rsa(), cn="NetMonitor Server")

    k_ec = ec.generate_private_key(ec.SECP256R1())
    c_ec = leaf("leaf-ec", k_ec, serial=0x5678)
    w("leaf-ec.sec1.key", _key_pem(k_ec, serialization.PrivateFormat.TraditionalOpenSSL))
    w("leaf-ec.pfx", pkcs12.serialize_key_and_certificates(
        b"netmon-bedrock-ec", k_ec, c_ec, [iss], serialization.BestAvailableEncryption(PFX_PASSWORD.encode())))
    leaf("leaf-ec384", ec.generate_private_key(ec.SECP384R1()))

    leaf("leaf-expired", _rsa(), not_before=NOW - dt.timedelta(days=400), not_after=NOW - dt.timedelta(days=10))
    leaf("leaf-expiring", _rsa(), not_after=NOW + dt.timedelta(days=10))
    leaf("leaf-wrongca", _rsa(), issuer=other, issuer_key=other_k)
    leaf("leaf-catrue", _rsa(), ca=True)
    leaf("leaf-nodigsig", _rsa(), digsig=False)
    leaf("leaf-rsa1024", _rsa(1024))
    leaf("leaf-rsa-root", _rsa(), issuer=root, issuer_key=root_k)

    srv_k = ec.generate_private_key(ec.SECP256R1())
    srv = _cert(srv_k, "localhost", iss, iss_k, ca=False, eku_client=False, eku_server=True,
                san_dns=["localhost", "bedrock-runtime.us-east-1.amazonaws.com", "bedrock-runtime.il-central-1.amazonaws.com",
                         "bedrock-agent-runtime.us-east-1.amazonaws.com", "rolesanywhere.us-east-1.amazonaws.com",
                         "bedrock.us-east-1.amazonaws.com"],
                san_ip=["127.0.0.1"])
    w("server.pem", _pem(srv)); w("server.key", _key_pem(srv_k)); w("server-bundle.pem", _pem(srv) + _pem(iss))

    manifest = {"dir": str(out), "leaf_cn": LEAF_CN, "pfx_password": PFX_PASSWORD, "key_password": KEY_PASSWORD,
                "files": files, "leaves": leaves,
                "anchors": {"issuing": files["issuing.pem"], "root": files["root.pem"]}}
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest


def sign_csr(out_dir: str | Path, csr_data: bytes, days: int = 365, ca: str = "issuing") -> bytes:
    make_pki(out_dir)
    d = Path(out_dir)
    try:
        csr = x509.load_pem_x509_csr(csr_data)
    except ValueError:
        csr = x509.load_der_x509_csr(csr_data)
    if not csr.is_signature_valid:
        raise ValueError("CSR signature invalid")
    name = "issuing" if ca == "issuing" else "other-ca"
    ca_cert = x509.load_pem_x509_certificate((d / f"{name}.pem").read_bytes())
    ca_key = serialization.load_pem_private_key((d / f"{name}.key").read_bytes(), None)
    pub = csr.public_key()
    b = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca_cert.subject).public_key(pub)
         .serial_number(x509.random_serial_number()).not_valid_before(NOW - dt.timedelta(minutes=5))
         .not_valid_after(NOW + dt.timedelta(days=days))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(_ku(True, False), critical=True)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(pub), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))
    return _pem(b.sign(ca_key, hashes.SHA256()))


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "sign-csr":
        days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 365
        ca = sys.argv[sys.argv.index("--ca") + 1] if "--ca" in sys.argv else "issuing"
        Path(sys.argv[4]).write_bytes(sign_csr(sys.argv[2], Path(sys.argv[3]).read_bytes(), days, ca))
        print(sys.argv[4])
        sys.exit(0)
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    m = make_pki(sys.argv[1])
    print(json.dumps({"dir": m["dir"], "leaves": sorted(m["leaves"])}))
