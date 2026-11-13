"""[1.3] LAG degraded (architecture "[1.3] Trunk / LAG alerts").

Part of the built-in link-down behaviour (no own toggle): active while `event_alerts` is ON and
the built-in `trap_link_down` rule is enabled and in scope for the device. When some but not
all members of a LAG are up, the device's `trap` event sensor gets a condition (source
`lag_degraded`, key = the LAG ifIndex, status warning, message `LAG <name> degraded (x of n
members up)`); it is removed when all members are up again, and also when every member is down
(a LAG that is fully down is the existing link-down alert). It expires like the link-down rule
(`clear_after_min` since the last refresh). Maintenance, acknowledge and notifications work as
for every other condition (the sensor status path is the same).

Inputs: member linkDown / linkUp traps (membership from the stored `switch_ports.lag_if_index`,
member states = the stored ifOperStatus overridden by the traps seen since), and a reconcile
after every successful port inventory run (fresh ifOperStatus; the trap overrides are reset)."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from netmon.ports import view

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.ingest.lag")

SOURCE = "lag_degraded"
BUILTIN_KEY = "trap_link_down"
MAX_LIVE = 10000


def degraded(up: int, total: int) -> bool:
    return total > 0 and 0 < up < total


def message(name: str, up: int, total: int) -> str:
    return f"LAG {name} degraded ({up} of {total} members up)"


def lag_states(
    rows: list[dict[str, Any]], live: dict[int, bool] | None = None
) -> dict[int, tuple[str, int, int]]:
    """{LAG ifIndex: (name, members up, members)} for the LAGs with at least one member.
    `live`: member ifIndex -> up from traps (overrides the stored oper_status)."""
    live = live or {}
    by_idx = {int(r["if_index"]): r for r in rows}
    members, _ = view.lag_groups(rows)
    out: dict[int, tuple[str, int, int]] = {}
    for lag, ms in members.items():
        if not ms:
            continue
        up = sum(1 for m in ms if live.get(m, str(by_idx[m].get("oper_status") or "") == "up"))
        out[lag] = (str(by_idx[lag].get("name") or lag), up, len(ms))
    return out


class LagMonitor:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.live: OrderedDict[tuple[int, int], bool] = OrderedDict()
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- gate
    def rule(self, device_id: int) -> Any:
        """The built-in link-down rule when the LAG check is active for the device."""
        ingest = getattr(self.rt, "ingest", None)
        es = getattr(self.rt, "event_sensors", None)
        if ingest is None or es is None or not ingest.alerts_on:
            return None
        for r in es.rules_for("trap", device_id):
            if r.builtin_key == BUILTIN_KEY:
                return r
        return None

    # ---------------------------------------------------------------- inputs
    def note_trap(self, device_id: int, if_index: int, up: bool) -> None:
        """A linkDown / linkUp trap (synchronous: keeps the order of the traps); the LAG check
        runs in the background."""
        key = (device_id, if_index)
        self.live[key] = up
        self.live.move_to_end(key)
        while len(self.live) > MAX_LIVE:
            self.live.popitem(last=False)
        if self.rule(device_id) is not None:
            self.rt.spawn(self.check(device_id, if_index))

    async def _rows(self, device_id: int) -> list[dict[str, Any]]:
        async with self.rt.sessionmaker() as session:
            return [
                dict(r)
                for r in (
                    await session.execute(
                        text(
                            "SELECT if_index, name, kind, lag_if_index, oper_status "
                            "FROM switch_ports WHERE device_id = :d"
                        ),
                        {"d": device_id},
                    )
                ).mappings()
            ]

    def _live_of(self, device_id: int) -> dict[int, bool]:
        return {idx: up for (d, idx), up in self.live.items() if d == device_id}

    async def check(self, device_id: int, if_index: int) -> None:
        """After a trap on `if_index`: re-evaluate its LAG (when it is a LAG member)."""
        try:
            async with self._lock:
                rule = self.rule(device_id)
                if rule is None:
                    return
                rows = await self._rows(device_id)
                _, lag_of = view.lag_groups(rows)
                lag = lag_of.get(if_index)
                if lag is None:
                    return
                states = lag_states(rows, self._live_of(device_id))
                if lag in states:
                    await self._apply(device_id, rule, {lag: states[lag]}, full=False)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("LAG check of device %s failed", device_id)

    async def reconcile(self, device_id: int) -> None:
        """After a successful port inventory run: every LAG of the device from the fresh
        ifOperStatus (the trap overrides of the device are dropped)."""
        try:
            async with self._lock:
                for key in [k for k in self.live if k[0] == device_id]:
                    del self.live[key]
                rule = self.rule(device_id)
                es = self.rt.event_sensors
                if rule is None:
                    if es is not None:
                        await es.remove_device_conditions(device_id, "trap", [SOURCE])
                    return
                states = lag_states(await self._rows(device_id))
                await self._apply(device_id, rule, states, full=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("LAG reconcile of device %s failed", device_id)

    # ---------------------------------------------------------------- conditions
    async def _apply(
        self,
        device_id: int,
        rule: Any,
        states: dict[int, tuple[str, int, int]],
        *,
        full: bool,
    ) -> None:
        es = self.rt.event_sensors
        if es is None:
            return
        now = self.rt.clock.now()
        exp = now + timedelta(minutes=int(rule.clear_after_min)) if rule.clear_after_min else None
        raise_items = [
            (SOURCE, str(lag), "warning", message(name, up, n), exp)
            for lag, (name, up, n) in sorted(states.items())
            if degraded(up, n)
        ]
        ok_keys = [str(lag) for lag, (_, up, n) in states.items() if not degraded(up, n)]
        if full:  # LAGs that no longer exist lose their condition too
            keep = {it[1] for it in raise_items}
            await es.remove_device_conditions(
                device_id,
                "trap",
                [SOURCE],
                keys=None if not keep else await self._stale_keys(device_id, keep),
            )
        elif ok_keys:
            await es.remove_device_conditions(device_id, "trap", [SOURCE], keys=ok_keys)
        if raise_items:
            await es.set_conditions(device_id, "trap", raise_items)

    async def _stale_keys(self, device_id: int, keep: set[str]) -> list[str]:
        async with self.rt.sessionmaker() as session:
            keys = (
                await session.execute(
                    text(
                        "SELECT c.key FROM event_conditions c JOIN sensors s ON s.id = c.sensor_id "
                        "WHERE s.device_id = :d AND s.type = 'trap' AND c.source = :src"
                    ),
                    {"d": device_id, "src": SOURCE},
                )
            ).scalars()
            return [k for k in keys if k not in keep]
