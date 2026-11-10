#!/usr/bin/env python3
"""CI smoke test of the ingest receivers through the published ports.

Used by .github/workflows/ci.yml (docker job) against the real compose stack:

  ingest-check.py listening       [2b-3] wait until the three trap/syslog receivers report
                                  "listening" (the features were turned on through
                                  PUT /api/settings)
  ingest-check.py send-and-find   [2b-3] send one syslog message over UDP 514, one over TCP
                                  514 and one SNMPv2c trap to UDP 162 on the host, then find
                                  them through GET /api/syslog and GET /api/traps
  ingest-check.py flows-listening [3] wait until the NetFlow (2055), IPFIX (4739) and sFlow
                                  (6343) receivers report "listening" (GET /api/flows/status)
  ingest-check.py flows-pipeline  [3] send a NetFlow v5, an IPFIX (template + data) and an
                                  sFlow v5 datagram to the published ports; the unknown
                                  exporters appear as "pending"; accept the sFlow agent and
                                  wait until its flows are written to the 1-minute tables

Authentication: the session cookie that curl stored in cookies.txt (the setup step).
Standard library only. Traps and flow datagrams are encoded by hand (no pysnmp on the runner).
"""

from __future__ import annotations

import json
import socket
import struct
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
NETFLOW_PORT = 2055
IPFIX_PORT = 4739
SFLOW_PORT = 6343
# sFlow exporters are keyed by the agent address inside the datagram (TEST-NET-1), NetFlow /
# IPFIX ones by the UDP source (the Docker bridge gateway when sent from the runner).
SFLOW_AGENT = "192.0.2.10"


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


def send_json(method: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        method=method,
        headers={"Cookie": cookie_header(), "Content-Type": "application/json"},
    )
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


# ---------------------------------------------------------------- flow datagrams
def ip4(dotted: str) -> bytes:
    return socket.inet_aton(dotted)


def netflow_v5() -> bytes:
    now = int(time.time())
    header = struct.pack("!HHIIIIBBH", 5, 1, 360000, now, 0, 1, 0, 0, 0)
    record = struct.pack(
        "!4s4s4sHHIIIIHHBBBBHHBBH",
        ip4("10.1.0.5"), ip4("10.2.0.9"), ip4("0.0.0.0"),  # src, dst, next hop
        3, 7,  # input / output ifIndex
        10, 15000,  # packets, octets
        359000, 360000,  # first / last (uptime ms)
        51000, 443,  # ports
        0, 0x18, 6, 0,  # pad, tcp flags, protocol TCP, tos
        0, 0, 24, 24, 0,  # AS, masks, pad
    )
    return header + record


def ipfix() -> bytes:
    fields = [(8, 4), (12, 4), (4, 1), (7, 2), (11, 2), (1, 8), (2, 8), (10, 4)]
    tmpl = struct.pack("!HH", 256, len(fields)) + b"".join(struct.pack("!HH", i, n) for i, n in fields)
    tmpl_set = struct.pack("!HH", 2, 4 + len(tmpl)) + tmpl
    rec = ip4("10.1.0.6") + ip4("10.2.0.10") + struct.pack("!BHHQQI", 17, 53000, 53, 400, 4, 5)
    data_set = struct.pack("!HH", 256, 4 + len(rec)) + rec
    body = tmpl_set + data_set
    return struct.pack("!HHIII", 10, 16 + len(body), int(time.time()), 1, 7) + body


def sflow_v5(seq_no: int) -> bytes:
    eth = bytes.fromhex("001122334455" "66778899aabb" "0800")
    payload_len = 8 + 100
    iph = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + payload_len, 1, 0, 64, 17, 0,
                      ip4("10.1.0.7"), ip4("10.2.0.11"))
    udph = struct.pack("!HHHH", 40000, 514, payload_len, 0)
    hdr = eth + iph + udph
    hdr_padded = hdr + b"\0" * (-len(hdr) % 4)
    raw = struct.pack("!IIII", 1, 14 + 20 + payload_len, 0, len(hdr)) + hdr_padded
    record = struct.pack("!II", 1, len(raw)) + raw  # flow record: raw packet header
    sample_body = struct.pack("!IIIIIIII", seq_no, 5, 4096, 4096 * seq_no, 0, 5, 9, 1) + record
    sample = struct.pack("!II", 1, len(sample_body)) + sample_body  # flow sample
    return struct.pack("!II4sIIII", 5, 1, ip4(SFLOW_AGENT), 0, seq_no, 360000, 1) + sample


