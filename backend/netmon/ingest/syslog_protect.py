"""[syslog-protection] Syslog flood protection at ingest: filter rules, deduplication and the
per-source storage rate limit (data-model "[syslog-protection]").

Owner's finding: one device that logs every SNMP GET wrote 85,075 syslog rows in 24 h (99.7 %
program=SNMP, severity info; 3 lines per request that differ by a sequence number). Traps have
"Repetitive traps" (netmon.ingest.collapse); syslog gets the same kind of protection here.

Order for every parsed, attributed syslog message (after event rules, config-backup triggers
and SIEM forwarding, which therefore still see every message — the trap-collapse principle):

1. filter rules (ordered, first enabled match wins): `drop` -> not stored, counted per rule;
   `keep` -> stored tagged with the rule id, pruned to the newest `keep_n` per source after
   the write;
2. deduplication (`syslog_dedup_enabled`, ON by default): the same (source, program, msgid,
   severity, message) within `syslog_dedup_window_s` of the first one is folded into that
   row's `repeat_count` / `last_seen` (flushed to the DB every couple of seconds);
3. per-source rate limit (`syslog_rate_limit_enabled`, ON by default): at most
   `syslog_rate_limit_per_min` new rows per source and minute; the excess is dropped.

Everything is in memory and O(rules) per message; state is bounded (dedup entries, tracked
sources) with FIFO/LRU eviction."""

from __future__ import annotations

import hashlib
import ipaddress
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from netmon.ingest.query import like_escape

DEDUP_MAX = 20000  # entries (16-byte digest + a few numbers each: ~4 MB at the cap)
TRACKED = 4096  # sources with rate-limit state / per-source counters
DEFAULT_DEDUP_WINDOW_S = 60
DEFAULT_RATE_PER_MIN = 3000
SOURCE_REASONS = ("filter_rule", "deduplicated", "source_rate_limit", "pruned")


@dataclass(slots=True)
class FilterRule:
    id: int
    name: str
    nets: list[Any] = field(default_factory=list)
    device_ids: frozenset[int] = frozenset()
    program: str = ""  # lower case
    prefix: bool = False
    msgid: str = ""  # lower case
    severity_min: int | None = None
    contains: str = ""  # lower case
    action: str = "drop"
    keep_n: int = 100

    @classmethod
    def from_row(cls, r: Any) -> FilterRule:
        nets = []
        for s in r["sources"] or []:
            try:
                nets.append(ipaddress.ip_network(str(s), strict=False))
            except ValueError:
                continue
        return cls(
            id=int(r["id"]),
            name=str(r["name"]),
            nets=nets,
            device_ids=frozenset(int(d) for d in r["device_ids"] or []),
            program=str(r["program"] or "").lower(),
            prefix=r["program_match"] == "prefix",
            msgid=str(r["msgid"] or "").lower(),
            severity_min=None if r["severity_min"] is None else int(r["severity_min"]),
            contains=str(r["message_contains"] or "").lower(),
            action=str(r["action"]),
            keep_n=int(r["keep_n"]),
        )

    def matches(self, rec: dict[str, Any], lowered: list[str | None]) -> bool:
        """`lowered` caches the lower-case message across rules ([None] until needed)."""
        if self.severity_min is not None and int(rec["severity"]) < self.severity_min:
            return False
        if self.program:
            p = str(rec["program"] or "").lower()
            if (not p.startswith(self.program)) if self.prefix else p != self.program:
                return False
        if self.msgid and str(rec["msgid"] or "").lower() != self.msgid:
            return False
        if self.nets or self.device_ids:
            hit = rec.get("device_id") is not None and int(rec["device_id"]) in self.device_ids
            if not hit and self.nets:
                try:
                    addr = ipaddress.ip_address(str(rec["source_ip"]))
                except ValueError:
                    return False
                hit = any(addr.version == n.version and addr in n for n in self.nets)
            if not hit:
                return False
        if self.contains:
            if lowered[0] is None:
                lowered[0] = str(rec["message"] or "").lower()
            if self.contains not in lowered[0]:
                return False
        return True


