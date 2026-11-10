"""[3b] IAM Roles Anywhere certificate handling (architecture § 14.2.3).

- `parse_upload`: a PEM bundle (leaf first or anywhere + chain) with an optional PEM key
  (PKCS#8 / PKCS#1 / SEC1, optionally encrypted), or a PKCS#12 `.pfx` with its password.
  The key is re-serialised as unencrypted PKCS#8 (stored AES-GCM encrypted by the caller).
- `validate_leaf`: v3 leaf (`CA:FALSE`), Digital Signature usage, valid now, RSA ≥ 2048 or
  ECDSA P-256 / P-384, SHA-256 or stronger.
- `generate_csr` (owner decision 3b, like the HTTPS make-csr flow): key generated and kept on
  the server; the CSR (CN, Digital Signature, Client Authentication) is downloaded for the
  internal CA (Mnidom-PKI-SERV-CA); the issued certificate is then uploaded *without* a key.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

MAX_UPLOAD = 64 * 1024
EXPIRING_DAYS = 30
CN_RE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_ ")


class CertError(ValueError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code  # invalid_certificate | invalid_private_key | key_mismatch
        self.reason = reason


@dataclass
class Material:
    cert: x509.Certificate
    chain: list[x509.Certificate] = field(default_factory=list)
    key: Any = None  # private key object or None

    @property
    def cert_pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    @property
    def chain_pem(self) -> str | None:
        if not self.chain:
            return None
        return "".join(
            c.public_bytes(serialization.Encoding.PEM).decode("ascii") for c in self.chain
        )

    @property
    def key_pem(self) -> str | None:
        return key_to_pkcs8_pem(self.key) if self.key is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


def load_pem_certs(data: bytes) -> list[x509.Certificate]:
    try:
        return list(x509.load_pem_x509_certificates(data))
    except ValueError:
        return []


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _password(pw: str | None) -> bytes | None:
    return pw.encode("utf-8") if pw else None


def load_key(data: bytes, password: str | None) -> Any:
    text = data.strip()
    if not text:
        raise CertError("invalid_private_key", "The private key is empty")
    try:
        if b"-----BEGIN" in text:
            key = serialization.load_pem_private_key(text, _password(password))
        else:
            key = serialization.load_der_private_key(text, _password(password))
    except TypeError:
        raise CertError(
            "invalid_private_key", "The private key is encrypted: enter its password"
        ) from None
    except ValueError:
        raise CertError(
            "invalid_private_key", "The private key cannot be read (wrong password or format)"
        ) from None
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise CertError("invalid_private_key", "Only RSA and ECDSA keys are supported")
    return key


def key_from_pkcs8_pem(pem: str) -> Any:
    return serialization.load_pem_private_key(pem.encode("ascii"), None)


def key_to_pkcs8_pem(key: Any) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _split_leaf(certs: list[x509.Certificate]) -> tuple[x509.Certificate, list[x509.Certificate]]:
    leaves = [c for c in certs if not _is_ca(c)]
    if not leaves:
        raise CertError("invalid_certificate", "The bundle has no end-entity certificate (CA:TRUE)")
    leaf = leaves[0]
    chain = [c for c in certs if c is not leaf]
    return leaf, order_chain(leaf, chain)


def order_chain(leaf: x509.Certificate, chain: list[x509.Certificate]) -> list[x509.Certificate]:
    """Issuing CA first, then its issuer, …; unrelated certificates are kept at the end."""
    out: list[x509.Certificate] = []
    rest = list(chain)
    current = leaf
    while rest:
        nxt = next((c for c in rest if c.subject == current.issuer), None)
        if nxt is None:
            break
        out.append(nxt)
        rest.remove(nxt)
        if nxt.subject == nxt.issuer:
            break
        current = nxt
    return out + rest


def parse_upload(
    file_data: bytes,
    key_data: bytes | None = None,
    password: str | None = None,
    *,
    now: datetime | None = None,
    pending_key_pem: str | None = None,
) -> Material:
    """Certificate (+ chain) and key from an upload. Without a key in the upload, the key of
    a pending CSR (`pending_key_pem`) is used when it matches the certificate."""
    if len(file_data) > MAX_UPLOAD or (key_data and len(key_data) > MAX_UPLOAD):
        raise CertError("invalid_certificate", "The file is larger than 64 KiB")
    key: Any = None
    if b"-----BEGIN" in file_data:
        certs = load_pem_certs(file_data)
        if not certs:
            raise CertError("invalid_certificate", "No PEM certificate found")
        leaf, chain = _split_leaf(certs)
        if key_data:
            key = load_key(key_data, password)
        elif b"PRIVATE KEY-----" in file_data:
            key = load_key(file_data, password)
    else:
        try:
            pkey, pcert, extra = pkcs12.load_key_and_certificates(file_data, _password(password))
        except ValueError:
            # DER certificate?
            try:
                pcert = x509.load_der_x509_certificate(file_data)
            except ValueError:
                raise CertError(
                    "invalid_certificate",
                    "Not a PEM certificate or a PKCS#12 file (or the password is wrong)",
                ) from None
            pkey, extra = None, []
        if pcert is None:
            raise CertError("invalid_certificate", "The PKCS#12 file has no certificate")
        leaf, chain = _split_leaf([pcert, *extra])
        key = pkey
        if key is None and key_data:
            key = load_key(key_data, password)
        if key is not None and not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
            raise CertError("invalid_private_key", "Only RSA and ECDSA keys are supported")
    validate_leaf(leaf, now or _now())
    if key is None and pending_key_pem:
        pending = key_from_pkcs8_pem(pending_key_pem)
        if key_matches(leaf, pending):
            key = pending
        else:
            raise CertError(
                "key_mismatch",
                "The certificate does not match the key of the generated CSR (upload its key)",
            )
    if key is None:
        raise CertError("invalid_private_key", "The private key is missing")
    if not key_matches(leaf, key):
        raise CertError("key_mismatch", "The private key does not match the certificate")
    return Material(leaf, chain, key)


def validate_leaf(cert: x509.Certificate, now: datetime) -> None:
    if cert.version != x509.Version.v3:
        raise CertError("invalid_certificate", "Not an X.509 v3 certificate")
    if _is_ca(cert):
        raise CertError("invalid_certificate", "The certificate is a CA certificate (CA:TRUE)")
    try:
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        if not ku.digital_signature:
            raise CertError("invalid_certificate", "Key usage lacks Digital Signature")
    except x509.ExtensionNotFound:
        pass
    if cert.not_valid_after_utc <= now:
        raise CertError("invalid_certificate", "The certificate has expired")
    if cert.not_valid_before_utc > now:
        raise CertError("invalid_certificate", "The certificate is not valid yet")
    pub = cert.public_key()
    if isinstance(pub, rsa.RSAPublicKey):
        if pub.key_size < 2048:
            raise CertError("invalid_certificate", "RSA keys must have at least 2048 bits")
    elif isinstance(pub, ec.EllipticCurvePublicKey):
        if pub.curve.name not in ("secp256r1", "secp384r1"):
            raise CertError("invalid_certificate", "ECDSA keys must use P-256 or P-384")
    else:
        raise CertError("invalid_certificate", "Unsupported key type (RSA or ECDSA required)")
    algo = cert.signature_hash_algorithm
    if algo is not None and algo.name in ("md5", "sha1"):
        raise CertError("invalid_certificate", "The certificate is signed with SHA-1 or weaker")


def _pub_bytes(key: Any) -> bytes:
    return key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def key_matches(cert: x509.Certificate, key: Any) -> bool:
    try:
        return _pub_bytes(cert.public_key()) == _pub_bytes(key.public_key())
    except Exception:  # noqa: BLE001
        return False


def key_info(pub: Any) -> tuple[str, int]:
    if isinstance(pub, rsa.RSAPublicKey):
        return "rsa", pub.key_size
    if isinstance(pub, ec.EllipticCurvePublicKey):
        return "ec", pub.curve.key_size
    return "unknown", 0


def cert_info(
    cert: x509.Certificate,
    chain: list[x509.Certificate],
    *,
    source: str,
    uploaded_at: str | None,
    key_unreadable: bool,
) -> dict[str, Any]:
    from netmon.services.serialize import iso

    kt, bits = key_info(cert.public_key())
    return {
        "source": source,
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "serial_hex": format(cert.serial_number, "x"),
        "serial_decimal": str(cert.serial_number),
        "not_before": iso(cert.not_valid_before_utc),
        "not_after": iso(cert.not_valid_after_utc),
        "key_type": kt,
        "key_bits": bits,
        "sha256": cert.fingerprint(hashes.SHA256()).hex(),
        "chain": [
            {"subject": c.subject.rfc4514_string(), "not_after": iso(c.not_valid_after_utc)}
            for c in chain
        ],
        "uploaded_at": uploaded_at,
        "key_unreadable": key_unreadable,
    }


def expiring(cert: x509.Certificate, now: datetime, days: int = EXPIRING_DAYS) -> bool:
    return (cert.not_valid_after_utc - now).total_seconds() < days * 86400


def der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def sha256_pem(pem: str | None) -> str | None:
    return hashlib.sha256(pem.encode()).hexdigest() if pem else None


# ---------------------------------------------------------------- CSR
def validate_cn(cn: str) -> str | None:
    cn = cn.strip()
    if not 1 <= len(cn) <= 64:
        return "1-64 characters"
    if not set(cn) <= CN_RE_CHARS:
        return "letters, digits, '.', '-', '_' and spaces only"
    return None


def generate_csr(
    common_name: str,
    key_type: str = "rsa",
    key_size: int = 2048,
    organization: str | None = None,
    organizational_unit: str | None = None,
) -> tuple[str, str]:
    """(PKCS#8 private key PEM, CSR PEM). RSA 2048/3072/4096 or EC P-256/P-384 (key_size 256 /
    384). The CSR requests Digital Signature, Client Authentication and CA:FALSE."""
    if key_type == "rsa":
        if key_size not in (2048, 3072, 4096):
            raise ValueError("RSA key size must be 2048, 3072 or 4096")
        key: Any = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    elif key_type == "ec":
        curve = {256: ec.SECP256R1(), 384: ec.SECP384R1()}.get(key_size)
        if curve is None:
            raise ValueError("EC key size must be 256 or 384")
        key = ec.generate_private_key(curve)
    else:
        raise ValueError("key type must be rsa or ec")
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, common_name.strip())]
    if organizational_unit:
        attrs.insert(0, x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organizational_unit))
    if organization:
        attrs.insert(0, x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization))
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name(attrs))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key_to_pkcs8_pem(key), csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
