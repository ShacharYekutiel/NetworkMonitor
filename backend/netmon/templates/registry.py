"""[2b-2] The current template registry: built-in library + user directory, swapped atomically
on reload (docs/data-model.md "Locations, precedence")."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from netmon.templates.library import load_dir

if TYPE_CHECKING:
    from netmon.runtime import Runtime

log = logging.getLogger("netmon.templates")

BUILTIN_DIR = Path(__file__).resolve().parent / "library"

# module-level views kept for compatibility: updated IN PLACE on every (re)load
TEMPLATES: list[dict[str, Any]] = []
BY_ID: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class Registry:
    templates: list[dict[str, Any]] = field(default_factory=list)  # priority desc, id asc
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    loaded_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    builtin_count: int = 0
    user_count: int = 0
    overridden: list[str] = field(default_factory=list)
    user_dir: str = ""


def sort_key(t: dict[str, Any]) -> tuple[int, str]:
    return (-int(t["priority"]), str(t["id"]))


def build(builtin_root: Path, user_root: Path | None) -> Registry:
    """Synchronous file I/O: load both sources and merge (a valid user template replaces the
    built-in template with the same id)."""
    builtin, errors = load_dir(builtin_root, "builtin")
    for e in errors:
        log.error("Built-in template error in %s (%s): %s", e["file"], e["path"], e["message"])
    user: list[dict[str, Any]] = []
    if user_root is not None:
        user, user_errors = load_dir(user_root, "user")
        for e in user_errors:
            log.warning("User template error in %s (%s): %s", e["file"], e["path"], e["message"])
        errors = errors + user_errors
    merged = {t["id"]: t for t in builtin}
    builtin_ids = set(merged)
    overridden = []
    for t in user:
        if t["id"] in builtin_ids:
            t["overrides_builtin"] = True
            overridden.append(t["id"])
        merged[t["id"]] = t
    templates = sorted(merged.values(), key=sort_key)
    return Registry(
        templates=templates,
        by_id={t["id"]: t for t in templates},
        errors=errors,
        loaded_at=datetime.now(UTC),
        builtin_count=len(builtin),
        user_count=len(user),
        overridden=sorted(overridden),
        user_dir=str(user_root.resolve()) if user_root is not None else "",
    )


_current: Registry = Registry()


def current() -> Registry:
    return _current


def install(reg: Registry) -> None:
    """Swap the registry atomically (one assignment) and refresh the module-level views."""
    global _current
    _current = reg
    TEMPLATES[:] = reg.templates
    BY_ID.clear()
    BY_ID.update(reg.by_id)


def load_builtin() -> Registry:
    reg = build(BUILTIN_DIR, None)
    install(reg)
    return reg


def user_dir(rt: Runtime) -> Path:
    return Path(rt.settings.data_dir) / "templates"


def _ensure_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create the user template directory %s: %s", path, exc)


async def reload(rt: Runtime, *, broadcast: bool = True) -> Registry:
    """Re-read the built-in library and the user directory and swap the registry. Never raises
    for bad user files (they are reported in `errors`)."""
    udir = user_dir(rt)
    await asyncio.to_thread(_ensure_dir, udir)
    reg = await asyncio.to_thread(build, BUILTIN_DIR, udir)
    install(reg)
    log.info(
        "Loaded %d templates (%d built-in, %d user, %d errors)",
        len(reg.templates),
        reg.builtin_count,
        reg.user_count,
        len(reg.errors),
    )
    if broadcast:
        rt.hub.broadcast(
            "templates.reloaded",
            {
                "loaded_at": _iso(reg.loaded_at),
                "count": len(reg.templates),
                "error_count": len(reg.errors),
            },
        )
    return reg


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def library_status(reg: Registry, is_admin: bool) -> dict[str, Any]:
    """TemplateLibraryStatus (errors and user_dir for admins only)."""
    return {
        "loaded_at": _iso(reg.loaded_at),
        "builtin_count": reg.builtin_count,
        "user_count": reg.user_count,
        "overridden": list(reg.overridden),
        "user_dir": reg.user_dir if is_admin else "",
        "errors": [dict(e) for e in reg.errors] if is_admin else [],
    }