def rule_sql(r: dict[str, Any], alias: str = "x") -> tuple[str, dict[str, Any]]:
    """The stored rows a rule matches, as SQL (same semantics as `FilterRule.matches`)."""
    a = alias
    conds: list[str] = []
    p: dict[str, Any] = {}
    src: list[str] = []
    if r.get("sources"):
        src.append(f"{a}.source_ip <<= ANY(CAST(:f_nets AS inet[]))")
        p["f_nets"] = [str(s) for s in r["sources"]]
    if r.get("device_ids"):
        src.append(f"{a}.device_id = ANY(CAST(:f_dids AS integer[]))")
        p["f_dids"] = [int(d) for d in r["device_ids"]]
    if src:
        conds.append("(" + " OR ".join(src) + ")")
    if r.get("program"):
        if r.get("program_match") == "prefix":
            conds.append(f"lower({a}.program) LIKE :f_prog ESCAPE '\\'")
            p["f_prog"] = like_escape(str(r["program"]).lower()) + "%"
        else:
            conds.append(f"lower({a}.program) = :f_prog")
            p["f_prog"] = str(r["program"]).lower()
    if r.get("msgid"):
        conds.append(f"lower({a}.msgid) = :f_msgid")
        p["f_msgid"] = str(r["msgid"]).lower()
    if r.get("severity_min") is not None:
        conds.append(f"{a}.severity >= :f_sev")
        p["f_sev"] = int(r["severity_min"])
    if r.get("message_contains"):
        conds.append(f"{a}.message ILIKE :f_msg ESCAPE '\\'")
        p["f_msg"] = "%" + like_escape(str(r["message_contains"])) + "%"
    return (" AND ".join(conds) or "TRUE"), p


class _Dup:
    __slots__ = ("first", "id", "time", "count", "last_seen", "dirty")

    def __init__(self, first: float) -> None:
        self.first = first
        self.id: int | None = None
        self.time: datetime | None = None
        self.count = 1
        self.last_seen: datetime | None = None
        self.dirty = False


@dataclass(slots=True)
class RepeatUpdate:
    id: int
    time: datetime
    count: int
    last_seen: datetime


