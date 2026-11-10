"""[3b] Usage counters and caps (architecture § 14.6.3, data-model `assistant_usage`).

Interface (`rt.assistant.usage`):

    await usage.check_and_reserve(user_id, "ask")             # before a question / AI job
        # raises AssistantError("assistant_cap_reached", details=[{"cap", "limit", "used",
        # "resets_at"}]); with count_question=True (default) it also counts the question.
        # purpose_daily_cap=N: per-purpose daily cap (counts `questions` of that purpose today,
        # all users) — used by the AI features (alert notes, correlation summaries, …).
    cost = await usage.record(user_id, "ask", usage_dict, model_calls=1, tool_calls=0)
        # after every model call (tokens; cost estimate at the current prices → Decimal|None)
    await usage.record_error(user_id, purpose, code)          # errors / throttles counters
    await usage.background_allowed()                          # False at the background stop %
    await usage.month_summary()                               # UsageMonthSummary (api.md)
    await usage.report("2026-11")                             # UsageReport (api.md)

user_id 0 = system (background purposes). Days are local dates in `settings.timezone`."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from netmon.assistant import config, pricing
from netmon.assistant.errors import AssistantError

PURPOSES = (
    "ask", "explain", "incident", "alert_note", "diff_explain", "digest", "rule_draft",
    "template_draft", "chat_ask", "report_summary", "test",
)  # fmt: skip
BACKGROUND_PURPOSES = frozenset({"alert_note", "incident", "digest", "report_summary"})
# purposes counted against the per-admin daily question cap (§ 14.6.3)
QUESTION_PURPOSES = frozenset({"ask", "explain", "rule_draft", "template_draft", "chat_ask"})
SYSTEM_USER = 0


# ---------------------------------------------------------------- pure
def cap_tokens(row: dict[str, Any]) -> int:
    """input + output + cache_write + ceil(cache_read / 10)."""
    return (
        int(row.get("input_tokens") or 0)
        + int(row.get("output_tokens") or 0)
        + int(row.get("cache_write_tokens") or 0)
        + math.ceil(int(row.get("cache_read_tokens") or 0) / 10)
    )


def is_background(purpose: str) -> bool:
    return purpose in BACKGROUND_PURPOSES


def month_bounds(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt


def projection_pct(used: float, cap: float, day: date) -> float:
    """Linear projection of the month's use at month end, in % of the cap."""
    if cap <= 0:
        return 0.0
    first, nxt = month_bounds(day)
    days_in_month = (nxt - first).days
    elapsed = day.day  # the current day counts as elapsed
    return round(used / max(elapsed, 1) * days_in_month / cap * 100, 1)


def cap_check(
    *,
    interactive: bool,
    tokens_used: int,
    token_cap: int,
    cost_used: Decimal | None,
    cost_cap: float | None,
    stop_pct: int,
) -> dict[str, Any] | None:
    """Which monthly cap blocks a purpose (None = allowed). Interactive purposes stop at
    100 %, background purposes at `stop_pct` % of either cap."""
    share = 1.0 if interactive else stop_pct / 100
    if token_cap and tokens_used >= token_cap * share:
        return {"cap": "monthly_tokens", "limit": token_cap, "used": tokens_used,
                "background_stop": not interactive}  # fmt: skip
    if (
        cost_cap is not None
        and cost_used is not None
        and cost_used >= Decimal(str(cost_cap)) * Decimal(str(share))
    ):
        return {"cap": "monthly_cost", "limit": cost_cap, "used": float(cost_used),
                "background_stop": not interactive}  # fmt: skip
    return None


