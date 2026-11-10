#!/usr/bin/env python3
"""Self-test of send_flows.py: an independent minimal decoder (QA oracle, not the product decoder) parses every
generated datagram of every scenario and the decoded totals must equal expected().

    python tests/sim/flows/selftest.py          # exit 0 when all scenarios round-trip

Also checks the data-model decoder vectors that the generator must reproduce (v5 sampling field 0x4064,
sFlow 1514 x 4096 = 6 201 344 bytes, IPFIX 305/306 = 1/99 -> rate 100).
"""

from __future__ import annotations

import ipaddress
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from send_flows import build, expected, load_scenario  # noqa: E402

FAIL: list[str] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f" - {detail}"))
    if not cond:
        FAIL.append(name)


def dec_v5(d: bytes) -> tuple[int, list[dict]]:
    ver, cnt = struct.unpack_from("!HH", d)
    samp = struct.unpack_from("!H", d, 22)[0]
    rate = samp & 0x3FFF if samp >> 14 else 1
    recs = []
    for i in range(cnt):
        o = 24 + 48 * i
        r = struct.unpack_from("!4s4s4sHHIIIIHHBBBB", d, o)
        recs.append({"bytes": r[6], "packets": r[5], "sport": r[9], "dport": r[10], "proto": r[13]})
    return rate or 1, recs


