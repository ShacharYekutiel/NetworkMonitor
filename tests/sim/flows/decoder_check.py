#!/usr/bin/env python3
"""Feeds send_flows.py datagrams through the PRODUCT decoders + MinuteAggregator (netmon.flows.decode /
aggregate) in-process and compares the 1-minute rows with send_flows.expected() (no DB, no sockets).

    backend/.venv/bin/python tests/sim/flows/decoder_check.py

Exit 1 on any mismatch. The receiver/worker/DB path is covered by E2E spec 54 and the backend integration tests.
"""

from __future__ import annotations

import ipaddress
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "backend"))
from send_flows import build, expected, flood_scenario, load_scenario  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" - {detail}"))
    if not ok:
        FAILS.append(name)


def run(proto: str, scen: str, exporter: str = "127.0.0.91") -> None:
    from netmon.flows.aggregate import MinuteAggregator
    from netmon.flows.decode import decode_datagram, exporter_of
    from netmon.flows.templates import TemplateCache

    sc = load_scenario(f"{scen}.yaml") if isinstance(scen, str) else scen
    exp = expected(proto, sc, exporter)
    cache = TemplateCache()
    agg = MinuteAggregator(top_apps=1000, top_hosts=2000, top_conversations=5000)
    now = time.time()
    minute = int(now // 60) * 60
    errors: dict[str, int] = {}
    rates: list = []
    exp_addr = None
    for d in build(proto, sc, exporter):
        sflow = proto == "sflow"
        exp_addr = exporter_of(d, exporter, sflow)
        res = decode_datagram(d, exporter, sflow, cache, now)
        for k, v in (getattr(res, "errors", None) or {}).items():
            errors[k] = errors.get(k, 0) + v
        recs = list(res.records)
        rates += [r.rate for r in recs]
        rate = next((r.rate for r in recs if r.rate), None)
        agg.add(exp_addr, minute, recs)
        _ = rate
    fl = agg.flush(None)
    tag = f"{proto}/{scen if isinstance(scen, str) else 'flood'}"
    check(f"{tag}: exporter address {exp['exporter']}", exp_addr == exp["exporter"], str(exp_addr))
    if not fl:
        check(f"{tag}: rows flushed", False, f"no flush; errors {errors}")
        return
    f = fl[0]
    check(f"{tag}: total bytes {exp['total_bytes']}", f.bytes == exp["total_bytes"],
          f"got {f.bytes} (rates seen {sorted(set(map(str, rates)))}, errors {errors})")
    check(f"{tag}: records {exp['records']}", f.records == exp["records"], f"got {f.records}")
    ifs = {f"{r[0]}/{r[1]}": r[2] for r in f.if_rows}
    check(f"{tag}: interface rows", ifs == exp["if"], f"got {ifs} want {exp['if']}")
    apps = {f"{r[0]}/{r[1]}": r[2] for r in f.app_rows}
    check(f"{tag}: application rows", apps == exp["apps"], f"got {apps} want {exp['apps']}")
    hs = {str(ipaddress.ip_address(r[0])) if not isinstance(r[0], str) else r[0]: r[3]
          for r in f.host_rows if r[1] == "src" and not r[2]}
    check(f"{tag}: source host rows", hs == exp["hosts_src"], f"got {hs} want {exp['hosts_src']}")
    if not sc["template_first"]:
        check(f"{tag}: data before template counted no_template", errors.get("no_template", 0) >= 1, str(errors))


def main() -> int:
    for proto, scen in [("v9", "s1"), ("sflow", "s2"), ("v9", "s3_data_before_template"),
                        ("ipfix", "s3_data_before_template"), ("v9", "s4_bidir_ipv6"), ("ipfix", "s5_ipfix_sampled"),
                        ("v5", "s6_v5_sampled"), ("v5", "s1"), ("ipfix", "s1"), ("ipfix", "s4_bidir_ipv6")]:
        try:
            run(proto, scen)
        except Exception as e:  # noqa: BLE001
            check(f"{proto}/{scen}", False, f"{type(e).__name__}: {e}")
    run("v9", flood_scenario(2000))
    print(f"{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
