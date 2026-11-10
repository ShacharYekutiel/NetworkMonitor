"""[3b] Pure redaction pipeline (architecture § 14.6.1, data-model § [3b] Redaction vectors).

Every string that leaves for Bedrock passes `apply(value, policy)`:

1. control characters are removed (except tab and newline),
2. secret patterns are replaced **always** (`<secret removed>`, `<pem removed>`, `<hash removed>`),
3. optionally (``policy.redact_ips``) internal IPv4/IPv6 addresses are replaced by stable
   per-conversation placeholders ``IP-1``, ``IP-2`` … (``IpMap``; the map is stored in the
   conversation, never sent),
4. optionally strings are capped (``policy.max_len``).

The reverse direction: `IpMap.detokenize(text)` (answers shown to the admin) and
`translate_args(args, ipmap)` (placeholders in the model's tool arguments; an unknown
placeholder raises `UnknownPlaceholder` → the tool answers ``invalid_arguments``).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

SECRET_MARK = "<secret removed>"
PEM_MARK = "<pem removed>"
HASH_MARK = "<hash removed>"
MARKS = (SECRET_MARK, PEM_MARK, HASH_MARK)

# --------------------------------------------------------------------------- control chars
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def clean_control(text: str) -> str:
    """Remove control characters (keeps ``\\t`` and ``\\n``) and bidi/zero-width overrides."""
    return _CONTROL.sub("", text)


# --------------------------------------------------------------------------- secrets
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]{1,64}-----.*?(?:-----END [A-Z0-9 ]{1,64}-----|\Z)", re.DOTALL
)
_KEYWORDS = (
    r"pre-shared-key|pre-shared|key[ -](?:string|chain)|authentication-key|auth-key|priv-key|"
    r"private-key|"
    r"passwd|password|secret|community|psk|token|bearer"
)
# qualifier words that are kept between the keyword and the value (``community read cipher``)
_QUALIFIERS = (
    r"(?:(?:read|write|ro|rw|cipher|simple|plain|plaintext|hash|hashed|encrypted|"
    r"irreversible-cipher|sha|sha256|sha512|md5|level)\s+)*"
)
_ALREADY = r"(?!<secret removed>|<hash removed>|<pem removed>)"
_SECRET = re.compile(
    rf"(?i)(?<![\w<-])(?P<kw>{_KEYWORDS})(?![\w-])"
    rf"(?P<sep>\s*[=:]\s*|\s+)"
    rf"(?P<qual>{_QUALIFIERS})"
    rf"{_ALREADY}(?:\d{{1,2}}\s+{_ALREADY})?"
    r"(?P<val>\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)"
)
_HASH = re.compile(r"(?<![\w$])\$(?:1|2[aby]?|5|6|8|9|c)\$[^\s,;]+")


def _secret_sub(m: re.Match[str]) -> str:
    return f"{m.group('kw')}{m.group('sep')}{m.group('qual')}{SECRET_MARK}"


def redact_secrets(text: str) -> str:
    """Secret values, PEM blocks and password hashes → placeholders (always applied)."""
    if not text:
        return text
    out = _PEM.sub(PEM_MARK, text)
    out = _SECRET.sub(_secret_sub, out)
    return _HASH.sub(HASH_MARK, out)


# --------------------------------------------------------------------------- internal IPs
_INTERNAL_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w]|\.\d)")
_IPV6 = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
PLACEHOLDER = re.compile(r"(?<![\w-])IP-(\d{1,6})(?!\w)")


def parse_networks(cidrs: Iterable[str]) -> tuple[Any, ...]:
    out = []
    for c in cidrs or ():
        try:
            out.append(ipaddress.ip_network(str(c).strip(), strict=False))
        except ValueError:
            continue
    return tuple(out)


def is_internal(addr: str, extra: tuple[Any, ...] = ()) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in (*_INTERNAL_NETS, *extra))


@dataclass
class IpMap:
    """Stable per-conversation placeholders (``{"IP-1": "10.0.0.9"}``)."""

    forward: dict[str, str] = field(default_factory=dict)  # real -> placeholder

    @classmethod
    def from_stored(cls, stored: Mapping[str, str] | None) -> IpMap:
        m = cls()
        for ph, real in (stored or {}).items():
            if isinstance(ph, str) and isinstance(real, str) and PLACEHOLDER.fullmatch(ph):
                m.forward[real] = ph
        return m

    def stored(self) -> dict[str, str]:
        return {ph: real for real, ph in self.forward.items()}

    @property
    def reverse(self) -> dict[str, str]:
        return self.stored()

    def token(self, real: str) -> str:
        ph = self.forward.get(real)
        if ph is None:
            ph = f"IP-{len(self.forward) + 1}"
            self.forward[real] = ph
        return ph

    def detokenize(self, text: str) -> str:
        if not text or not self.forward:
            return text
        rev = self.reverse
        return PLACEHOLDER.sub(lambda m: rev.get(m.group(0), m.group(0)), text)


def _norm_ip(raw: str) -> str | None:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    return str(ip)


def tokenize_ips(text: str, ipmap: IpMap, extra: tuple[Any, ...] = ()) -> str:
    if not text:
        return text

    def sub(m: re.Match[str]) -> str:
        raw = m.group(0)
        norm = _norm_ip(raw)
        if norm is None or not is_internal(norm, extra):
            return raw
        return ipmap.token(norm)

    out = _IPV4.sub(sub, text)
    if ":" in out:
        out = _IPV6.sub(sub, out)
    return out


# --------------------------------------------------------------------------- policy / apply
@dataclass
class Policy:
    redact_ips: bool = False
    ipmap: IpMap | None = None
    internal_cidrs: tuple[Any, ...] = ()
    max_len: int | None = None

    @classmethod
    def make(
        cls,
        *,
        redact_ips: bool = False,
        stored_map: Mapping[str, str] | None = None,
        internal_cidrs: Iterable[str] = (),
        max_len: int | None = None,
    ) -> Policy:
        return cls(
            redact_ips=redact_ips,
            ipmap=IpMap.from_stored(stored_map) if redact_ips else None,
            internal_cidrs=parse_networks(internal_cidrs),
            max_len=max_len,
        )


def cap(text: str, n: int | None) -> str:
    if n is None or len(text) <= n:
        return text
    return text[: max(0, n - 1)] + "…"


def apply_text(text: str, policy: Policy | None = None) -> str:
    out = redact_secrets(clean_control(text))
    if policy is not None:
        if policy.redact_ips and policy.ipmap is not None:
            out = tokenize_ips(out, policy.ipmap, policy.internal_cidrs)
        out = cap(out, policy.max_len)
    return out


def apply(value: Any, policy: Policy | None = None) -> Any:
    """Recursively redact every string inside `value` (dict keys are ours and kept)."""
    if isinstance(value, str):
        return apply_text(value, policy)
    if isinstance(value, Mapping):
        return {k: apply(v, policy) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [apply(v, policy) for v in value]
    return value


# --------------------------------------------------------------------------- reverse direction
class UnknownPlaceholder(ValueError):
    def __init__(self, placeholder: str) -> None:
        super().__init__(f"unknown address placeholder {placeholder}")
        self.placeholder = placeholder


def translate_args(value: Any, ipmap: IpMap | None) -> Any:
    """Placeholders in tool arguments → real addresses. Raises `UnknownPlaceholder`."""
    if isinstance(value, str):
        if not PLACEHOLDER.search(value):
            return value
        rev = ipmap.reverse if ipmap is not None else {}

        def sub(m: re.Match[str]) -> str:
            real = rev.get(m.group(0))
            if real is None:
                raise UnknownPlaceholder(m.group(0))
            return real

        return PLACEHOLDER.sub(sub, value)
    if isinstance(value, Mapping):
        return {k: translate_args(v, ipmap) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [translate_args(v, ipmap) for v in value]
    return value


def detokenize(value: Any, ipmap: IpMap | None) -> Any:
    if ipmap is None:
        return value
    if isinstance(value, str):
        return ipmap.detokenize(value)
    if isinstance(value, Mapping):
        return {k: detokenize(v, ipmap) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [detokenize(v, ipmap) for v in value]
    return value
