#!/usr/bin/env python3
"""Runs the PRODUCT session engine + normaliser (netmon.configbackup.session / normalize / drivers) against a
running SSH simulator lab and compares the stored text with expected/<device>/<version>.txt.

    backend/.venv/bin/python tests/sim/ssh/engine_check.py --control http://127.0.0.1:18622

No database: this checks the engine/driver/normaliser layer only (the DB-backed runner is covered by the
backend integration tests and E2E spec 52). Prints PASS/FAIL per case; exit 1 on any failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "backend"))
from expected import PERSONALITY_DRIVER, leaked_secrets  # noqa: E402
from fake_device import allowed_commands  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" - {detail}"), flush=True)
    if not ok:
        FAILS.append(name)


def ctl(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def first_diff(a: str, b: str) -> str:
    al, bl = a.split("\n"), b.split("\n")
    for i, (x, y) in enumerate(zip(al, bl, strict=False)):
        if x != y:
            return f"line {i + 1}: got {x!r} want {y!r}"
    return f"lengths {len(al)} vs {len(bl)}"


async def main(base: str) -> None:
    from netmon.configbackup import drivers as drv_mod
    from netmon.configbackup.credentials import SshCreds
    from netmon.configbackup.normalize import normalize
    from netmon.configbackup.session import SessionError, run_session

    lib = drv_mod.load_builtin()
    drivers = getattr(lib, "drivers", None) or getattr(lib, "by_id", None) or lib
    errors = getattr(lib, "errors", [])
    check("all 7 shipped drivers load without errors", not errors and len(drivers) == 7, f"{errors} {list(drivers)}")
    devs = {d["name"]: d for d in ctl(base, "GET", "/devices")}
    ctl(base, "DELETE", "/log")
    pins: dict[str, str] = {}

    def creds(dev: dict, **kw) -> SshCreds:
        return SshCreds(username="netmon-backup", port=dev["port"], password=kw.get("password", "Backup-pass-1"),
                        enable_password="Enable-pass-1", legacy_algorithms=kw.get("legacy", False),
                        private_key=kw.get("private_key"))

    def pin_check(name: str):
        def f(info) -> bool:
            fp = info.fingerprint
            if name not in pins:
                pins[name] = fp
                return True
            return pins[name] == fp
        return f

    async def backup(dev: dict, **kw):
        d = drivers[PERSONALITY_DRIVER[dev["personality"]]]
        res = await run_session(dev["address"], creds(dev, **kw), d, pin_check(dev["name"]),
                                total_timeout=kw.get("timeout", 60))
        return normalize(res.outputs, d, True).text

    for name, dev in devs.items():
        if dev["flags"]:
            continue
        for v in ("v1", "v2"):
            ctl(base, "POST", f"/devices/{name}/state", {"version": v})
            try:
                got = await backup(dev)
                want = (HERE / "expected" / name / f"{v}.txt").read_text()
                check(f"{name} {v}: engine output normalised == expected", got == want, first_diff(got, want))
                leaks = [s for s in leaked_secrets(got) if name != "sw-cw1" or "cHJpdktl" not in s]
                check(f"{name} {v}: no fixture secret stored", not leaks, str(leaks))
            except SessionError as e:
                check(f"{name} {v}: backup", False, f"{e.code}: {e}")
        ctl(base, "POST", f"/devices/{name}/state", {"version": "v1"})

    # legacy-only with and without the flag
    leg = devs["sw-legacy"]
    try:
        await backup(leg)
        check("legacy-only device without legacy_algorithms fails", False,
              "succeeded: asyncssh offers group14-sha1 + ssh-rsa by default (contract assumption wrong)")
    except SessionError as e:
        check("legacy-only device without legacy_algorithms fails (connect_refused per engine)", True, e.code)
        print(f"     -> code {e.code}: {e}")
    try:
        got = await backup(leg, legacy=True)
        check("legacy-only device with legacy_algorithms ok", got == (HERE / "expected/sw-legacy/v1.txt").read_text())
    except SessionError as e:
        check("legacy-only device with legacy_algorithms ok", False, f"{e.code}: {e}")

    strict = devs.get("sw-legacy1")
    if strict:
        try:
            await backup(strict)
            check("legacy-strict device (group1-sha1/CBC) without legacy_algorithms fails", False, "succeeded")
        except SessionError as e:
            check("legacy-strict device (group1-sha1/CBC) without legacy_algorithms fails", True, e.code)
        try:
            got = await backup(strict, legacy=True)
            check("legacy-strict device with legacy_algorithms ok",
                  got == (HERE / "expected/sw-legacy1/v1.txt").read_text())
        except SessionError as e:
            check("legacy-strict device with legacy_algorithms ok", False, f"{e.code}: {e}")

    # auth failure
    try:
        await backup(devs["sw-cw1"], password="wrong")
        check("wrong password -> auth_failed", False, "succeeded")
    except SessionError as e:
        check("wrong password -> auth_failed", e.code == "auth_failed", e.code)

    # public-key auth
    ck = ctl(base, "GET", "/client-key")
    try:
        got = await backup(devs["r1"], password=None, private_key=ck["private_key"])
        check("public-key auth (Cisco, enable step)", got == (HERE / "expected/r1/v1.txt").read_text())
    except SessionError as e:
        check("public-key auth", False, f"{e.code}: {e}")

    # hang -> prompt_timeout within the total timeout
    t0 = time.monotonic()
    try:
        await backup(devs["sw-hang"], timeout=40)
        check("hang-after-login -> prompt_timeout", False, "succeeded")
    except SessionError as e:
        dt = time.monotonic() - t0
        check(f"hang-after-login -> prompt_timeout ({dt:.0f} s)", e.code == "prompt_timeout" and dt < 41, e.code)

    # huge -> too_large
    try:
        await backup(devs["rb-huge"])
        check("huge (9 MiB) -> too_large", False, "succeeded")
    except SessionError as e:
        check("huge (9 MiB) -> too_large", e.code == "too_large", e.code)

    # host key rotated -> host_key_changed before any auth attempt
    rb = devs["rb1"]
    await backup(rb)  # pins
    ctl(base, "POST", "/devices/rb1/rotate-host-key")
    ctl(base, "DELETE", "/log")
    try:
        await backup(rb)
        check("rotated host key -> host_key_changed", False, "succeeded")
    except SessionError as e:
        check("rotated host key -> host_key_changed", e.code == "host_key_changed", e.code)
    log = ctl(base, "GET", "/log?device=rb1")
    check("no auth attempt reached the device after the key change", not any(x["event"].startswith("auth")
                                                                             for x in log), json.dumps(log)[:300])
    ctl(base, "POST", "/devices/rb1/restore-host-key")

    # connect refused
    ctl(base, "POST", "/devices/fgt1/stop")
    try:
        await backup(devs["fgt1"])
        check("listener down -> connect_refused", False, "succeeded")
    except SessionError as e:
        check("listener down -> connect_refused", e.code == "connect_refused", e.code)
    ctl(base, "POST", "/devices/fgt1/start")

    bad = [x for x in ctl(base, "GET", "/log") if x["event"] in ("command", "exec") and x["command"]
           not in allowed_commands()]
    check("SSH sim command log: only allow-listed commands", not bad, json.dumps(bad)[:400])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", default="http://127.0.0.1:18622")
    a = ap.parse_args()
    asyncio.run(main(a.control))
    print(f"{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)
