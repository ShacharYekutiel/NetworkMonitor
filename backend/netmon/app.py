"""FastAPI application factory: routers, error envelope, guards, SPA."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.datastructures import Headers
from starlette.routing import Match
from starlette.staticfiles import StaticFiles

from netmon import __version__
from netmon.api import (  # [3]
    alerting,
    assistant_chat,  # [3b] ask / conversations (developer B)
    assistant_features,  # [3b] AI features (developer C)
    assistant_settings,  # [3b]
    audit,
    auth,
    chatack,
    configbackup,
    dashboard,
    devices,
    discovery,
    events,
    flows,
    groups,
    health,
    ingest,
    kiosk,
    ldap,
    maintenance,
    newdev,
    ports,
    reports,
    sensors,
    sso,
    templates,
    testclock,
    timeline,
    tokens,
    topology,
    ws,
)
from netmon.api import settings as settings_api
from netmon.api.errors import error_response, install_error_handlers
from netmon.auth.tokens import bearer_from_header, is_barred
from netmon.config import Settings, load_settings
from netmon.runtime import Runtime
from netmon.services.auth import LoginLimiter, cookie_secure, set_session_cookie

log = logging.getLogger("netmon.app")

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
SETUP_EXEMPT_PREFIXES = ("/api/setup", "/api/auth/")
# [3] multipart/form-data uploads; [3b] Roles Anywhere certificate
UPLOAD_PATHS = {"/api/sso/kerberos/keytab", "/api/assistant/cert"}
SETUP_EXEMPT_PATHS = {"/api/health", "/api/settings/public", "/api/setup"}

PLACEHOLDER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>NetworkMonitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family:sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem">
<h1>NetworkMonitor</h1>
<p>Frontend not built &mdash; run <code>npm run build</code> in frontend/.</p>
<p>The API is available at <a href="/api/health">/api/health</a>.</p>
</body></html>
"""


def _split_host(netloc: str) -> tuple[str, int | None]:
    parts = urlsplit(f"//{netloc}")
    try:
        port = parts.port
    except ValueError:
        port = None
    return (parts.hostname or "").lower(), port


def origin_matches(origin: str, headers: Headers, scheme: str, trust_proxy: bool) -> bool:
    """True when Origin's host:port equals the request Host (or X-Forwarded-Host if trusted).

    A missing port means the default port of the Origin's scheme on both sides (browsers omit
    default ports in both headers)."""
    o = urlsplit(origin)
    if not o.scheme or not o.netloc:
        return False
    default = 443 if o.scheme.lower() in ("https", "wss") else 80
    o_host, o_port = _split_host(o.netloc)
    host_header = headers.get("host", "")
    if trust_proxy and headers.get("x-forwarded-host"):
        host_header = headers["x-forwarded-host"].split(",")[0].strip()
    if not host_header:
        return False
    h_host, h_port = _split_host(host_header)
    return o_host == h_host and (o_port or default) == (h_port or default)


