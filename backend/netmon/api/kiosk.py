"""[3] NOC kiosk API (api.md § NOC kiosk, architecture § 13.7).

Management `/api/kiosk-screens*` (admin; 409 while `noc` is OFF). Kiosk access
`GET /api/kiosk/view` and WS `/ws/kiosk` use no session: the screen token (header
`X-Netmon-Kiosk` / first WS message) plus the screen's allowed sources; every failure is
`404 not_found` (no oracle), 30 failures per IP per 5 min → 429."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from netmon import kiosk
from netmon.api.deps import DB, RT, Admin, Audit, client_ip, feature_on
from netmon.api.errors import ApiError, error_response, not_found
from netmon.kiosk.hub import KioskHub
from netmon.kiosk.view import build_view
from netmon.services import audit, settings_p3c
from netmon.services.features import require
from netmon.services.serialize import iso

log = logging.getLogger("netmon.api.kiosk")
router = APIRouter()

AUTH_TIMEOUT_S = 5.0
COLUMNS = (
    "k.id, k.name, k.enabled, k.token_hash, k.token_prefix, k.allowed_sources, "
    "k.scope_group_id, g.name AS scope_group_name, k.widgets, k.language, k.show_addresses, "
    "k.refresh_s, k.last_seen_at, k.last_seen_ip, k.created_at, k.updated_at"
)
FROM = "FROM kiosk_screens k LEFT JOIN groups g ON g.id = k.scope_group_id"
KioskWidget = Literal["summary", "down_devices", "problems", "recent_alerts", "map", "group_tiles"]


def _hub(rt: Any) -> KioskHub:
    h = getattr(rt, "kiosk", None)
    if h is None:
        h = rt.kiosk = KioskHub(rt)
    return h


def _limiter(request_or_ws: Any) -> kiosk.FailureLimiter:
    st = request_or_ws.app.state
    lim = getattr(st, "kiosk_limiter", None)
    if lim is None:
        lim = st.kiosk_limiter = kiosk.FailureLimiter()
    return lim


def screen_out(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "enabled": r.enabled,
        "token_prefix": r.token_prefix,
        "allowed_sources": list(r.allowed_sources or []),
        "scope_group_id": r.scope_group_id,
        "scope_group_name": r.scope_group_name,
        "widgets": list(r.widgets or []),
        "language": r.language,
        "show_addresses": r.show_addresses,
        "refresh_s": r.refresh_s,
        "last_seen_at": iso(r.last_seen_at),
        "last_seen_ip": r.last_seen_ip,
        "created_at": iso(r.created_at),
        "updated_at": iso(r.updated_at),
    }


class ScreenIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    allowed_sources: list[str] | None = Field(default=None, min_length=1, max_length=64)
    scope_group_id: int | None = None
    widgets: list[KioskWidget] | None = Field(default=None, min_length=1, max_length=6)
    language: Literal["en", "he"] | None = None
    show_addresses: bool | None = None
    refresh_s: int | None = Field(default=None, ge=10, le=300)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("allowed_sources")
    @classmethod
    def _sources(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out: list[str] = []
        for s in v:
            try:
                n = kiosk.normalize_source(s)
            except ValueError:
                raise ValueError(f"not an IP address or CIDR: {s!r}") from None
            if n not in out:
                out.append(n)
        return out

    @field_validator("widgets")
    @classmethod
    def _widgets(cls, v: list[str] | None) -> list[str] | None:
        if v is not None and len(set(v)) != len(v):
            raise ValueError("widgets must be distinct")
        return v


def _url(base: str | None, token: str) -> str | None:
    return f"{base}/noc#{token}" if base else None


async def _get(session: DB, sid: int) -> Any:
    r = (
        await session.execute(text(f"SELECT {COLUMNS} {FROM} WHERE k.id = :i"), {"i": sid})
    ).first()
    if r is None:
        raise not_found("Kiosk screen")
    return r


def _audit_values(r: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "name",
        "enabled",
        "allowed_sources",
        "scope_group_id",
        "widgets",
        "language",
        "show_addresses",
        "refresh_s",
    )
    return {k: r.get(k) for k in keys}


async def _check_group(session: DB, gid: int | None) -> None:
    if gid is None:
        return
    ok = (await session.execute(text("SELECT 1 FROM groups WHERE id = :g"), {"g": gid})).first()
    if ok is None:
        raise ApiError(
            422, "validation_error", "Unknown group", [{"loc": ["body", "scope_group_id"]}]
        )


def _manage(rt: Any) -> None:
    require(rt, "noc")


@router.get("/api/kiosk-screens")
async def list_screens(_: Admin, rt: RT, session: DB) -> dict:
    _manage(rt)
    rows = (await session.execute(text(f"SELECT {COLUMNS} {FROM} ORDER BY lower(k.name)"))).all()
    return {"items": [screen_out(r) for r in rows]}


@router.post("/api/kiosk-screens", status_code=201)
async def create_screen(body: ScreenIn, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    _manage(rt)
    errs = []
    if not body.name:
        errs.append({"loc": ["body", "name"], "msg": "name is required"})
    if not body.allowed_sources:
        errs.append({"loc": ["body", "allowed_sources"], "msg": "at least one IP or CIDR"})
    if errs:
        raise ApiError(422, "validation_error", errs[0]["msg"], errs)
    count = (await session.execute(text("SELECT count(*) FROM kiosk_screens"))).scalar_one()
    if count >= kiosk.MAX_SCREENS:
        raise ApiError(422, "validation_error", f"At most {kiosk.MAX_SCREENS} kiosk screens")
    await _check_group(session, body.scope_group_id)
    token = kiosk.new_token()
    values = {
        "name": body.name,
        "enabled": True if body.enabled is None else body.enabled,
        "allowed_sources": body.allowed_sources,
        "scope_group_id": body.scope_group_id,
        "widgets": body.widgets or ["summary", "down_devices", "problems", "recent_alerts"],
        "language": body.language or "en",
        "show_addresses": bool(body.show_addresses),
        "refresh_s": body.refresh_s or 30,
    }
    try:
        sid = (
            await session.execute(
                text(
                    "INSERT INTO kiosk_screens (name, enabled, token_hash, token_prefix, "
                    "allowed_sources, scope_group_id, widgets, language, show_addresses, "
                    "refresh_s) VALUES (:name, :enabled, :h, :p, :allowed_sources, "
                    ":scope_group_id, :widgets, :language, :show_addresses, :refresh_s) "
                    "RETURNING id"
                ),
                {**values, "h": kiosk.token_hash(token), "p": kiosk.token_prefix(token)},
            )
        ).scalar_one()
    except IntegrityError:
        await session.rollback()
        raise ApiError(
            409, "conflict", "A kiosk screen with this name already exists", [{"field": "name"}]
        ) from None
    await audit.record(
        session,
        ctx,
        "kiosk_screen.create",
        audit.entity("kiosk_screen", sid, values["name"]),
        audit.created(_audit_values(values)),
    )
    await session.commit()
    rt.config_changed("kiosk_screen", sid, "created")
    base = await settings_p3c.public_base_url(session)
    return {"token": token, "url": _url(base, token), "item": screen_out(await _get(session, sid))}


@router.patch("/api/kiosk-screens/{sid}")
async def patch_screen(sid: int, body: ScreenIn, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    _manage(rt)
    row = await _get(session, sid)
    before = {k: getattr(row, k) for k in _audit_values({}).keys()}
    before["allowed_sources"] = list(row.allowed_sources or [])
    before["widgets"] = list(row.widgets or [])
    after = dict(before)
    for k in body.model_fields_set:
        v = getattr(body, k)
        if v is None and k != "scope_group_id":
            continue
        after[k] = v
    await _check_group(session, after["scope_group_id"])
    try:
        await session.execute(
            text(
                "UPDATE kiosk_screens SET name = :name, enabled = :enabled, "
                "allowed_sources = :allowed_sources, scope_group_id = :scope_group_id, "
                "widgets = :widgets, language = :language, show_addresses = :show_addresses, "
                "refresh_s = :refresh_s, updated_at = now() WHERE id = :id"
            ),
            {**after, "id": sid},
        )
    except IntegrityError:
        await session.rollback()
        raise ApiError(
            409, "conflict", "A kiosk screen with this name already exists", [{"field": "name"}]
        ) from None
    changes = audit.diff(before, after)
    if changes:
        await audit.record(
            session,
            ctx,
            "kiosk_screen.update",
            audit.entity("kiosk_screen", sid, after["name"]),
            changes,
        )
    await session.commit()
    rt.config_changed("kiosk_screen", sid, "updated")
    hub = _hub(rt)
    if not after["enabled"]:
        await hub.close_screen(sid)
    else:
        await hub.refresh(sid)
    return screen_out(await _get(session, sid))


@router.post("/api/kiosk-screens/{sid}/regenerate")
async def regenerate(sid: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> dict:
    _manage(rt)
    row = await _get(session, sid)
    token = kiosk.new_token()
    await session.execute(
        text(
            "UPDATE kiosk_screens SET token_hash = :h, token_prefix = :p, updated_at = now() "
            "WHERE id = :i"
        ),
        {"h": kiosk.token_hash(token), "p": kiosk.token_prefix(token), "i": sid},
    )
    await audit.record(
        session, ctx, "kiosk_screen.regenerate", audit.entity("kiosk_screen", sid, row.name)
    )
    await session.commit()
    await _hub(rt).close_screen(sid)
    rt.config_changed("kiosk_screen", sid, "updated")
    base = await settings_p3c.public_base_url(session)
    return {"token": token, "url": _url(base, token), "item": screen_out(await _get(session, sid))}


@router.delete("/api/kiosk-screens/{sid}", status_code=204)
async def delete_screen(sid: int, _: Admin, rt: RT, session: DB, ctx: Audit) -> Response:
    _manage(rt)
    row = await _get(session, sid)
    await audit.record(
        session,
        ctx,
        "kiosk_screen.delete",
        audit.entity("kiosk_screen", sid, row.name),
        audit.deleted({"name": row.name}),
    )
    await session.execute(text("DELETE FROM kiosk_screens WHERE id = :i"), {"i": sid})
    await session.commit()
    await _hub(rt).close_screen(sid)
    rt.config_changed("kiosk_screen", sid, "deleted")
    return Response(status_code=204)


# ---------------------------------------------------------------- kiosk access
async def resolve_screen(rt: Any, session: Any, token: str | None, ip: str) -> tuple[Any, str]:
    """(screen row, "") on success, (None, reason) otherwise."""
    if not feature_on(rt, "noc"):
        return None, "feature_off"
    if not kiosk.plausible_token(token):
        return None, "bad_token"
    assert token is not None
    row = (
        await session.execute(
            text(f"SELECT {COLUMNS} {FROM} WHERE k.token_hash = :h"),
            {"h": kiosk.token_hash(token)},
        )
    ).first()
    if row is None or not kiosk.hash_matches(token, row.token_hash):
        return None, "bad_token"
    if not row.enabled:
        return None, "disabled"
    if not kiosk.guard(ip, list(row.allowed_sources or [])):
        return None, "ip"
    return row, ""


def _not_found() -> JSONResponse:
    return error_response(404, "not_found", "Not found", headers={"Cache-Control": "no-store"})


async def _seen(rt: Any, session: Any, row: Any, ip: str) -> None:
    now = rt.clock.now()
    if (
        row.last_seen_at is None
        or (now - row.last_seen_at).total_seconds() >= 60
        or (row.last_seen_ip != ip)
    ):
        await session.execute(
            text("UPDATE kiosk_screens SET last_seen_at = :t, last_seen_ip = :ip WHERE id = :i"),
            {"t": now, "ip": ip[:45], "i": row.id},
        )
        await session.commit()


@router.get("/api/kiosk/view")
async def kiosk_view(request: Request, rt: RT, session: DB) -> Response:
    ip = client_ip(request, rt)
    lim = _limiter(request)
    if lim.blocked(ip):
        return error_response(429, "rate_limited", "Too many requests")
    row, reason = await resolve_screen(rt, session, request.headers.get(kiosk.HEADER), ip)
    if row is None:
        lim.fail(ip, reason)
        log.info("Kiosk request refused (%s) from %s", reason, ip)
        return _not_found()
    view = await build_view(rt, session, row)
    await _seen(rt, session, row, ip)
    return JSONResponse(view, headers={"Cache-Control": "no-store"})


@router.websocket("/ws/kiosk")
async def kiosk_ws(ws: WebSocket) -> None:
    rt = ws.app.state.rt
    await ws.accept()
    ip = client_ip(ws, rt)
    lim = _limiter(ws)
    if lim.blocked(ip):
        await ws.close(code=4401)
        return
    try:
        raw = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_S)
        msg = json.loads(raw)
    except (TimeoutError, ValueError, WebSocketDisconnect, RuntimeError, KeyError):
        lim.fail(ip, "ws_auth")
        await _close(ws, 4401)
        return
    token = msg.get("token") if isinstance(msg, dict) and msg.get("type") == "kiosk_auth" else None
    async with rt.sessionmaker() as session:
        row, reason = await resolve_screen(rt, session, token, ip)
        if row is not None:
            await _seen(rt, session, row, ip)
    if row is None:
        lim.fail(ip, reason)
        log.info("Kiosk WebSocket refused (%s) from %s", reason, ip)
        await _close(ws, 4401)
        return
    hub = _hub(rt)
    hub.add(row.id, ws)
    try:
        await ws.send_text(json.dumps({"type": "kiosk.ready"}))
        while True:
            raw = await ws.receive_text()
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("Kiosk WebSocket closed: %s", exc)
    finally:
        hub.remove(row.id, ws)


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code=code)
    except Exception:  # noqa: BLE001
        pass
