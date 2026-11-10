"""[3] IPFIX (RFC 7011) decoder (pure). Template set 2, options template set 3, data sets
>= 256, withdrawals (field count 0), variable-length fields and enterprise-specific fields
(skipped by length). Shares the set walker with NetFlow v9."""

from __future__ import annotations

import struct

from netmon.flows.decode_netflow import walk_sets
from netmon.flows.records import Decoded
from netmon.flows.templates import TemplateCache

IPFIX_HEADER = struct.Struct(">HHIII")


def decode_ipfix(data: bytes, exporter: str, cache: TemplateCache, now: float) -> Decoded:
    res = Decoded("ipfix", exporter)
    try:
        if len(data) < IPFIX_HEADER.size:
            res.error("parse_error")
            return res
        _, length, _, _, domain = IPFIX_HEADER.unpack_from(data, 0)
        if length < IPFIX_HEADER.size or length > len(data):
            res.error("parse_error")
            return res
        view = memoryview(data)[IPFIX_HEADER.size : length]
        walk_sets(view, exporter, domain, cache, now, res, v9=False)
    except Exception:  # noqa: BLE001 - never raise past the caller
        res.error("parse_error")
    return res