# ---------------------------------------------------------------- service
_UPSERT = text(
    """
    INSERT INTO assistant_usage (day, user_id, purpose, questions, model_calls, tool_calls,
        errors, throttles, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
        cost_estimate)
    VALUES (:day, :user_id, :purpose, :questions, :model_calls, :tool_calls, :errors,
        :throttles, :input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens,
        :cost)
    ON CONFLICT (day, user_id, purpose) DO UPDATE SET
        questions = assistant_usage.questions + EXCLUDED.questions,
        model_calls = assistant_usage.model_calls + EXCLUDED.model_calls,
        tool_calls = assistant_usage.tool_calls + EXCLUDED.tool_calls,
        errors = assistant_usage.errors + EXCLUDED.errors,
        throttles = assistant_usage.throttles + EXCLUDED.throttles,
        input_tokens = assistant_usage.input_tokens + EXCLUDED.input_tokens,
        output_tokens = assistant_usage.output_tokens + EXCLUDED.output_tokens,
        cache_read_tokens = assistant_usage.cache_read_tokens + EXCLUDED.cache_read_tokens,
        cache_write_tokens = assistant_usage.cache_write_tokens + EXCLUDED.cache_write_tokens,
        cost_estimate = assistant_usage.cost_estimate + EXCLUDED.cost_estimate
    """
)
_MONTH_TOTALS = text(
    """
    SELECT coalesce(sum(input_tokens), 0) AS input_tokens,
           coalesce(sum(output_tokens), 0) AS output_tokens,
           coalesce(sum(cache_read_tokens), 0) AS cache_read_tokens,
           coalesce(sum(cache_write_tokens), 0) AS cache_write_tokens,
           coalesce(sum(cost_estimate), 0) AS cost
    FROM assistant_usage WHERE day >= :a AND day < :b
    """
)


def _iso(dt: datetime) -> str:
    from netmon.services.serialize import iso

    return str(iso(dt))


