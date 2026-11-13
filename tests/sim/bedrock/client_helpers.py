"""Test-side AWS clients for driving the Bedrock mock without the product (self-tests, debugging, backend tests
that need a reference request). SigV4 via botocore; the Roles Anywhere X.509 signer is a small independent
implementation of the documented process (docs/architecture.md §14.2.3).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa


def amz_now(offset_s: int = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_s)).strftime("%Y%m%dT%H%M%SZ")


def model_path(model_id: str, op: str = "converse-stream") -> str:
    return f"/model/{quote(model_id, safe='')}/{op}"


def sigv4_headers(method: str, url: str, body: bytes, *, access_key: str, secret_key: str, token: str | None = None,
                  region: str = "us-east-1", service: str = "bedrock", amz_date: str | None = None,
                  extra: dict[str, str] | None = None) -> dict[str, str]:
    u = urlsplit(url)
    headers = {"Host": u.netloc, "Content-Type": "application/json", "X-Amz-Date": amz_date or amz_now()}
    if token:
        headers["X-Amz-Security-Token"] = token
    headers.update(extra or {})
    req = AWSRequest(method=method, url=url, headers=headers, data=body)
    req.context["timestamp"] = headers["X-Amz-Date"]
    a = SigV4Auth(Credentials(access_key, secret_key, token), service, region)
    canonical = a.canonical_request(req)
    sig = a.signature(a.string_to_sign(req, canonical), req)
    signed = a.signed_headers(a.headers_to_sign(req))
    headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{headers['X-Amz-Date'][:8]}/{region}/"
                                f"{service}/aws4_request, SignedHeaders={signed}, Signature={sig}")
    return headers


def x509_headers(method: str, url: str, body: bytes, *, cert_pem: bytes, key_pem: bytes, chain_pems: list[bytes] = (),
                 region: str = "us-east-1", amz_date: str | None = None, password: bytes | None = None) -> dict[str, str]:
    cert = x509.load_pem_x509_certificate(cert_pem)
    key = serialization.load_pem_private_key(key_pem, password)
    u = urlsplit(url)
    headers = {"Host": u.netloc, "Content-Type": "application/json", "X-Amz-Date": amz_date or amz_now(),
               "X-Amz-X509": base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()}
    if chain_pems:
        headers["X-Amz-X509-Chain"] = ",".join(
            base64.b64encode(x509.load_pem_x509_certificate(p).public_bytes(serialization.Encoding.DER)).decode()
            for p in chain_pems)
    req = AWSRequest(method=method, url=url, headers=headers, data=body)
    req.context["timestamp"] = headers["X-Amz-Date"]
    a = SigV4Auth(Credentials("x", "x"), "rolesanywhere", region)
    canonical = a.canonical_request(req)
    signed = a.signed_headers(a.headers_to_sign(req))
    alg = "AWS4-X509-RSA-SHA256" if isinstance(key, rsa.RSAPrivateKey) else "AWS4-X509-ECDSA-SHA256"
    scope = f"{headers['X-Amz-Date'][:8]}/{region}/rolesanywhere/aws4_request"
    sts = "\n".join([alg, headers["X-Amz-Date"], scope, hashlib.sha256(canonical.encode()).hexdigest()])
    if isinstance(key, rsa.RSAPrivateKey):
        sig = key.sign(sts.encode(), padding.PKCS1v15(), hashes.SHA256())
    else:
        sig = key.sign(sts.encode(), ec.ECDSA(hashes.SHA256()))
    headers["Authorization"] = (f"{alg} Credential={cert.serial_number}/{scope}, SignedHeaders={signed}, "
                                f"Signature={sig.hex()}")
    return headers


def converse_body(question: str, *, tools: list[str] | None = None, messages: list[dict] | None = None,
                  system: str = "You are the NetworkMonitor assistant. Purpose: ask", cache: bool = True,
                  tool_choice: dict | None = None, max_tokens: int = 2000) -> dict[str, Any]:
    body: dict[str, Any] = {
        "system": [{"text": system}] + ([{"cachePoint": {"type": "default"}}] if cache else []),
        "messages": messages or [{"role": "user", "content": [{"text": question}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
    }
    if tools is not None:
        specs = [{"toolSpec": {"name": t, "description": t, "inputSchema": {"json": {"type": "object", "properties": {},
                                                                                     "additionalProperties": True}}}}
                 for t in tools]
        body["toolConfig"] = {"tools": specs + ([{"cachePoint": {"type": "default"}}] if cache else []),
                              "toolChoice": tool_choice or {"auto": {}}}
    return body


def call(base_url: str, path: str, body: dict | None, *, access_key: str, secret_key: str, token: str | None = None,
         region: str = "us-east-1", method: str = "POST", verify: Any = True, timeout: float = 30) -> httpx.Response:
    raw = json.dumps(body).encode() if body is not None else b""
    url = base_url + path
    h = sigv4_headers(method, url, raw, access_key=access_key, secret_key=secret_key, token=token, region=region)
    return httpx.request(method, url, content=raw, headers=h, verify=verify, timeout=timeout)
