#!/usr/bin/env python3
"""SNMP agent simulator for E2E tests: snmpsim with tests/sim/snmp/public.snmprec.

    backend/.venv/bin/python tests/sim/snmp_agent.py --port 11161

Community `public` (v1/v2c). The device "netmon-sim" exposes:
  - SNMPv2-MIB system (sysUpTime grows in real time)
  - IF-MIB ifTable/ifXTable: 1 lo, 2 eth0 (up, ~10 Mbit/s in, ~1 Mbit/s out, 1 error/s), 3 eth1 (oper down)
    The octet counters use snmpsim's `numeric` variation module, so they grow with wall time.
  - HOST-RESOURCES-MIB hrStorage (RAM 62.5 % used, "/" 25 %) and hrProcessorLoad (20 %, 40 % -> avg 30 %)

Runs snmpsim-command-responder in the foreground (from the same venv as this interpreter) after
copying the data into a private temp directory, dropping to nobody:nogroup when run as root, and
prints "READY <port>" once the agent answers a GET.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _probe(port: int, timeout: float = 0.5) -> bool:
    """Send a minimal SNMPv2c GET for sysName.0 and wait for any response."""
    # Pre-encoded BER: GET community "public" request-id 1 for 1.3.6.1.2.1.1.5.0
    oid = bytes([0x06, 0x08, 0x2B, 0x06, 0x01, 0x02, 0x01, 0x01, 0x05, 0x00])
    varbind = bytes([0x30, len(oid) + 2]) + oid + bytes([0x05, 0x00])
    vbl = bytes([0x30, len(varbind)]) + varbind
    pdu_body = bytes([0x02, 0x01, 0x01, 0x02, 0x01, 0x00, 0x02, 0x01, 0x00]) + vbl
    pdu = bytes([0xA0, len(pdu_body)]) + pdu_body
    comm = b"public"
    msg_body = bytes([0x02, 0x01, 0x01, 0x04, len(comm)]) + comm + pdu
    msg = bytes([0x30, len(msg_body)]) + msg_body
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg, ("127.0.0.1", port))
        s.recvfrom(4096)
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=11161)
    p.add_argument("--data", default=str(HERE / "snmp"))
    a = p.parse_args()

    exe = shutil.which("snmpsim-command-responder") or str(
        Path(sys.executable).parent / "snmpsim-command-responder"
    )
    work = Path(tempfile.mkdtemp(prefix="netmon-e2e-snmpsim-"))
    work.chmod(0o755)
    data = work / "data"
    shutil.copytree(a.data, data)
    for f in [data, *data.rglob("*")]:
        f.chmod(0o755 if f.is_dir() else 0o644)
    cache = work / "cache"
    cache.mkdir()
    args = [
        exe,
        f"--data-dir={data}",
        f"--cache-dir={cache}",
        f"--agent-udpv4-endpoint=127.0.0.1:{a.port}",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        shutil.chown(cache, "nobody")
        args += ["--process-user=nobody", "--process-group=nogroup"]
    proc = subprocess.Popen(args)

    def stop(*_: object) -> None:
        proc.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.time() + 30
    while time.time() < deadline and proc.poll() is None:
        if _probe(a.port):
            print(f"READY {a.port}", flush=True)
            break
        time.sleep(0.2)
    else:
        print("snmpsim did not start", file=sys.stderr, flush=True)
        proc.kill()
        return 1
    rc = proc.wait()
    shutil.rmtree(work, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
