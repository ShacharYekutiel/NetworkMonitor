"""[3] Datagram dispatch (pure): the NetFlow/IPFIX sockets detect the protocol by the 16-bit
version field (5, 9, 10), the sFlow socket accepts sFlow v5 only."""

from __future__ import annotations

from netmon.flows.decode_ipfix import decode_ipfix
from netmon.flows.decode_netflow import decode_v5, decode_v9
from netmon.flows.decode_sflow import agent_address, decode_sflow
from netmon.flows.records import Decoded
from netmon.flows.templates import TemplateCache


def exporter_of(data: bytes, source: str, sflow: bool) -> str | None:
    """The exporter address of a datagram without decoding it (None: not decodable)."""
    if sflow:
        agent = agent_address(data)
        if agent is None:
            return None
        return source if agent in ("0.0.0.0", "::") else agent
    return source


def decode_datagram(
    data: bytes, source: str, sflow: bool, cache: TemplateCache, now: float
) -> Decoded:
    if sflow:
        return decode_sflow(data, source)
    if len(data) < 2:
        res = Decoded(None, source)
        res.error("parse_error")
        return res
    version = (data[0] << 8) | data[1]
    if version == 5:
        return decode_v5(data, source)
    if version == 9:
        return decode_v9(data, source, cache, now)
    if version == 10:
        return decode_ipfix(data, source, cache, now)
    res = Decoded(None, source)
    res.error("unsupported")
    return res
