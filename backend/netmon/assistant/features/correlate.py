"""[3b] Alert correlation and incidents (`ai_correlation`, architecture § 14.8.2).

`CorrelationService.on_events(events)` is called after status_change events are committed
(`netmon.services.events.published`, never in the alert send path). A bounded queue feeds one
worker; each transition is processed in ≤ a few queries (the dependency/topology graph is
cached and refreshed every 60 s or on `invalidate()`). The AI summary (light model) runs in its
own background task and never blocks the grouping."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from netmon.assistant.features import common
from netmon.assistant.features.correlate_pure import (
    PROBLEM_STATUSES,
    Graph,
    Problem,
    cluster,
    growth_due,
    joins,
    root_candidates,
    title_en,
    title_fields,
)

__all__ = ["CorrelationService", "cluster", "incident_out", "member_out"]

log = logging.getLogger("netmon.assistant.correlate")

GRAPH_TTL_S = 60.0
STALE_AFTER = timedelta(hours=6)
SUMMARY_SPACING_S = 120.0
LOOKBACK = timedelta(minutes=30)
QUEUE_MAX = 2000
INC_COLS = (
    "id, state, opened_at, last_change_at, resolved_at, title, title_fields, root_device_id, "
    "root_candidates, ai_title, ai_summary, ai_root_guess, ai_summaries, member_count, note_sent"
)

SUMMARY_TOOL = {
    "name": "report_incident_summary",
    "description": "Report the incident summary.",
    "inputSchema": {
        "json": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "summary"],
            "properties": {
                "title": {"type": "string", "maxLength": 80},
                "summary": {"type": "string"},
                "root_cause_guess": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "device_id": {"type": "integer"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                },
            },
        }
    },
}
SUMMARY_BLOCK = (
    "Task: summarise a group of correlated monitoring alerts (an incident) for the network "
    "administrators. Use only the data given. Output with the tool report_incident_summary: "
    "`title` (≤ 80 characters), `summary` (≤ 3 sentences: what failed, what is affected, what "
    "the timeline suggests) and `root_cause_guess` (`device_id` must be one of the member "
    "devices; confidence low/medium/high; omit it when unclear). Mark guesses as hypotheses."
)


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return v.isoformat().replace("+00:00", "Z")


def _parse(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def incident_out(
    r: Any, names: dict[int, str], counts: dict[str, int], generated_at: Any = None
) -> dict[str, Any]:
    tf = common.jsonb(r.title_fields) or {}
    guess = common.jsonb(r.ai_root_guess)
    ai = None
    if r.ai_summary:
        ai = {
            "title": r.ai_title or "",
            "summary": r.ai_summary,
            "root_guess": guess if isinstance(guess, dict) and guess.get("device_id") else None,
            "generated_at": _iso(generated_at) or _iso(r.last_change_at),
        }
    root = r.root_device_id
    return {
        "id": r.id,
        "state": r.state,
        "opened_at": _iso(r.opened_at),
        "last_change_at": _iso(r.last_change_at),
        "resolved_at": _iso(r.resolved_at),
        "title": r.title,
        "title_fields": tf,
        "root_device": {"id": root, "name": names.get(root, "")} if root is not None else None,
        "root_candidates": [{"id": d, "name": names.get(d, "")} for d in (r.root_candidates or [])],
        "ai": ai,
        "member_count": r.member_count,
        "down": counts.get("down", 0),
        "unreachable": counts.get("unreachable", 0),
        "warning": counts.get("warning", 0),
        "recovered": counts.get("recovered", 0),
    }


def member_out(m: Any, root: int | None) -> dict[str, Any]:
    return {
        "event_id": m.event_id,
        "sensor_id": m.sensor_id,
        "sensor_name": getattr(m, "sensor_name", None),
        "device_id": m.device_id,
        "device_name": getattr(m, "device_name", None),
        "status": m.status,
        "time": _iso(m.time),
        "recovered_at": _iso(m.recovered_at),
        "is_root": m.device_id is not None and m.device_id == root,
    }


async def incident_counts(session: Any, ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {i: {} for i in ids}
    if not ids:
        return out
    for r in await session.execute(
        text(
            "SELECT incident_id, status, (recovered_at IS NOT NULL) AS rec, count(*) AS n "
            "FROM assistant_incident_members WHERE incident_id = ANY(:ids) "
            "GROUP BY incident_id, status, rec"
        ),
        {"ids": ids},
    ):
        d = out.setdefault(r.incident_id, {})
        if r.rec:
            d["recovered"] = d.get("recovered", 0) + r.n
        else:
            d[r.status] = d.get(r.status, 0) + r.n
    return out


async def device_names(session: Any, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {
        r.id: r.name
        for r in await session.execute(
            text("SELECT id, name FROM devices WHERE id = ANY(:ids)"), {"ids": list(ids)}
        )
    }


class CorrelationService:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.on = False
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(QUEUE_MAX)
        self._worker: asyncio.Task[None] | None = None
        self._stale_task: asyncio.Task[None] | None = None
        self._graph: Graph | None = None
        self._graph_at = 0.0
        self._summary_tasks: set[asyncio.Task[Any]] = set()
        self._summary_at: dict[int, float] = {}
        self._summarized_count: dict[int, int] = {}
        self._idle = asyncio.Event()
        self._idle.set()
        self.last_status: str | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.on:
            return
        self.on = True
        self._worker = asyncio.create_task(self._run(), name="netmon-ai-correlate")
        self._stale_task = asyncio.create_task(self._stale_loop(), name="netmon-ai-stale")

    async def stop(self, *, mark_stale: bool = True) -> None:
        if not self.on:
            return
        self.on = False
        for t in (self._worker, self._stale_task, *self._summary_tasks):
            if t is not None:
                t.cancel()
        for t in (self._worker, self._stale_task, *list(self._summary_tasks)):
            if t is not None:
                with contextlib.suppress(BaseException):
                    await t
        self._worker = self._stale_task = None
        self._summary_tasks.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._idle.set()
        if mark_stale:
            with contextlib.suppress(Exception):
                async with self.rt.sessionmaker() as session:
                    await session.execute(
                        text("UPDATE assistant_incidents SET state = 'stale' WHERE state = 'open'")
                    )
                    await session.commit()

    def invalidate(self) -> None:
        self._graph = None

    async def idle(self) -> None:
        """Tests: wait until the queue and the summaries are processed."""
        while True:
            await self._queue.join()
            pending = [t for t in self._summary_tasks if not t.done()]
            if not pending and self._queue.empty():
                return
            await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------ input
    def on_events(self, events: list[dict[str, Any]]) -> None:
        if not self.on:
            return
        for ev in events:
            if ev.get("kind") != "status_change" or ev.get("sensor_id") is None:
                continue
            if ev.get("old_status") == ev.get("new_status"):
                continue
            try:
                self._queue.put_nowait(ev)
            except asyncio.QueueFull:
                log.warning("Correlation queue full; transition %s skipped", ev.get("id"))

    async def _run(self) -> None:
        while True:
            ev = await self._queue.get()
            try:
                await self.process(ev)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Correlating event %s failed", ev.get("id"))
            finally:
                self._queue.task_done()

    async def _stale_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            with contextlib.suppress(Exception):
                await self.mark_stale()

    async def mark_stale(self) -> int:
        now = self.rt.clock.now()
        async with self.rt.sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "UPDATE assistant_incidents SET state = 'stale' WHERE state = 'open' "
                        f"AND last_change_at < :cut RETURNING {INC_COLS}"
                    ),
                    {"cut": now - STALE_AFTER},
                )
            ).all()
            await session.commit()
        for r in rows:
            await self._broadcast(r.id)
        return len(rows)

    # ------------------------------------------------------------ graph
    async def graph(self, session: Any) -> Graph:
        if self._graph is not None and time.monotonic() - self._graph_at < GRAPH_TTL_S:
            return self._graph
        from netmon.services import dependencies
        from netmon.services.status import load_inventory

        inv = await load_inventory(session)
        parents: dict[int, int | None] = {}
        for did in inv.devices:
            eff = dependencies.effective_parent(inv, did)
            parents[did] = eff.device_id if eff is not None else None
        links = [
            (r.a_device_id, r.b_device_id)
            for r in await session.execute(
                text(
                    "SELECT a_device_id, b_device_id FROM topology_links "
                    "WHERE b_device_id IS NOT NULL AND NOT stale"
                )
            )
        ]
        names = {did: d["name"] for did, d in inv.devices.items()}
        self._graph = Graph.build(parents, links, names)
        self._graph_at = time.monotonic()
        return self._graph

    # ------------------------------------------------------------ processing
    async def process(self, ev: dict[str, Any]) -> int | None:
        """One transition. Returns the incident id it touched (tests)."""
        new = ev.get("new_status")
        t = _parse(ev.get("time")) if ev.get("time") else self.rt.clock.now()
        if new in PROBLEM_STATUSES:
            return await self._problem(ev, t)
        return await self._recovered(ev, t)

    async def _settings(self, session: Any) -> tuple[float, int]:
        window = await common.int_setting(session, "correlation_window_min")
        min_alerts = await common.int_setting(session, "correlation_min_alerts")
        return max(1, min(30, window)) * 60.0, max(2, min(50, min_alerts))

    async def _problem(self, ev: dict[str, Any], t: datetime) -> int | None:
        p = Problem(int(ev["id"]), ev.get("device_id"), ev.get("sensor_id"), ev["new_status"], t)
        async with self.rt.sessionmaker() as session:
            window_s, min_alerts = await self._settings(session)
            g = await self.graph(session)
            # 1. grow an open incident
            incs = (
                await session.execute(
                    text(
                        f"SELECT {INC_COLS} FROM assistant_incidents WHERE state = 'open' "
                        "AND last_change_at >= :since ORDER BY opened_at"
                    ),
                    {"since": t - timedelta(seconds=window_s)},
                )
            ).all()
            for inc in incs:
                members = await self._members(session, inc.id)
                if any(m.event_id == p.event_id for m in members):
                    return inc.id
                if joins(p, members, inc.last_change_at, g, window_s):
                    await self._add_members(session, inc.id, [p])
                    all_members = [*members, p]
                    await self._update(session, inc.id, all_members, g, t)
                    await session.commit()
                    await self._broadcast(inc.id)
                    self._maybe_summary(inc.id, len(all_members))
                    return int(inc.id)
            # 2. a new incident from recent unclustered problems
            rows = (
                await session.execute(
                    text(
                        "SELECT e.id, e.device_id, e.sensor_id, e.new_status, e.time FROM events e "
                        "WHERE e.kind = 'status_change' AND e.new_status IN "
                        "('down','unreachable','warning') AND e.time >= :since "
                        "AND e.time <= :until AND e.sensor_id IS NOT NULL AND NOT EXISTS "
                        "(SELECT 1 FROM assistant_incident_members m WHERE m.event_id = e.id) "
                        "AND EXISTS (SELECT 1 FROM sensors s WHERE s.id = e.sensor_id "
                        "AND s.status IN ('down','unreachable','warning')) "
                        "ORDER BY e.time LIMIT 2000"
                    ),
                    {"since": t - LOOKBACK, "until": t + timedelta(seconds=1)},
                )
            ).all()
            probs = {
                r.id: Problem(r.id, r.device_id, r.sensor_id, r.new_status, r.time) for r in rows
            }
            probs[p.event_id] = p
            groups = cluster(list(probs.values()), g, window_s, min_alerts)
            mine = next(
                (c for c in groups if any(m.event_id == p.event_id for m in c.members)), None
            )
            if mine is None:
                return None
            members = mine.members
            roots = root_candidates(members, g)
            root = roots[0] if roots else None
            tf = title_fields(members, root, g)
            first = min(m.time for m in members)
            inc_id = (
                await session.execute(
                    text(
                        "INSERT INTO assistant_incidents (state, opened_at, last_change_at, title, "
                        "title_fields, root_device_id, root_candidates, member_count) VALUES "
                        "('open', :o, :l, :title, CAST(:tf AS jsonb), :root, :cands, :n) "
                        "RETURNING id"
                    ),
                    {
                        "o": first,
                        "l": mine.last_change,
                        "title": title_en(tf),
                        "tf": _json(tf),
                        "root": root,
                        "cands": roots[:10],
                        "n": len(members),
                    },
                )
            ).scalar_one()
            await self._add_members(session, inc_id, members)
            await session.commit()
        await self._broadcast(inc_id)
        self._maybe_summary(inc_id, len(members))
        return int(inc_id)

    async def _members(self, session: Any, incident_id: int) -> list[Any]:
        rows = (
            await session.execute(
                text(
                    "SELECT event_id, device_id, sensor_id, status, time, recovered_at "
                    "FROM assistant_incident_members WHERE incident_id = :i ORDER BY time"
                ),
                {"i": incident_id},
            )
        ).all()
        return [Problem(r.event_id, r.device_id, r.sensor_id, r.status, r.time) for r in rows]

    async def _add_members(self, session: Any, incident_id: int, members: list[Problem]) -> None:
        for m in members:
            await session.execute(
                text(
                    "INSERT INTO assistant_incident_members (incident_id, event_id, sensor_id, "
                    "device_id, status, time) VALUES (:i, :e, :s, :d, :st, :t) "
                    "ON CONFLICT DO NOTHING"
                ),
                {
                    "i": incident_id,
                    "e": m.event_id,
                    "s": m.sensor_id,
                    "d": m.device_id,
                    "st": m.status,
                    "t": m.time,
                },
            )

    async def _update(
        self, session: Any, incident_id: int, members: list[Problem], g: Graph, t: datetime
    ) -> None:
        roots = root_candidates(members, g)
        root = roots[0] if roots else None
        tf = title_fields(members, root, g)
        await session.execute(
            text(
                "UPDATE assistant_incidents SET last_change_at = greatest(last_change_at, :t), "
                "member_count = :n, root_device_id = :root, root_candidates = :cands, "
                "title = :title, title_fields = CAST(:tf AS jsonb) WHERE id = :i"
            ),
            {
                "t": t,
                "n": len(members),
                "root": root,
                "cands": roots[:10],
                "title": title_en(tf),
                "tf": _json(tf),
                "i": incident_id,
            },
        )

    async def _recovered(self, ev: dict[str, Any], t: datetime) -> int | None:
        sid = ev.get("sensor_id")
        if sid is None:
            return None
        async with self.rt.sessionmaker() as session:
            touched = (
                (
                    await session.execute(
                        text(
                            "UPDATE assistant_incident_members m SET recovered_at = :t FROM "
                            "assistant_incidents i WHERE m.incident_id = i.id AND i.state = 'open' "
                            "AND m.sensor_id = :s AND m.recovered_at IS NULL RETURNING "
                            "m.incident_id"
                        ),
                        {"t": t, "s": sid},
                    )
                )
                .scalars()
                .all()
            )
            ids = sorted(set(touched))
            for iid in ids:
                left = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM assistant_incident_members WHERE incident_id = "
                            ":i "
                            "AND recovered_at IS NULL"
                        ),
                        {"i": iid},
                    )
                ).scalar()
                if not left:
                    await session.execute(
                        text(
                            "UPDATE assistant_incidents SET state = 'resolved', resolved_at = :t, "
                            "last_change_at = :t WHERE id = :i"
                        ),
                        {"t": t, "i": iid},
                    )
                else:
                    await session.execute(
                        text(
                            "UPDATE assistant_incidents SET last_change_at = "
                            "greatest(last_change_at, :t) WHERE id = :i"
                        ),
                        {"t": t, "i": iid},
                    )
            await session.commit()
        for iid in ids:
            await self._broadcast(iid)
        return ids[0] if ids else None

    # ------------------------------------------------------------ output
    async def load(self, session: Any, incident_id: int) -> dict[str, Any] | None:
        r = (
            await session.execute(
                text(f"SELECT {INC_COLS} FROM assistant_incidents WHERE id = :i"),
                {"i": incident_id},
            )
        ).first()
        if r is None:
            return None
        names = await device_names(session, {r.root_device_id, *(r.root_candidates or [])})
        counts = (await incident_counts(session, [r.id]))[r.id]
        gen = (
            await session.execute(
                text(
                    "SELECT max(created_at) FROM assistant_artifacts WHERE kind = "
                    "'incident_summary' "
                    "AND key LIKE :k AND status = 'ok'"
                ),
                {"k": f"incident:{r.id}:%"},
            )
        ).scalar()
        return incident_out(r, names, counts, gen)

    async def _broadcast(self, incident_id: int) -> None:
        try:
            async with self.rt.sessionmaker() as session:
                inc = await self.load(session, incident_id)
            if inc is not None:
                self.rt.hub.broadcast("incident.update", {"incident": inc}, admin_only=True)
        except Exception:  # noqa: BLE001
            log.debug("incident broadcast failed", exc_info=True)

    # ------------------------------------------------------------ AI summary
    def _maybe_summary(self, incident_id: int, member_count: int, force: bool = False) -> None:
        if not common.on(self.rt, "assistant"):
            return
        if not force:
            last = self._summary_at.get(incident_id)
            if last is not None and time.monotonic() - last < SUMMARY_SPACING_S:
                return
            done = self._summarized_count.get(incident_id, 0)
            if done and not growth_due(member_count, done, 1):
                return
        task = asyncio.create_task(self.summarize(incident_id, force=force))
        self._summary_tasks.add(task)
        task.add_done_callback(self._summary_tasks.discard)

    async def summarize(self, incident_id: int, *, force: bool = False) -> str:
        """Generate one AI summary. Returns a status (`ok` or a skip reason). Never raises."""
        rt = self.rt
        try:
            async with rt.sessionmaker() as session:
                r = (
                    await session.execute(
                        text(f"SELECT {INC_COLS} FROM assistant_incidents WHERE id = :i"),
                        {"i": incident_id},
                    )
                ).first()
                if r is None:
                    return "not_found"
                if r.ai_summaries >= 5:
                    return "cap"
                if (
                    not force
                    and r.ai_summaries > 0
                    and not growth_due(
                        r.member_count,
                        self._summarized_count.get(incident_id, r.member_count),
                        r.ai_summaries,
                    )
                ):
                    return "not_due"
                cap = await common.int_setting(session, "correlation_daily_summaries")
                used = await common.purpose_count_today(
                    rt, session, "incident_summary", status=None
                )
                if used >= cap:
                    await self._skip(incident_id, r.ai_summaries + 1, "cap")
                    return "cap"
                facts = await self._facts(session, r)
                a = await common.assistant_settings(session)
                policy = common.redact_policy(a)
                system = await common.system_prompt(
                    rt,
                    "incident",
                    SUMMARY_BLOCK,
                    redaction=bool(a.get("redact_internal_ips")),
                    session=session,
                )
            self._summary_at[incident_id] = time.monotonic()
            facts = common.redact_value(facts, policy)
            comp = await common.complete(
                rt,
                purpose="incident",
                system=system,
                messages=[common.json_message("Incident data (JSON):", facts)],
                light=True,
                max_tokens=400,
                timeout_s=60,
                tools=[SUMMARY_TOOL],
                tool_choice=SUMMARY_TOOL["name"],
            )
            out = comp.tool_input if isinstance(comp.tool_input, dict) else {}
            title = str(out.get("title") or "").strip()[:80]
            summary = str(common.detokenize(str(out.get("summary") or ""), policy)).strip()[:1200]
            title = str(common.detokenize(title, policy))
            if not summary:
                raise common.AiError("invalid_output", "The model returned no summary")
            guess = (
                out.get("root_cause_guess")
                if isinstance(out.get("root_cause_guess"), dict)
                else None
            )
            member_devices = {m["device_id"] for m in facts.get("members", [])}
            if guess:
                try:
                    did = int(guess.get("device_id"))
                except (TypeError, ValueError):
                    did = None
                conf = (
                    guess.get("confidence")
                    if guess.get("confidence") in ("low", "medium", "high")
                    else "low"
                )
                guess = {"device_id": did, "confidence": conf} if did in member_devices else None
            async with rt.sessionmaker() as session:
                n = (
                    await session.execute(
                        text(
                            "UPDATE assistant_incidents SET ai_title = :t, ai_summary = :s, "
                            "ai_root_guess = CAST(:g AS jsonb), ai_summaries = ai_summaries + 1 "
                            "WHERE id = :i RETURNING ai_summaries, member_count"
                        ),
                        {
                            "t": title or None,
                            "s": summary,
                            "g": _json(guess) if guess else None,
                            "i": incident_id,
                        },
                    )
                ).one()
                await common.put_artifact(
                    session,
                    "incident_summary",
                    f"incident:{incident_id}:{n.ai_summaries}",
                    "ok",
                    content=summary,
                    data={"title": title, "root_guess": guess},
                    comp=comp,
                )
                await session.commit()
            self._summarized_count[incident_id] = n.member_count
            self.last_status = "ok"
            await common.audit_system(
                rt,
                "assistant.incident_summary",
                "incident",
                incident_id,
                {"incident_id": incident_id, "status": "ok", **common.usage_changes(comp)},
            )
            await self._broadcast(incident_id)
            if await common.setting_bool(rt, "correlation_attach_to_alert"):
                notes = getattr(getattr(rt, "ai_features", None), "notes", None)
                if notes is not None:
                    await notes.incident_note(incident_id)
            return "ok"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - degrade silently
            err = common.to_ai_error(exc)
            self.last_status = err.code
            log.info("Incident %s summary skipped: %s", incident_id, err.code)
            await common.audit_system(
                rt,
                "assistant.incident_summary",
                "incident",
                incident_id,
                {"incident_id": incident_id, "status": "skipped", "skip_reason": err.code},
            )
            return err.code

    async def _skip(self, incident_id: int, n: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            async with self.rt.sessionmaker() as session:
                await common.put_artifact(
                    session,
                    "incident_summary",
                    f"incident:{incident_id}:{n}",
                    "skipped",
                    skip_reason=reason,
                )
                await session.commit()

    async def _facts(self, session: Any, r: Any) -> dict[str, Any]:
        """Members, roots, edges among members, maintenance, config changes and flaps of the
        member devices in the last 60 min (the summary input of § 14.8.2)."""
        rows = (
            await session.execute(
                text(
                    "SELECT m.event_id, m.device_id, m.sensor_id, m.status, m.time, "
                    "m.recovered_at, "
                    "d.name AS device_name, s.name AS sensor_name, left(e.message, 200) AS message "
                    "FROM assistant_incident_members m LEFT JOIN devices d ON d.id = m.device_id "
                    "LEFT JOIN sensors s ON s.id = m.sensor_id LEFT JOIN events e ON e.id = "
                    "m.event_id "
                    "WHERE m.incident_id = :i ORDER BY m.time LIMIT 200"
                ),
                {"i": r.id},
            )
        ).all()
        devs = sorted({x.device_id for x in rows if x.device_id is not None})
        g = await self.graph(session)
        edges = []
        for a in devs:
            p = g.parents.get(a)
            if p in devs:
                edges.append({"kind": "dependency", "parent": p, "child": a})
            for b in g.neighbours_of(a):
                if b in devs and a < b:
                    edges.append({"kind": "topology", "a": a, "b": b})
        since = r.opened_at - timedelta(minutes=60)
        cfg = []
        flaps = []
        maint = []
        with contextlib.suppress(Exception):
            cfg = [
                {
                    "device_id": c.device_id,
                    "time": _iso(c.collected_at),
                    "added": c.added,
                    "removed": c.removed,
                }
                for c in await session.execute(
                    text(
                        "SELECT device_id, collected_at, added, removed FROM config_versions "
                        "WHERE device_id = ANY(:d) AND collected_at >= :s AND NOT initial "
                        "ORDER BY collected_at DESC LIMIT 20"
                    ),
                    {"d": devs, "s": since},
                )
            ]
        with contextlib.suppress(Exception):
            flaps = [
                {
                    "device_id": f.device_id,
                    "port": f.port_name,
                    "started_at": _iso(f.started_at),
                    "transitions": f.transitions,
                }
                for f in await session.execute(
                    text(
                        "SELECT device_id, port_name, started_at, transitions FROM "
                        "port_flap_episodes "
                        "WHERE device_id = ANY(:d) AND started_at >= :s ORDER BY started_at DESC "
                        "LIMIT 20"
                    ),
                    {"d": devs, "s": since},
                )
            ]
        with contextlib.suppress(Exception):
            maint = [
                {"device_id": e.device_id, "time": _iso(e.time)}
                for e in await session.execute(
                    text(
                        "SELECT DISTINCT device_id, max(time) AS time FROM events WHERE "
                        "device_id = ANY(:d) AND in_maintenance AND time >= :s GROUP BY device_id"
                    ),
                    {"d": devs, "s": since},
                )
            ]
        return {
            "deterministic_title": r.title,
            "opened_at": _iso(r.opened_at),
            "members": [
                {
                    "device_id": x.device_id,
                    "device": x.device_name,
                    "sensor": x.sensor_name,
                    "status": x.status,
                    "time": _iso(x.time),
                    "recovered_at": _iso(x.recovered_at),
                    "message": x.message or "",
                }
                for x in rows
            ],
            "root_candidates": [
                {"id": d, "name": g.names.get(d, "")} for d in (r.root_candidates or [])
            ],
            "edges": edges,
            "maintenance": maint,
            "config_changes": cfg,
            "port_flaps": flaps,
        }


def _json(v: Any) -> str:
    import json

    return json.dumps(v, ensure_ascii=False, default=str)
