"""[3b] Pure ``nm:`` link grammar, validation and answer sanitising (architecture § 14.3.7,
§ 14.5 item 2).

The model cites facts as ``[label](nm:<ref>)``. A link survives only when its ref was issued by
a tool of the same conversation (`known` refs); every other link (external URLs, forged refs,
``javascript:`` …) is rewritten to its label as plain text. Images are removed (alt text kept),
HTML tags are stripped and reference-style link definitions dropped, so no stored answer can
carry an exfiltration channel. Fenced code blocks and inline code spans are left untouched (the
renderers show them as text).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

_REF_PATTERNS = (
    r"device/\d{1,10}",
    r"sensor/\d{1,10}",
    r"group/\d{1,10}",
    r"port/\d{1,10}/\d{1,10}",
    r"event/\d{1,19}",
    r"syslog/\d{1,19}",
    r"trap/\d{1,19}",
    r"maintenance/\d{1,10}",
    r"config/\d{1,10}/\d{1,19}",
    r"config-diff/\d{1,10}/\d{1,19}/\d{1,19}",
    r"flows/[A-Za-z0-9_.~%=&+-]{1,300}",
    r"topology/\d{1,10}",
    r"incident/\d{1,19}",
    r"kb/\d{1,3}",
)
REF_RE = re.compile(r"(?:" + "|".join(_REF_PATTERNS) + r")")
MAX_KNOWN_REFS = 5000


def is_ref(ref: Any) -> bool:
    return isinstance(ref, str) and REF_RE.fullmatch(ref) is not None


def route(ref: str) -> str | None:
    """SPA route of a ref (``kb/<n>`` has none: its URL is the KB source)."""
    if not is_ref(ref):
        return None
    kind, _, rest = ref.partition("/")
    parts = rest.split("/")
    if kind in ("device", "sensor", "group"):
        return f"/{kind}s/{parts[0]}"
    if kind == "port":
        return f"/devices/{parts[0]}?tab=ports&port={parts[1]}"
    if kind == "event":
        return f"/events?id={parts[0]}"
    if kind in ("syslog", "trap"):
        return f"/logs?tab={kind}&id={parts[0]}"
    if kind == "maintenance":
        return f"/maintenance?id={parts[0]}"
    if kind == "config":
        return f"/devices/{parts[0]}?tab=configuration&version={parts[1]}"
    if kind == "config-diff":
        return f"/devices/{parts[0]}?tab=configuration&from={parts[1]}&to={parts[2]}"
    if kind == "flows":
        return f"/flows?{rest}"
    if kind == "topology":
        return f"/topology?focus={parts[0]}"
    if kind == "incident":
        return f"/incidents/{parts[0]}"
    return None


def absolute_url(
    ref: str, base_url: str | None, kb_sources: Mapping[str, str] | None = None
) -> str | None:
    """Telegram / email: absolute link for a server-issued ref (KB: its https source URL)."""
    if ref.startswith("kb/"):
        url = (kb_sources or {}).get(ref)
        return url if isinstance(url, str) and url.startswith("https://") else None
    path = route(ref)
    if path is None or not base_url:
        return None
    return base_url.rstrip("/") + path


def flows_ref(params: Mapping[str, Any]) -> str:
    """``flows/<query>`` built by the server (url-encoded, ≤ 300 chars)."""
    items = [
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}"
        for k, v in params.items()
        if v is not None and v != ""
    ]
    q = "&".join(items)[:300]
    return f"flows/{q}" if q else "flows/range=24h"


# --------------------------------------------------------------------------- refs in tool data
REF_KEYS_SUFFIX = "_ref"


def extract_refs(value: Any, out: list[str] | None = None) -> list[str]:
    """Every ``ref`` / ``*_ref`` string in a tool result (document order, deduplicated)."""
    if out is None:
        out = []
    if isinstance(value, Mapping):
        for k, v in value.items():
            if (k == "ref" or str(k).endswith(REF_KEYS_SUFFIX)) and is_ref(v):
                if v not in out:
                    out.append(v)
            elif isinstance(v, Mapping | list | tuple):
                extract_refs(v, out)
    elif isinstance(value, list | tuple):
        for v in value:
            extract_refs(v, out)
    return out


def labels_of(value: Any, out: dict[str, str] | None = None) -> dict[str, str]:
    """Best-effort label per ref from tool data (``name`` / ``title`` next to ``ref``)."""
    if out is None:
        out = {}
    if isinstance(value, Mapping):
        ref = value.get("ref")
        if is_ref(ref) and ref not in out:
            for key in ("name", "title", "sensor_name", "device_name", "trap_name", "if_name"):
                v = value.get(key)
                if isinstance(v, str) and v:
                    out[ref] = v[:120]
                    break
        for v in value.values():
            if isinstance(v, Mapping | list | tuple):
                labels_of(v, out)
    elif isinstance(value, list | tuple):
        for v in value:
            labels_of(v, out)
    return out


def merge_known(known: Sequence[str], new: Iterable[str]) -> list[str]:
    """Append new refs; keep at most MAX_KNOWN_REFS (oldest dropped)."""
    out = list(known)
    seen = set(out)
    for r in new:
        if r not in seen:
            out.append(r)
            seen.add(r)
    if len(out) > MAX_KNOWN_REFS:
        out = out[-MAX_KNOWN_REFS:]
    return out


# --------------------------------------------------------------------------- answer sanitising
_FENCE = re.compile(r"(^|\n)(```|~~~)[^\n]*\n.*?(?:\n\2[^\n]*(?=\n|$)|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_IMAGE = re.compile(r"!\[([^\]\n]{0,300})\]\([^)\n]{0,2000}\)")
_IMAGE_REF = re.compile(r"!\[([^\]\n]{0,300})\]\[[^\]\n]{0,100}\]")
_LINK = re.compile(r"\[([^\]\n]{0,300})\]\(\s*<?([^)\s>]{0,2000})>?(?:\s+\"[^\"\n]*\")?\s*\)")
_REF_DEF = re.compile(r"(?m)^[ ]{0,3}\[[^\]\n]{1,300}\]:[ \t]*\S+.*$\n?")
_AUTOLINK = re.compile(r"<((?:https?|ftp|mailto|javascript|data|file):[^>\s]{0,2000})>", re.I)
_PLACEHOLDERS = ("<secret removed>", "<hash removed>", "<pem removed>")
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]{0,40}(?:\s[^<>]{0,500})?/?>")
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)


@dataclass
class Sanitized:
    text: str
    sources: list[dict[str, Any]]
    dropped_links: int = 0
    dropped_images: int = 0


def _segments(text: str) -> list[tuple[bool, str]]:
    """Split into (is_code, chunk) pieces: fenced blocks and inline code are code."""
    out: list[tuple[bool, str]] = []
    pos = 0
    for m in _FENCE.finditer(text):
        start = m.start() + len(m.group(1))
        if start > pos:
            out.extend(_inline_segments(text[pos:start]))
        out.append((True, text[start : m.end()]))
        pos = m.end()
    if pos < len(text):
        out.extend(_inline_segments(text[pos:]))
    return out


def _inline_segments(text: str) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    pos = 0
    for m in _INLINE_CODE.finditer(text):
        if m.start() > pos:
            out.append((False, text[pos : m.start()]))
        out.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append((False, text[pos:]))
    return out


def _strip_tags(text: str) -> str:
    text = _COMMENT.sub("", text)

    def tag(m: re.Match[str]) -> str:
        return m.group(0) if m.group(0) in _PLACEHOLDERS else ""

    return _TAG.sub(tag, text)


def sanitize_answer(
    text: str,
    known: Iterable[str],
    *,
    labels: Mapping[str, str] | None = None,
    kb_sources: Mapping[str, str] | None = None,
) -> Sanitized:
    """Keep only ``[label](nm:<known ref>)`` links; everything else becomes plain text.
    Returns the text and the ``sources`` (deduplicated refs in order of first use)."""
    known_set = set(known)
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped = {"links": 0, "images": 0}

    def image(m: re.Match[str]) -> str:
        dropped["images"] += 1
        return m.group(1)

    def link(m: re.Match[str]) -> str:
        label, target = m.group(1), m.group(2)
        if target.startswith("nm:"):
            ref = target[3:]
            if ref in known_set and is_ref(ref):
                if ref not in seen:
                    seen.add(ref)
                    src: dict[str, Any] = {
                        "ref": ref,
                        "label": (label or (labels or {}).get(ref) or ref)[:200],
                        "url": None,
                    }
                    if ref.startswith("kb/"):
                        src["url"] = (kb_sources or {}).get(ref)
                    sources.append(src)
                return f"[{label}](nm:{ref})"
        dropped["links"] += 1
        return label

    pieces: list[str] = []
    for is_code, chunk in _segments(text or ""):
        if is_code:
            pieces.append(chunk)
            continue
        c = _IMAGE.sub(image, chunk)
        c = _IMAGE_REF.sub(image, c)
        c = _REF_DEF.sub("", c)
        c = _LINK.sub(link, c)
        c = _AUTOLINK.sub(lambda m: m.group(1), c)
        c = _strip_tags(c)
        pieces.append(c)
    return Sanitized("".join(pieces), sources, dropped["links"], dropped["images"])


def links_in(text: str) -> list[tuple[str, str]]:
    """(label, target) of every Markdown link outside code (tests, renderers)."""
    out: list[tuple[str, str]] = []
    for is_code, chunk in _segments(text or ""):
        if not is_code:
            out.extend((m.group(1), m.group(2)) for m in _LINK.finditer(chunk))
    return out
