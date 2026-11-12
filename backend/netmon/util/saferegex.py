"""[1.3.3] Safe, linear-time regular expressions via Google RE2.

Python's `re` is a backtracking engine and is vulnerable to catastrophic
backtracking (ReDoS): a pattern like ``(a+)+$`` against ``"a" * 40 + "!"`` makes
`re` explore exponentially many paths and freezes the whole process. Because
event-rule regexes reach the *unauthenticated* UDP syslog/trap ingest path and
config-backup search runs attacker-controlled patterns, a single crafted value
could hang the entire uvicorn event loop.

RE2 matches in time linear in the input with a bounded memory budget and never
backtracks, so it removes the ReDoS class on the hot path without per-match
timeouts. The trade-off is that RE2 rejects constructs that cannot be matched in
linear time -- backreferences (``\1``) and lookaround (``(?=...)``) -- which we
turn into a clear validation error at rule-save / search time rather than a
silent hang.

This module is a leaf (it imports nothing from `netmon`) so the hot-path module
`netmon.ingest.rules` can import it without a cycle; callers pass the length
caps they already own (``rules.MAX_REGEX`` / ``rules.MAX_TEXT_SCAN``).
"""

from __future__ import annotations

import re2

__all__ = ["SafeRegex", "SafePatternError", "compile_safe", "check_safe"]

# Defaults mirror netmon.ingest.rules.MAX_REGEX / MAX_TEXT_SCAN; callers pass the
# real constants so there is a single source of truth, but these keep the module
# safe on its own.
DEFAULT_MAX_PATTERN = 200
DEFAULT_MAX_INPUT = 8192

# Cap the compiled program / DFA memory per pattern. RE2 stays linear in time but
# a large pattern can still build a big automaton; 8 MiB is far above anything a
# 200-char pattern needs and bounds worst-case memory.
_MAX_MEM = 8 << 20


class SafePatternError(ValueError):
    """A pattern is too long, does not compile under RE2, or uses a feature RE2
    rejects (backreferences, lookaround)."""


def _options(ignorecase: bool) -> re2.Options:
    opts = re2.Options()
    opts.log_errors = False  # we surface errors ourselves; keep RE2 off stderr
    opts.max_mem = _MAX_MEM
    opts.case_sensitive = not ignorecase
    return opts


class SafeRegex:
    """Thin wrapper over a compiled RE2 pattern.

    Matching always truncates the input to ``max_input`` characters as defence in
    depth, mirroring the ``value[:MAX_TEXT_SCAN]`` slicing the callers already do.
    """

    __slots__ = ("_rx", "_max_input", "pattern", "groups")

    def __init__(self, rx: re2.Pattern, pattern: str, max_input: int) -> None:
        self._rx = rx
        self._max_input = max_input
        self.pattern = pattern
        self.groups = rx.groups  # number of capture groups

    def _clip(self, text: str) -> str:
        if self._max_input is not None and len(text) > self._max_input:
            return text[: self._max_input]
        return text

    def search(self, text: str) -> re2.Match | None:
        return self._rx.search(self._clip(text))

    def finditer(self, text: str):  # noqa: ANN201 - re2 iterator
        return self._rx.finditer(self._clip(text))


def _compile(pattern: str, *, ignorecase: bool, multiline: bool) -> re2.Pattern:
    # RE2 anchors ^/$ to the whole text by default; `(?m)` switches them to line
    # boundaries, matching Python's re.MULTILINE. Prepending another inline-flag
    # group is valid even when the user's pattern already starts with one.
    src = f"(?m){pattern}" if multiline else pattern
    return re2.compile(src, _options(ignorecase))


def compile_safe(
    pattern: str | None,
    *,
    ignorecase: bool = True,
    multiline: bool = False,
    max_input: int = DEFAULT_MAX_INPUT,
) -> SafeRegex | None:
    """Compile ``pattern`` with RE2, or return ``None`` when it is empty or RE2
    cannot compile it (a backtracking feature, or a syntax error).

    Returning ``None`` for an unsupported pattern matches the existing
    ``Rule.regex()`` contract where an uncompilable pattern is treated as
    no-match; dangerous or unsupported patterns are rejected earlier at save time
    by :func:`check_safe`.
    """
    if not pattern:
        return None
    try:
        rx = _compile(pattern, ignorecase=ignorecase, multiline=multiline)
    except (re2.error, ValueError):
        return None
    return SafeRegex(rx, pattern, max_input)


def check_safe(
    pattern: str,
    *,
    max_len: int = DEFAULT_MAX_PATTERN,
    ignorecase: bool = True,
    multiline: bool = False,
) -> None:
    """Validate ``pattern`` for storage/use, raising :class:`SafePatternError`
    when it is too long, does not compile under RE2, or uses a feature RE2
    rejects (backreferences, lookaround). Used at rule-save and search-input
    validation so a dangerous or unsupported pattern can never be stored or run.
    """
    if len(pattern) > max_len:
        raise SafePatternError(f"at most {max_len} characters")
    try:
        _compile(pattern, ignorecase=ignorecase, multiline=multiline)
    except (re2.error, ValueError) as exc:
        detail = exc.args[0] if exc.args else str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise SafePatternError(f"invalid or unsupported regular expression: {detail}") from None
