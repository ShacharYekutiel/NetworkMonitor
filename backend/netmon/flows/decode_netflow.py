"""[3] NetFlow v5 and v9 (RFC 3954) decoders (pure, architecture 13.3.3).

`decode_v5(data, exporter)` and `decode_v9(data, exporter, cache, now)` never raise: problems
are counted in `Decoded.errors` (`parse_error` per datagram or per flow set, `no_template`
per data set without a known template). The flow-set walker and the record builder are
shared with IPFIX (`netmon.flows.decode_ipfix`)."""

from __future__ import annotations

import struct
from typing import Any

from netmon.flows import templates as tp
from netmon.flows.records import ZERO4, Decoded, FlowRecord
from netmon.flows.templates import Template, TemplateCache, TemplateError

V5_HEADER = struct.Struct(">HHIIIIBBH")
V5_RECORD = struct.Struct(">4s4s4xHHII8xHHxBBx4x4x")  # 48 bytes
V5_MAX_COUNT = 30
V9_HEADER = struct.Struct(">HHIIII")
SET_HEADER = struct.Struct(">HH")
FIELD = struct.Struct(">HH")


# ------------------------------------------------------------------ v5
def decode_v5(data: bytes, exporter: str) -> Decoded:
    res = Decoded("netflow5", exporter)
    try:
        if len(data) < V5_HEADER.size:
            res.error("parse_error")
            return res
        _, count, _, _, _, _, _, _, sampling = V5_HEADER.unpack_from(data, 0)
        if count > V5_MAX_COUNT or len(data) < V5_HEADER.size + count * V5_RECORD.size:
            res.error("parse_error")
            return res
        interval = sampling & 0x3FFF
        rate = interval if interval > 1 else None
        out = res.records
        off = V5_HEADER.size
        for (
            src,
            dst,
            in_if,
            out_if,
            pkts,
            octets,
            sport,
            dport,
            _flags,
            proto,
        ) in V5_RECORD.iter_unpack(data[off : off + count * V5_RECORD.size]):
            if not octets and not pkts:
                continue
            if proto not in (6, 17, 132):
                sport = dport = 0
            out.append(FlowRecord(src, dst, sport, dport, proto, octets, pkts, in_if, out_if, rate))
    except Exception:  # noqa: BLE001 - never raise past the caller
        res.records.clear()
        res.error("parse_error")
    return res


# ------------------------------------------------------------------ v9
def decode_v9(data: bytes, exporter: str, cache: TemplateCache, now: float) -> Decoded:
    res = Decoded("netflow9", exporter)
    try:
        if len(data) < V9_HEADER.size:
            res.error("parse_error")
            return res
        domain = V9_HEADER.unpack_from(data, 0)[5]
        walk_sets(memoryview(data)[V9_HEADER.size :], exporter, domain, cache, now, res, v9=True)
    except Exception:  # noqa: BLE001
        res.error("parse_error")
    return res


def walk_sets(
    view: memoryview,
    exporter: str,
    domain: int,
    cache: TemplateCache,
    now: float,
    res: Decoded,
    *,
    v9: bool,
) -> None:
    """Iterate the sets of a v9 / IPFIX message (the `count` header field is ignored: the
    message is walked by length). A set header that does not fit stops the walk
    (`parse_error`); a bad set body is counted and the next set is processed."""
    n = len(view)
    off = 0
    tmpl_id, opt_id = (0, 1) if v9 else (2, 3)
    while n - off >= 4:
        set_id, length = SET_HEADER.unpack_from(view, off)
        if length < 4 or off + length > n:
            res.error("parse_error")
            return
        body = view[off + 4 : off + length]
        off += length
        if set_id == tmpl_id:
            if not _template_set(body, exporter, domain, cache, now, res, v9, options=False):
                res.error("parse_error")
        elif set_id == opt_id:
            if not _template_set(body, exporter, domain, cache, now, res, v9, options=True):
                res.error("parse_error")
        elif set_id >= 256:
            t = cache.get(exporter, domain, set_id, now)
            if t is None:
                res.error("no_template")
                continue
            rows, bad = t.rows(body)
            if bad:
                res.error("parse_error")
            if t.options:
                _apply_options(rows, t, exporter, domain, cache)
            elif rows:
                build_records(rows, t, exporter, domain, cache, res.records, v9=v9)
        else:
            # v9 ids 2-255 / IPFIX ids 4-255 are reserved
            res.error("unsupported")
    if off != n and any(view[off:]):
        res.error("parse_error")


