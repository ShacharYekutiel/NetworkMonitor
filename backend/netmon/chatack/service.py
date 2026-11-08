"""[3] Chat acknowledge service (architecture § 13.8).

- `decorate_payload` (alert engine): an ack token + `ack_url` for problem / escalation /
  reminder notifications while `chat_ack` is ON.
- `channel_payload` / `telegram_markup` (senders): per-channel use of the token/link.
- `problem_of` / `acknowledge`: the token's problem check and the 2b-1 acknowledge semantics
  (`acknowledge` event, `sensor.ack`, alert state), audited with `changes.via`."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from netmon.chatack import tokens

ACK_STAGES = ("problem", "escalation", "reminder")
DEFAULT_TTL_H = 168
TEXTS = {
    "en": {
        "button": "Acknowledge",
        "acknowledged": "Acknowledged",
        "not_linked": (
            "Your Telegram account is not linked to NetworkMonitor (My profile → Chat accounts)"
        ),
        "not_admin": "Only administrators can acknowledge",
        "expired": "This button has expired",
        "invalid": "This button is not valid",
        "stale": "This problem is no longer active",
        "already": "Already acknowledged by {by}",
        "rate_limited": "Too many presses; try again in a minute",
        "linked": "Linked to NetworkMonitor user {user}",
        "link_failed": "This link code is invalid or expired; create a new one in NetworkMonitor",
        "unlinked": "This Telegram account is no longer linked to NetworkMonitor",
        "not_linked_unlink": "This Telegram account is not linked",
    },
    "he": {
        "button": "אישור טיפול",
        "acknowledged": "אושר",
        "not_linked": "חשבון הטלגרם שלך אינו מקושר ל-NetworkMonitor (הפרופיל שלי ← חשבונות צ'אט)",
        "not_admin": "רק מנהלים יכולים לאשר טיפול",
        "expired": "תוקף הכפתור פג",
        "invalid": "הכפתור אינו תקף",
        "stale": "התקלה כבר אינה פעילה",
        "already": "כבר אושר על ידי {by}",
        "rate_limited": "יותר מדי לחיצות; נסו שוב בעוד דקה",
        "linked": "קושר למשתמש NetworkMonitor {user}",
        "link_failed": "קוד הקישור אינו תקף או שפג תוקפו; צרו קוד חדש ב-NetworkMonitor",
        "unlinked": "חשבון הטלגרם הזה כבר אינו מקושר ל-NetworkMonitor",
        "not_linked_unlink": "חשבון הטלגרם הזה אינו מקושר",
    },
}


def text_for(lang: str | None, key: str, **kw: Any) -> str:
    return TEXTS.get(lang or "en", TEXTS["en"])[key].format(**kw)


def chat_ack_on(rt: Any) -> bool:
    features = getattr(rt, "features", None)
    return bool(features is not None and features.enabled("chat_ack"))


def now_min(rt: Any) -> int:
    return int(rt.clock.now().timestamp() // 60) if rt is not None else int(time.time() // 60)


def key_of(rt: Any) -> bytes:
    return tokens.derive_key(rt.secret_key)


def make_token(rt: Any, sensor_id: int, event_id: int, ttl_h: int | None = None) -> str:
    ttl = int(ttl_h or DEFAULT_TTL_H)
    return tokens.encode(key_of(rt), sensor_id, event_id, now_min(rt) + ttl * 60)


def decorate_payload(rt: Any, payload: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Adds `ack_token` (internal) and `ack_url` while `chat_ack` is ON."""
    payload.setdefault("ack_url", None)
    if not chat_ack_on(rt) or payload.get("stage") not in ACK_STAGES:
        return payload
    sensor = payload.get("sensor") or {}
    event_id = payload.get("event_id")
    if sensor.get("id") is None or event_id is None:
        return payload
    try:
        ttl = int(cfg.get("chat_ack_ttl_h") or DEFAULT_TTL_H)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_H
    token = make_token(rt, int(sensor["id"]), int(event_id), ttl)
    payload["ack_token"] = token
    base = cfg.get("public_base_url")
    if isinstance(base, str) and base.strip():
        payload["ack_url"] = f"{base.strip().rstrip('/')}/ack#{token}"
    return payload