class SyslogProtection:
    def __init__(self, dedup_max: int = DEDUP_MAX, tracked: int = TRACKED) -> None:
        self.rules: list[FilterRule] = []
        self.dedup_on = True
        self.window_s = float(DEFAULT_DEDUP_WINDOW_S)
        self.rate_on = True
        self.per_min = DEFAULT_RATE_PER_MIN
        self.dedup_max = dedup_max
        self.tracked = tracked
        self._dups: OrderedDict[bytes, _Dup] = OrderedDict()
        self._final: list[RepeatUpdate] = []  # evicted dirty entries, flushed next
        self._rate: OrderedDict[str, list[int]] = OrderedDict()  # ip -> [minute, rows]
        self.sources: OrderedDict[str, dict[str, int]] = OrderedDict()
        self.rule_hits: dict[int, int] = {}  # since boot
        self.pending_hits: dict[int, tuple[int, datetime]] = {}  # not yet in the DB

    # ---------------------------------------------------------------- config
    def configure(self, *, dedup_on: bool, window_s: int, rate_on: bool, per_min: int) -> None:
        if not dedup_on or float(window_s) != self.window_s:
            self._flush_all_to_final()
        self.dedup_on = dedup_on
        self.window_s = float(window_s)
        self.rate_on = rate_on
        self.per_min = int(per_min)

    def set_rules(self, rows: list[Any]) -> None:
        self.rules = [FilterRule.from_row(r) for r in rows if r["enabled"]]

    def reset(self) -> None:
        self._dups.clear()
        self._final.clear()
        self._rate.clear()
        self.sources.clear()
        self.rule_hits.clear()
        self.pending_hits.clear()

    # ---------------------------------------------------------------- hot path
    def check(self, rec: dict[str, Any], mono: float | None = None) -> str | None:
        """None: store `rec` (it may carry `filter_rule_id` / `_dedup`); otherwise the reason it
        is not stored: `filter_rule` (`rec["_filter_rule"]` = rule id), `deduplicated`
        (`rec["_dedup_ref"]` = the stored row id or None) or `source_rate_limit`."""
        now = time.monotonic() if mono is None else mono
        ip = str(rec["source_ip"])
        rec["filter_rule_id"] = None
        if self.rules:
            lowered: list[str | None] = [None]
            for rule in self.rules:
                if rule.matches(rec, lowered):
                    self._hit(rule.id, rec["time"])
                    if rule.action == "drop":
                        rec["_filter_rule"] = rule.id
                        self._count(ip, "filter_rule")
                        return "filter_rule"
                    rec["filter_rule_id"] = rule.id
                    break
        key: bytes | None = None
        if self.dedup_on:
            key = self.key(rec)
            t = rec["time"].timestamp()
            d = self._dups.get(key)
            if d is not None and t - d.first < self.window_s:
                d.count += 1
                d.last_seen = rec["time"]
                d.dirty = True
                rec["_dedup_ref"] = d.id
                self._count(ip, "deduplicated")
                return "deduplicated"
            if d is not None:  # window over: a new row
                self._retire(key)
        if self.rate_on and not self._rate_ok(ip, now):
            self._count(ip, "source_rate_limit")
            return "source_rate_limit"
        if key is not None:
            self._dups[key] = _Dup(rec["time"].timestamp())
            rec["_dedup"] = key
            while len(self._dups) > self.dedup_max:
                self._retire(next(iter(self._dups)))
        return None

    @staticmethod
    def key(rec: dict[str, Any]) -> bytes:
        h = hashlib.blake2b(digest_size=16)
        h.update(
            f"{rec['source_ip']}\0{rec['program']}\0{rec['msgid']}\0{rec['severity']}\0".encode()
        )
        h.update(str(rec["message"]).encode("utf-8", "surrogatepass"))
        return h.digest()

    def _rate_ok(self, ip: str, now: float) -> bool:
        minute = int(now // 60)
        st = self._rate.get(ip)
        if st is None:
            st = [minute, 0]
            self._rate[ip] = st
            if len(self._rate) > self.tracked:
                self._rate.popitem(last=False)
        else:
            self._rate.move_to_end(ip)
            if st[0] != minute:
                st[0], st[1] = minute, 0
        if st[1] >= self.per_min:
            return False
        st[1] += 1
        return True

    def _count(self, ip: str, reason: str, n: int = 1) -> None:
        c = self.sources.get(ip)
        if c is None:
            c = dict.fromkeys(SOURCE_REASONS, 0)
            self.sources[ip] = c
            if len(self.sources) > self.tracked:
                self.sources.popitem(last=False)
        else:
            self.sources.move_to_end(ip)
        c[reason] += n

    def count_pruned(self, ip: str, n: int) -> None:
        if n:
            self._count(ip, "pruned", n)

    def _hit(self, rule_id: int, t: datetime) -> None:
        self.rule_hits[rule_id] = self.rule_hits.get(rule_id, 0) + 1
        n, _ = self.pending_hits.get(rule_id, (0, t))
        self.pending_hits[rule_id] = (n + 1, t)

    # ---------------------------------------------------------------- after the write
    def stored(self, rec: dict[str, Any], id_: int) -> None:
        key = rec.get("_dedup")
        if key is None:
            return
        d = self._dups.get(key)
        if d is not None and d.id is None:
            d.id = id_
            d.time = rec["time"]

    def not_stored(self, rec: dict[str, Any]) -> None:
        """The write failed: identical messages start a new row instead of a lost one."""
        key = rec.get("_dedup")
        if key is not None:
            d = self._dups.get(key)
            if d is not None and d.id is None:
                del self._dups[key]

    def _retire(self, key: bytes) -> None:
        d = self._dups.pop(key)
        if d.dirty and d.id is not None and d.time is not None and d.last_seen is not None:
            self._final.append(RepeatUpdate(d.id, d.time, d.count, d.last_seen))

    def close_all(self) -> None:
        """Close every open dedup window (pipeline stop): the next message starts a row."""
        self._flush_all_to_final()

    def _flush_all_to_final(self) -> None:
        for key in list(self._dups):
            self._retire(key)

    def expire(self, now_ts: float) -> None:
        """Drop entries whose window is over (oldest first: the dict is in first-seen order)."""
        while self._dups:
            key = next(iter(self._dups))
            if now_ts - self._dups[key].first < self.window_s:
                break
            self._retire(key)

    def repeat_updates(self) -> list[RepeatUpdate]:
        """Dirty repeat counts to write (absolute values: re-writing is harmless)."""
        out = self._final
        self._final = []
        for d in self._dups.values():
            if d.dirty and d.id is not None and d.time is not None and d.last_seen is not None:
                out.append(RepeatUpdate(d.id, d.time, d.count, d.last_seen))
                d.dirty = False
        return out

    def take_hits(self) -> dict[int, tuple[int, datetime]]:
        out = self.pending_hits
        self.pending_hits = {}
        return out

    def give_back_hits(self, hits: dict[int, tuple[int, datetime]]) -> None:
        for rid, (n, t) in hits.items():
            m, t2 = self.pending_hits.get(rid, (0, t))
            self.pending_hits[rid] = (m + n, max(t, t2))

    # ---------------------------------------------------------------- status
    def state_size(self) -> dict[str, int]:
        return {"dedup_entries": len(self._dups), "rate_sources": len(self._rate)}

    def top_sources(self, n: int = 20) -> list[dict[str, Any]]:
        rows = [{"source_ip": ip, **c, "total": sum(c.values())} for ip, c in self.sources.items()]
        rows.sort(key=lambda r: (-r["total"], r["source_ip"]))
        return rows[:n]
