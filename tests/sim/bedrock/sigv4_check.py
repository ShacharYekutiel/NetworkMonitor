"""Request-signature verification for the Bedrock mock (docs/architecture.md §14.11).

* SigV4 (`AWS4-HMAC-SHA256`) is re-computed with **botocore's `SigV4Auth`** - an implementation independent of
  `netmon.assistant.sigv4` (botocore is a test-only dependency).
* IAM Roles Anywhere (`AWS4-X509-RSA-SHA256` / `AWS4-X509-ECDSA-SHA256`): the canonical request is built by
  botocore too; the string to sign is `alg \\n amz-date \\n scope \\n hex(sha256(canonical))` and its signature is
  checked with the leaf certificate's public key; the leaf is checked against the trust anchor (directly or
  through `X-Amz-X509-Chain`).

Every function returns `(ok, aws_error_type, message, info)`; the mock turns a failure into the AWS error.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
from typing import Any

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

AUTH_RE = re.compile(
    r"^(?P<alg>AWS4-[A-Z0-9-]+)\s+Credential=(?P<cred>[^,\s]+),\s*SignedHeaders=(?P<sh>[^,\s]+),\s*Signature=(?P<sig>[0-9a-f]+)\s*$"
)
SKEW_S = 300


def parse_auth(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    m = AUTH_RE.match(value.strip())
    if not m:
        return None
    cred = m.group("cred").split("/")
    if len(cred) != 5 or cred[4] != "aws4_request":
        return None
    return {"alg": m.group("alg"), "key": cred[0], "date": cred[1], "region": cred[2], "service": cred[3],
            "signed_headers": m.group("sh").split(";"), "signature": m.group("sig")}


def _aws_request(method: str, host: str, raw_path: str, query: str, headers: dict[str, str], signed: list[str],
                 body: bytes) -> AWSRequest:
    """An AWSRequest with exactly the signed headers (botocore signs every header it is given)."""
    hdrs = {}
    lower = {k.lower(): v for k, v in headers.items()}
    for h in signed:
        if h not in lower:
            raise KeyError(h)
        hdrs[h] = lower[h]
    url = f"https://{host}{raw_path}" + (f"?{query}" if query else "")
    return AWSRequest(method=method, url=url, headers=hdrs, data=body)


def _clock_check(amz_date: str, now: dt.datetime) -> str | None:
    try:
        t = dt.datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return "invalid X-Amz-Date"
    if abs((now - t).total_seconds()) > SKEW_S:
        fmt = "%Y%m%dT%H%M%SZ"
        return (f"Signature expired: {amz_date} is now earlier than {(now - dt.timedelta(seconds=SKEW_S)).strftime(fmt)} "
                f"({now.strftime(fmt)} - 5 min.)") if t < now else (
                f"Signature not yet current: {amz_date} is still later than {(now + dt.timedelta(seconds=SKEW_S)).strftime(fmt)} "
                f"({now.strftime(fmt)} + 5 min.)")
    return None


def verify_sigv4(method: str, host: str, raw_path: str, query: str, headers: dict[str, str], body: bytes, *,
                 lookup_secret, now: dt.datetime, expected_service: str = "bedrock") -> tuple[bool, str | None, str, dict]:
    """lookup_secret(access_key_id) -> (secret, session_token|None, expired: bool) | None."""
    lower = {k.lower(): v for k, v in headers.items()}
    a = parse_auth(lower.get("authorization"))
    if not a or a["alg"] != "AWS4-HMAC-SHA256":
        return False, "IncompleteSignatureException", "Authorization header requires 'Credential', 'Signature' and 'SignedHeaders' parameters.", {}
    info = {"access_key_id": a["key"], "region": a["region"], "service": a["service"],
            "signed_headers": a["signed_headers"], "amz_date": lower.get("x-amz-date")}
    for need in ("host", "x-amz-date"):
        if need not in a["signed_headers"]:
            return False, "IncompleteSignatureException", f"'{need}' must be a signed header", info
    if a["service"] != expected_service:
        return False, "InvalidSignatureException", f"Credential should be scoped to correct service: '{expected_service}'.", info
    amz_date = lower.get("x-amz-date", "")
    if a["date"] != amz_date[:8]:
        return False, "InvalidSignatureException", "Date in Credential scope does not match YYYYMMDD from ISO-8601 version of date from HTTP", info
    sec = lookup_secret(a["key"])
    if sec is None:
        return False, "UnrecognizedClientException", "The security token included in the request is invalid.", info
    secret, token, expired = sec
    if token is not None:
        if lower.get("x-amz-security-token") != token:
            return False, "UnrecognizedClientException", "The security token included in the request is invalid.", info
        if "x-amz-security-token" not in a["signed_headers"]:
            # AWS accepts an unsigned session token header; we require it to be signed (contract vector 2)
            info["warning"] = "x-amz-security-token not signed"
        if expired:
            return False, "ExpiredTokenException", "The security token included in the request is expired", info
    if "x-amz-content-sha256" in lower and lower["x-amz-content-sha256"] != hashlib.sha256(body).hexdigest():
        return False, "InvalidSignatureException", "The provided 'x-amz-content-sha256' header does not match what was computed.", info
    try:
        req = _aws_request(method, lower.get("host", host), raw_path, query, headers, a["signed_headers"], body)
    except KeyError as e:
        return False, "InvalidSignatureException", f"signed header {e} is missing", info
    req.context["timestamp"] = amz_date
    auth = SigV4Auth(Credentials(a["key"], secret, token), a["service"], a["region"])
    canonical = auth.canonical_request(req)
    sts = auth.string_to_sign(req, canonical)
    want = auth.signature(sts, req)
    info["canonical_request_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    if not hmac.compare_digest(want, a["signature"]):
        info["canonical_request"] = canonical
        return False, "InvalidSignatureException", (
            "The request signature we calculated does not match the signature you provided. Check your AWS Secret "
            "Access Key and signing method. Consult the service documentation for details."), info
    skew = _clock_check(amz_date, now)
    if skew:
        return False, "InvalidSignatureException", skew, info
    return True, None, "ok", info


# ------------------------------------------------------------------------------------------ Roles Anywhere
def _b64_der(v: str) -> x509.Certificate:
    return x509.load_der_x509_certificate(base64.b64decode(v.strip()))


def _issued_by(child: x509.Certificate, parent: x509.Certificate) -> bool:
    if child.issuer != parent.subject:
        return False
    try:
        child.verify_directly_issued_by(parent)
        return True
    except (ValueError, TypeError, InvalidSignature):
        return False


def verify_x509_request(method: str, host: str, raw_path: str, query: str, headers: dict[str, str], body: bytes, *,
                        anchors: list[x509.Certificate], now: dt.datetime, crl_serials: set[int] | None = None
                        ) -> tuple[bool, str | None, str, dict]:
    lower = {k.lower(): v for k, v in headers.items()}
    a = parse_auth(lower.get("authorization"))
    if not a or a["alg"] not in ("AWS4-X509-RSA-SHA256", "AWS4-X509-ECDSA-SHA256"):
        return False, "AccessDeniedException", "Invalid authorization header (expected AWS4-X509-*-SHA256)", {}
    info: dict[str, Any] = {"alg": a["alg"], "serial": a["key"], "region": a["region"], "service": a["service"],
                            "signed_headers": a["signed_headers"], "chain": False}
    if a["service"] != "rolesanywhere":
        return False, "AccessDeniedException", "Credential scope service must be rolesanywhere", info
    for need in ("host", "x-amz-date", "x-amz-x509"):
        if need not in a["signed_headers"]:
            return False, "AccessDeniedException", f"'{need}' must be a signed header", info
    try:
        leaf = _b64_der(lower["x-amz-x509"])
    except Exception:  # noqa: BLE001
        return False, "AccessDeniedException", "Unable to parse X-Amz-X509 certificate", info
    chain: list[x509.Certificate] = []
    if "x-amz-x509-chain" in lower:
        info["chain"] = True
        if "x-amz-x509-chain" not in a["signed_headers"]:
            return False, "AccessDeniedException", "'x-amz-x509-chain' must be a signed header", info
        try:
            chain = [_b64_der(p) for p in lower["x-amz-x509-chain"].split(",") if p.strip()]
        except Exception:  # noqa: BLE001
            return False, "AccessDeniedException", "Unable to parse X-Amz-X509-Chain", info
    info["subject"] = leaf.subject.rfc4514_string()
    info["leaf_serial"] = str(leaf.serial_number)
    cns = leaf.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)  # [4] source identity
    info["leaf_cn"] = str(cns[0].value) if cns else None
    if a["key"] != str(leaf.serial_number):
        return False, "AccessDeniedException", (
            f"Credential serial {a['key']} does not match the certificate serial {leaf.serial_number} (decimal)"), info
    amz_date = lower.get("x-amz-date", "")
    if a["date"] != amz_date[:8]:
        return False, "AccessDeniedException", "Date in Credential scope does not match X-Amz-Date", info
    try:
        req = _aws_request(method, lower.get("host", host), raw_path, query, headers, a["signed_headers"], body)
    except KeyError as e:
        return False, "AccessDeniedException", f"signed header {e} is missing", info
    req.context["timestamp"] = amz_date
    canonical = SigV4Auth(Credentials("x", "x"), "rolesanywhere", a["region"]).canonical_request(req)
    scope = f"{a['date']}/{a['region']}/rolesanywhere/aws4_request"
    sts = "\n".join([a["alg"], amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    info["string_to_sign"] = sts
    sig = bytes.fromhex(a["signature"])
    pub = leaf.public_key()
    try:
        if a["alg"] == "AWS4-X509-RSA-SHA256":
            if not isinstance(pub, rsa.RSAPublicKey):
                raise InvalidSignature()
            pub.verify(sig, sts.encode(), padding.PKCS1v15(), hashes.SHA256())
        else:
            if not isinstance(pub, ec.EllipticCurvePublicKey):
                raise InvalidSignature()
            pub.verify(sig, sts.encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        info["canonical_request"] = canonical
        return False, "AccessDeniedException", "Signature validation failed", info
    skew = _clock_check(amz_date, now)
    if skew:
        return False, "AccessDeniedException", skew, info
    # certificate checks (what Roles Anywhere enforces)
    bc = None
    try:
        bc = leaf.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        pass
    if bc is not None and bc.ca:
        return False, "AccessDeniedException", "Untrusted certificate. Insufficient certificate (CA:TRUE end-entity)", info
    try:
        ku = leaf.extensions.get_extension_for_class(x509.KeyUsage).value
        if not ku.digital_signature:
            return False, "AccessDeniedException", "Untrusted certificate. Insufficient certificate (no digitalSignature)", info
    except x509.ExtensionNotFound:
        pass
    if not (leaf.not_valid_before_utc <= now <= leaf.not_valid_after_utc):
        return False, "AccessDeniedException", "Untrusted certificate. Certificate expired or not yet valid", info
    if crl_serials and leaf.serial_number in crl_serials:
        return False, "AccessDeniedException", "Untrusted certificate. Certificate revoked", info
    path = [leaf] + chain
    for i in range(len(path) - 1):
        if not _issued_by(path[i], path[i + 1]):
            return False, "AccessDeniedException", "Untrusted certificate. Insufficient certificate (broken chain)", info
    top = path[-1]
    if not any(top.fingerprint(hashes.SHA256()) == t.fingerprint(hashes.SHA256()) or _issued_by(top, t) for t in anchors):
        return False, "AccessDeniedException", "Untrusted certificate. Insufficient certificate", info
    return True, None, "ok", info
