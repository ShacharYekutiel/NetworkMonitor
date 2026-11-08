"""WebSocket /ws."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from netmon.api.errors import ApiError
from netmon.services.auth import COOKIE_NAME
from netmon.services.serialize import user_out

log = logging.getLogger("netmon.ws")
router = APIRouter()
WS_TOPICS = ("syslog", "trap")  # [2b-3]


def _origin_ok(ws: WebSocket, trust_proxy: bool) -> bool:
    from netmon.app import origin_matches

    origin = ws.headers.get("origin")
    return origin is None or origin_matches(origin, ws.headers, ws.url.scheme, trust_proxy)


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    rt = ws.app.state.rt
    from netmon.api.deps import authenticate

    await ws.accept()
    if not _origin_ok(ws, rt.settings.netmon_trust_proxy):
        await ws.close(code=1008)
        return
    if not await rt.has_users():
        await ws.close(code=4403)
        return
    try:
        async with rt.sessionmaker() as session:
            user, _ = await authenticate(rt, session, ws.cookies.get(COOKIE_NAME))
    except ApiError:
        await ws.close(code=4401)
        return
    from netmon.realtime.hub import make_message

    hello = make_message(
        "hello",
        {"user": user_out(user), "version": rt.version, "timescale": rt.timescale_enabled},
    )
    client = rt.hub.register(ws, first=hello, user_id=user.id, is_admin=user.role == "admin")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "ping":
                client.queue.put_nowait(make_message("pong", {}))
            elif isinstance(msg, dict) and msg.get("type") in ("subscribe", "unsubscribe"):
                # [2b-3] live views; unknown topics are ignored
                topics = msg.get("topics")
                wanted = (
                    {t for t in topics if t in WS_TOPICS} if isinstance(topics, list) else set()
                )
                if msg["type"] == "subscribe":
                    client.topics |= wanted
                else:
                    client.topics -= wanted
                client.queue.put_nowait(
                    make_message("subscribed", {"topics": sorted(client.topics)})
                )
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("WebSocket closed: %s", exc)
    finally:
        await rt.hub.unregister(client)
