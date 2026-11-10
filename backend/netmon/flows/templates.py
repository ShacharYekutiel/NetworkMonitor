"""[3] NetFlow v9 / IPFIX templates (pure): compiled record layouts and the bounded template
cache keyed by (exporter address, source id / observation domain, template id).

A template is compiled once at definition time: fixed-length templates get a `struct.Struct`
that extracts only the information elements the collector uses (all other fields become pad
bytes), variable-length (IPFIX) templates are walked field by field."""

from __future__ import annotations

import struct
from collections import OrderedDict
from typing import Any

from netmon.flows.limits import TEMPLATE_IDLE_S, TEMPLATES_PER_DOMAIN, TEMPLATES_TOTAL

VARLEN = 65535

# information elements (IANA ids; v9 shares the numbering for these)
IE_OCTETS, IE_PACKETS = 1, 2
IE_PROTO, IE_TCP_FLAGS = 4, 6
IE_SPORT, IE_DPORT = 7, 11
IE_SRC4, IE_DST4 = 8, 12
IE_IN_IF, IE_OUT_IF = 10, 14
IE_OUT_BYTES, IE_OUT_PKTS = 23, 24  # v9 OUT_BYTES / OUT_PKTS (reverse direction)
IE_SRC6, IE_DST6 = 27, 28
IE_SAMPLING_INTERVAL = 34
IE_SAMPLER_ID, IE_SAMPLER_MODE, IE_SAMPLER_RANDOM = 48, 49, 50
IE_DIRECTION = 61
IE_TOTAL_OCTETS, IE_TOTAL_PACKETS = 85, 86
IE_SELECTOR_ID = 302
IE_SAMPLING_PKT_INTERVAL, IE_SAMPLING_PKT_SPACE = 305, 306

ADDRESS_IES = {IE_SRC4: 4, IE_DST4: 4, IE_SRC6: 16, IE_DST6: 16}
DATA_IES = frozenset(
    {
        IE_OCTETS,
        IE_PACKETS,
        IE_PROTO,
        IE_SPORT,
        IE_DPORT,
        IE_SRC4,
        IE_DST4,
        IE_IN_IF,
        IE_OUT_IF,
        IE_OUT_BYTES,
        IE_OUT_PKTS,
        IE_SRC6,
        IE_DST6,
        IE_SAMPLING_INTERVAL,
        IE_SAMPLER_ID,
        IE_DIRECTION,
        IE_TOTAL_OCTETS,
        IE_TOTAL_PACKETS,
        IE_SELECTOR_ID,
    }
)
OPTION_IES = frozenset(
    {
        IE_SAMPLING_INTERVAL,
        IE_SAMPLER_ID,
        IE_SAMPLER_RANDOM,
        IE_SELECTOR_ID,
        IE_SAMPLING_PKT_INTERVAL,
        IE_SAMPLING_PKT_SPACE,
    }
)
_INT_FMT = {1: "B", 2: "H", 4: "I", 8: "Q"}


class TemplateError(ValueError):
    pass