def channel_payload(channel: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """What a channel may see: the internal token never leaves; `ack_url` per config
    (teams: `ack_link` default true; email: default false; webhook: always)."""
    out = {k: v for k, v in payload.items() if k != "ack_token"}
    cfg = channel.get("config") or {}
    ctype = channel.get("type")
    if ctype == "email" and not cfg.get("ack_link", False):
        out["ack_url"] = None
    if ctype == "teams" and not cfg.get("ack_link", True):
        out["ack_url"] = None
    if ctype == "telegram":
        out["ack_url"] = None  # carried by the reply markup, not the text
        if payload.get("ack_token"):
            out["_ack"] = {"token": payload["ack_token"], "url": payload.get("ack_url")}
    return out


def telegram_markup(
    cfg: dict[str, Any], payload: dict[str, Any], lang: str
) -> dict[str, Any] | None:
    ack = payload.get("_ack")
    if not ack:
        return None
    label = text_for(lang, "button")
    if cfg.get("ack_buttons"):
        return {"inline_keyboard": [[{"text": label, "callback_data": ack["token"]}]]}
    if cfg.get("ack_link") and ack.get("url"):
        return {"inline_keyboard": [[{"text": label, "url": ack["url"]}]]}
    return None


# ---------------------------------------------------------------- the problem a token names
@dataclass
class Problem:
    sensor: Any
    device: Any
    event_id: int
    current_event_id: int | None
    problem_since: Any

    @property
    def active(self) -> bool:
        return self.sensor.status in ("down", "warning") and self.current_event_id == self.event_id


async def problem_of(
    session: AsyncSession, sensor_id: int, event_id: int, lock: bool = False
) -> Problem | None:
    from netmon.models import Device, Sensor

    stmt = select(Sensor).where(Sensor.id == sensor_id)
    if lock:
        stmt = stmt.with_for_update()
    sensor = (await session.execute(stmt)).scalar_one_or_none()
    if sensor is None:
        return None
    device = await session.get(Device, sensor.device_id)
    row = (
        await session.execute(
            text(
                "SELECT id, time FROM events WHERE kind = 'status_change' AND sensor_id = :s "
                "AND new_status IN ('down', 'warning') ORDER BY time DESC, id DESC LIMIT 1"
            ),
            {"s": sensor_id},
        )
    ).first()
    since = None
    ev = (
        await session.execute(
            text("SELECT time FROM events WHERE id = :e AND sensor_id = :s"),
            {"e": event_id, "s": sensor_id},
        )
    ).first()
    if ev is not None:
        since = ev.time
    return Problem(sensor, device, event_id, row.id if row else None, since)


async def acknowledge(
    rt: Any,
    session: AsyncSession,
    problem: Problem,
    user_id: int,
    username: str,
    comment: str,
    ctx: Any,
    via: str,
) -> bool:
    """2b-1 acknowledge semantics. Returns False when already acknowledged (idempotent).
    The caller commits nothing: this commits and publishes."""
    from netmon.services import audit
    from netmon.services import events as events_svc
    from netmon.services.serialize import acknowledgement_out

    s, d = problem.sensor, problem.device
    if s.ack_at is not None:
        return False
    comment = (comment or "").strip()[:1000]
    s.ack_at = rt.clock.now()
    s.ack_user_id = user_id
    s.ack_by = username
    s.ack_comment = comment
    s.ack_status = s.status
    ev = await events_svc.add_event(
        session,
        "acknowledge",
        f"Acknowledged by {username}" + (f": {comment}" if comment else ""),
        sensor_id=s.id,
        device_id=d.id,
        group_id=d.group_id,
        sensor_name=s.name,
        device_name=d.name,
        old_status=s.status,
        new_status=s.status,
        in_maintenance=rt.maintenance is not None and rt.maintenance.in_maintenance(d.id),
    )
    await audit.record(
        session,
        ctx,
        "sensor.acknowledge",
        audit.entity("sensor", s.id, s.name, device_id=d.id),
        {"status": [s.status, s.status], "comment": [None, comment], "via": [None, via]},
    )
    await session.commit()
    events_svc.published(rt, [ev])
    rt.hub.broadcast(
        "sensor.ack",
        {"id": s.id, "device_id": s.device_id, "acknowledgement": acknowledgement_out(s)},
    )
    if rt.alerts is not None:
        rt.alerts.request_acknowledged(s.id)
    rt.request_rollup()
    return True
