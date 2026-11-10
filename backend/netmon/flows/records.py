"""[3] The decoded flow record and the decode result (pure)."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field

ZERO4 = b"\x00\x00\x00\x00"

PROTOCOLS = ("netflow5", "netflow9", "ipfix", "sflow5")


class FlowRecord:
    """One flow (NetFlow/IPFIX) or one sampled packet (sFlow). Addresses are raw bytes (4 or
    16); `rate` is the sampling rate found in the datagram / template context (None = not
    known; the worker applies the precedence of architecture 13.3.4); `egress` is
    flowDirection = 1."""

    __slots__ = (
        "src",
        "dst",
        "sport",
        "dport",
        "proto",
        "nbytes",
        "packets",
        "in_if",
        "out_if",
        "rate",
        "egress",
    )

    def __init__(
        self,
        src: bytes,
        dst: bytes,
        sport: int,
        dport: int,
        proto: int,
        nbytes: int,
        packets: int,
        in_if: int,
        out_if: int,
        rate: int | None = None,
        egress: bool = False,
    ) -> None:
        self.src = src
        self.dst = dst
        self.sport = sport
        self.dport = dport
        self.proto = proto
        self.nbytes = nbytes
        self.packets = packets
        self.in_if = in_if
        self.out_if = out_if
        self.rate = rate
        self.egress = egress

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"FlowRecord({ip_text(self.src)}:{self.sport}->{ip_text(self.dst)}:{self.dport} "
            f"p{self.proto} {self.nbytes}B {self.packets}p if {self.in_if}->{self.out_if} "
            f"rate={self.rate} egress={self.egress})"
        )


@dataclass
class Decoded:
    """Result of decoding one datagram. `exporter` is the address the data is attributed to
    (NetFlow/IPFIX: the UDP source; sFlow: the agent address). `errors` counts problems by
    drop reason (`parse_error`, `no_template`, `unsupported`); records already decoded are
    kept."""

    protocol: str | None
    exporter: str
    records: list[FlowRecord] = field(default_factory=list)
    errors: dict[str, int] = field(default_factory=dict)
    templates: int = 0  # template / options-template records accepted

    def error(self, reason: str, n: int = 1) -> None:
        self.errors[reason] = self.errors.get(reason, 0) + n


def ip_text(raw: bytes) -> str:
    """Bytes -> canonical text; IPv4-mapped IPv6 -> IPv4; anything else -> 0.0.0.0."""
    n = len(raw)
    if n == 4:
        return socket.inet_ntoa(raw)
    if n == 16:
        if raw[:12] == b"\x00" * 10 + b"\xff\xff":
            return socket.inet_ntoa(raw[12:])
        return socket.inet_ntop(socket.AF_INET6, raw)
    return "0.0.0.0"
