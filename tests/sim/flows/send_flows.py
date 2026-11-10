#!/usr/bin/env python3
"""NetFlow v5 / v9 / IPFIX / sFlow v5 datagram generator for the phase-3 flow tests (architecture.md §13.11,
data-model.md § [3] Flow collector). Stdlib + PyYAML only. No root: the socket binds to a loopback source address
(127.0.0.x simulates an exporter).

    python tests/sim/flows/send_flows.py --to 127.0.0.1:2055 --from 127.0.0.91 --proto v9 --scenario s1.yaml
    python tests/sim/flows/send_flows.py --proto sflow --scenario s2.yaml --expect      # print expected only
    python tests/sim/flows/send_flows.py --to 127.0.0.1:2055 --proto v5 --flood 50000 --seconds 1

Scenario YAML (paths are relative to tests/sim/flows/scenarios/ unless absolute):
    name: s1
    sampling: 1            # v5: header interval; v9: options template (34); ipfix: 305/306; sflow: sample rate
    sampling_in: options   # v9/ipfix: options | none (rate not exported)
    template_first: true   # false: data before template (-> no_template), then template + data again
    bidirectional: false   # v9: also export OUT_BYTES/OUT_PKTS (23/24) = reverse records
    agent: 10.0.0.12       # sFlow agent address (exporter key); default = --from
    vlan: 20               # sFlow: 802.1Q tag in the raw header
    ipfix_extras: false    # IPFIX: add a variable-length field and an enterprise-specific field to the template
    flows:                 # one record each (sFlow: `samples` packet samples of `frame_length` bytes)
      - {src: 10.0.0.1, dst: 10.0.0.9, sport: 51000, dport: 443, proto: 6, bytes: 600, packets: 4,
         in_if: 1, out_if: 2, direction: 0, out_bytes: 0, out_packets: 0, frame_length: 1514, samples: 1}

--expect prints the expected 1-minute aggregates (the scenario totals after sampling correction), keyed like the
API: {"exporter", "protocol", "rate", "records", "total_bytes", "if": {"<ifIndex>/<in|out>": bytes},
"apps": {"<proto>/<service port>": bytes}, "hosts_src": {ip: bytes}, "hosts_dst": {ip: bytes},
"conversations": [{in_if, out_if, src_ip, dst_ip, protocol, service_port, bytes, packets, flows}], "packets"}.
Without --expect the datagrams are sent and the same JSON is printed with "sent": <datagrams>.
Importable: load_scenario, build(proto, scenario, exporter) -> list[bytes], expected(proto, scenario, exporter),
service_port(proto, sp, dp).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import struct
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SCENARIOS = HERE / "scenarios"

# Small subset of backend/netmon/data/services.tsv (data-model § Services map) used by the oracle. Scenario ports
# are chosen so that the rule's outcome does not depend on the rest of the file.
KNOWN = {(6, 22), (6, 80), (6, 443), (17, 53), (6, 53), (17, 123), (17, 161), (17, 162), (6, 389), (6, 636),
         (6, 445), (17, 514), (6, 1433), (6, 3389), (6, 5985), (6, 5986), (6, 8080), (6, 8443), (17, 2055),
         (17, 4739), (17, 6343)}
NO_PORTS = {1, 58, 47, 50}
CHUNK = 24  # data records per NetFlow v9 / IPFIX datagram (stays below a 1500-byte MTU)


def service_port(proto: int, sp: int, dp: int) -> int:
    """data-model § [3] Services map rule."""
    if proto in NO_PORTS or proto not in (6, 17, 132):
        return 0
    if (proto, dp) in KNOWN:
        return dp
    if (proto, sp) in KNOWN:
        return sp
    lo = min(sp, dp)
    if lo < 1024:
        return lo
    if sp < 49152 or dp < 49152:
        return lo
    return 0


def load_scenario(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_absolute() and not p.exists():
        p = SCENARIOS / p
    sc = yaml.safe_load(p.read_text())
    sc.setdefault("sampling", 1)
    sc.setdefault("sampling_in", "options")
    sc.setdefault("template_first", True)
    sc.setdefault("bidirectional", False)
    sc.setdefault("ipfix_extras", False)
    for f in sc["flows"]:
        f.setdefault("sport", 0)
        f.setdefault("dport", 0)
        f.setdefault("proto", 6)
        f.setdefault("packets", 1)
        f.setdefault("in_if", 0)
        f.setdefault("out_if", 0)
        f.setdefault("direction", 0)
        f.setdefault("tcp_flags", 0x18 if f["proto"] == 6 else 0)
        f.setdefault("out_bytes", 0)
        f.setdefault("out_packets", 0)
        f.setdefault("frame_length", f.get("bytes", 64))
        f.setdefault("samples", 1)
        f.setdefault("bytes", f["frame_length"])
    return sc


def _ip(a: str) -> bytes:
    return ipaddress.ip_address(a).packed


def _is6(f: dict) -> bool:
    return ipaddress.ip_address(f["src"]).version == 6


# ---------------------------------------------------------------- NetFlow v5
def build_v5(sc: dict) -> list[bytes]:
    flows = [f for f in sc["flows"] if not _is6(f)]
    rate = int(sc["sampling"])
    samp = (0x4000 | (rate & 0x3FFF)) if rate > 1 else 0
    out = []
    now = int(time.time())
    seq = 0
    for i in range(0, len(flows), 30):
        chunk = flows[i : i + 30]
        hdr = struct.pack("!HHIIIIBBH", 5, len(chunk), 100000, now, 0, seq, 0, 0, samp)
        body = b""
        for f in chunk:
            body += struct.pack("!4s4s4sHHIIIIHHBBBBHHBBH", _ip(f["src"]), _ip(f["dst"]), b"\0\0\0\0",
                                f["in_if"], f["out_if"], f["packets"], f["bytes"], 99000, 99900,
                                f["sport"], f["dport"], 0, f["tcp_flags"], f["proto"], 0, 0, 0, 24, 24, 0)
        seq += len(chunk)
        out.append(hdr + body)
    return out


# ---------------------------------------------------------------- NetFlow v9
def _pad4(b: bytes) -> bytes:
    return b + b"\0" * ((4 - len(b) % 4) % 4)


def _v9_fields(v6: bool, bidir: bool) -> list[tuple[int, int]]:
    f = [(27, 16), (28, 16)] if v6 else [(8, 4), (12, 4)]
    f += [(7, 2), (11, 2), (4, 1), (1, 4), (2, 4), (10, 2), (14, 2), (6, 1), (61, 1)]
    if bidir:
        f += [(23, 4), (24, 4)]
    return f


def _v9_record(f: dict, v6: bool, bidir: bool) -> bytes:
    b = _ip(f["src"]) + _ip(f["dst"])
    b += struct.pack("!HHBIIHHBB", f["sport"], f["dport"], f["proto"], f["bytes"], f["packets"], f["in_if"],
                     f["out_if"], f["tcp_flags"], f["direction"])
    if bidir:
        b += struct.pack("!II", f["out_bytes"], f["out_packets"])
    return b


def build_v9(sc: dict, source_id: int = 1) -> list[bytes]:
    now = int(time.time())
    seq = [0]

    def packet(flowsets: list[bytes], count: int) -> bytes:
        seq[0] += 1
        return struct.pack("!HHIIII", 9, count, 100000, now, seq[0], source_id) + b"".join(flowsets)

    bidir = bool(sc["bidirectional"])
    groups = {256: [f for f in sc["flows"] if not _is6(f)], 258: [f for f in sc["flows"] if _is6(f)]}
    tmpl_sets: list[bytes] = []
    data_pkts: list[tuple[list[bytes], int]] = []  # one datagram per chunk of <= CHUNK records
    for tid, flows in groups.items():
        if not flows:
            continue
        fields = _v9_fields(tid == 258, bidir)
        t = struct.pack("!HH", tid, len(fields)) + b"".join(struct.pack("!HH", a, b) for a, b in fields)
        tmpl_sets.append(struct.pack("!HH", 0, 4 + len(t)) + t)
        for i in range(0, len(flows), CHUNK):
            chunk = flows[i : i + CHUNK]
            recs = b"".join(_v9_record(f, tid == 258, bidir) for f in chunk)
            body = _pad4(struct.pack("!HH", tid, 0) + recs)
            data_pkts.append(([struct.pack("!HH", tid, len(body)) + body[4:]], len(chunk)))
    opt_sets = []
    if int(sc["sampling"]) > 1 and sc["sampling_in"] == "options":
        # options template 257: scope System (1, 4 bytes); options 34 SAMPLING_INTERVAL (4), 35 ALGORITHM (1)
        ot = struct.pack("!HHHHHHHH", 257, 4, 8, 1, 4, 34, 4, 35) + struct.pack("!H", 1)
        ot = _pad4(ot)
        opt_sets.append(struct.pack("!HH", 1, 4 + len(ot)) + ot)
        od = _pad4(struct.pack("!IIB", 0, int(sc["sampling"]), 2))
        opt_sets.append(struct.pack("!HH", 257, 4 + len(od)) + od)
    out = []
    data = [packet(sets, n) for sets, n in data_pkts]
    if sc["template_first"]:
        out.append(packet(tmpl_sets + opt_sets, len(tmpl_sets) + len(opt_sets)))
        out += data
    else:
        out += data                                     # -> no_template
        out.append(packet(tmpl_sets + opt_sets, len(tmpl_sets) + len(opt_sets)))
        out += [packet(sets, n) for sets, n in data_pkts]
    return out


# ---------------------------------------------------------------- IPFIX
def _ipfix_fields(v6: bool, extras: bool) -> list[tuple[int, int, int | None]]:
    f: list[tuple[int, int, int | None]] = [(27, 16, None), (28, 16, None)] if v6 else [(8, 4, None), (12, 4, None)]
    f += [(7, 2, None), (11, 2, None), (4, 1, None), (1, 8, None), (2, 8, None), (10, 4, None), (14, 4, None),
          (6, 1, None), (61, 1, None)]
    if extras:
        f += [(94, 65535, None), (100 | 0x8000, 4, 9)]  # variable-length applicationDescription; PEN 9 field
    return f


def _ipfix_record(f: dict, v6: bool, extras: bool) -> bytes:
    b = _ip(f["src"]) + _ip(f["dst"])
    b += struct.pack("!HHBQQIIBB", f["sport"], f["dport"], f["proto"], f["bytes"], f["packets"], f["in_if"],
                     f["out_if"], f["tcp_flags"], f["direction"])
    if extras:
        desc = f.get("app_desc", "qa-application").encode()
        b += (struct.pack("!B", len(desc)) if len(desc) < 255 else struct.pack("!BH", 255, len(desc))) + desc
        b += struct.pack("!I", 0xDEADBEEF)
    return b


def build_ipfix(sc: dict, domain: int = 1) -> list[bytes]:
    now = int(time.time())
    seq = [0]

    def message(sets: list[bytes], nrec: int) -> bytes:
        body = b"".join(sets)
        m = struct.pack("!HHIII", 10, 16 + len(body), now, seq[0], domain) + body
        seq[0] += nrec
        return m

    extras = bool(sc["ipfix_extras"])
    groups = {300: [f for f in sc["flows"] if not _is6(f)], 302: [f for f in sc["flows"] if _is6(f)]}
    tsets: list[bytes] = []
    dpkts: list[tuple[bytes, int]] = []
    for tid, flows in groups.items():
        if not flows:
            continue
        fields = _ipfix_fields(tid == 302, extras)
        t = struct.pack("!HH", tid, len(fields))
        for ie, ln, pen in fields:
            t += struct.pack("!HH", ie, ln) + (struct.pack("!I", pen) if pen is not None else b"")
        tsets.append(struct.pack("!HH", 2, 4 + len(t)) + t)
        for i in range(0, len(flows), CHUNK):
            chunk = flows[i : i + CHUNK]
            recs = b"".join(_ipfix_record(f, tid == 302, extras) for f in chunk)
            dpkts.append((struct.pack("!HH", tid, 4 + len(recs)) + recs, len(chunk)))
    osets = []
    rate = int(sc["sampling"])
    if rate > 1 and sc["sampling_in"] == "options":
        # options template 301: scope observationDomainId (149, 4); options 305 samplingPacketInterval (4),
        # 306 samplingPacketSpace (4)  -> rate = (interval + space) / interval = (1 + rate-1) / 1
        ot = struct.pack("!HHH", 301, 3, 1) + struct.pack("!HHHHHH", 149, 4, 305, 4, 306, 4)
        osets.append(struct.pack("!HH", 3, 4 + len(ot)) + ot)
        od = struct.pack("!III", domain, 1, rate - 1)
        osets.append(struct.pack("!HH", 301, 4 + len(od)) + od)
    out = []
    if sc["template_first"]:
        out.append(message(tsets + osets, 0))
        out += [message([d], k) for d, k in dpkts]
    else:
        out += [message([d], k) for d, k in dpkts]
        out.append(message(tsets + osets, 0))
        out += [message([d], k) for d, k in dpkts]
    return out


def ipfix_withdrawal(domain: int = 1, tid: int = 300) -> bytes:
    t = struct.pack("!HH", tid, 0)
    s = struct.pack("!HH", 2, 4 + len(t)) + t
    return struct.pack("!HHIII", 10, 16 + len(s), int(time.time()), 0, domain) + s


# ---------------------------------------------------------------- sFlow v5
def _raw_header(f: dict, vlan: int | None) -> bytes:
    eth = bytes.fromhex("001122334455") + bytes.fromhex("00aabbccdd01")
    v6 = _is6(f)
    if vlan:
        eth += struct.pack("!HH", 0x8100, vlan & 0x0FFF)
    eth += struct.pack("!H", 0x86DD if v6 else 0x0800)
    l4 = struct.pack("!HH", f["sport"], f["dport"])
    if f["proto"] == 6:
        l4 += struct.pack("!IIBBHHH", 1, 0, 0x50, f["tcp_flags"], 65535, 0, 0)
    elif f["proto"] == 17:
        l4 += struct.pack("!HH", f["frame_length"] - 14 - 20, 0)
    if v6:
        ip = struct.pack("!IHBB", 0x60000000, f["frame_length"] - 54, f["proto"], 64) + _ip(f["src"]) + _ip(f["dst"])
    else:
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, f["frame_length"] - 14, 1, 0, 64, f["proto"], 0,
                         _ip(f["src"]), _ip(f["dst"]))
    return (eth + ip + l4)[:128]


def build_sflow(sc: dict, agent: str) -> list[bytes]:
    rate = int(sc["sampling"])
    vlan = sc.get("vlan")
    samples = []
    seq = 0
    for f in sc["flows"]:
        for _ in range(int(f["samples"])):
            seq += 1
            hdr = _raw_header(f, vlan)
            rec = struct.pack("!IIII", 1, f["frame_length"], 4, len(hdr)) + _pad4(hdr)
            record = struct.pack("!II", 1, len(rec)) + rec
            # expanded flow sample (format 3)
            body = struct.pack("!IIIIIIIIIII", seq, 0, f["in_if"], rate, rate * seq, 0, 0, f["in_if"], 0,
                               f["out_if"], 1) + record
            samples.append(struct.pack("!II", 3, len(body)) + body)
    out = []
    a = ipaddress.ip_address(agent)
    for i in range(0, len(samples), 8):
        chunk = samples[i : i + 8]
        hdr = struct.pack("!II", 5, 1 if a.version == 4 else 2) + a.packed
        hdr += struct.pack("!IIII", 0, i // 8 + 1, 100000, len(chunk))
        out.append(hdr + b"".join(chunk))
    return out


def build(proto: str, sc: dict, exporter: str) -> list[bytes]:
    if proto == "v5":
        return build_v5(sc)
    if proto == "v9":
        return build_v9(sc)
    if proto == "ipfix":
        return build_ipfix(sc)
    if proto == "sflow":
        return build_sflow(sc, sc.get("agent") or exporter)
    raise SystemExit(f"unknown --proto {proto}")


# ---------------------------------------------------------------- oracle
def expected(proto: str, sc: dict, exporter: str) -> dict:
    rate = int(sc["sampling"])
    if proto in ("v9", "ipfix") and sc["sampling_in"] != "options":
        rate = 1
    if proto == "v5" and rate > 0x3FFF:
        rate = 1
    recs = []
    for f in sc["flows"]:
        if proto == "v5" and _is6(f):
            continue
        if proto == "sflow":
            n = int(f["samples"])
            recs += [dict(f, bytes=f["frame_length"], packets=1) for _ in range(n)]
            continue
        recs.append(f)
        if proto == "v9" and sc["bidirectional"] and f["out_bytes"]:
            recs.append(dict(f, src=f["dst"], dst=f["src"], sport=f["dport"], dport=f["sport"], in_if=f["out_if"],
                             out_if=f["in_if"], bytes=f["out_bytes"], packets=f["out_packets"]))
    ifs: dict[str, int] = defaultdict(int)
    apps: dict[str, int] = defaultdict(int)
    hs: dict[str, int] = defaultdict(int)
    hd: dict[str, int] = defaultdict(int)
    conv: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
    total = packets = 0
    for r in recs:
        b, p = r["bytes"] * rate, r["packets"] * rate
        total += b
        packets += p
        in_if, out_if = r["in_if"], r["out_if"]
        if r.get("direction") == 1:  # egress record: swap for interface accounting (§13.3.3)
            in_if, out_if = out_if, in_if
        if in_if:
            ifs[f"{in_if}/in"] += b
        if out_if:
            ifs[f"{out_if}/out"] += b
        sp = service_port(r["proto"], r["sport"], r["dport"])
        apps[f"{r['proto']}/{sp}"] += b
        hs[r["src"]] += b
        hd[r["dst"]] += b
        c = conv[(in_if, out_if, r["src"], r["dst"], r["proto"], sp)]
        c[0] += b
        c[1] += p
        c[2] += 1
    return {
        "exporter": sc.get("agent") or exporter if proto == "sflow" else exporter,
        "protocol": {"v5": "netflow5", "v9": "netflow9", "ipfix": "ipfix", "sflow": "sflow5"}[proto],
        "rate": rate, "records": len(recs), "total_bytes": total, "packets": packets,
        "if": dict(ifs), "apps": dict(apps), "hosts_src": dict(hs), "hosts_dst": dict(hd),
        "conversations": sorted(
            [{"in_if": k[0], "out_if": k[1], "src_ip": k[2], "dst_ip": k[3], "protocol": k[4], "service_port": k[5],
              "bytes": v[0], "packets": v[1], "flows": v[2]} for k, v in conv.items()],
            key=lambda x: -x["bytes"]),
    }


def flood_scenario(n: int) -> dict:
    flows = [{"src": f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}", "dst": "10.0.0.9", "sport": 40000 + i % 20000,
              "dport": 443, "proto": 6, "bytes": 1000, "packets": 1, "in_if": 1, "out_if": 2} for i in range(n)]
    return load_dict({"name": "flood", "flows": flows})


def load_dict(sc: dict) -> dict:
    import tempfile  # noqa: PLC0415

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as t:
        yaml.safe_dump(sc, t)
    return load_scenario(t.name)


def send(datagrams: list[bytes], to: str, src: str, rate: float = 0, seconds: float = 0) -> int:
    host, port = to.rsplit(":", 1)
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_DGRAM)
    if src:
        s.bind((src, 0))
    sent = 0
    t0 = time.monotonic()
    gap = (seconds / len(datagrams)) if seconds and datagrams else (1.0 / rate if rate else 0)
    for i, d in enumerate(datagrams):
        s.sendto(d, (host, int(port)))
        sent += 1
        if gap:
            delay = t0 + (i + 1) * gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    s.close()
    return sent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", default="127.0.0.1:2055")
    ap.add_argument("--from", dest="src", default="127.0.0.91")
    ap.add_argument("--proto", choices=["v5", "v9", "ipfix", "sflow"], required=True)
    ap.add_argument("--scenario")
    ap.add_argument("--expect", action="store_true", help="print the expected aggregates only")
    ap.add_argument("--flood", type=int, default=0, help="N synthetic records instead of a scenario")
    ap.add_argument("--seconds", type=float, default=0, help="spread the datagrams over this many seconds")
    ap.add_argument("--rate", type=float, default=0, help="datagrams per second (0 = as fast as possible)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--raw-hex", help="send these exact bytes (hostile datagram tests)")
    ap.add_argument("--withdraw", action="store_true", help="IPFIX: send a template withdrawal for 300")
    a = ap.parse_args()
    if a.raw_hex:
        n = send([bytes.fromhex(a.raw_hex)], a.to, a.src)
        print(json.dumps({"sent": n}))
        return 0
    if a.withdraw:
        print(json.dumps({"sent": send([ipfix_withdrawal()], a.to, a.src)}))
        return 0
    if a.flood:
        sc = flood_scenario(a.flood)
    elif a.scenario:
        sc = load_scenario(a.scenario)
    else:
        ap.error("--scenario or --flood")
    exp = expected(a.proto, sc, a.src)
    if a.expect:
        print(json.dumps(exp, indent=1))
        return 0
    dgrams = build(a.proto, sc, a.src) * a.repeat
    exp["sent"] = send(dgrams, a.to, a.src, a.rate, a.seconds)
    if a.repeat > 1:
        for k in ("records", "total_bytes", "packets"):
            exp[k] *= a.repeat
    print(json.dumps(exp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