class Template:
    """A compiled data or options template.

    `fields`: (ie, length, enterprise) in wire order; ie = -1 for fields the collector never
    reads (v9 option scope fields, enterprise-specific fields). `index[ie]` is the position of
    that element in the tuple produced by `rows()`."""

    __slots__ = (
        "tid",
        "fields",
        "options",
        "min_len",
        "fixed",
        "struct",
        "index",
        "convert",
        "last_used",
    )

    def __init__(
        self, tid: int, fields: list[tuple[int, int, int]], options: bool, now: float = 0.0
    ) -> None:
        if tid < 256:
            raise TemplateError("template id < 256")
        if not fields:
            raise TemplateError("template without fields")
        self.tid = tid
        self.fields = tuple(fields)
        self.options = options
        self.last_used = now
        wanted = OPTION_IES if options else DATA_IES
        self.fixed = all(length != VARLEN for _, length, _ in fields)
        self.min_len = sum(1 if length == VARLEN else length for _, length, _ in fields)
        if self.min_len <= 0:
            raise TemplateError("zero-length template")
        self.index: dict[int, int] = {}
        self.convert: tuple[int, ...] = ()
        self.struct: struct.Struct | None = None
        fmt = [">"]
        convert = []
        pos = 0
        for ie, length, ent in fields:
            take = ent == 0 and ie in wanted and ie not in self.index and length != VARLEN
            if take and ie in ADDRESS_IES and length != ADDRESS_IES[ie]:
                take = False
            if take and length == 0:
                take = False
            if not take:
                if length != VARLEN:
                    fmt.append(f"{length}x" if length else "")
                continue
            self.index[ie] = pos
            if ie in ADDRESS_IES:
                fmt.append(f"{length}s")
            elif length in _INT_FMT:
                fmt.append(_INT_FMT[length])
            else:
                fmt.append(f"{length}s")
                convert.append(pos)
            pos += 1
        self.convert = tuple(convert)
        if self.fixed:
            self.struct = struct.Struct("".join(fmt))
        else:
            # variable-length templates are walked in `rows()`; `index` positions refer to
            # the order of the wanted fields as they appear on the wire
            self.index = {}
            pos = 0
            for ie, length, ent in fields:
                if ent == 0 and ie in wanted and ie not in self.index:
                    if ie in ADDRESS_IES and length not in (ADDRESS_IES[ie], VARLEN):
                        continue
                    self.index[ie] = pos
                    pos += 1

    @property
    def record_len(self) -> int:
        return self.min_len

    def rows(self, body: bytes | memoryview) -> tuple[list[Any], bool]:
        """Decode the records of one data set. Returns (rows, error) where error means the set
        was truncated / malformed (rows decoded before the problem are kept). Trailing bytes
        shorter than a record are padding."""
        if self.fixed:
            assert self.struct is not None
            rl = self.min_len
            n = len(body) // rl
            if n == 0:
                return [], len(body) >= 4  # a set shorter than one record
            raw = self.struct.iter_unpack(body[: n * rl])
            if not self.convert:
                return list(raw), False
            out = []
            for tup in raw:
                row = list(tup)
                for i in self.convert:
                    row[i] = int.from_bytes(row[i], "big")
                out.append(row)
            return out, False
        return self._rows_variable(bytes(body))

    def _rows_variable(self, body: bytes) -> tuple[list[Any], bool]:
        out: list[Any] = []
        n = len(body)
        off = 0
        width = len(self.index)
        wanted = OPTION_IES if self.options else DATA_IES
        while n - off >= self.min_len:
            row: list[Any] = [None] * width
            seen: set[int] = set()
            for ie, length, ent in self.fields:
                if length == VARLEN:
                    if off >= n:
                        return out, True
                    length = body[off]
                    off += 1
                    if length == 255:
                        if off + 2 > n:
                            return out, True
                        length = (body[off] << 8) | body[off + 1]
                        off += 2
                if off + length > n:
                    return out, True
                if ent == 0 and ie in wanted and ie not in seen and ie in self.index:
                    seen.add(ie)
                    chunk = body[off : off + length]
                    if ie in ADDRESS_IES:
                        row[self.index[ie]] = chunk if length == ADDRESS_IES[ie] else None
                    else:
                        row[self.index[ie]] = int.from_bytes(chunk, "big") if length else None
                off += length
            out.append(row)
        return out, not out and n >= 4  # a set shorter than one record


