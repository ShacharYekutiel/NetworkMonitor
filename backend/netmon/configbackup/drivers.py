"""[3] Config backup driver library (data-model § [3] Config backup drivers).

One YAML file per driver in `netmon/configbackup/drivers/`. Pure parts (unit-tested):
`validate_driver(data, file_id)` and `match_driver(drivers, templates, sys_object_id, override)`.
The command allow-list is enforced here: a driver that could send anything other than a
read-only backup command or a session-only paging command is rejected as a whole."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import yaml

log = logging.getLogger("netmon.configbackup.drivers")

ID_RE = re.compile(r"^[a-z0-9_]{2,64}$")
SESSION_SETUP_ALLOWED = frozenset(
    {
        "screen-length disable",
        "no page",
        "terminal length 0",
        "terminal width 0",
        "terminal pager 0",
        "set clienv rows 0",
    }
)
BACKUP_PREFIXES = ("display ", "show ", "/export", 'clish -c "show ')
FORBIDDEN_CHARS = (";", "|", "&", ">", "<", "`", "$(", "\n", "\r")
FORBIDDEN_FIRST_WORDS = frozenset(
    {
        "save",
        "write",
        "commit",
        "configure",
        "config",
        "conf",
        "copy",
        "delete",
        "erase",
        "reload",
        "reboot",
        "reset",
        "system-view",
        "undo",
        "set",
        "add",
        "remove",
        "/system",
    }
)
ENABLE_COMMANDS = frozenset({"enable"})
PRE_PROMPT_SEND_RE = re.compile(r"^[\r\n ]{1,4}$")  # keystrokes only, never a command
TOP_KEYS = frozenset(
    {
        "id",
        "name",
        "version",
        "confidence",
        "priority",
        "match",
        "mode",
        "prompt",
        "pre_prompt",
        "enable",
        "session_setup",
        "commands",
        "error_patterns",
        "ignore_lines",
        "secrets",
        "strip_ansi",
        "legacy_algorithms",
        "timeouts",
        "notes",
    }
)
MAX_REGEX = 300
MAX_PATTERNS = 64


class DriverError(ValueError):
    pass


@dataclass(frozen=True)
class Command:
    command: str
    fallback: str | None = None


@dataclass(frozen=True)
class Enable:
    when: str
    command: str
    password_prompt: str


@dataclass(frozen=True)
class Driver:
    id: str
    name: str
    version: int
    confidence: str
    priority: int
    mode: str  # shell | exec
    commands: tuple[Command, ...]
    match_templates: tuple[str, ...] = ()
    match_sys_object_id: tuple[str, ...] = ()
    prompt: str | None = None
    pre_prompt: tuple[tuple[str, str], ...] = ()
    enable: Enable | None = None
    session_setup: tuple[str, ...] = ()
    error_patterns: tuple[str, ...] = ()
    ignore_lines: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    strip_ansi: bool = True
    legacy_algorithms: bool = False
    prompt_s: float = 30.0
    command_s: float = 90.0
    notes: str = ""
    _rx: dict[str, re.Pattern[str]] = field(default_factory=dict, repr=False, compare=False)

    def rx(self, pattern: str) -> re.Pattern[str]:
        r = self._rx.get(pattern)
        if r is None:
            r = re.compile(pattern)
            self._rx[pattern] = r
        return r

    def out(self) -> dict[str, Any]:
        """api.md `ConfigDriver`."""
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "confidence": self.confidence,
            "mode": self.mode,
            "commands": [c.command for c in self.commands],
            "match_templates": list(self.match_templates),
            "notes": self.notes,
        }


# ------------------------------------------------------------------ pure validation
def _first_word(cmd: str) -> str:
    parts = cmd.strip().split()
    return parts[0].lower() if parts else ""


def check_backup_command(cmd: Any, where: str) -> str:
    """A backup command (or fallback): `display `, `show `, `/export` or `clish -c "show `,
    no shell metacharacters, no forbidden first word."""
    if not isinstance(cmd, str) or not cmd.strip():
        raise DriverError(f"{where}: must be a non-empty string")
    if cmd != cmd.strip():
        raise DriverError(f"{where}: leading or trailing white space")
    if len(cmd) > 200:
        raise DriverError(f"{where}: longer than 200 characters")
    for ch in FORBIDDEN_CHARS:
        if ch in cmd:
            raise DriverError(f"{where}: contains the forbidden sequence {ch!r}")
    low = cmd.lower()
    if not any(low.startswith(p) for p in BACKUP_PREFIXES) and low != "/export":
        raise DriverError(
            f"{where}: a backup command must start with 'display ', 'show ', '/export' or "
            "'clish -c \"show '"
        )
    if _first_word(cmd) in FORBIDDEN_FIRST_WORDS:
        raise DriverError(f"{where}: forbidden command {_first_word(cmd)!r}")
    if low.startswith("clish "):
        inner = cmd.split('"', 1)[1] if '"' in cmd else ""
        if not inner.endswith('"') or inner.count('"') != 1:
            raise DriverError(f"{where}: clish -c needs one quoted show command")
        if _first_word(inner[:-1]) != "show":
            raise DriverError(f"{where}: clish -c may only run a show command")
    return cmd


def check_setup_command(cmd: Any, where: str) -> str:
    if not isinstance(cmd, str):
        raise DriverError(f"{where}: must be a string")
    norm = " ".join(cmd.split()).lower()
    if norm not in SESSION_SETUP_ALLOWED:
        raise DriverError(
            f"{where}: {cmd!r} is not an allowed session command "
            f"({', '.join(sorted(SESSION_SETUP_ALLOWED))})"
        )
    for ch in FORBIDDEN_CHARS:
        if ch in cmd:
            raise DriverError(f"{where}: contains the forbidden sequence {ch!r}")
    return cmd.strip()


def _regex(value: Any, where: str, groups: int | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise DriverError(f"{where}: must be a non-empty regular expression")
    if len(value) > MAX_REGEX:
        raise DriverError(f"{where}: longer than {MAX_REGEX} characters")
    try:
        rx = re.compile(value)
    except re.error as exc:
        raise DriverError(f"{where}: invalid regular expression ({exc})") from None
    if groups is not None and rx.groups < groups:
        raise DriverError(f"{where}: needs a capture group")
    return value


def _regex_list(value: Any, where: str, groups: int | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_PATTERNS:
        raise DriverError(f"{where}: must be a list (at most {MAX_PATTERNS})")
    return tuple(_regex(v, f"{where}[{i}]", groups) for i, v in enumerate(value))


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise DriverError(f"{where}: must be a list of strings")
    return tuple(value)


def _int(value: Any, where: str, lo: int, hi: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise DriverError(f"{where}: must be an integer {lo}-{hi}")
    return value


def validate_driver(data: Any, file_id: str | None = None) -> Driver:
    """Validate one parsed driver file. Raises DriverError (one message) on any problem."""
    if not isinstance(data, dict):
        raise DriverError("the file must contain a mapping")
    unknown = set(data) - TOP_KEYS
    if unknown:
        raise DriverError(f"unknown keys: {', '.join(sorted(map(str, unknown)))}")
    did = data.get("id")
    if not isinstance(did, str) or not ID_RE.match(did):
        raise DriverError("id: must match ^[a-z0-9_]{2,64}$")
    if file_id is not None and did != file_id:
        raise DriverError(f"id: {did!r} differs from the file name {file_id!r}")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise DriverError("name: 1-128 characters")
    version = _int(data.get("version"), "version", 1, 1_000_000)
    confidence = data.get("confidence")
    if confidence not in ("verified", "unverified"):
        raise DriverError("confidence: must be verified or unverified")
    priority = _int(data.get("priority", 1000), "priority", 0, 100_000)
    match = data.get("match") or {}
    if not isinstance(match, dict) or set(match) - {"templates", "sys_object_id"}:
        raise DriverError("match: only templates and sys_object_id")
    templates = _str_list(match.get("templates"), "match.templates")
    oids = _str_list(match.get("sys_object_id"), "match.sys_object_id")
    for o in oids:
        if not re.match(r"^[0-9.]+$", o):
            raise DriverError(f"match.sys_object_id: {o!r} is not an OID prefix")
    mode = data.get("mode")
    if mode not in ("shell", "exec"):
        raise DriverError("mode: must be shell or exec")
    prompt = data.get("prompt")
    if mode == "shell":
        prompt = _regex(prompt, "prompt")
    elif prompt is not None:
        prompt = _regex(prompt, "prompt")
    pre: list[tuple[str, str]] = []
    pre_in = data.get("pre_prompt") or []
    if not isinstance(pre_in, list) or len(pre_in) > 4:
        raise DriverError("pre_prompt: a list of at most 4 entries")
    for i, p in enumerate(pre_in):
        if not isinstance(p, dict) or set(p) != {"pattern", "send"}:
            raise DriverError(f"pre_prompt[{i}]: needs exactly pattern and send")
        send = p["send"]
        if not isinstance(send, str) or not PRE_PROMPT_SEND_RE.match(send):
            raise DriverError(f"pre_prompt[{i}].send: only Enter / space keystrokes")
        pre.append((_regex(p["pattern"], f"pre_prompt[{i}].pattern"), send))
    enable = None
    en = data.get("enable")
    if en is not None:
        if mode != "shell":
            raise DriverError("enable: only for shell mode")
        if not isinstance(en, dict) or set(en) != {"when", "command", "password_prompt"}:
            raise DriverError("enable: needs exactly when, command and password_prompt")
        if not isinstance(en["command"], str) or en["command"].strip().lower() not in (
            ENABLE_COMMANDS
        ):
            raise DriverError("enable.command: only 'enable'")
        enable = Enable(
            _regex(en["when"], "enable.when"),
            en["command"].strip(),
            _regex(en["password_prompt"], "enable.password_prompt"),
        )
    setup_in = data.get("session_setup") or []
    if not isinstance(setup_in, list) or len(setup_in) > 8:
        raise DriverError("session_setup: a list of at most 8 commands")
    if setup_in and mode != "shell":
        raise DriverError("session_setup: only for shell mode")
    setup = tuple(check_setup_command(c, f"session_setup[{i}]") for i, c in enumerate(setup_in))
    cmds_in = data.get("commands")
    if not isinstance(cmds_in, list) or not 1 <= len(cmds_in) <= 4:
        raise DriverError("commands: 1-4 entries")
    cmds = []
    for i, c in enumerate(cmds_in):
        if not isinstance(c, dict) or "command" not in c or set(c) - {"command", "fallback"}:
            raise DriverError(f"commands[{i}]: needs command (and optional fallback)")
        fb = c.get("fallback")
        if fb is not None and mode != "exec":
            raise DriverError(f"commands[{i}].fallback: only for exec mode")
        cmds.append(
            Command(
                check_backup_command(c["command"], f"commands[{i}].command"),
                check_backup_command(fb, f"commands[{i}].fallback") if fb is not None else None,
            )
        )
    strip_ansi = data.get("strip_ansi", True)
    legacy = data.get("legacy_algorithms", False)
    if not isinstance(strip_ansi, bool) or not isinstance(legacy, bool):
        raise DriverError("strip_ansi / legacy_algorithms: must be booleans")
    timeouts = data.get("timeouts") or {}
    if not isinstance(timeouts, dict) or set(timeouts) - {"prompt_s", "command_s"}:
        raise DriverError("timeouts: only prompt_s and command_s")
    prompt_s = _int(timeouts.get("prompt_s", 30), "timeouts.prompt_s", 1, 600)
    command_s = _int(timeouts.get("command_s", 90), "timeouts.command_s", 1, 600)
    notes = data.get("notes") or ""
    if not isinstance(notes, str):
        raise DriverError("notes: must be a string")
    return Driver(
        id=did,
        name=name.strip(),
        version=version,
        confidence=confidence,
        priority=priority,
        mode=mode,
        commands=tuple(cmds),
        match_templates=templates,
        match_sys_object_id=oids,
        prompt=prompt,
        pre_prompt=tuple(pre),
        enable=enable,
        session_setup=setup,
        error_patterns=_regex_list(data.get("error_patterns"), "error_patterns"),
        ignore_lines=_regex_list(data.get("ignore_lines"), "ignore_lines"),
        secrets=_regex_list(data.get("secrets"), "secrets", groups=1),
        strip_ansi=strip_ansi,
        legacy_algorithms=legacy,
        prompt_s=float(prompt_s),
        command_s=float(command_s),
        notes=notes,
    )


# ------------------------------------------------------------------ pure matching
def ordered(drivers: dict[str, Driver] | list[Driver]) -> list[Driver]:
    items = list(drivers.values()) if isinstance(drivers, dict) else list(drivers)
    return sorted(items, key=lambda d: (d.priority, d.id))


def match_driver(
    drivers: dict[str, Driver],
    templates: list[str] | tuple[str, ...] | set[str],
    sys_object_id: str | None,
    override: str | None = None,
) -> tuple[str | None, str | None]:
    """(driver id, source) with source `override` | `template` | `sys_object_id`, or
    (None, None). An override naming an unknown driver resolves to no driver."""
    if override:
        return (override, "override") if override in drivers else (None, None)
    tset = {t for t in templates if t}
    order = ordered(drivers)
    for d in order:
        if tset & set(d.match_templates):
            return d.id, "template"
    oid = (sys_object_id or "").strip().lstrip(".")
    if oid:
        for d in order:
            for prefix in d.match_sys_object_id:
                p = prefix.lstrip(".")
                if oid == p.rstrip(".") or oid.startswith(p if p.endswith(".") else p + "."):
                    return d.id, "sys_object_id"
    return None, None


# ------------------------------------------------------------------ loading
@dataclass
class Library:
    drivers: dict[str, Driver] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)


def load_texts(texts: list[tuple[str, str]]) -> Library:
    """`texts`: (file name, YAML text). Invalid files are skipped with an error entry."""
    lib = Library()
    for fname, body in sorted(texts):
        file_id = fname.rsplit("/", 1)[-1].removesuffix(".yaml")
        try:
            data = yaml.safe_load(body)
            drv = validate_driver(data, file_id)
            if drv.id in lib.drivers:
                raise DriverError(f"duplicate driver id {drv.id!r}")
            lib.drivers[drv.id] = drv
        except yaml.YAMLError as exc:
            # no parser text in the API: file and line are enough to find the mistake
            mark = getattr(exc, "problem_mark", None)
            msg = f"invalid YAML at line {mark.line + 1}" if mark is not None else "invalid YAML"
            lib.errors.append({"file": fname, "error": msg})
            log.warning("Config backup driver %s skipped: %s", fname, msg)
        except DriverError as exc:
            msg = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
            lib.errors.append({"file": fname, "error": msg})
            log.warning("Config backup driver %s skipped: %s", fname, msg)
    return lib


def load_builtin() -> Library:
    texts = []
    base = resources.files("netmon.configbackup") / "drivers"
    for entry in base.iterdir():
        if entry.name.endswith(".yaml"):
            texts.append((entry.name, entry.read_text(encoding="utf-8")))
    return load_texts(texts)


_LIBRARY: Library | None = None


def library() -> Library:
    global _LIBRARY
    if _LIBRARY is None:
        _LIBRARY = load_builtin()
    return _LIBRARY


def get(driver_id: str | None) -> Driver | None:
    if not driver_id:
        return None
    return library().drivers.get(driver_id)