def _template_set(
    body: memoryview,
    exporter: str,
    domain: int,
    cache: TemplateCache,
    now: float,
    res: Decoded,
    v9: bool,
    options: bool,
) -> bool:
    """Parse the template records of one set; False when malformed (templates parsed before
    the problem are kept)."""
    n = len(body)
    off = 0
    while n - off >= 4:
        tid, count = FIELD.unpack_from(body, off)
        if tid == 0 and count == 0 and not any(body[off:]):
            return True  # padding
        off += 4
        try:
            if v9:
                if options:
                    # v9: template id, option scope length, option length (bytes)
                    if off + 2 > n:
                        return False
                    scope_len, opt_len = count, (body[off] << 8) | body[off + 1]
                    off += 2
                    if scope_len % 4 or opt_len % 4 or off + scope_len + opt_len > n:
                        return False
                    fields = []
                    for i in range(scope_len // 4):
                        _, length = FIELD.unpack_from(body, off + 4 * i)
                        fields.append((-1, length, 0))  # scope types are not IEs
                    off += scope_len
                    for i in range(opt_len // 4):
                        ie, length = FIELD.unpack_from(body, off + 4 * i)
                        fields.append((ie, length, 0))
                    off += opt_len
                    if not fields or tid < 256:
                        return False
                    cache.put(exporter, domain, Template(tid, fields, True, now), now)
                    res.templates += 1
                    # options templates are padded to a 4-byte boundary at the set end
                    continue
                if count == 0:
                    return False  # v9 has no withdrawal
                fields, off = _fields(body, off, count, ipfix=False)
            else:
                if count == 0:
                    # withdrawal: id 2 / 3 withdraws every (options) template of the domain
                    if tid in (2, 3):
                        cache.withdraw_all(exporter, domain, options=tid == 3)
                    else:
                        cache.withdraw(exporter, domain, tid)
                    continue
                scope = 0
                if options:
                    if off + 2 > n:
                        return False
                    scope = (body[off] << 8) | body[off + 1]
                    off += 2
                    if scope == 0 or scope > count:
                        return False
                fields, off = _fields(body, off, count, ipfix=True)
            cache.put(exporter, domain, Template(tid, fields, options, now), now)
            res.templates += 1
        except (TemplateError, struct.error, IndexError):
            return False
    return True


def _fields(
    body: memoryview, off: int, count: int, *, ipfix: bool
) -> tuple[list[tuple[int, int, int]], int]:
    n = len(body)
    fields = []
    for _ in range(count):
        if off + 4 > n:
            raise TemplateError("truncated template")
        ie, length = FIELD.unpack_from(body, off)
        off += 4
        ent = 0
        if ipfix and ie & 0x8000:
            if off + 4 > n:
                raise TemplateError("truncated template")
            ent = int.from_bytes(body[off : off + 4], "big") or 1
            off += 4
            ie &= 0x7FFF
        if not ipfix and length == tp.VARLEN:
            raise TemplateError("variable length in v9")
        fields.append((ie, length, ent))
    return fields, off


def _apply_options(
    rows: list[Any], t: Template, exporter: str, domain: int, cache: TemplateCache
) -> None:
    ix = t.index
    for row in rows:
        rate = tp.options_rate(row, ix)
        if rate is None or rate < 1:
            continue
        sid = None
        for ie in (tp.IE_SAMPLER_ID, tp.IE_SELECTOR_ID):
            i = ix.get(ie)
            if i is not None and isinstance(row[i], int):
                sid = row[i]
                break
        if sid is None:
            cache.exporter_rate[exporter] = rate
        else:
            cache.set_sampler(exporter, domain, sid, rate)


def build_records(
    rows: list[Any],
    t: Template,
    exporter: str,
    domain: int,
    cache: TemplateCache,
    out: list[FlowRecord],
    *,
    v9: bool,
) -> None:
    """Rows of a data template -> FlowRecord (bi-directional v9 records with OUT_BYTES produce a
    second, reverse record; flowDirection = egress swaps the interfaces)."""
    ix = t.index
    g = ix.get
    i_s4, i_d4, i_s6, i_d6 = g(8, -1), g(12, -1), g(27, -1), g(28, -1)
    i_sp, i_dp, i_pr = g(7, -1), g(11, -1), g(4, -1)
    i_b = g(1, g(85, -1))
    i_p = g(2, g(86, -1))
    i_ob, i_op = (g(23, -1), g(24, -1)) if v9 else (-1, -1)
    i_in, i_out, i_dir = g(10, -1), g(14, -1), g(61, -1)
    i_si = g(34, -1)
    i_sid = g(48, g(302, -1))
    base_rate = cache.exporter_rate.get(exporter)
    for row in rows:
        src = row[i_s4] if i_s4 >= 0 else None
        if src is None and i_s6 >= 0:
            src = row[i_s6]
        dst = row[i_d4] if i_d4 >= 0 else None
        if dst is None and i_d6 >= 0:
            dst = row[i_d6]
        src = src or ZERO4
        dst = dst or ZERO4
        proto = (row[i_pr] or 0) if i_pr >= 0 else 0
        if proto in (6, 17, 132):
            sport = (row[i_sp] or 0) if i_sp >= 0 else 0
            dport = (row[i_dp] or 0) if i_dp >= 0 else 0
        else:
            sport = dport = 0
        in_if = (row[i_in] or 0) if i_in >= 0 else 0
        out_if = (row[i_out] or 0) if i_out >= 0 else 0
        egress = i_dir >= 0 and row[i_dir] == 1
        if egress:
            in_if, out_if = out_if, in_if
        rate = base_rate
        if i_si >= 0 and row[i_si]:
            rate = row[i_si]
        elif i_sid >= 0 and row[i_sid] is not None:
            rate = cache.sampler_rate(exporter, domain, row[i_sid]) or base_rate
        nbytes = (row[i_b] or 0) if i_b >= 0 else 0
        pkts = (row[i_p] or 0) if i_p >= 0 else 0
        if nbytes or pkts:
            out.append(
                FlowRecord(src, dst, sport, dport, proto, nbytes, pkts, in_if, out_if, rate, egress)
            )
        if i_ob >= 0:
            ob = row[i_ob] or 0
            op = (row[i_op] or 0) if i_op >= 0 else 0
            if ob or op:
                out.append(
                    FlowRecord(dst, src, dport, sport, proto, ob, op, out_if, in_if, rate, egress)
                )