def _setup_exempt(path: str) -> bool:
    return path in SETUP_EXEMPT_PATHS or path.startswith(SETUP_EXEMPT_PREFIXES)


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
    start_options: dict[str, Any] | None = None,
    manage_runtime: bool = True,
) -> FastAPI:
    settings = settings or load_settings()
    rt = runtime or Runtime(settings)
    opts = start_options or {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if manage_runtime:
            await rt.start(**opts)
        try:
            yield
        finally:
            if manage_runtime:
                await rt.stop()

    app = FastAPI(
        title="NetworkMonitor",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.rt = rt
    app.state.login_limiter = LoginLimiter()
    # [2b-3] API tokens and Active Directory
    from netmon.auth.ldap import Directory
    from netmon.auth.tokens import InvalidTokenLimiter, UseStamper

    app.state.token_limiter = InvalidTokenLimiter()
    app.state.token_stamper = UseStamper()
    app.state.directory = Directory()
    install_error_handlers(app)

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        path = request.url.path
        trust = rt.settings.netmon_trust_proxy
        # [2b-3] API tokens: never on auth/users/tokens; no ambient credential → no CSRF check
        bearer = bearer_from_header(request.headers.get("authorization")) is not None
        if bearer and path.startswith("/api/") and is_barred(path):
            return error_response(403, "forbidden", "API tokens cannot be used on this endpoint")
        if request.method in MUTATING:
            origin = request.headers.get("origin")
            if (
                origin
                and not bearer
                and not origin_matches(origin, request.headers, request.url.scheme, trust)
            ):
                return error_response(403, "forbidden", "Cross-origin request rejected")
            if path.startswith("/api/") and path not in UPLOAD_PATHS:
                length = request.headers.get("content-length")
                has_body = (length is not None and length.strip() not in ("", "0")) or (
                    "transfer-encoding" in request.headers
                )
                if has_body:
                    ctype = request.headers.get("content-type", "")
                    if ctype.split(";")[0].strip().lower() != "application/json":
                        return error_response(
                            415, "unsupported_media_type", "Content-Type must be application/json"
                        )
        if path.startswith("/api/") and not _setup_exempt(path):
            try:
                if not await rt.has_users():
                    return error_response(403, "setup_required", "Initial setup is required")
            except Exception:  # noqa: BLE001
                log.exception("Setup check failed")
        response: Response = await call_next(request)
        token = getattr(request.state, "renew_token", None)
        if token:
            set_session_cookie(
                response, token, cookie_secure(request, rt.settings.netmon_cookie_secure, trust)
            )
        return response

    for r in (
        health.router,
        auth.router,
        ldap.router,  # [2b-3]
        audit.router,  # [2b-3]
        tokens.router,  # [2b-3]
        groups.router,
        devices.router,
        sensors.router,
        events.router,
        dashboard.router,
        alerting.router,
        settings_api.router,
        discovery.router,
        templates.router,
        maintenance.router,
        topology.router,
        ports.router,
        ingest.router,  # [2b-3]
        flows.router,  # [3]
        configbackup.router,  # [3]
        newdev.router,  # [3]
        sso.router,  # [3]
        reports.router,  # [3]
        kiosk.router,  # [3]
        chatack.router,  # [3]
        assistant_chat.router,  # [3b]
        assistant_settings.router,  # [3b] connection, test, certificate, usage, knowledge
        assistant_features.router,  # [3b] explain, incidents, diffs, digests, drafts
        timeline.router,
        testclock.router,
        ws.router,
    ):
        app.include_router(r)

    _mount_spa(app, settings.frontend_dist)
    return app


def _mount_spa(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    has_spa = index.is_file()
    if has_spa and (dist / "assets").is_dir():

        class ImmutableStatic(StaticFiles):
            async def get_response(self, path: str, scope: Any) -> Response:
                resp = await super().get_response(path, scope)
                if resp.status_code == 200:
                    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return resp

        app.mount("/assets", ImmutableStatic(directory=dist / "assets"), name="assets")

    dist_resolved = dist.resolve()

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def spa(path: str, request: Request) -> Response:
        if path == "api" or path.startswith("api/"):
            for route in app.router.routes:
                if route is request.scope.get("route"):
                    continue
                match, _ = route.matches({**request.scope, "type": "http"})
                if match == Match.PARTIAL:
                    return error_response(405, "method_not_allowed", "Method not allowed")
            return error_response(404, "not_found", "Not found")
        if request.method not in ("GET", "HEAD"):
            return error_response(405, "method_not_allowed", "Method not allowed")
        if not has_spa:
            return HTMLResponse(PLACEHOLDER, headers={"Cache-Control": "no-cache"})
        if path:
            candidate = (dist / path).resolve()
            try:
                candidate.relative_to(dist_resolved)
                inside = True
            except ValueError:
                inside = False
            if inside and candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
