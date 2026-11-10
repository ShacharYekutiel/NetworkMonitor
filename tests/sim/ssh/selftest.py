#!/usr/bin/env python3
"""Self-test of the SSH simulator with a reference backup client (QA oracle; not the product engine).

    python tests/sim/ssh/selftest.py --control http://127.0.0.1:18622

Starts nothing: point it at a running fake_device.py --lab. For every lab personality it logs in like the
contract's session engine (architecture.md §13.2.3: PTY vt100 511x1000, prompt regex at the end of the cleaned
buffer, pre_prompt answers, enable, session setup, backup command; exec mode with fallback), normalises with
expected.normalize and compares with expected/<device>/<version>.txt. Then: paging without the paging command
shows the marker; hang-after-login -> no prompt within 3 s; huge -> > 8 MiB; legacy-only refuses modern-only
clients and accepts group14-sha1/ssh-rsa; a rotated host key is refused by a pinning client before any auth
attempt reaches the device (command log); public-key auth; wrong password; the command log only holds
allow-listed commands. Exit 0 when everything passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import urllib.request
from pathlib import Path

import asyncssh

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from expected import DRIVERS, PERSONALITY_DRIVER, leaked_secrets, normalize  # noqa: E402
from fake_device import allowed_commands  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PASSWORD = "Backup-pass-1"
ENABLE = "Enable-pass-1"
USER = "netmon-backup"
RESULTS: list[tuple[str, bool, str]] = []


def ctl(base: str, method: str, path: str, body: dict | None = None) -> object:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} {name}{(' - ' + detail) if detail and not ok else ''}", flush=True)


# NOTE (QA finding): asyncssh never calls validate_host_public_key when known_hosts=None (host key checking is
# disabled entirely), so the contract's `known_hosts=None` + override (architecture §13.2.3) would accept ANY
# key. An empty known-hosts object (import_known_hosts("")) keeps ~/.ssh out and routes every key to the callback.
class Pinned(asyncssh.SSHClient):
    def __init__(self, pin: str | None):
        self.pin = pin
        self.seen: str | None = None

    def validate_host_public_key(self, host: str, addr: str, port: int, key: asyncssh.SSHKey) -> bool:
        self.seen = key.get_fingerprint("sha256")
        return self.pin is None or self.seen == self.pin


async def connect(dev: dict, pin: str | None = None, legacy: bool = False, **kw: object):
    client = Pinned(pin)
    opts: dict = {}
    if legacy:
        opts = {"kex_algs": ["diffie-hellman-group14-sha1", "curve25519-sha256"]}
        kw["server_host_key_algs"] = ["ssh-rsa", "ssh-ed25519", "rsa-sha2-256"]
    conn, _ = await asyncssh.create_connection(
        lambda: client, dev["address"], dev["port"], username=kw.pop("username", USER),
        password=kw.pop("password", PASSWORD), known_hosts=asyncssh.import_known_hosts(""), agent_path=None,
        config=None, server_host_key_algs=kw.pop("server_host_key_algs", "default"),
        client_keys=kw.pop("client_keys", None), connect_timeout=10, login_timeout=15, **opts, **kw)
    return conn, client


async def read_until_prompt(proc, prompt_re: re.Pattern, pre: list, timeout: float = 10.0) -> str:
    buf = ""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    answered: set[str] = set()
    while True:
        cleaned = ANSI.sub("", buf).replace("\r", "")
        for pat, send in pre:
            if pat in cleaned and pat not in answered:
                answered.add(pat)
                proc.stdin.write(send)
        last = cleaned.split("\n")[-1]
        if prompt_re.search(last):
            return buf
        left = end - loop.time()
        if left <= 0:
            raise TimeoutError(f"prompt timeout, tail={cleaned[-120:]!r}")
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(65536), left)
        except TimeoutError as e:
            raise TimeoutError(f"prompt timeout, tail={cleaned[-120:]!r}") from e
        if not chunk:
            raise EOFError("closed")
        buf += chunk


def strip_echo_prompt(raw: str, cmd: str) -> str:
    text = ANSI.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[0].strip() == cmd:
        lines = lines[1:]
    if lines:
        lines = lines[:-1]  # trailing prompt line
    return "\n".join(lines)


async def backup_shell(dev: dict, drv: dict, pin: str | None = None, setup: bool = True,
                       timeout: float = 10.0) -> str:
    conn, _ = await connect(dev, pin)
    try:
        proc = await conn.create_process(term_type="vt100", term_size=(511, 1000), encoding="utf-8")
        prompt_re = re.compile(drv["prompt"])
        await read_until_prompt(proc, prompt_re, drv.get("pre_prompt", []), timeout)
        if drv.get("enable_when"):
            # the prompt ends with ">" -> enable
            proc.stdin.write("enable\n")
            await read_until_prompt(proc, re.compile(r"Password: ?$"), [], timeout)
            proc.stdin.write(ENABLE + "\n")
            await read_until_prompt(proc, prompt_re, [], timeout)
        for c in drv.get("setup", []) if setup else []:
            proc.stdin.write(c + "\n")
            await read_until_prompt(proc, prompt_re, [], timeout)
        outs = []
        for c in drv["commands"]:
            proc.stdin.write(c + "\n")
            outs.append(strip_echo_prompt(await read_until_prompt(proc, prompt_re, [], timeout), c))
        proc.stdin.write("exit\n")
        return "\n".join(outs)
    finally:
        conn.close()


async def backup_exec(dev: dict, drv: dict, legacy: bool = False) -> str:
    conn, _ = await connect(dev, legacy=legacy)
    try:
        outs = []
        for c in drv["commands"]:
            r = await conn.run(c, timeout=30)
            if r.exit_status != 0 and c in drv.get("fallback", {}):
                r = await conn.run(drv["fallback"][c], timeout=30)
            if r.exit_status != 0:
                raise RuntimeError(f"exec {c!r} -> {r.exit_status} {r.stderr!r}")
            outs.append(r.stdout)
        return "\n".join(outs)
    finally:
        conn.close()


async def backup(dev: dict, **kw: object) -> str:
    drv = DRIVERS[PERSONALITY_DRIVER[dev["personality"]]]
    if drv["mode"] == "exec":
        return await backup_exec(dev, drv, legacy=bool(kw.get("legacy")))
    return await backup_shell(dev, drv, **kw)  # type: ignore[arg-type]


async def run(base: str) -> None:
    devs = {d["name"]: d for d in ctl(base, "GET", "/devices")}  # type: ignore[union-attr]
    ctl(base, "DELETE", "/log")
    for name, dev in devs.items():
        if dev["flags"]:
            continue
        ctl(base, "POST", f"/devices/{name}/state", {"version": "v1"})
        drvid = PERSONALITY_DRIVER[dev["personality"]]
        for v in ("v1", "v2"):
            ctl(base, "POST", f"/devices/{name}/state", {"version": v})
            try:
                got = normalize(await backup(dev), drvid)
                want = (HERE / "expected" / name / f"{v}.txt").read_text()
                check(f"{name} {v} normalised == expected", got == want, _first_diff(got, want))
                check(f"{name} {v} no fixture secret leaked", not leaked_secrets(got) or name == "sw-cw1",
                      str(leaked_secrets(got)))
            except Exception as e:  # noqa: BLE001
                check(f"{name} {v} backup", False, f"{type(e).__name__}: {e}")
        ctl(base, "POST", f"/devices/{name}/state", {"version": "v1"})
        # unchanged second run: same normalised text (volatile lines ignored)
        if dev["personality"] in ("cisco_ios", "mikrotik", "fortigate", "checkpoint_clish"):
            a = normalize(await backup(dev), drvid)
            await asyncio.sleep(1.1)
            b = normalize(await backup(dev), drvid)
            check(f"{name} volatile lines ignored (two runs equal)", a == b, _first_diff(a, b))

    # paging: without the paging command the marker appears and the prompt does not come back by itself
    cw = devs["sw-cw1"]
    try:
        await backup_shell(cw, DRIVERS["hpe_comware"], setup=False, timeout=3)
        check("paging without screen-length disable blocks at the More marker", False, "got the prompt")
    except TimeoutError as e:
        check("paging without screen-length disable blocks at the More marker", "More" in str(e), str(e))

    # hang after login
    try:
        await backup_shell(devs["sw-hang"], DRIVERS["hpe_comware"], timeout=3)
        check("hang-after-login -> prompt timeout", False, "got a prompt")
    except TimeoutError:
        check("hang-after-login -> prompt timeout", True)

    # huge
    out = await backup(devs["rb-huge"])
    check("huge output > 8 MiB", len(out.encode()) > 8 * 1024 * 1024, str(len(out)))

    # legacy only
    leg = devs["sw-legacy"]
    try:
        conn, _ = await connect(leg, kex_algs=["curve25519-sha256"], server_host_key_algs=["ssh-ed25519",
                                                                                           "rsa-sha2-256"])
        conn.close()
        check("legacy-only refuses modern-only algorithms", False, "connected")
    except (asyncssh.Error, OSError) as e:
        check("legacy-only refuses modern-only algorithms", True, str(e))
    try:
        conn, _ = await connect(leg, legacy=True)
        conn.close()
        check("legacy-only accepts group14-sha1 + ssh-rsa", True)
    except (asyncssh.Error, OSError) as e:
        check("legacy-only accepts group14-sha1 + ssh-rsa", False, str(e))

    # wrong password
    try:
        conn, _ = await connect(cw, password="wrong")
        conn.close()
        check("wrong password refused", False)
    except asyncssh.PermissionDenied:
        check("wrong password refused", True)

    # public key auth
    ck = ctl(base, "GET", "/client-key")
    key = asyncssh.import_private_key(ck["private_key"])  # type: ignore[index]
    try:
        conn, _ = await connect(cw, password=None, client_keys=[key])
        conn.close()
        check("public-key auth", True)
    except asyncssh.Error as e:
        check("public-key auth", False, str(e))

    # host key rotation: a pinning client refuses before auth (no auth_* events after the rotation)
    rb = devs["rb1"]
    pin = rb["fingerprint"]
    rot = ctl(base, "POST", "/devices/rb1/rotate-host-key")
    ctl(base, "DELETE", "/log")
    try:
        conn, _ = await connect(rb, pin=pin)
        conn.close()
        check("rotated host key refused by a pinned client", False, "connected")
    except (asyncssh.Error, OSError):
        check("rotated host key refused by a pinned client", rot["fingerprint"] != pin)  # type: ignore[index]
    log = ctl(base, "GET", "/log?device=rb1")
    check("no auth attempt reached the device after the host-key change",
          not any(x["event"].startswith("auth") for x in log), json.dumps(log)[:300])  # type: ignore[union-attr]
    ctl(base, "POST", "/devices/rb1/restore-host-key")

    # read-only guarantee from the command log
    allowed = allowed_commands()
    bad = [x for x in ctl(base, "GET", "/log") if x["event"] in ("command", "exec")  # type: ignore[union-attr]
           and x["command"] not in allowed]
    check("command log holds only allow-listed commands", not bad, json.dumps(bad)[:300])


def _first_diff(a: str, b: str) -> str:
    al, bl = a.split("\n"), b.split("\n")
    for i, (x, y) in enumerate(zip(al, bl, strict=False)):
        if x != y:
            return f"line {i + 1}: got {x!r} want {y!r}"
    return f"lengths {len(al)} vs {len(bl)}" if len(al) != len(bl) else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", default="http://127.0.0.1:18622")
    a = ap.parse_args()
    asyncio.run(run(a.control))
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
