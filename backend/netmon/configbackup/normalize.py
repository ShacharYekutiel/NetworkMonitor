"""[3] Pure config-backup helpers (architecture § 13.2.5, data-model "Normalisation vectors",
"Retention selection", `ssh_host_keys` decisions): normalisation and secret masking, hashing,
unified diff, retention selection, host-key decision."""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

MASK = "<secret removed>"
DIFF_CONTEXT = 3
DIFF_MAX_LINES = 20000
ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()*+][0-9A-Za-z]"  # charset selection
    r"|\x1b[@-Z\\-_=>78]"  # other two-byte sequences
)
OVERSTRIKE_RE = re.compile(r"[^\x08\n]\x08")
CRLF_RE = re.compile(r"\r+\n")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class NormalizedConfig:
    text: str
    sha256: str
    lines: int
    size_bytes: int


def clean_terminal(raw: str, strip_ansi: bool = True) -> str:
    """CRLF/CR -> LF, ANSI escapes and backspace overstrikes removed, other control
    characters dropped (tabs kept)."""
    s = CRLF_RE.sub("\n", raw)
    if strip_ansi:
        s = ANSI_RE.sub("", s)
    while True:
        s2 = OVERSTRIKE_RE.sub("", s)
        if s2 == s:
            break
        s = s2
    s = s.replace("\r", "\n")
    return CTRL_RE.sub("", s)


def mask_line(line: str, patterns: list[re.Pattern[str]]) -> str:
    """Replace the group-1 captures of every pattern with `<secret removed>`.

    Every pattern is matched against the ORIGINAL line and the union of the (non-empty) group-1
    spans is replaced once. Matching the original (not the progressively masked) line keeps the
    rules independent of their order and stops a rule from matching inside the mask text itself
    (e.g. `secret (\\S+)` on `... <secret removed>`); a line may carry several secrets (SNMPv3
    auth + priv keys), each found by `finditer`."""
    spans: list[tuple[int, int]] = []
    for rx in patterns:
        for m in rx.finditer(line):
            if m.lastindex is None or m.start(1) < 0 or m.end(1) <= m.start(1):
                continue
            spans.append((m.start(1), m.end(1)))
    if not spans:
        return line
    spans.sort()
    out: list[str] = []
    pos = 0
    cur_a, cur_b = spans[0]
    for a, b in spans[1:]:
        if a <= cur_b:
            cur_b = max(cur_b, b)
            continue
        out.append(line[pos:cur_a])
        out.append(MASK)
        pos = cur_b
        cur_a, cur_b = a, b
    out.append(line[pos:cur_a])
    out.append(MASK)
    out.append(line[cur_b:])
    return "".join(out)


def _strip_echo(lines: list[str], command: str, prompt: re.Pattern[str] | None) -> list[str]:
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines):
        first = lines[i].rstrip()
        if first.strip() == command:
            return lines[i + 1 :]
        if first.endswith(command):
            head = first[: -len(command)].rstrip()
            if not head or (prompt is not None and prompt.search(head)):
                return lines[i + 1 :]
    return lines[i:]


def _strip_prompt(lines: list[str], prompt: re.Pattern[str] | None) -> list[str]:
    j = len(lines)
    while j > 0 and not lines[j - 1].strip():
        j -= 1
    if j > 0 and prompt is not None and prompt.search(lines[j - 1].rstrip()):
        return lines[: j - 1]
    return lines[:j]


def normalize_output(
    raw: str,
    command: str,
    *,
    prompt: str | None = None,
    ignore_lines: list[str] | tuple[str, ...] = (),
    secrets: list[str] | tuple[str, ...] = (),
    mask: bool = True,
    strip_ansi: bool = True,
) -> list[str]:
    """One command's output -> cleaned lines (echo, trailing prompt and ignored lines removed,
    trailing white space stripped, secrets masked when `mask`)."""
    prx = re.compile(prompt) if prompt else None
    ign = [re.compile(p) for p in ignore_lines]
    sec = [re.compile(p) for p in secrets] if mask else []
    lines = clean_terminal(raw, strip_ansi).split("\n")
    lines = _strip_echo(lines, command, prx)
    lines = _strip_prompt(lines, prx)
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if any(rx.match(line) for rx in ign):
            continue
        out.append(mask_line(line, sec) if sec else line)
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return out


def normalize(
    outputs: list[tuple[str, str]],
    driver: Any,
    mask: bool = True,
) -> NormalizedConfig:
    """`outputs`: (command, raw output) per backup command. With more than one command the
    outputs are joined with a header line `# ==== <command> ====`."""
    parts: list[str] = []
    multi = len(outputs) > 1
    for command, raw in outputs:
        lines = normalize_output(
            raw,
            command,
            prompt=getattr(driver, "prompt", None),
            ignore_lines=getattr(driver, "ignore_lines", ()),
            secrets=getattr(driver, "secrets", ()),
            mask=mask,
            strip_ansi=getattr(driver, "strip_ansi", True),
        )
        if multi:
            parts.append(f"# ==== {command} ====")
        parts.extend(lines)
    text = "\n".join(parts) + "\n" if parts else ""
    return finish(text)


def finish(text: str) -> NormalizedConfig:
    data = text.encode("utf-8")
    return NormalizedConfig(
        text=text,
        sha256=hashlib.sha256(data).hexdigest(),
        lines=text.count("\n"),
        size_bytes=len(data),
    )


def diff(
    old: str,
    new: str,
    *,
    from_name: str = "a",
    to_name: str = "b",
    context: int = DIFF_CONTEXT,
    max_lines: int = DIFF_MAX_LINES,
) -> tuple[str, int, int, bool]:
    """Unified diff -> (text, added, removed, truncated). Counts cover the whole diff; the
    text is capped at `max_lines` lines."""
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=from_name,
            tofile=to_name,
            n=context,
            lineterm="",
        )
    )
    added = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
    removed = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
    return ("\n".join(lines) + ("\n" if lines else "")), added, removed, truncated


def line_changes(old: str, new: str) -> tuple[int, int]:
    """(added, removed) lines of `new` vs `old`."""
    added = removed = 0
    sm = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def select_expired(
    versions: list[tuple[int, datetime]],
    now: datetime,
    max_versions: int,
    retention_days: int,
    min_versions: int,
) -> list[int]:
    """`versions`: (id, collected_at) of one device. Newest first; keep index 0 and indexes
    < min_versions; of the rest delete index >= max_versions or older than retention_days."""
    ordered = sorted(versions, key=lambda v: (v[1], v[0]), reverse=True)
    cutoff = now - timedelta(days=retention_days)
    out = []
    for i, (vid, at) in enumerate(ordered):
        if i == 0 or i < min_versions:
            continue
        if i >= max_versions or at < cutoff:
            out.append(vid)
    return out


def host_key_decision(row: Any, presented_fingerprint: str) -> str:
    """`pin` (no row) | `accept` (pinned, same key) | `reject_changed`."""
    if row is None:
        return "pin"
    state = row["state"] if isinstance(row, dict) else row.state
    fp = row["fingerprint"] if isinstance(row, dict) else row.fingerprint
    if state == "pinned" and fp == presented_fingerprint:
        return "accept"
    return "reject_changed"
