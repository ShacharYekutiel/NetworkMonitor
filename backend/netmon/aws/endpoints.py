"""[4] AWS endpoint URLs and the save-time URL rules (architecture § 15.1.3, § 15.14).

`endpoint(service, region, overrides)` is pure: the override for `(service, region)` when one
is configured, else `https://<service>.<region>.amazonaws.com` (partition `aws` only). The
service and region are checked against fixed patterns before they reach a URL, so every URL the
app builds itself comes from validated tokens; admin-typed URLs (overrides, proxy) pass
`endpoint_error` / `proxy_error` (netguard) at save time and the 1.4.1 egress guard at connect
time (`netmon.aws.transport.make_client`)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from netmon.util import netguard

# 3b REGION_RE (moved here from netmon.assistant.config, which re-exports it)
REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-\d$")
# 4-0a services with an endpoint override; later PRs add ec2, directconnect, logs, monitoring,
# s3, route53, sns
SERVICES = ("sts", "rolesanywhere")
SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
MAX_OVERRIDES = 40


def static_resolver(host: str) -> list[Any]:
    """A netguard resolver that never looks a name up: the save-time check then refuses only
    what is blocked without DNS (IP literals, loopback and metadata names). Names are checked
    off the event loop by `netmon.aws.connections.host_checks`, and the 1.4.1 egress guard
    checks every address again at connect time."""
    return []


def endpoint_error(
    url: str | None, *, test_mode: bool, resolver: Any = static_resolver
) -> str | None:
    """`https://host[:port]` only (no path, query or user info); `http://` only in test mode;
    netguard (loopback only in test mode, never link-local / metadata). No DNS lookup by
    default (`static_resolver`)."""
    if url is None or url == "":
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return "invalid URL"
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "must be https://host"
    if parts.scheme == "http" and not test_mode:
        return "must use https://"
    if parts.username or parts.password:
        return "must not contain credentials"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return "must not contain a path"
    return netguard.blocked_endpoint_host(
        parts.hostname, allow_loopback=test_mode, resolver=resolver
    )


def proxy_error(
    url: str | None, *, test_mode: bool = False, resolver: Any = static_resolver
) -> str | None:
    if not url:
        return "required for proxy mode custom"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "invalid URL"
    if parts.scheme not in ("http", "https") or not parts.hostname or port is None:
        return "must be http(s)://host:port"
    if parts.username or parts.password:
        return "credentials in the proxy URL are not supported"
    if parts.path not in ("", "/"):
        return "must not contain a path"
    return netguard.blocked_endpoint_host(
        parts.hostname, allow_loopback=test_mode, resolver=resolver
    )


def url_host(url: str | None) -> str | None:
    """The host name of an admin-typed URL (None when it has none or does not parse)."""
    if not url:
        return None
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def override_errors(value: Any, *, test_mode: bool) -> list[str]:
    """Problems of an `endpoint_overrides` list (≤ 40 rows, unique (service, region), known
    service, region pattern, `endpoint_error` on the URL)."""
    if value is None:
        return []
    if not isinstance(value, list):
        return ["must be a list"]
    if len(value) > MAX_OVERRIDES:
        return [f"at most {MAX_OVERRIDES} rows"]
    errs: list[str] = []
    seen: set[tuple[str, str]] = set()
    for i, row in enumerate(value):
        if not isinstance(row, Mapping):
            errs.append(f"row {i + 1}: invalid entry")
            continue
        svc, region, url = row.get("service"), row.get("region"), row.get("url")
        if svc not in SERVICES:
            errs.append(f"row {i + 1}: service must be one of {', '.join(SERVICES)}")
        if not isinstance(region, str) or not REGION_RE.match(region):
            errs.append(f"row {i + 1}: invalid AWS region")
        if not isinstance(url, str) or not url:
            errs.append(f"row {i + 1}: url required")
        else:
            e = endpoint_error(url, test_mode=test_mode)
            if e:
                errs.append(f"row {i + 1}: {e}")
        key = (str(svc), str(region))
        if key in seen:
            errs.append(f"row {i + 1}: duplicate ({svc}, {region})")
        seen.add(key)
    return errs


def override_for(
    service: str, region: str, overrides: Iterable[Mapping[str, Any]] | None
) -> str | None:
    for row in overrides or ():
        if row.get("service") == service and row.get("region") == region and row.get("url"):
            return str(row["url"]).rstrip("/")
    return None


def endpoint(
    service: str, region: str, overrides: Iterable[Mapping[str, Any]] | None = None
) -> str:
    if not SERVICE_RE.match(service or ""):
        raise ValueError("invalid AWS service name")
    if not REGION_RE.match(region or ""):
        raise ValueError("invalid AWS region")
    url = override_for(service, region, overrides)
    if url:
        return url
    return f"https://{service}.{region}.amazonaws.com"


# ---------------------------------------------------------------- connection view helpers
def ra_region(view: Mapping[str, Any]) -> str:
    """The Roles Anywhere region of a connection view (or a 1.4.x assistant object)."""
    return str(view.get("ra_region") or view["region"])


def ra_endpoint(view: Mapping[str, Any]) -> str:
    url = view.get("ra_endpoint_url")
    if url:
        return str(url).rstrip("/")
    return f"https://rolesanywhere.{ra_region(view)}.amazonaws.com"


def sts_endpoint(view: Mapping[str, Any]) -> str:
    url = view.get("sts_endpoint_url")
    if url:
        return str(url).rstrip("/")
    return endpoint("sts", str(view["region"]))
