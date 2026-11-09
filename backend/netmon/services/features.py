"""[2b-3] Feature toggles (architecture section 12.0, data-model "Feature toggles").

Every 2b-3 feature is behind a `feature_<name>_enabled` setting (default false). `rt.features`
(`FeatureManager`) holds the current values and starts/stops the runtime parts of a feature
without a restart. Routers guard their endpoints with the `feature_disabled(...)` dependency:

    from netmon.services.features import feature_disabled
    router = APIRouter(dependencies=[Depends(feature_disabled("audit"))])
    @router.get("/api/x", dependencies=[Depends(feature_disabled("traps", "syslog"))])  # any-of

or call `require(rt, "lag")` / `rt.features.enabled("lag")` inline. A disabled feature
answers `409 feature_disabled` with `details: [{"feature": "<name>"}]`.

Other modules can react to a change with `rt.features.add_listener(callback)`; the callback
(sync or async) gets `(name, on)` after the built-in start/stop of that feature.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import Request

from netmon.api.errors import ApiError

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.features")

FEATURES: tuple[str, ...] = (
    "traps",
    "syslog",
    "event_alerts",
    "lag",
    "ldap",
    "audit",
    "timeline",
    "flapping",
    "siem_forwarding",
)
# Teams notifications and API tokens were toggles before; they are now always available. A
# stored `feature_teams_enabled` / `feature_api_tokens_enabled` row is ignored (settings
# `get_all` only reads known keys) and `PUT /api/settings` ignores those keys like any other
# unknown key.
APPLY_TIMEOUT_S = 5.0

Listener = Callable[[str, bool], Awaitable[None] | None]


def setting_key(name: str) -> str:
    return f"feature_{name}_enabled"


SETTING_KEYS = {setting_key(n): n for n in FEATURES}


def feature_error(*names: str) -> ApiError:
    which = " or ".join(names)
    return ApiError(
        409,
        "feature_disabled",
        f"The feature {which} is turned off",
        [{"feature": n} for n in names],
    )


def require(rt: Any, *names: str) -> None:
    """Raise 409 feature_disabled unless at least one of `names` is ON."""
    features = getattr(rt, "features", None)
    if features is None or not any(features.enabled(n) for n in names):
        raise feature_error(*names)


def feature_disabled(*names: str) -> Callable[[Request], None]:
    """Router dependency: 409 `feature_disabled` unless one of `names` is ON."""
    for n in names:
        if n not in FEATURES:
            raise ValueError(f"unknown feature {n!r}")

    def dependency(request: Request) -> None:
        require(request.app.state.rt, *names)

    dependency.__name__ = f"feature_{'_or_'.join(names)}"
    return dependency


class FeatureManager:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self.values: dict[str, bool] = dict.fromkeys(FEATURES, False)
        self._listeners: list[Listener] = []
        self._lock = asyncio.Lock()
        self.started = False

    # ---------------------------------------------------------------- read
    def enabled(self, name: str) -> bool:
        return bool(self.values.get(name, False))

    def snapshot(self) -> dict[str, bool]:
        return {n: bool(self.values.get(n, False)) for n in FEATURES}

    def add_listener(self, callback: Listener) -> None:
        self._listeners.append(callback)

    async def load(self) -> dict[str, bool]:
        from netmon.services import settings as settings_svc

        async with self.rt.sessionmaker() as session:
            values = await settings_svc.get_all(session)
        self.values = {n: bool(values.get(setting_key(n), False)) for n in FEATURES}
        return self.snapshot()

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Startup: load the toggles and start what is ON (forwarders -> pipeline -> rules
        -> flapping -> receivers, see netmon.ingest.service)."""
        await self.load()
        ingest = getattr(self.rt, "ingest", None)
        if ingest is not None:
            await ingest.start(self.snapshot())
        self.started = True

    async def stop(self) -> None:
        ingest = getattr(self.rt, "ingest", None)
        if ingest is not None:
            await ingest.stop()
        self.started = False

    async def apply(self, changed: dict[str, bool]) -> None:
        """After `PUT /api/settings` committed: start/stop the changed features and broadcast
        `features.changed`. A failing start leaves the toggle ON (the error is reported by the
        component, e.g. the ingest status)."""
        changed = {n: bool(v) for n, v in changed.items() if n in FEATURES}
        if not changed:
            return
        async with self._lock:
            effective = {n: v for n, v in changed.items() if self.values.get(n) != v}
            self.values.update(changed)
            for name, on in effective.items():
                await self._apply_one(name, on)
        self.rt.hub.broadcast("features.changed", {"features": self.snapshot()})

    async def _apply_one(self, name: str, on: bool) -> None:
        ingest = getattr(self.rt, "ingest", None)
        if ingest is not None and self.started:
            try:
                await asyncio.wait_for(ingest.feature_changed(name, on), APPLY_TIMEOUT_S * 2)
            except TimeoutError:
                log.warning("Applying feature %s=%s took too long", name, on)
            except Exception:  # noqa: BLE001 - a toggle never breaks the request
                log.exception("Applying feature %s=%s failed", name, on)
        for cb in list(self._listeners):
            try:
                res = cb(name, on)
                if inspect.isawaitable(res):
                    await asyncio.wait_for(res, APPLY_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                log.exception("Feature listener failed for %s", name)

    async def reset(self) -> None:
        """Tests: everything OFF (as on a fresh install)."""
        await self.apply(dict.fromkeys(FEATURES, False))