class UsageService:
    def __init__(self, rt: Any) -> None:
        self.rt = rt

    # ------------------------------------------------------------ time
    async def _tz(self, session: Any) -> ZoneInfo:
        from netmon.services import settings_p3c

        name = await settings_p3c.timezone(session)
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001
            return ZoneInfo("UTC")

    def _now(self) -> datetime:
        clock = getattr(self.rt, "clock", None)
        if clock is not None:
            return clock.now()
        from datetime import UTC

        return datetime.now(UTC)

    async def today(self, session: Any) -> tuple[date, ZoneInfo]:
        tz = await self._tz(session)
        return self._now().astimezone(tz).date(), tz

    # ------------------------------------------------------------ reads
    async def _month_totals(self, session: Any, day: date) -> tuple[int, Decimal]:
        a, b = month_bounds(day)
        row = (await session.execute(_MONTH_TOTALS, {"a": a, "b": b})).mappings().one()
        return cap_tokens(dict(row)), Decimal(str(row["cost"] or 0))

    async def questions_today(self, session: Any, user_id: int, day: date) -> int:
        v = await session.scalar(
            text(
                "SELECT coalesce(sum(questions), 0) FROM assistant_usage "
                "WHERE day = :d AND user_id = :u AND purpose = ANY(:p)"
            ),
            {"d": day, "u": user_id, "p": sorted(QUESTION_PURPOSES)},
        )
        return int(v or 0)

    async def purpose_today(self, session: Any, purpose: str, day: date) -> int:
        v = await session.scalar(
            text(
                "SELECT coalesce(sum(questions), 0) FROM assistant_usage "
                "WHERE day = :d AND purpose = :p"
            ),
            {"d": day, "p": purpose},
        )
        return int(v or 0)

    # ------------------------------------------------------------ caps
    async def check(
        self,
        user_id: int,
        purpose: str,
        *,
        purpose_daily_cap: int | None = None,
        question: bool | None = None,
        session: Any = None,
    ) -> None:
        """Raise `assistant_cap_reached` when a cap blocks this purpose (no counting).
        `question=False` skips the per-user daily question cap (e.g. a follow-up model call
        of a question that was already counted)."""
        if session is None:
            async with self.rt.sessionmaker() as s:
                await self._check(s, user_id, purpose, purpose_daily_cap, question)
        else:
            await self._check(session, user_id, purpose, purpose_daily_cap, question)

    async def _check(
        self,
        session: Any,
        user_id: int,
        purpose: str,
        purpose_daily_cap: int | None,
        question: bool | None = None,
    ) -> date:
        day, tz = await self.today(session)
        sc = await config.scalars(session)
        cfg = await config.load(session)
        tomorrow = datetime.combine(day + timedelta(days=1), time(0), tz)
        next_month = datetime.combine(month_bounds(day)[1], time(0), tz)
        interactive = not is_background(purpose)
        tokens, cost = await self._month_totals(session, day)
        cost_cap = sc.get("assistant_monthly_cost_cap")
        blocked = cap_check(
            interactive=interactive,
            tokens_used=tokens,
            token_cap=int(sc["assistant_monthly_token_cap"]),
            cost_used=cost if config.prices_set(cfg.get("prices")) else None,
            cost_cap=cost_cap if config.prices_set(cfg.get("prices")) else None,
            stop_pct=int(sc["assistant_background_stop_pct"]),
        )
        if blocked:
            raise _cap_error({**blocked, "resets_at": _iso(next_month)})
        if interactive and user_id and purpose in QUESTION_PURPOSES and question is not False:
            limit = int(sc["assistant_daily_questions_per_user"])
            used = await self.questions_today(session, user_id, day)
            if used >= limit:
                raise _cap_error(
                    {"cap": "user_daily", "limit": limit, "used": used, "resets_at": _iso(tomorrow)}
                )
        if purpose_daily_cap is not None:
            used = await self.purpose_today(session, purpose, day)
            if used >= purpose_daily_cap:
                raise _cap_error(
                    {"cap": "purpose_daily", "limit": purpose_daily_cap, "used": used,
                     "resets_at": _iso(tomorrow), "purpose": purpose}
                )  # fmt: skip
        return day

    async def check_and_reserve(
        self,
        user_id: int,
        purpose: str,
        *,
        count_question: bool = True,
        purpose_daily_cap: int | None = None,
    ) -> None:
        """Check every cap; on success count the question (`questions + 1`) in the same
        transaction (serialised per user/purpose by the row lock of the upsert)."""
        async with self.rt.sessionmaker() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('assistant_usage'), :u)"),
                {"u": int(user_id)},
            )
            day = await self._check(session, user_id, purpose, purpose_daily_cap)
            if count_question:
                await self._upsert(session, day, user_id, purpose, questions=1)
            await session.commit()

    async def background_allowed(self) -> bool:
        try:
            await self.check(SYSTEM_USER, "digest")
        except AssistantError:
            return False
        return True

    # ------------------------------------------------------------ writes
    async def _upsert(self, session: Any, day: date, user_id: int, purpose: str, **kw: Any) -> None:
        params = {
            "day": day,
            "user_id": int(user_id),
            "purpose": purpose,
            "questions": 0,
            "model_calls": 0,
            "tool_calls": 0,
            "errors": 0,
            "throttles": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost": Decimal(0),
        }
        params.update(kw)
        await session.execute(_UPSERT, params)

    async def record(
        self,
        user_id: int,
        purpose: str,
        usage: dict[str, Any] | None = None,
        *,
        model_calls: int = 1,
        tool_calls: int = 0,
        errors: int = 0,
        throttles: int = 0,
        questions: int = 0,
    ) -> Decimal | None:
        """Count one model call's tokens (Bedrock `usage` or our counter names) and return
        its cost estimate (None without prices)."""
        from netmon.assistant.bedrock import usage_of

        u = usage or {}
        if "inputTokens" in u or "outputTokens" in u:
            u = usage_of(u)
        async with self.rt.sessionmaker() as session:
            day, _ = await self.today(session)
            cfg = await config.load(session)
            cost = pricing.estimate(u, cfg.get("prices"))
            await self._upsert(
                session,
                day,
                user_id,
                purpose,
                questions=questions,
                model_calls=model_calls,
                tool_calls=tool_calls,
                errors=errors,
                throttles=throttles,
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_read_tokens=int(u.get("cache_read_tokens") or 0),
                cache_write_tokens=int(u.get("cache_write_tokens") or 0),
                cost=cost or Decimal(0),
            )
            await session.commit()
        return cost

    async def record_error(self, user_id: int, purpose: str, code: str) -> None:
        await self.record(
            user_id,
            purpose,
            None,
            model_calls=0,
            errors=1,
            throttles=1 if code == "bedrock_throttled" else 0,
        )

    async def cost_of(self, usage: dict[str, Any]) -> Decimal | None:
        async with self.rt.sessionmaker() as session:
            cfg = await config.load(session)
        return pricing.estimate(usage, cfg.get("prices"))

    # ------------------------------------------------------------ reports
    async def month_summary(self, month: str | None = None, session: Any = None) -> dict[str, Any]:
        if session is None:
            async with self.rt.sessionmaker() as s:
                return await self._month_summary(s, month)
        return await self._month_summary(session, month)

    async def _month_summary(self, session: Any, month: str | None) -> dict[str, Any]:
        today, _ = await self.today(session)
        day = _parse_month(month) if month else today
        sc = await config.scalars(session)
        cfg = await config.load(session)
        tokens, cost = await self._month_totals(session, day)
        has_prices = config.prices_set(cfg.get("prices"))
        cap = int(sc["assistant_monthly_token_cap"])
        cost_cap = sc.get("assistant_monthly_cost_cap") if has_prices else None
        pct = tokens / cap * 100 if cap else 0.0
        if cost_cap:
            pct = max(pct, float(cost) / float(cost_cap) * 100)
        current = (day.year, day.month) == (today.year, today.month)
        ref_day = today if current else month_bounds(day)[1] - timedelta(days=1)
        proj = projection_pct(tokens, cap, ref_day)
        if cost_cap:
            proj = max(proj, projection_pct(float(cost), float(cost_cap), ref_day))
        stop = int(sc["assistant_background_stop_pct"])
        return {
            "month": f"{day.year:04d}-{day.month:02d}",
            "tokens": tokens,
            "token_cap": cap,
            "cost_estimate": float(cost) if has_prices else None,
            "cost_cap": cost_cap,
            "currency": (cfg.get("prices") or {}).get("currency") if has_prices else None,
            "pct_of_cap": round(pct, 1),
            "projection_pct": proj,
            "background_stopped": current and pct >= stop,
        }

    async def report(self, month: str | None = None) -> dict[str, Any]:
        async with self.rt.sessionmaker() as session:
            summary = await self._month_summary(session, month)
            day = _parse_month(summary["month"])
            a, b = month_bounds(day)
            has_cost = summary["cost_estimate"] is not None
            p = {"a": a, "b": b}

            def cost(v: Any) -> float | None:
                return float(v) if has_cost and v is not None else None

            rows = await session.execute(
                text(
                    "SELECT day, sum(questions) q, sum(input_tokens) i, sum(output_tokens) o, "
                    "sum(cache_read_tokens) cr, sum(cache_write_tokens) cw, sum(cost_estimate) c "
                    "FROM assistant_usage WHERE day >= :a AND day < :b GROUP BY day ORDER BY day"
                ),
                p,
            )
            by_day = [
                {
                    "day": r.day.isoformat(),
                    "questions": int(r.q or 0),
                    "tokens": cap_tokens(_tok(r)),
                    "cost_estimate": cost(r.c),
                }
                for r in rows
            ]
            rows = await session.execute(
                text(
                    "SELECT u.user_id, coalesce(us.username, '') AS username, sum(questions) q, "
                    "sum(input_tokens) i, sum(output_tokens) o, sum(cache_read_tokens) cr, "
                    "sum(cache_write_tokens) cw, sum(cost_estimate) c FROM assistant_usage u "
                    "LEFT JOIN users us ON us.id = u.user_id WHERE day >= :a AND day < :b "
                    "GROUP BY u.user_id, us.username ORDER BY sum(questions) DESC, u.user_id"
                ),
                p,
            )
            by_user = [
                {
                    "user_id": int(r.user_id),
                    "username": "system" if r.user_id == SYSTEM_USER else (r.username or "?"),
                    "questions": int(r.q or 0),
                    "tokens": cap_tokens(_tok(r)),
                    "cost_estimate": cost(r.c),
                }
                for r in rows
            ]
            rows = await session.execute(
                text(
                    "SELECT purpose, sum(model_calls) calls, sum(input_tokens) i, "
                    "sum(output_tokens) o, sum(cache_read_tokens) cr, sum(cache_write_tokens) cw, "
                    "sum(cost_estimate) c FROM assistant_usage WHERE day >= :a AND day < :b "
                    "GROUP BY purpose ORDER BY purpose"
                ),
                p,
            )
            by_purpose = [
                {
                    "purpose": r.purpose,
                    "calls": int(r.calls or 0),
                    "tokens": cap_tokens(_tok(r)),
                    "cost_estimate": cost(r.c),
                }
                for r in rows
            ]
            tz = (await self.today(session))[1]
            ta = datetime.combine(a, time(0), tz)
            tb = datetime.combine(b, time(0), tz)
            tp = {"a": ta, "b": tb}
            rows = await session.execute(
                text(
                    "SELECT tool, count(*) calls, count(*) FILTER (WHERE NOT ok) errs, "
                    "avg(duration_ms) ms FROM assistant_tool_calls WHERE started_at >= :a "
                    "AND started_at < :b GROUP BY tool ORDER BY count(*) DESC, tool LIMIT 20"
                ),
                tp,
            )
            top_tools = [
                {"tool": r.tool, "calls": int(r.calls), "errors": int(r.errs),
                 "avg_ms": int(round(float(r.ms or 0)))}
                for r in rows
            ]  # fmt: skip
            rows = await session.execute(
                text(
                    "SELECT error_code, count(*) n FROM assistant_messages "
                    "WHERE role = 'assistant' AND error_code IS NOT NULL "
                    "AND created_at >= :a AND created_at < :b "
                    "GROUP BY error_code ORDER BY count(*) DESC, error_code"
                ),
                tp,
            )
            errors = [{"code": r.error_code, "count": int(r.n)} for r in rows]
            fb = (
                await session.execute(
                    text(
                        "SELECT count(*) FILTER (WHERE feedback = 1) up, "
                        "count(*) FILTER (WHERE feedback = -1) down FROM assistant_messages "
                        "WHERE created_at >= :a AND created_at < :b"
                    ),
                    tp,
                )
            ).one()
            last12 = []
            m = a
            for _ in range(12):
                ma, mb = month_bounds(m)
                r = (
                    await session.execute(
                        text(
                            "SELECT sum(input_tokens) i, sum(output_tokens) o, "
                            "sum(cache_read_tokens) cr, sum(cache_write_tokens) cw, "
                            "sum(cost_estimate) c FROM assistant_usage "
                            "WHERE day >= :a AND day < :b"
                        ),
                        {"a": ma, "b": mb},
                    )
                ).one()
                last12.append(
                    {
                        "month": f"{ma.year:04d}-{ma.month:02d}",
                        "tokens": cap_tokens(_tok(r)),
                        "cost_estimate": cost(r.c)
                        if r.c is not None
                        else (0.0 if has_cost else None),
                    }
                )
                m = (ma - timedelta(days=1)).replace(day=1)
        return {
            **summary,
            "by_day": by_day,
            "by_user": by_user,
            "by_purpose": by_purpose,
            "top_tools": top_tools,
            "errors": errors,
            "feedback": {"up": int(fb.up or 0), "down": int(fb.down or 0)},
            "last_12_months": last12,
        }

    async def purge(self, keep_days: int = 730) -> int:
        async with self.rt.sessionmaker() as session:
            day, _ = await self.today(session)
            res = await session.execute(
                text("DELETE FROM assistant_usage WHERE day < :d"),
                {"d": day - timedelta(days=keep_days)},
            )
            await session.commit()
        return int(res.rowcount or 0)


def _tok(r: Any) -> dict[str, int]:
    return {
        "input_tokens": int(r.i or 0),
        "output_tokens": int(r.o or 0),
        "cache_read_tokens": int(r.cr or 0),
        "cache_write_tokens": int(r.cw or 0),
    }


def _parse_month(month: str) -> date:
    y, m = month.split("-")
    return date(int(y), int(m), 1)


def _cap_error(detail: dict[str, Any]) -> AssistantError:
    cap = detail["cap"]
    msg = {
        "user_daily": "You reached your daily number of assistant questions.",
        "monthly_tokens": "The monthly assistant token budget is used up.",
        "monthly_cost": "The monthly assistant cost budget is used up.",
        "purpose_daily": "The daily limit of this AI feature is reached.",
        "chat_hourly": "Too many questions from the chat in the last hour.",
    }.get(cap, "An assistant limit is reached.")
    if detail.get("background_stop"):
        msg = "Background AI work stopped: the monthly budget reached the background limit."
    return AssistantError("assistant_cap_reached", msg, [detail])
