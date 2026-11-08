#!/usr/bin/env python3
"""Flow decode + aggregate benchmark (architecture.md §13.3.2: "decode + aggregate ≈ 10–20 µs per record").

    backend/.venv/bin/python tests/sim/flows/bench.py [--records 100000] [--proto v9|ipfix|v5|sflow|all]

Builds datagrams with send_flows.py and runs them through the product's pure decoders and MinuteAggregator
(netmon.flows.*) in this process, single thread. Prints a Markdown table for the PR: records/s, µs per record,
CPU seconds per 10 000 records, peak RSS. The product entry points are looked up by the names the contract
gives (modules decode_netflow / decode_ipfix / decode_sflow / templates / aggregate); when the backend is not
there yet the script says which import failed and exits 2.
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "backend"))
from send_flows import build, flood_scenario  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, default=100_000)
    ap.add_argument("--proto", default="all")
    a = ap.parse_args()
    try:
        from netmon.flows.aggregate import MinuteAggregator
        from netmon.flows.decode import decode_datagram
        from netmon.flows.templates import TemplateCache
    except ImportError as e:
        print(f"bench: product flow modules not available: {e}", file=sys.stderr)
        return 2
    sc = flood_scenario(min(a.records, 20_000))
    protos = ["v5", "v9", "ipfix", "sflow"] if a.proto == "all" else [a.proto]
    print("| Protocol | Records | Wall s | CPU s | Records/s | µs/record | CPU s per 10 000 | Peak RSS MB |")
    print("|---|---|---|---|---|---|---|---|")
    for proto in protos:
        dgrams = build(proto, sc, "127.0.0.91")
        reps = max(1, a.records // len(sc["flows"]))
        cache = TemplateCache()
        agg = MinuteAggregator()
        minute = int(time.time() // 60) * 60
        n = 0
        c0, t0 = time.process_time(), time.perf_counter()
        for _ in range(reps):
            for d in dgrams:
                res = decode_datagram(d, "127.0.0.91", proto == "sflow", cache, time.time())
                n += len(res.records)
                agg.add(res.exporter, minute, res.records)
        agg.flush(None)
        wall, cpu = time.perf_counter() - t0, time.process_time() - c0
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        rps = n / wall if wall else 0
        print(f"| {proto} | {n} | {wall:.2f} | {cpu:.2f} | {rps:,.0f} | {1e6 * wall / max(n, 1):.1f} | "
              f"{10_000 * cpu / max(n, 1):.3f} | {rss:.0f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
