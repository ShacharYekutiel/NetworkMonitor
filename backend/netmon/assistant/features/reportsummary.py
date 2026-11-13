"""[3b] AI narrative summary at the top of reports (`ai_report_summary`, § 14.8.10).

Input is the report's own `ReportData` only (lists cut to the top 20 rows per section, IP
redaction per setting); no tools. Generated after `compute`, before render, with its own 60 s
deadline; any failure, cap or timeout → the report renders without it. Every percentage in the
text must appear in the data (± 0.01) or the summary is dropped (`unverified_numbers`)."""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from typing import Any

from sqlalchemy import text

from netmon.assistant.features import common

log = logging.getLogger("netmon.assistant.report_summary")

DEADLINE_S = 60.0
TOP_ROWS = 20
MAX_WORDS = 120
PCT_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[.,]\d+)?)\s?%")

BLOCK = (
    "Task: write one paragraph (at most 120 words) that summarises the report data for the "
    "network administrators: availability, the main incidents and notable changes. Use only "
    "numbers that appear in the data, written exactly as given (percentages with the same "
    "decimals); do not compute new numbers. No links, no headings, no lists, plain text."
)


def trim_data(data: dict[str, Any]) -> dict[str, Any]:
    """The report data for the model: lists cut to the top 20 rows per section (pure)."""
    out = copy.deepcopy(data)

    def cut(v: Any) -> Any:
        if isinstance(v, list):
            return [cut(x) for x in v[:TOP_ROWS]]
        if isinstance(v, dict):
            return {k: cut(x) for k, x in v.items()}
        return v

    out = cut(out)
    out.pop("ai_summary", None)
    return out  # type: ignore[no-any-return]


def numbers_in(data: Any) -> list[float]:
    found: list[float] = []

    def visit(v: Any) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, int | float):
            found.append(float(v))
        elif isinstance(v, dict):
            for x in v.values():
                visit(x)
        elif isinstance(v, list):
            for x in v:
                visit(x)

    visit(data)
    return found


def verify_percentages(summary: str, data: Any, tol: float = 0.01) -> bool:
    """Every `NN.N%` in the text appears in the data (± tol). Fractions in the data (0–1) are
    also compared ×100."""
    nums = numbers_in(data)
    cands = set(nums) | {n * 100 for n in nums if 0 <= n <= 1}
    for m in PCT_RE.finditer(summary or ""):
        v = float(m.group(1).replace(",", "."))
        if not any(abs(v - c) <= tol + 1e-9 for c in cands):
            return False
    return True


def clean(summary: str) -> str:
    s = " ".join((summary or "").split())
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)  # no links
    words = s.split(" ")
    if len(words) > MAX_WORDS:
        s = " ".join(words[:MAX_WORDS]).rstrip(",;") + "…"
    return s


async def summarize(
    rt: Any, report: dict[str, Any], run: dict[str, Any], data: dict[str, Any]
) -> tuple[str | None, str | None]:
    """(summary, ai_summary_status) for one report run. Never raises; never longer than the
    60 s deadline. `ai_summary_status` None = the report did not opt in."""
    rid = report.get("id")
    if rid is None:
        return None, None
    try:
        async with rt.sessionmaker() as session:
            opted = (
                await session.execute(
                    text("SELECT ai_summary FROM reports WHERE id = :i"), {"i": rid}
                )
            ).scalar()
            if not opted:
                return None, None
            if not await common.active(rt, "ai_report_summary", session):
                return None, "disabled"
            a = await common.assistant_settings(session)
            lang = report.get("language") or "en"
            system = await common.system_prompt(
                rt,
                "report_summary",
                BLOCK + f" Write in {'Hebrew' if lang == 'he' else 'English'}.",
                redaction=bool(a.get("redact_internal_ips")),
                session=session,
            )
    except Exception:  # noqa: BLE001
        log.warning("Report %s AI summary: preparation failed", rid, exc_info=True)
        return None, "failed"
    policy = common.redact_policy(a)
    facts = common.redact_value(trim_data(data), policy)
    comp = None
    status: str
    reason = ""
    summary: str | None = None
    try:
        comp = await asyncio.wait_for(
            common.complete(
                rt,
                purpose="report_summary",
                system=system,
                messages=[common.json_message("Report data (JSON):", facts)],
                max_tokens=400,
                timeout_s=DEADLINE_S,
            ),
            DEADLINE_S,
        )
        text_ = clean(str(common.detokenize(comp.text, policy)))
        if not text_:
            status, reason = "failed", "empty answer"
        elif not verify_percentages(text_, data):
            status = "unverified_numbers"
        else:
            summary, status = text_, "ok"
    except TimeoutError:
        status = "timeout"
    except Exception as exc:  # noqa: BLE001
        err = common.to_ai_error(exc)
        reason = f"{err.code}: {err.message}"[:300]
        status = (
            "skipped_cap"
            if err.code in ("background_stop", "assistant_cap_reached", "cap")
            else ("timeout" if err.code == "assistant_timeout" else "failed")
        )
    try:
        async with rt.sessionmaker() as session:
            await common.put_artifact(
                session,
                "report_summary",
                f"report:{run.get('id')}",
                "ok" if summary else "skipped",
                content=summary or "",
                skip_reason=None if summary else status,
                data={"report_id": rid},
                comp=comp,
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        pass
    await common.audit_system(
        rt,
        "assistant.report_summary",
        "report",
        rid,
        {
            "report_id": rid,
            "run_id": run.get("id"),
            "status": status,
            **({"reason": reason} if reason else {}),
            **common.usage_changes(comp),
        },
        name=report.get("name"),
    )
    if status != "ok":
        # owner's office test: the report page only said "AI summary: failed"
        log.warning("Report %s AI summary %s%s", rid, status, f" ({reason})" if reason else "")
    return summary, status


async def set_status(rt: Any, run_id: int, status: str | None) -> None:
    if status is None:
        return
    try:
        async with rt.sessionmaker() as session:
            await session.execute(
                text("UPDATE report_runs SET ai_summary_status = :s WHERE id = :i"),
                {"s": status, "i": run_id},
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        pass