class TemplateCache:
    """Bounded cache: `per_domain` templates per (exporter, domain), `total` overall (least
    recently used evicted), idle templates expire after `idle_s`. Also holds the sampling
    learnt from options data: per sampler / selector id and exporter-wide."""

    def __init__(
        self,
        per_domain: int = TEMPLATES_PER_DOMAIN,
        total: int = TEMPLATES_TOTAL,
        idle_s: float = TEMPLATE_IDLE_S,
    ) -> None:
        self.per_domain = per_domain
        self.total = total
        self.idle_s = idle_s
        self._t: OrderedDict[tuple[str, int, int], Template] = OrderedDict()
        self._domains: dict[tuple[str, int], int] = {}
        # sampler / selector id -> rate, per (exporter, domain); exporter-wide rate
        self.samplers: OrderedDict[tuple[str, int, int], int] = OrderedDict()
        self.exporter_rate: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._t)

    def get(self, exporter: str, domain: int, tid: int, now: float) -> Template | None:
        key = (exporter, domain, tid)
        t = self._t.get(key)
        if t is None:
            return None
        if now - t.last_used > self.idle_s:
            self._remove(key)
            return None
        t.last_used = now
        self._t.move_to_end(key)
        return t

    def put(self, exporter: str, domain: int, t: Template, now: float) -> None:
        key = (exporter, domain, t.tid)
        dom = (exporter, domain)
        t.last_used = now
        if key in self._t:
            self._t[key] = t  # a redefinition replaces the template
            self._t.move_to_end(key)
            return
        if self._domains.get(dom, 0) >= self.per_domain:
            victim = next(k for k in self._t if (k[0], k[1]) == dom)
            self._remove(victim)
        while len(self._t) >= self.total:
            self._remove(next(iter(self._t)))
        self._t[key] = t
        self._domains[dom] = self._domains.get(dom, 0) + 1

    def withdraw(self, exporter: str, domain: int, tid: int) -> None:
        self._remove((exporter, domain, tid))

    def withdraw_all(self, exporter: str, domain: int, options: bool) -> None:
        for key in [
            k
            for k, t in self._t.items()
            if k[0] == exporter and k[1] == domain and t.options == options
        ]:
            self._remove(key)

    def _remove(self, key: tuple[str, int, int]) -> None:
        if self._t.pop(key, None) is not None:
            dom = (key[0], key[1])
            left = self._domains.get(dom, 1) - 1
            if left <= 0:
                self._domains.pop(dom, None)
            else:
                self._domains[dom] = left

    def expire(self, now: float) -> int:
        old = [k for k, t in self._t.items() if now - t.last_used > self.idle_s]
        for k in old:
            self._remove(k)
        return len(old)

    def forget_exporter(self, exporter: str) -> None:
        for k in [k for k in self._t if k[0] == exporter]:
            self._remove(k)
        for k in [k for k in self.samplers if k[0] == exporter]:
            self.samplers.pop(k, None)
        self.exporter_rate.pop(exporter, None)

    def clear(self) -> None:
        self._t.clear()
        self._domains.clear()
        self.samplers.clear()
        self.exporter_rate.clear()

    # ---------------------------------------------------------------- sampling
    def set_sampler(self, exporter: str, domain: int, sampler: int, rate: int) -> None:
        key = (exporter, domain, sampler)
        self.samplers[key] = rate
        self.samplers.move_to_end(key)
        while len(self.samplers) > self.total:
            self.samplers.popitem(last=False)

    def sampler_rate(self, exporter: str, domain: int, sampler: int) -> int | None:
        return self.samplers.get((exporter, domain, sampler))


def options_rate(row: list[Any], index: dict[int, int]) -> int | None:
    """Sampling rate from an options data record: samplingInterval (34), else the v9 random
    interval (50), else IPFIX packet interval / space (305/306): (interval + space) /
    interval."""

    def val(ie: int) -> int | None:
        i = index.get(ie)
        if i is None:
            return None
        v = row[i]
        return v if isinstance(v, int) else None

    v = val(IE_SAMPLING_INTERVAL)
    if v:
        return v
    v = val(IE_SAMPLER_RANDOM)
    if v:
        return v
    interval = val(IE_SAMPLING_PKT_INTERVAL)
    space = val(IE_SAMPLING_PKT_SPACE)
    if interval:
        return max(1, round((interval + (space or 0)) / interval))
    return None
