"""[3] SSH session engine (architecture § 13.2.3): asyncssh connect with our own host-key
check (TOFU pinning, checked during key exchange before authentication), shell (PTY) or exec
mode, prompt detection, paging commands, timeouts and an output cap. Returns the raw text per
backup command; never stores anything and never logs credentials or output.

Only commands from a validated driver are sent (`netmon.configbackup.drivers`)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from netmon.configbackup.credentials import SshCreds
from netmon.configbackup.drivers import Driver
from netmon.configbackup.normalize import clean_terminal

log = logging.getLogger("netmon.configbackup.session")
logging.getLogger("asyncssh").setLevel(logging.WARNING)

MAX_OUTPUT = 8 * 1024 * 1024
CONNECT_TIMEOUT_S = 15.0
LOGIN_TIMEOUT_S = 30.0
KEEPALIVE_S = 15
TERM_SIZE = (511, 1000)
READ_CHUNK = 65536
TAIL = 4096  # bytes of the buffer cleaned for prompt detection
ERROR_SCAN_LINES = 10
LEGACY_KEX = ("diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1")
LEGACY_CIPHERS = ("aes128-cbc", "3des-cbc")
LEGACY_HOST_KEYS = ("ssh-rsa",)


class SessionError(Exception):
    """`code` is a `backup_error` value; `step` the failing step."""

    def __init__(
        self, code: str, message: str, step: str = "command", steps: list[dict] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:500]
        self.step = step
        self.steps = steps or []


@dataclass
class HostKeyInfo:
    algorithm: str
    fingerprint: str
    public_key: str

    def detail(self) -> str:
        return f"{self.algorithm} {self.fingerprint}"


@dataclass
class SessionResult:
    outputs: list[tuple[str, str]] = field(default_factory=list)
    host_key: HostKeyInfo | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)


HostKeyCheck = Callable[[HostKeyInfo], bool]


def key_info(key: Any) -> HostKeyInfo:
    pub = key.export_public_key("openssh")
    if isinstance(pub, bytes):
        pub = pub.decode("ascii", "replace")
    parts = pub.strip().split()
    return HostKeyInfo(
        algorithm=key.get_algorithm(),
        fingerprint=key.get_fingerprint("sha256"),
        public_key=" ".join(parts[:2]),
    )


def _algs(creds: SshCreds, driver: Driver | None) -> dict[str, Any]:
    legacy = creds.legacy_algorithms or bool(driver and driver.legacy_algorithms)
    if not legacy:
        return {}
    from asyncssh.encryption import get_default_encryption_algs, get_encryption_algs
    from asyncssh.kex import get_default_kex_algs, get_kex_algs

    def merged(default: list[bytes], available: list[bytes], extra: tuple[str, ...]) -> list:
        out = [a.decode() for a in default]
        avail = {a.decode() for a in available}
        out += [a for a in extra if a in avail and a not in out]
        return out

    return {
        "kex_algs": merged(get_default_kex_algs(), get_kex_algs(), LEGACY_KEX),
        "encryption_algs": merged(
            get_default_encryption_algs(), get_encryption_algs(), LEGACY_CIPHERS
        ),
    }


class _Steps:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._t = time.monotonic()

    def mark(self, step: str, ok: bool, detail: str = "") -> None:
        now = time.monotonic()
        self.items.append(
            {"step": step, "ok": ok, "detail": detail[:300], "ms": int((now - self._t) * 1000)}
        )
        self._t = now


def _last_line(buf: str) -> str:
    tail = clean_terminal(buf[-TAIL:])
    return tail.rsplit("\n", 1)[-1]


class _Shell:
    def __init__(self, proc: Any, driver: Driver, steps: _Steps) -> None:
        self.proc = proc
        self.driver = driver
        self.steps = steps
        assert driver.prompt is not None
        self.prompt = re.compile(driver.prompt)

    async def read_until(
        self,
        patterns: list[re.Pattern[str]],
        timeout: float,
        *,
        answer_pre: bool = False,
        step: str = "prompt",
    ) -> tuple[str, int]:
        """Read until one of `patterns` matches the last (cleaned) line. Returns (buffer,
        index of the pattern)."""
        buf: list[str] = []
        size = 0
        answered: set[int] = set()
        deadline = time.monotonic() + timeout
        while True:
            joined = "".join(buf[-8:]) if len(buf) > 8 else "".join(buf)
            last = _last_line(joined)
            for i, rx in enumerate(patterns):
                if rx.search(last):
                    return "".join(buf), i
            if answer_pre:
                tail = clean_terminal(joined[-TAIL:])
                for i, (pat, send) in enumerate(self.driver.pre_prompt):
                    if i not in answered and re.search(pat, tail):
                        answered.add(i)
                        self.proc.stdin.write(send)
                        break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionError(
                    "prompt_timeout", f"No prompt within {int(timeout)} s", step, self.steps.items
                )
            try:
                chunk = await asyncio.wait_for(self.proc.stdout.read(READ_CHUNK), remaining)
            except TimeoutError:
                raise SessionError(
                    "prompt_timeout", f"No prompt within {int(timeout)} s", step, self.steps.items
                ) from None
            if not chunk:
                raise SessionError(
                    "command_error", "The device closed the session", step, self.steps.items
                )
            size += len(chunk)
            if size > MAX_OUTPUT:
                raise SessionError("too_large", "The output exceeds 8 MiB", step, self.steps.items)
            buf.append(chunk)

    def _check_errors(self, output: str, command: str, step: str) -> None:
        lines = clean_terminal(output).split("\n")[: ERROR_SCAN_LINES + 1]
        for line in lines[1:] if lines and command in lines[0] else lines:
            for pat in self.driver.error_patterns:
                if re.search(pat, line):
                    raise SessionError(
                        "command_error",
                        f"{command}: {line.strip()[:200]}",
                        step,
                        self.steps.items,
                    )

    async def run(
        self, creds: SshCreds, *, command_limit: int | None = None
    ) -> list[tuple[str, str]]:
        d = self.driver
        buf, _ = await self.read_until([self.prompt], d.prompt_s, answer_pre=True)
        if d.enable is not None:
            if re.search(d.enable.when, _last_line(buf)):
                self.proc.stdin.write(d.enable.command + "\n")
                pw = re.compile(d.enable.password_prompt)
                buf, idx = await self.read_until([pw, self.prompt], d.prompt_s, step="prompt")
                if idx == 0:
                    self.proc.stdin.write((creds.enable_password or "") + "\n")
                    buf, _ = await self.read_until([self.prompt], d.prompt_s)
                if re.search(d.enable.when, _last_line(buf)):
                    raise SessionError("auth_failed", "enable failed", "prompt", self.steps.items)
        self.steps.mark("prompt", True)
        for cmd in d.session_setup:
            self.proc.stdin.write(cmd + "\n")
            out, _ = await self.read_until([self.prompt], d.prompt_s, step="setup")
            self._check_errors(out, cmd, "setup")
        if d.session_setup:
            self.steps.mark("setup", True, ", ".join(d.session_setup))
        outputs: list[tuple[str, str]] = []
        for c in d.commands[:command_limit]:
            self.proc.stdin.write(c.command + "\n")
            out, _ = await self.read_until([self.prompt], d.command_s, step="command")
            self._check_errors(out, c.command, "command")
            outputs.append((c.command, out))
        self.steps.mark("command", True, ", ".join(c for c, _ in outputs))
        try:
            self.proc.stdin.write("exit\n")
        except Exception:  # noqa: BLE001
            pass
        return outputs


async def _exec_one(conn: Any, cmd: str, timeout: float) -> tuple[int | None, str]:
    proc = await conn.create_process(cmd, encoding="utf-8", errors="replace")
    buf: list[str] = []
    size = 0
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SessionError("prompt_timeout", f"{cmd}: no answer within {int(timeout)} s")
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(READ_CHUNK), remaining)
            except TimeoutError:
                raise SessionError(
                    "prompt_timeout", f"{cmd}: no answer within {int(timeout)} s"
                ) from None
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_OUTPUT:
                raise SessionError("too_large", "The output exceeds 8 MiB")
            buf.append(chunk)
        try:
            await asyncio.wait_for(proc.wait_closed(), 5)
        except TimeoutError:
            pass
        return proc.exit_status, "".join(buf)
    finally:
        proc.close()


async def _exec_mode(
    conn: Any, driver: Driver, steps: _Steps, command_limit: int | None
) -> list[tuple[str, str]]:
    outputs: list[tuple[str, str]] = []
    for c in driver.commands[:command_limit]:
        tried = [c.command] + ([c.fallback] if c.fallback else [])
        last_err = ""
        for cmd in tried:
            status, out = await _exec_one(conn, cmd, driver.command_s)
            err = None
            if status not in (0, None):
                err = f"exit status {status}"
            else:
                for line in clean_terminal(out).split("\n")[:ERROR_SCAN_LINES]:
                    if any(re.search(p, line) for p in driver.error_patterns):
                        err = line.strip()[:200]
                        break
            if err is None:
                outputs.append((cmd, out))
                break
            last_err = f"{cmd}: {err}"
        else:
            raise SessionError("command_error", last_err, "command", steps.items)
    steps.mark("command", True, ", ".join(c for c, _ in outputs))
    return outputs


async def run_session(
    host: str,
    creds: SshCreds,
    driver: Driver,
    host_key_check: HostKeyCheck,
    *,
    total_timeout: float = 120.0,
    command_limit: int | None = None,
) -> SessionResult:
    """Connect, check the host key, authenticate, run the driver. Raises SessionError."""
    steps = _Steps()
    try:
        return await asyncio.wait_for(
            _run(host, creds, driver, host_key_check, steps, command_limit), total_timeout
        )
    except TimeoutError:
        last = steps.items[-1]["step"] if steps.items else "connect"
        code = (
            "connect_timeout"
            if last in ("connect", "host_key") and len(steps.items) < 3
            else ("prompt_timeout")
        )
        raise SessionError(
            code, f"The backup did not finish within {int(total_timeout)} s", last, steps.items
        ) from None


async def _run(
    host: str,
    creds: SshCreds,
    driver: Driver,
    host_key_check: HostKeyCheck,
    steps: _Steps,
    command_limit: int | None,
) -> SessionResult:
    import asyncssh

    result = SessionResult(steps=steps.items)
    seen: dict[str, Any] = {}

    class _Client(asyncssh.SSHClient):
        def validate_host_public_key(self, host_: str, addr: str, port: int, key: Any) -> bool:
            info = key_info(key)
            seen["key"] = info
            try:
                ok = bool(host_key_check(info))
            except Exception:  # noqa: BLE001
                log.exception("Host key check failed")
                ok = False
            seen["ok"] = ok
            return ok

    client_keys: list[Any] = []
    if creds.private_key:
        try:
            client_keys = [asyncssh.import_private_key(creds.private_key, creds.passphrase)]
        except Exception:  # noqa: BLE001
            raise SessionError(
                "auth_failed", "The private key cannot be read", "auth", steps.items
            ) from None
    try:
        conn = await asyncssh.connect(
            host,
            port=creds.port,
            username=creds.username,
            password=creds.password,
            client_keys=client_keys,
            passphrase=None,
            known_hosts=([], [], []),  # empty trust list: every key goes to the callback
            server_host_key_algs="default",
            agent_path=None,
            agent_forwarding=False,
            config=None,
            client_host_keysign=False,
            connect_timeout=CONNECT_TIMEOUT_S,
            login_timeout=LOGIN_TIMEOUT_S,
            keepalive_interval=KEEPALIVE_S,
            preferred_auth=(
                "publickey,keyboard-interactive,password"
                if client_keys
                else "keyboard-interactive,password"
            ),
            client_factory=_Client,
            **_algs(creds, driver),
        )
    except asyncssh.HostKeyNotVerifiable:
        info = seen.get("key")
        result.host_key = info
        steps.mark("connect", True)
        steps.mark("host_key", False, info.detail() if info else "")
        err = SessionError(
            "host_key_changed",
            f"The host key changed ({info.detail() if info else 'unknown key'})",
            "host_key",
            steps.items,
        )
        err.host_key = info  # type: ignore[attr-defined]
        raise err from None
    except asyncssh.PermissionDenied as exc:
        _mark_pre_auth(steps, seen)
        steps.mark("auth", False, str(exc.reason or "Permission denied"))
        raise SessionError(
            "auth_failed", f"Permission denied ({exc.reason or 'auth'})", "auth", steps.items
        ) from None
    except TimeoutError:
        if "key" in seen:
            _mark_pre_auth(steps, seen)
            steps.mark("auth", False, "timeout")
            raise SessionError("connect_timeout", "Login timed out", "auth", steps.items) from None
        steps.mark("connect", False, "timeout")
        raise SessionError(
            "connect_timeout", f"No answer from {host}:{creds.port}", "connect", steps.items
        ) from None
    except ConnectionRefusedError:
        steps.mark("connect", False, "refused")
        raise SessionError(
            "connect_refused", f"Connection refused by {host}:{creds.port}", "connect", steps.items
        ) from None
    except asyncssh.DisconnectError as exc:
        if "key" in seen:
            _mark_pre_auth(steps, seen)
            steps.mark("auth", False, str(exc.reason or exc))
            raise SessionError(
                "auth_failed",
                f"Disconnected during login: {exc.reason or exc}",
                "auth",
                steps.items,
            ) from None
        steps.mark("connect", False, str(exc.reason or exc))
        raise SessionError(
            "connect_refused",
            f"SSH negotiation failed: {exc.reason or exc}",
            "connect",
            steps.items,
        ) from None
    except OSError as exc:
        steps.mark("connect", False, exc.strerror or str(exc))
        raise SessionError(
            "connect_refused", f"Cannot connect: {exc.strerror or exc}", "connect", steps.items
        ) from None
    _mark_pre_auth(steps, seen)
    steps.mark("auth", True, creds.username)
    result.host_key = seen.get("key")
    try:
        async with conn:
            if driver.mode == "exec":
                steps.mark("prompt", True, "exec mode")
                result.outputs = await _exec_mode(conn, driver, steps, command_limit)
            else:
                proc = await conn.create_process(
                    term_type="vt100",
                    term_size=TERM_SIZE,
                    encoding="utf-8",
                    errors="replace",
                )
                try:
                    shell = _Shell(proc, driver, steps)
                    result.outputs = await shell.run(creds, command_limit=command_limit)
                finally:
                    proc.close()
    except SessionError as exc:
        exc.steps = steps.items
        if not steps.items or steps.items[-1]["ok"]:
            steps.mark(exc.step, False, exc.message)
        raise
    except asyncssh.Error as exc:
        steps.mark("command", False, str(exc))
        raise SessionError("command_error", f"SSH error: {exc}", "command", steps.items) from None
    except OSError as exc:
        steps.mark("command", False, str(exc))
        raise SessionError(
            "command_error", f"Connection lost: {exc}", "command", steps.items
        ) from None
    return result


def _mark_pre_auth(steps: _Steps, seen: dict[str, Any]) -> None:
    if any(s["step"] == "host_key" for s in steps.items):
        return
    steps.mark("connect", True)
    info = seen.get("key")
    steps.mark("host_key", True, info.detail() if info else "")
