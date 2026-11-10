"""[3] Pure 1-minute aggregation (architecture 13.3.6, data-model "Aggregation vectors").

`MinuteAggregator.add(exporter, minute, records, override)` accumulates sampling-corrected
records per (exporter, minute); `flush(up_to_minute)` returns one `Flush` per closed
(exporter, minute) with the rows of the four 1-minute tables: `flow_if_1m` exact,
`flow_app_1m` / `flow_host_1m` (per direction) / `flow_conv_1m` top-N by bytes (ties: packets,
then key) plus one "other" row each, so every table sums to the exporter's total.

At most `max_keys` distinct keys per minute (all tables, all exporters); new keys after that
merge into the "other" rows (interface keys are dropped) and the record is counted in
`overflow` (`aggregation_overflow`)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from netmon.flows.limits import MAX_AGG_KEYS
from netmon.flows.records import FlowRecord, ip_text
from netmon.flows.services import ServiceMap, default_map

OTHER_IP = "0.0.0.0"


@dataclass
class Flush:
    exporter: str
    minute: int  # epoch seconds of the minute start (UTC)
    if_rows: list[tuple[int, str, int, int, int]] = field(default_factory=list)
    app_rows: list[tuple[int, int, int, int, int]] = field(default_factory=list)
    host_rows: list[tuple[str, str, bool, int, int, int]] = field(default_factory=list)
    conv_rows: list[tuple[int, int, str, str, int, int, int, bool, int, int, int]] = field(
        default_factory=list
    )
    records: int = 0
    bytes: int = 0
    packets: int = 0

    @property
    def row_count(self) -> int:
        return len(self.if_rows) + len(self.app_rows) + len(self.host_rows) + len(self.conv_rows)


class _Bucket:
    __slots__ = (
        "ifs",
        "apps",
        "hosts",
        "convs",
        "app_other",
        "host_other",
        "conv_other",
        "records",
        "bytes",
        "packets",
    )

    def __init__(self) -> None:
        self.ifs: dict[tuple[int, str], list[int]] = {}
        self.apps: dict[tuple[int, int], list[int]] = {}
        self.hosts: dict[tuple[bytes, str], list[int]] = {}
        self.convs: dict[tuple[Any, ...], list[int]] = {}
        self.app_other = [0, 0, 0]
        self.host_other = {"src": [0, 0, 0], "dst": [0, 0, 0]}
        self.conv_other = [0, 0, 0]
        self.records = 0
        self.bytes = 0
        self.packets = 0


def _rank(item: tuple[Any, list[int]]) -> tuple[int, int, Any]:
    return (-item[1][0], -item[1][1], item[0])


def _top(
    acc: dict[Any, list[int]], n: int, other: list[int]
) -> tuple[list[tuple[Any, list[int]]], list[int]]:
    items = sorted(acc.items(), key=_rank)
    top, rest = items[:n], items[n:]
    o = list(other)
    for _, v in rest:
        o[0] += v[0]
        o[1] += v[1]
        o[2] += v[2]
    return top, o


class MinuteAggregator:
    def __init__(
        self,
        top_apps: int = 100,
        top_hosts: int = 200,
        top_conversations: int = 500,
        services: ServiceMap | None = None,
        max_keys: int = MAX_AGG_KEYS,
    ) -> None:
        self.top_apps = top_apps
        self.top_hosts = top_hosts
        self.top_conversations = top_conversations
        self.services = services or default_map()
        self.max_keys = max_keys
        self.buckets: dict[tuple[str, int], _Bucket] = {}
        self.keys: dict[int, int] = {}  # minute -> distinct keys
        self.overflow = 0

    def configure(
        self,
        top_apps: int | None = None,
        top_hosts: int | None = None,
        top_conversations: int | None = None,
        services: ServiceMap | None = None,
    ) -> None:
        if top_apps:
            self.top_apps = top_apps
        if top_hosts:
            self.top_hosts = top_hosts
        if top_conversations:
            self.top_conversations = top_conversations
        if services is not None:
            self.services = services

    @property
    def key_count(self) -> int:
        return sum(self.keys.values())

    def add(
        self,
        exporter: str,
        minute: int,
        records: Iterable[FlowRecord],
        override: int | None = None,
    ) -> int:
        """Add records (bytes/packets multiplied by the effective sampling rate: `override`
        first, else the record's rate, else 1). Returns the number of records added."""
        bk = (exporter, minute)
        b = self.buckets.get(bk)
        if b is None:
            b = self.buckets[bk] = _Bucket()
        nkeys = self.keys.get(minute, 0)
        cap = self.max_keys
        svc = self.services.service_port
        ifs, apps, hosts, convs = b.ifs, b.apps, b.hosts, b.convs
        n = 0
        tb = tp = 0
        for r in records:
            rate = override or r.rate or 1
            by = r.nbytes * rate
            pk = r.packets * rate
            n += 1
            tb += by
            tp += pk
            over = False
            proto, sport, dport = r.proto, r.sport, r.dport
            sp = svc(proto, sport, dport)
            in_if, out_if = r.in_if, r.out_if
            # interfaces (exact; dropped only past the key cap)
            if in_if:
                k = (in_if, "in")
                v = ifs.get(k)
                if v is not None:
                    v[0] += by
                    v[1] += pk
                    v[2] += 1
                elif nkeys < cap:
                    ifs[k] = [by, pk, 1]
                    nkeys += 1
                else:
                    over = True
            if out_if:
                k = (out_if, "out")
                v = ifs.get(k)
                if v is not None:
                    v[0] += by
                    v[1] += pk
                    v[2] += 1
                elif nkeys < cap:
                    ifs[k] = [by, pk, 1]
                    nkeys += 1
                else:
                    over = True
            # applications
            k2 = (proto, sp)
            v = apps.get(k2)
            if v is None:
                if nkeys < cap:
                    v = apps[k2] = [0, 0, 0]
                    nkeys += 1
                else:
                    v = b.app_other
                    over = True
            v[0] += by
            v[1] += pk
            v[2] += 1
            # hosts
            for hk, d in (((r.src, "src"), "src"), ((r.dst, "dst"), "dst")):
                v = hosts.get(hk)
                if v is None:
                    if nkeys < cap:
                        v = hosts[hk] = [0, 0, 0]
                        nkeys += 1
                    else:
                        v = b.host_other[d]
                        over = True
                v[0] += by
                v[1] += pk
                v[2] += 1
            # conversations (client side collapsed: the non-service port is 0)
            if sp and sp == dport:
                cs, cd = 0, sp
            elif sp and sp == sport:
                cs, cd = sp, 0
            else:
                cs = cd = 0
            k3 = (in_if, out_if, r.src, r.dst, proto, cs, cd)
            v = convs.get(k3)
            if v is None:
                if nkeys < cap:
                    v = convs[k3] = [0, 0, 0]
                    nkeys += 1
                else:
                    v = b.conv_other
                    over = True
            v[0] += by
            v[1] += pk
            v[2] += 1
            if over:
                self.overflow += 1
        b.records += n
        b.bytes += tb
        b.packets += tp
        self.keys[minute] = nkeys
        return n

    def minutes(self) -> list[int]:
        return sorted({m for _, m in self.buckets})

    def flush(self, up_to_minute: int | None = None) -> list[Flush]:
        """Close every bucket whose minute <= `up_to_minute` (None: all)."""
        out = []
        for key in sorted(self.buckets, key=lambda k: (k[1], k[0])):
            exporter, minute = key
            if up_to_minute is not None and minute > up_to_minute:
                continue
            out.append(self._flush_one(exporter, minute, self.buckets.pop(key)))
        for m in [m for m in self.keys if up_to_minute is None or m <= up_to_minute]:
            if not any(bm == m for _, bm in self.buckets):
                del self.keys[m]
        return out

    def discard(self) -> int:
        """Drop everything not flushed (feature turned OFF). Returns the records dropped."""
        n = sum(b.records for b in self.buckets.values())
        self.buckets.clear()
        self.keys.clear()
        return n

    def _flush_one(self, exporter: str, minute: int, b: _Bucket) -> Flush:
        f = Flush(exporter, minute, records=b.records, bytes=b.bytes, packets=b.packets)
        f.if_rows = [(k[0], k[1], v[0], v[1], v[2]) for k, v in sorted(b.ifs.items())]
        top, o = _top(b.apps, self.top_apps, b.app_other)
        f.app_rows = [(k[0], k[1], v[0], v[1], v[2]) for k, v in top]
        if any(o):
            f.app_rows.append((0, -1, o[0], o[1], o[2]))
        for d in ("src", "dst"):
            acc = {k: v for k, v in b.hosts.items() if k[1] == d}
            top, o = _top(acc, self.top_hosts, b.host_other[d])
            merged: dict[str, list[int]] = {}  # IPv4-mapped IPv6 and IPv4 share one text
            for k, v in top:
                m = merged.setdefault(ip_text(k[0]), [0, 0, 0])
                m[0] += v[0]
                m[1] += v[1]
                m[2] += v[2]
            f.host_rows.extend((ip, d, False, v[0], v[1], v[2]) for ip, v in merged.items())
            if any(o):
                f.host_rows.append((OTHER_IP, d, True, o[0], o[1], o[2]))
        top, o = _top(b.convs, self.top_conversations, b.conv_other)
        f.conv_rows = [
            (k[0], k[1], ip_text(k[2]), ip_text(k[3]), k[4], k[5], k[6], False, v[0], v[1], v[2])
            for k, v in top
        ]
        if any(o):
            f.conv_rows.append((0, 0, OTHER_IP, OTHER_IP, 0, 0, 0, True, o[0], o[1], o[2]))
        return f
