"""[2b-2] MAC / IP search over port_macs and arp_entries (api.md "GET /api/search/mac")."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from sqlalchemy import text

from netmon.api.errors import ApiError
from netmon.ports import oui, view
from netmon.ports.collect import locally_administered
from netmon.ports.read import load_context, mac_details, neighbors_of
from netmon.services.serialize import iso
from netmon.services.status import load_inventory

MAX_ITEMS = 200
MAX_ARP = 50
_SEP = re.compile(r"[\s:\-.]")
_HEX = re.compile(r"^[0-9a-f]{6,12}$")


def _invalid(loc: str, msg: str) -> ApiError:
    return ApiError(
        422, "validation_error", "Invalid search", [{"loc": ["query", loc], "msg": msg}]
    )


def normalize_query(q: str | None, ip: str | None) -> dict[str, str | None]:
    """-> {mac, mac_prefix, ip}. Exactly one of q / ip; q = 6-12 hex digits after removing
    separators (12 = exact MAC, else a prefix); ip = IPv4."""
    if (q is None or q == "") == (ip is None or ip == ""):
        raise _invalid("q", "exactly one of q and ip is required")
    if q:
        digits = _SEP.sub("", q.strip().lower())
        if not _HEX.match(digits):
            raise _invalid("q", "must be a MAC address or 6-12 hex digits")
        if len(digits) == 12:
            mac = ":".join(digits[i : i + 2] for i in range(0, 12, 2))
            return {"mac": mac, "mac_prefix": None, "ip": None}
        return {"mac": None, "mac_prefix": digits, "ip": None}
    try:
        addr = ipaddress.IPv4Address((ip or "").strip())
    except ValueError:
        raise _invalid("ip", "must be an IPv4 address") from None
    return {"mac": None, "mac_prefix": None, "ip": str(addr)}


async def search_mac(session: Any, q: str | None, ip: str | None) -> dict[str, Any]:
    query = normalize_query(q, ip)
    arp_rows: list[dict[str, Any]] = []
    if query["ip"] is not None:
        arp_rows = [
            dict(r)
            for r in (
                await session.execute(
                    text(
                        "SELECT a.*, d.name AS source_device_name FROM arp_entries a "
                        "JOIN devices d ON d.id = a.source_device_id WHERE a.ip = :ip "
                        "ORDER BY a.present DESC, a.last_seen DESC, a.source_device_id"
                    ),
                    {"ip": query["ip"]},
                )
            ).mappings()
        ]
        macs = list(dict.fromkeys(r["mac"] for r in arp_rows))
        cond, params = "pm.mac = ANY(:macs)", {"macs": macs}
    elif query["mac"] is not None:
        cond, params = "pm.mac = :mac", {"mac": query["mac"]}
    else:
        cond, params = "replace(pm.mac, ':', '') LIKE :p", {"p": f"{query['mac_prefix']}%"}
    rows = [
        dict(r)
        for r in (
            await session.execute(
                text(
                    "SELECT pm.*, d.name AS device_name, d.host AS device_host, "
                    "sp.name AS port, sp.descr AS port_descr, sp.alias AS port_alias, "
                    "sp.mac_count FROM port_macs pm JOIN devices d ON d.id = pm.device_id "
                    "LEFT JOIN switch_ports sp ON sp.device_id = pm.device_id "
                    f"AND sp.if_index = pm.if_index WHERE {cond} "
                    "ORDER BY pm.present DESC, pm.last_seen DESC LIMIT 2000"
                ),
                params,
            )
        ).mappings()
    ]
    inv = await load_inventory(session)
    ctx = await load_context(session, inv, sorted({r["device_id"] for r in rows}))
    det = await mac_details(session, [r["mac"] for r in rows], ctx.reverse_dns)
    items = []
    for r in rows:
        port_row = {
            "if_index": r["if_index"],
            "name": r["port"] or "",
            "descr": r["port_descr"] or "",
        }
        is_up, _ = view.uplink(
            neighbors_of(ctx, r["device_id"], port_row), int(r["mac_count"] or 0), ctx.threshold
        )
        d = det.get(r["mac"], {})
        items.append(
            {
                "mac": r["mac"],
                "vendor": oui.vendor(r["mac"]),
                "locally_administered": locally_administered(r["mac"]),
                "ip": d.get("ip"),
                "ips": list(d.get("ips") or []),
                "hostname": d.get("hostname"),
                "device_id": r["device_id"],
                "device_name": r["device_name"],
                "device_host": r["device_host"],
                "if_index": r["if_index"],
                "port": r["port"] or str(r["if_index"]),
                "port_alias": r["port_alias"] or "",
                "vlan": r["vlan"],
                "is_uplink": is_up,
                "present": bool(r["present"]),
                "first_seen": iso(r["first_seen"]),
                "last_seen": iso(r["last_seen"]),
                "best": False,
                "_ts": r["last_seen"].timestamp(),
            }
        )

    def order(i: dict[str, Any]) -> tuple[Any, ...]:
        return (not i["present"], i["is_uplink"], -i["_ts"], i["device_name"], i["if_index"])

    items.sort(key=order)
    seen: set[str] = set()
    for i in items:
        if i["mac"] in seen:
            continue
        seen.add(i["mac"])
        i["best"] = i["present"] and not i["is_uplink"]
    items.sort(key=lambda i: (not i["best"], *order(i)))
    items = [{k: v for k, v in i.items() if k != "_ts"} for i in items[:MAX_ITEMS]]

    if query["ip"] is None:
        macs = sorted({i["mac"] for i in items})
        if macs:
            arp_rows = [
                dict(r)
                for r in (
                    await session.execute(
                        text(
                            "SELECT a.*, d.name AS source_device_name FROM arp_entries a "
                            "JOIN devices d ON d.id = a.source_device_id WHERE a.mac = ANY(:m) "
                            "ORDER BY a.present DESC, a.last_seen DESC, a.ip"
                        ),
                        {"m": macs},
                    )
                ).mappings()
            ]
    names: dict[str, str | None] = {}
    if arp_rows and ctx.reverse_dns:
        for r in await session.execute(
            text("SELECT ip, name FROM dns_cache WHERE ip = ANY(:ips)"),
            {"ips": sorted({a["ip"] for a in arp_rows})},
        ):
            names[r.ip] = r.name
    arp = [
        {
            "ip": a["ip"],
            "mac": a["mac"],
            "hostname": names.get(a["ip"]),
            "source_device_id": a["source_device_id"],
            "source_device_name": a["source_device_name"],
            "present": bool(a["present"]),
            "first_seen": iso(a["first_seen"]),
            "last_seen": iso(a["last_seen"]),
        }
        for a in arp_rows[:MAX_ARP]
    ]
    return {"query": query, "items": items, "arp": arp}
