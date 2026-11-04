"""Notification channels and alert rules."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from netmon.alerts.email import SmtpError
from netmon.alerts.engine import build_payload, send_to_channel
from netmon.alerts.webhook import WebhookError
from netmon.api.common import commit_or_conflict
from netmon.api.deps import DB, RT, Admin, Page
from netmon.api.errors import ApiError, conflict, not_found, validation_error
from netmon.models import AlertRule, AlertRuleChannel, NotificationChannel, Sensor
from netmon.schemas import (
    AlertRuleCreate,
    AlertRulePatch,
    EmailConfig,
    NotificationChannelCreate,
    NotificationChannelPatch,
    WebhookConfig,
)
from netmon.services import settings as settings_svc
from netmon.services.serialize import iso, notification_channel_out, now_utc, page

router = APIRouter()


def validate_channel_config(type_: str, config: Any) -> dict[str, Any]:
    model = EmailConfig if type_ == "email" else WebhookConfig
    try:
        return model.model_validate(config).model_dump()
    except ValidationError as exc:
        details = [
            {
                "loc": ["body", "config", *[p for p in e["loc"]]],
                "msg": str(e["msg"]).removeprefix("Value error, "),
            }
            for e in exc.errors()
        ]
        raise ApiError(422, "validation_error", "Invalid channel config", details) from exc


async def _unique_channel_name(session: DB, name: str, exclude: int | None = None) -> None:
    q = select(NotificationChannel.id).where(func.lower(NotificationChannel.name) == name.lower())
    if exclude is not None:
        q = q.where(NotificationChannel.id != exclude)
    if (await session.execute(q)).first():
        raise conflict("name")


@router.get("/api/notification-channels")
async def list_channels(_: Admin, session: DB, pg: Page) -> dict:
    total = (
        await session.execute(select(func.count()).select_from(NotificationChannel))
    ).scalar_one()
    rows = (
        await session.execute(
            select(NotificationChannel)
            .order_by(NotificationChannel.name)
            .offset(pg.offset)
            .limit(pg.page_size)
        )
    ).scalars()
    return page([notification_channel_out(c) for c in rows], total, pg.page, pg.page_size)


@router.post("/api/notification-channels", status_code=201)
async def create_channel(body: NotificationChannelCreate, _: Admin, rt: RT, session: DB) -> dict:
    cfg = validate_channel_config(body.type, body.config)
    await _unique_channel_name(session, body.name.strip())
    ch = NotificationChannel(
        name=body.name.strip(), type=body.type, config=cfg, enabled=body.enabled
    )
    session.add(ch)
    await commit_or_conflict(session)
    await session.refresh(ch)
    rt.config_changed("notification_channel", ch.id, "created")
    return notification_channel_out(ch)


@router.get("/api/notification-channels/{channel_id}")
async def get_channel(channel_id: int, _: Admin, session: DB) -> dict:
    ch = await session.get(NotificationChannel, channel_id)
    if ch is None:
        raise not_found("Notification channel")
    return notification_channel_out(ch)


@router.patch("/api/notification-channels/{channel_id}")
async def patch_channel(
    channel_id: int, body: NotificationChannelPatch, _: Admin, rt: RT, session: DB
) -> dict:
    ch = await session.get(NotificationChannel, channel_id)
    if ch is None:
        raise not_found("Notification channel")
    fields = body.model_fields_set
    if "type" in fields and body.type is not None and body.type != ch.type:
        raise validation_error(["body", "type"], "the channel type cannot be changed")
    if "name" in fields and body.name:
        await _unique_channel_name(session, body.name.strip(), ch.id)
        ch.name = body.name.strip()
    if "enabled" in fields and body.enabled is not None:
        ch.enabled = body.enabled
    if "config" in fields and body.config is not None:
        ch.config = validate_channel_config(ch.type, body.config)
    await commit_or_conflict(session)
    await session.refresh(ch)
    rt.config_changed("notification_channel", ch.id, "updated")
    return notification_channel_out(ch)


@router.delete("/api/notification-channels/{channel_id}", status_code=204)
async def delete_channel(channel_id: int, _: Admin, rt: RT, session: DB) -> Response:
    ch = await session.get(NotificationChannel, channel_id)
    if ch is None:
        raise not_found("Notification channel")
    await session.delete(ch)
    await session.commit()
    rt.config_changed("notification_channel", channel_id, "deleted")
    return Response(status_code=204)


@router.post("/api/notification-channels/{channel_id}/test")
async def test_channel(channel_id: int, _: Admin, rt: RT, session: DB) -> dict:
    ch = await session.get(NotificationChannel, channel_id)
    if ch is None:
        raise not_found("Notification channel")
    cfg = await settings_svc.get_all(session)
    payload = build_payload(None, None, None, None, None, None, test_time=iso(now_utc()))
    channel = {"id": ch.id, "name": ch.name, "type": ch.type, "config": dict(ch.config)}
    await session.close()
    sender = rt.alerts.sender if rt.alerts is not None else send_to_channel
    try:
        await sender(channel, payload, cfg["smtp"], cfg["default_language"])
    except SmtpError as exc:
        raise ApiError(502, "smtp_error", str(exc)) from exc
    except WebhookError as exc:
        raise ApiError(502, "webhook_error", str(exc)) from exc
    return {"ok": True, "message": "sent"}


# ---------------------------------------------------------------- alert rules
async def _rule_out(session: DB, r: AlertRule) -> dict:
    chans = (
        (
            await session.execute(
                select(AlertRuleChannel.channel_id)
                .where(AlertRuleChannel.rule_id == r.id)
                .order_by(AlertRuleChannel.channel_id)
            )
        )
        .scalars()
        .all()
    )
    sensor_name = None
    if r.sensor_id is not None:
        sensor_name = (
            await session.execute(select(Sensor.name).where(Sensor.id == r.sensor_id))
        ).scalar_one_or_none()
    return {
        "id": r.id,
        "name": r.name,
        "sensor_id": r.sensor_id,
        "sensor_name": sensor_name,
        "on_statuses": list(r.on_statuses),
        "delay_s": r.delay_s,
        "enabled": r.enabled,
        "channel_ids": list(chans),
        "created_at": iso(r.created_at),
        "updated_at": iso(r.updated_at),
    }


async def _check_refs(session: DB, sensor_id: int | None, channel_ids: list[int] | None) -> None:
    if sensor_id is not None and await session.get(Sensor, sensor_id) is None:
        raise validation_error(["body", "sensor_id"], "sensor does not exist")
    if channel_ids is not None:
        found = set(
            (
                await session.execute(
                    select(NotificationChannel.id).where(NotificationChannel.id.in_(channel_ids))
                )
            ).scalars()
        )
        missing = [c for c in channel_ids if c not in found]
        if missing:
            raise validation_error(["body", "channel_ids"], f"unknown channel id(s): {missing}")


def _dedupe(values: list[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


@router.get("/api/alert-rules")
async def list_rules(_: Admin, session: DB, pg: Page, sensor_id: str | None = None) -> dict:
    stmt = select(AlertRule)
    if sensor_id is not None:
        if sensor_id == "global":
            stmt = stmt.where(AlertRule.sensor_id.is_(None))
        else:
            try:
                stmt = stmt.where(AlertRule.sensor_id == int(sensor_id))
            except ValueError as exc:
                raise validation_error(
                    ["query", "sensor_id"], "must be an integer or 'global'"
                ) from exc
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(AlertRule.name, AlertRule.id).offset(pg.offset).limit(pg.page_size)
            )
        )
        .scalars()
        .all()
    )
    return page([await _rule_out(session, r) for r in rows], total, pg.page, pg.page_size)


@router.post("/api/alert-rules", status_code=201)
async def create_rule(body: AlertRuleCreate, _: Admin, rt: RT, session: DB) -> dict:
    channel_ids = _dedupe(body.channel_ids)
    await _check_refs(session, body.sensor_id, channel_ids)
    r = AlertRule(
        name=body.name.strip(),
        sensor_id=body.sensor_id,
        on_statuses=_dedupe(list(body.on_statuses)),
        delay_s=body.delay_s,
        enabled=body.enabled,
    )
    session.add(r)
    await session.flush()
    for cid in channel_ids:
        session.add(AlertRuleChannel(rule_id=r.id, channel_id=cid))
    await commit_or_conflict(session)
    await session.refresh(r)
    rt.config_changed("alert_rule", r.id, "created")
    return await _rule_out(session, r)


@router.get("/api/alert-rules/{rule_id}")
async def get_rule(rule_id: int, _: Admin, session: DB) -> dict:
    r = await session.get(AlertRule, rule_id)
    if r is None:
        raise not_found("Alert rule")
    return await _rule_out(session, r)


@router.patch("/api/alert-rules/{rule_id}")
async def patch_rule(rule_id: int, body: AlertRulePatch, _: Admin, rt: RT, session: DB) -> dict:
    r = await session.get(AlertRule, rule_id)
    if r is None:
        raise not_found("Alert rule")
    fields = body.model_fields_set
    channel_ids = _dedupe(body.channel_ids) if body.channel_ids is not None else None
    if "channel_ids" in fields and body.channel_ids is None:
        raise validation_error(["body", "channel_ids"], "must be a non-empty list")
    if "on_statuses" in fields and body.on_statuses is None:
        raise validation_error(["body", "on_statuses"], "must be a non-empty list")
    await _check_refs(session, body.sensor_id if "sensor_id" in fields else None, channel_ids)
    if "name" in fields and body.name:
        r.name = body.name.strip()
    if "sensor_id" in fields:
        r.sensor_id = body.sensor_id
    if body.on_statuses is not None:
        r.on_statuses = _dedupe(list(body.on_statuses))
    if body.delay_s is not None:
        r.delay_s = body.delay_s
    if body.enabled is not None:
        r.enabled = body.enabled
    if channel_ids is not None:
        await session.execute(delete(AlertRuleChannel).where(AlertRuleChannel.rule_id == r.id))
        for cid in channel_ids:
            session.add(AlertRuleChannel(rule_id=r.id, channel_id=cid))
    await commit_or_conflict(session)
    await session.refresh(r)
    rt.config_changed("alert_rule", r.id, "updated")
    return await _rule_out(session, r)


@router.delete("/api/alert-rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: int, _: Admin, rt: RT, session: DB) -> Response:
    r = await session.get(AlertRule, rule_id)
    if r is None:
        raise not_found("Alert rule")
    await session.delete(r)
    await session.commit()
    rt.config_changed("alert_rule", rule_id, "deleted")
    return Response(status_code=204)
