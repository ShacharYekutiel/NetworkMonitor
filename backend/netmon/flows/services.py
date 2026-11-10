"""[3] Services map (pure, data-model "Services map"): `(protocol, src_port, dst_port) ->
service port` and the application name, from `data/services.tsv` plus the admin overrides
(`flow_app_overrides`, which win over the file)."""

from __future__ import annotations

import functools
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent.parent / "data" / "services.tsv"
PORT_PROTOCOLS = frozenset({6, 17, 132})
PROTO_NAMES = {
    1: "icmp",
    2: "igmp",
    6: "tcp",
    17: "udp",
    47: "gre",
    50: "esp",
    51: "ah",
    58: "icmpv6",
    89: "ospf",
    103: "pim",
    112: "vrrp",
    132: "sctp",
}
EPHEMERAL = 49152
MAX_OVERRIDES = 200


def proto_name(proto: int) -> str:
    return PROTO_NAMES.get(proto, f"ip-{proto}")


def parse_tsv(text: str) -> dict[tuple[int, int], str]:
    out: dict[tuple[int, int], str] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            out[(int(parts[0]), int(parts[1]))] = parts[2].strip()
        except ValueError:
            continue
    return out


@functools.lru_cache(maxsize=1)
def builtin() -> dict[tuple[int, int], str]:
    try:
        return parse_tsv(DATA.read_text(encoding="utf-8"))
    except OSError:
        return {}


class ServiceMap:
    def __init__(
        self,
        table: dict[tuple[int, int], str] | None = None,
        overrides: Iterable[dict[str, Any]] = (),
    ) -> None:
        names = dict(builtin() if table is None else table)
        for o in list(overrides)[:MAX_OVERRIDES]:
            try:
                names[(int(o["protocol"]), int(o["port"]))] = str(o["name"])
            except (KeyError, TypeError, ValueError):
                continue
        self.names = names

    def service_port(self, proto: int, sp: int, dp: int) -> int:
        if proto not in PORT_PROTOCOLS:
            return 0
        names = self.names
        if (proto, dp) in names:
            return dp
        if (proto, sp) in names:
            return sp
        # the lower port when it is < 1024, else the lower port when either is < 49152 (both
        # rules pick the lower of the two), else 0 (both ephemeral)
        low = sp if sp < dp else dp
        return low if low < EPHEMERAL else 0

    def app_name(self, proto: int, port: int) -> str | None:
        if port < 0:
            return None
        name = self.names.get((proto, port))
        if name is None and port == 0 and proto not in PORT_PROTOCOLS:
            return proto_name(proto)
        return name

    def key(self, proto: int, port: int) -> str:
        if port < 0:
            return "other"
        if proto not in PORT_PROTOCOLS:
            return proto_name(proto)
        return f"{proto_name(proto)}/{port}"

    def label(self, proto: int, port: int) -> str:
        if port < 0:
            return "other"
        key = self.key(proto, port)
        name = self.app_name(proto, port)
        if name is None or name == key:
            return key
        return f"{name} ({key})"


@functools.lru_cache(maxsize=1)
def default_map() -> ServiceMap:
    return ServiceMap()


def service_port(proto: int, sp: int, dp: int) -> int:
    """data-model vectors: (6, 51000, 443) -> 443; (17, 60000, 61000) -> 0; (1, 0, 0) -> 0."""
    return default_map().service_port(proto, sp, dp)
