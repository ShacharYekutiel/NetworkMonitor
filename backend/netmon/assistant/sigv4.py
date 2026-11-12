"""[3b] Pure AWS request signing (architecture § 14.1, data-model "[3b] Signing vectors").

- SigV4 `AWS4-HMAC-SHA256` for Bedrock Runtime / Agent Runtime / control plane (service
  name `bedrock`): canonical request with the path *double-encoded* (every segment of the
  already-encoded request path is encoded again; the rule for every service but S3).
- IAM Roles Anywhere `CreateSession`: the same canonical request, algorithm
  `AWS4-X509-RSA-SHA256` / `AWS4-X509-ECDSA-SHA256`, credential = the leaf certificate's serial
  number in decimal, signature = the private key's signature over the string to sign (hex), no
  HMAC key derivation. Headers `X-Amz-X509` (base64 DER leaf) and `X-Amz-X509-Chain`
  (comma-separated base64 DER, issuing CA first) are signed.

No I/O; the private key operation goes through `X509Signer` (cryptography)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, unquote, urlsplit

ALG_HMAC = "AWS4-HMAC-SHA256"
ALG_X509_RSA = "AWS4-X509-RSA-SHA256"
ALG_X509_ECDSA = "AWS4-X509-ECDSA-SHA256"
UNRESERVED = "-_.~"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def amz_date(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uri_encode(value: str, safe: str = UNRESERVED) -> str:
    return quote(value, safe=safe)


def model_path(model_id: str, operation: str) -> str:
    """Request path of a Bedrock Runtime model operation: the model id (or ARN) is
    percent-encoded with no safe characters (`:` → `%3A`, `/` → `%2F`)."""
    return f"/model/{quote(model_id, safe='')}/{operation}"


def canonical_uri(path: str, double_encode: bool = True) -> str:
    if not path:
        return "/"
    if not double_encode:
        return path
    return "/".join(uri_encode(seg) for seg in path.split("/")) or "/"


def canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        pairs.append((uri_encode(unquote(k)), uri_encode(unquote(v))))
    pairs.sort()
    return "&".join(f"{k}={v}" for k, v in pairs)


def canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """(canonical header block incl. the trailing newline, signed header list)."""
    items: dict[str, str] = {}
    for name, value in headers.items():
        key = name.strip().lower()
        val = " ".join(str(value).strip().split())
        items[key] = f"{items[key]},{val}" if key in items else val
    names = sorted(items)
    block = "".join(f"{n}:{items[n]}\n" for n in names)
    return block, ";".join(names)


def canonical_request(
    method: str,
    path: str,
    query: str,
    headers: Mapping[str, str],
    payload_hash: str,
    double_encode: bool = True,
) -> tuple[str, str]:
    """(canonical request, signed headers)."""
    block, signed = canonical_headers(headers)
    creq = "\n".join(
        [
            method.upper(),
            canonical_uri(path, double_encode),
            canonical_query(query),
            block,
            signed,
            payload_hash,
        ]
    )
    return creq, signed


def credential_scope(date: str, region: str, service: str) -> str:
    return f"{date}/{region}/{service}/aws4_request"


def string_to_sign(algorithm: str, amzdate: str, scope: str, creq: str) -> str:
    return "\n".join([algorithm, amzdate, scope, sha256_hex(creq.encode("utf-8"))])


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    k = hmac.new(f"AWS4{secret_key}".encode(), date.encode(), hashlib.sha256).digest()
    for part in (region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    return k


def host_header(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port
    default = 443 if parts.scheme == "https" else 80
    return host if port in (None, default) else f"{host}:{port}"


@dataclass(frozen=True)
class SignedRequest:
    headers: dict[str, str]  # every header to send (signed ones + Authorization)
    canonical_request: str
    string_to_sign: str
    signed_headers: str
    signature: str


def sign_v4(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    amzdate: str,
    session_token: str | None = None,
    double_encode: bool = True,
) -> SignedRequest:
    """SigV4 over `headers` + Host + X-Amz-Date (+ X-Amz-Security-Token); all are signed.
    `url` carries the already-encoded request path (e.g. `model_path(...)`)."""
    parts = urlsplit(url)
    out = {k: v for k, v in headers.items() if k.lower() not in ("host", "authorization")}
    out["Host"] = host_header(url)
    out["X-Amz-Date"] = amzdate
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    payload_hash = sha256_hex(body)
    creq, signed = canonical_request(
        method, parts.path, parts.query, out, payload_hash, double_encode
    )
    scope = credential_scope(amzdate[:8], region, service)
    sts = string_to_sign(ALG_HMAC, amzdate, scope, creq)
    sig = hmac.new(
        signing_key(secret_key, amzdate[:8], region, service), sts.encode(), hashlib.sha256
    ).hexdigest()
    out["Authorization"] = (
        f"{ALG_HMAC} Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    )
    return SignedRequest(out, creq, sts, signed, sig)


# ---------------------------------------------------------------- Roles Anywhere (X.509)
class X509Signer:
    """Private-key signature for the Roles Anywhere signing process. RSA: PKCS#1 v1.5 with
    SHA-256; EC (P-256 / P-384): ECDSA SHA-256, DER-encoded signature."""

    def __init__(self, private_key: object) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec, rsa

        if isinstance(private_key, rsa.RSAPrivateKey):
            self.algorithm = ALG_X509_RSA
        elif isinstance(private_key, ec.EllipticCurvePrivateKey):
            self.algorithm = ALG_X509_ECDSA
        else:
            raise ValueError("unsupported key type (RSA or ECDSA P-256/P-384 required)")
        self._key = private_key

    def sign(self, data: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding

        if self.algorithm == ALG_X509_RSA:
            return self._key.sign(data, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[attr-defined]
        return self._key.sign(data, ec.ECDSA(hashes.SHA256()))  # type: ignore[attr-defined]


def b64der(der: bytes) -> str:
    return base64.b64encode(der).decode("ascii")


def sign_x509(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    cert_der: bytes,
    serial: int,
    chain_der: Sequence[bytes],
    signer: X509Signer,
    region: str,
    amzdate: str,
    service: str = "rolesanywhere",
) -> SignedRequest:
    out = {k: v for k, v in headers.items() if k.lower() not in ("host", "authorization")}
    out["Host"] = host_header(url)
    out["X-Amz-Date"] = amzdate
    out["X-Amz-X509"] = b64der(cert_der)
    if chain_der:
        out["X-Amz-X509-Chain"] = ",".join(b64der(c) for c in chain_der)
    return sign_x509_headers(
        method, url, out, body, serial=serial, signer=signer, region=region, amzdate=amzdate,
        service=service,
    )  # fmt: skip


def sign_x509_headers(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    serial: int,
    signer: X509Signer,
    region: str,
    amzdate: str,
    service: str = "rolesanywhere",
) -> SignedRequest:
    """Sign exactly `headers` (already containing Host, X-Amz-Date, X-Amz-X509…)."""
    parts = urlsplit(url)
    out = dict(headers)
    creq, signed = canonical_request(method, parts.path, parts.query, out, sha256_hex(body))
    scope = credential_scope(amzdate[:8], region, service)
    sts = string_to_sign(signer.algorithm, amzdate, scope, creq)
    sig = signer.sign(sts.encode("utf-8")).hex()
    out["Authorization"] = (
        f"{signer.algorithm} Credential={serial}/{scope}, SignedHeaders={signed}, Signature={sig}"
    )
    return SignedRequest(out, creq, sts, signed, sig)
