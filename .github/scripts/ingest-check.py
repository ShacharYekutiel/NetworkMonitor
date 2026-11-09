#!/usr/bin/env python3
"""CI smoke test of the phase 2b-3 trap / syslog receivers through the published ports.

Used by .github/workflows/ci.yml (docker job) against the real compose stack:

  ingest-check.py listening       wait until the three receivers report "listening"
                                  (the features were turned on through PUT /api/settings)
  ingest-check.py send-and-find   send one syslog message over UDP 514, one over TCP 514
                                  and one SNMPv2c trap to UDP 162 on the host, then find
                                  them through GET /api/syslog and GET /api/traps

Authentication: the session cookie that curl stored in cookies.txt (the setup step).
Standard library only. The trap is BER-encoded by hand (no pysnmp on the runner).
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.parse
import urllib.request
import uuid

BASE = "http://localhost:8080"
HOST = "127.0.0.1"
TRAP_PORT = 162
SYSLOG_PORT = 514
COOKIE_FILE = "cookies.txt"


def cookie_header() -> str:
    pairs = []
    with open(COOKIE_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_") :]
            elif not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) >= 7:
                pairs.append(f"{fields[5]}={fields[6]}")
    return "; ".join(pairs)


def get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"Cookie": cookie_header()})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def wait(what: str, check, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = check()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - keep polling, report the last error
            last = exc
        time.sleep(1)
    sys.exit(f"timeout waiting for {what}; last: {last!r}")


# ---------------------------------------------------------------- BER (SNMPv2c trap)
def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def ber_int(v: int, tag: int = 0x02) -> bytes:
    return tlv(tag, v.to_bytes(max(1, (v.bit_length() + 8) // 8), "big", signed=True))


def ber_oid(dotted: str) -> bytes:
    arcs = [int(a) for a in dotted.split(".")]
    out = bytearray([40 * arcs[0] + arcs[1]])
    for arc in arcs[2:]:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        out += bytes(reversed(chunk))
    return tlv(0x06, bytes(out))


def seq(*items: bytes) -> bytes:
    return tlv(0x30, b"".join(items))


def v2c_trap(community: str, marker: str) -> bytes:
    varbinds = seq(
        seq(ber_oid("1.3.6.1.2.1.1.3.0"), ber_int(12345, tag=0x43)),  # sysUpTime.0 = TimeTicks
        seq(ber_oid("1.3.6.1.6.3.1.1.4.1.0"), ber_oid("1.3.6.1.6.3.1.1.5.1")),  # snmpTrapOID.0 = coldStart
        seq(ber_oid("1.3.6.1.2.1.1.6.0"), tlv(0x04, marker.encode())),  # sysLocation.0
    )
    pdu = tlv(0xA7, ber_int(4242) + ber_int(0) + ber_int(0) + varbinds)  # SNMPv2-Trap-PDU
    return seq(ber_int(1), tlv(0x04, community.encode()), pdu)  # version 1 = v2c


# ---------------------------------------------------------------- commands
def listening() -> None:
    def all_listening():
        st = get("/api/ingest/status")
        states = {r["kind"]: (r["state"], r["port"], r.get("error")) for r in st["receivers"]}
        if any(s[0] == "error" for s in states.values()):
            sys.exit(f"receiver error: {states}")
        return states if all(s[0] == "listening" for s in states.values()) and len(states) == 3 else None

    states = wait("trap + syslog receivers listening", all_listening)
    expected = {"trap": 1162, "syslog_udp": 1514, "syslog_tcp": 1514}
    ports = {k: v[1] for k, v in states.items()}
    assert ports == expected, ports
    print("receivers listening:", states)


def send_and_find() -> None:
    marker = "ci-" + uuid.uuid4().hex[:12]
    udp_msg = f"<190>Jan  1 00:00:00 ci-switch netmon-ci: {marker} udp"
    tcp_msg = f"<190>Jan  1 00:00:00 ci-switch netmon-ci: {marker} tcp"

    def send():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(udp_msg.encode(), (HOST, SYSLOG_PORT))
            s.sendto(v2c_trap("public", marker), (HOST, TRAP_PORT))
        with socket.create_connection((HOST, SYSLOG_PORT), timeout=5) as s:
            s.sendall(tcp_msg.encode() + b"\n")

    def found():
        # UDP may be lost before docker-proxy / the receiver are fully ready: resend.
        send()
        q = urllib.parse.quote(marker)
        logs = get(f"/api/syslog?q={q}&limit=10")["items"]
        traps = get(f"/api/traps?q={q}&limit=10")["items"]
        transports = {m["transport"] for m in logs}
        if {"udp", "tcp"} <= transports and traps:
            return logs, traps
        return None

    logs, traps = wait("syslog (udp+tcp) and trap to be stored", found, timeout=90)
    trap = traps[0]
    assert trap["trap_oid"] == "1.3.6.1.6.3.1.1.5.1", trap
    print("syslog stored:", [(m["transport"], m["program"], m["source_ip"]) for m in logs])
    print("trap stored:", trap["trap_oid"], trap.get("summary"), trap.get("source_ip"))


if __name__ == "__main__":
    cmds = {"listening": listening, "send-and-find": send_and_find}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(f"usage: {sys.argv[0]} {'|'.join(cmds)}")
    cmds[sys.argv[1]]()