# ---------------------------------------------------------------- commands
def listening() -> None:
    expected = {"trap": 1162, "syslog_udp": 1514, "syslog_tcp": 1514}

    def all_listening():
        st = get("/api/ingest/status")
        # Only the 2b-3 receivers: later phases may list more kinds here (they stay off).
        states = {
            r["kind"]: (r["state"], r["port"], r.get("error"))
            for r in st["receivers"]
            if r["kind"] in expected
        }
        if any(s[0] == "error" for s in states.values()):
            sys.exit(f"receiver error: {states}")
        return states if all(s[0] == "listening" for s in states.values()) and len(states) == 3 else None

    states = wait("trap + syslog receivers listening", all_listening)
    ports = {k: v[1] for k, v in states.items()}
    assert ports == expected, ports
    print("receivers listening:", states)


def flows_listening() -> None:
    expected = {"netflow": NETFLOW_PORT, "ipfix": IPFIX_PORT, "sflow": SFLOW_PORT}

    def all_listening():
        st = get("/api/flows/status")
        states = {r["kind"]: (r["state"], r["port"], r.get("error")) for r in st["receivers"]}
        if any(s[0] == "error" for s in states.values()):
            sys.exit(f"flow receiver error: {states}")
        ok = set(states) == set(expected) and all(s[0] == "listening" for s in states.values())
        return states if ok else None

    states = wait("netflow + ipfix + sflow receivers listening", all_listening)
    ports = {k: v[1] for k, v in states.items()}
    assert ports == expected, ports
    print("flow receivers listening:", states)


def flows_pipeline() -> None:
    seq_no = [0]

    def send():
        seq_no[0] += 1
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(netflow_v5(), (HOST, NETFLOW_PORT))
            s.sendto(ipfix(), (HOST, IPFIX_PORT))
            s.sendto(sflow_v5(seq_no[0]), (HOST, SFLOW_PORT))

    def exporters_seen():
        # UDP may be lost before docker-proxy / the receivers are fully ready: resend.
        send()
        items = get("/api/flows/exporters")["items"]
        by_addr = {e["address"]: e for e in items}
        if SFLOW_AGENT in by_addr and len(by_addr) >= 2:
            return by_addr
        return None

    exporters = wait("flow exporters to appear", exporters_seen, timeout=90)
    print("exporters:", {a: (e["state"], e["protocols"]) for a, e in exporters.items()})
    agent = exporters[SFLOW_AGENT]
    # Unknown (no device with that address): listed, its data dropped until accepted.
    assert agent["state"] == "pending", agent
    st = get("/api/flows/status")
    assert st["counters"]["received_datagrams"] >= 3, st["counters"]
    assert st["counters"]["dropped"]["parse_error"] == 0, st["counters"]
    assert st["rollup"] in ("cagg", "table"), st["rollup"]

    send_json("PATCH", f"/api/flows/exporters/{agent['id']}", {"state": "accepted"})

    # Accepted: records are aggregated per minute and written at the minute close (+5 s).
    def stored():
        send()
        c = get("/api/flows/status")["counters"]
        return c if c["stored_rows"] > 0 else None

    counters = wait("flow rows to be stored", stored, timeout=150)
    print("flow counters:", json.dumps(counters))


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
    cmds = {
        "listening": listening,
        "send-and-find": send_and_find,
        "flows-listening": flows_listening,
        "flows-pipeline": flows_pipeline,
    }
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(f"usage: {sys.argv[0]} {'|'.join(cmds)}")
    cmds[sys.argv[1]]()