class V9:
    def __init__(self) -> None:
        self.t: dict[int, list[tuple[int, int, int | None]]] = {}
        self.rate = 1
        self.no_template = 0

    def dec(self, d: bytes, ipfix: bool) -> list[dict]:
        o = 16 if ipfix else 20
        recs: list[dict] = []
        end = struct.unpack_from("!H", d, 2)[0] if ipfix else len(d)
        while o + 4 <= end:
            sid, ln = struct.unpack_from("!HH", d, o)
            body = d[o + 4 : o + ln]
            if sid in ((2,) if ipfix else (0,)):
                p = 0
                while p + 4 <= len(body):
                    tid, fc = struct.unpack_from("!HH", body, p)
                    p += 4
                    fields = []
                    for _ in range(fc):
                        ie, fl = struct.unpack_from("!HH", body, p)
                        p += 4
                        pen = None
                        if ipfix and ie & 0x8000:
                            pen = struct.unpack_from("!I", body, p)[0]
                            p += 4
                        fields.append((ie & 0x7FFF, fl, pen))
                    if fc == 0:
                        self.t.pop(tid, None)
                    else:
                        self.t[tid] = fields
            elif sid in ((3,) if ipfix else (1,)):
                if ipfix:
                    tid, fc, _sc = struct.unpack_from("!HHH", body, 0)
                    p, fields = 6, []
                    for _ in range(fc):
                        ie, fl = struct.unpack_from("!HH", body, p)
                        p += 4
                        fields.append((ie, fl, None))
                else:
                    tid, sl, ol = struct.unpack_from("!HHH", body, 0)
                    p, fields = 6, []
                    for _ in range((sl + ol) // 4):
                        ie, fl = struct.unpack_from("!HH", body, p)
                        p += 4
                        fields.append((1000 + ie if len(fields) < sl // 4 else ie, fl, None))
                self.t[tid] = fields
                self.t[tid].append((-1, 0, None))  # marker: options
            elif sid >= 256:
                if sid not in self.t:
                    self.no_template += 1
                else:
                    recs += self._data(sid, body)
            o += ln
        return recs

    def _data(self, tid: int, body: bytes) -> list[dict]:
        fields = self.t[tid]
        is_opt = fields and fields[-1][0] == -1
        fields = [f for f in fields if f[0] != -1]
        out = []
        p = 0
        while p < len(body):
            r: dict = {}
            start = p
            for ie, fl, _pen in fields:
                if fl == 65535:
                    n = body[p]
                    p += 1
                    if n == 255:
                        n = struct.unpack_from("!H", body, p)[0]
                        p += 2
                    p += n
                    continue
                if p + fl > len(body):
                    return out
                r[ie] = int.from_bytes(body[p : p + fl], "big") if fl <= 8 else body[p : p + fl]
                p += fl
            if p == start:
                break
            if is_opt:
                if 34 in r:
                    self.rate = r[34]
                if 305 in r:
                    self.rate = (r[305] + r.get(306, 0)) // r[305]
                continue
            out.append({"bytes": r.get(1, 0), "packets": r.get(2, 0), "out_bytes": r.get(23, 0),
                        "sport": r.get(7), "dport": r.get(11), "proto": r.get(4),
                        "src": str(ipaddress.ip_address(r[8] if 8 in r else r[27]))})
            if len(body) - p < 4:
                break
        return out


def dec_sflow(d: bytes) -> tuple[str, list[dict]]:
    ver, at = struct.unpack_from("!II", d)
    alen = 4 if at == 1 else 16
    agent = str(ipaddress.ip_address(d[8 : 8 + alen]))
    o = 8 + alen + 12
    n = struct.unpack_from("!I", d, o)[0]
    o += 4
    recs = []
    for _ in range(n):
        fmt, ln = struct.unpack_from("!II", d, o)
        body = d[o + 8 : o + 8 + ln]
        o += 8 + ln
        if fmt != 3:
            continue
        rate = struct.unpack_from("!I", body, 12)[0]
        nrec = struct.unpack_from("!I", body, 40)[0]
        p = 44
        for _ in range(nrec):
            rf, rl = struct.unpack_from("!II", body, p)
            if rf == 1:
                proto, frame = struct.unpack_from("!II", body, p + 8)
                recs.append({"bytes": frame * rate, "packets": rate})
            p += 8 + rl
    return agent, recs


def main() -> int:
    for proto, scen in [("v9", "s1"), ("sflow", "s2"), ("v9", "s3_data_before_template"), ("ipfix",
                        "s3_data_before_template"), ("v9", "s4_bidir_ipv6"), ("ipfix", "s5_ipfix_sampled"),
                        ("v5", "s6_v5_sampled"), ("v5", "s1"), ("ipfix", "s1"), ("ipfix", "s4_bidir_ipv6")]:
        sc = load_scenario(f"{scen}.yaml")
        exp = expected(proto, sc, "127.0.0.91")
        dgrams = build(proto, sc, "127.0.0.91")
        total = 0
        recs = 0
        if proto == "v5":
            for d in dgrams:
                rate, rs = dec_v5(d)
                total += sum(r["bytes"] for r in rs) * rate
                recs += len(rs)
        elif proto == "sflow":
            for d in dgrams:
                agent, rs = dec_sflow(d)
                total += sum(r["bytes"] for r in rs)
                recs += len(rs)
            ok(f"{proto}/{scen} agent", agent == exp["exporter"], agent)
        else:
            v = V9()
            rs_all = []
            for d in dgrams:
                rs_all += v.dec(d, proto == "ipfix")
            for r in rs_all:
                total += (r["bytes"] + r["out_bytes"]) * v.rate
                recs += 1 + (1 if r["out_bytes"] else 0)
            if not sc["template_first"]:
                ok(f"{proto}/{scen} data before template counted once as no_template", v.no_template == 1,
                   str(v.no_template))
        ok(f"{proto}/{scen} decoded total == expected ({exp['total_bytes']})", total == exp["total_bytes"],
           f"{total}")
        ok(f"{proto}/{scen} records == expected ({exp['records']})", recs == exp["records"], f"{recs}")
    # vectors
    d = build("v5", load_scenario("s6_v5_sampled.yaml"), "x")[0]
    ok("v5 header sampling field 0x4064", struct.unpack_from("!H", d, 22)[0] == 0x4064)
    e = expected("sflow", load_scenario("s2.yaml"), "127.0.0.91")
    ok("sflow vector 1514 x 4096 per sample", e["conversations"][0]["bytes"] in (6201344 * 3, 6201344 * 5))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
