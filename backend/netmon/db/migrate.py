"""Programmatic Alembic upgrade."""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy.ext.asyncio import AsyncEngine

from netmon.config import PACKAGE_DIR

log = logging.getLogger("netmon.db.migrate")


def _alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(PACKAGE_DIR / "migrations"))
    return cfg


def newer_database_revision(sync_conn, cfg: Config) -> str | None:  # type: ignore[no-untyped-def]
    """[1.4.1] The revision stored in the database when this version's migration scripts do not
    know it, i.e. the database was migrated by a NEWER release (the automatic container rollback
    after a failed deploy starts the previous image on the already migrated database).
    None = the database is at a known revision, or has no version table yet."""
    current = MigrationContext.configure(sync_conn).get_current_revision()
    if current is None:
        return None
    try:
        known = ScriptDirectory.from_config(cfg).get_revision(current)
    except CommandError:
        return current
    return None if known is not None else current


def current_revision(sync_conn) -> str | None:  # type: ignore[no-untyped-def]
    return MigrationContext.configure(sync_conn).get_current_revision()


async def db_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        return await conn.run_sync(current_revision)


async def run_migrations(
    engine: AsyncEngine, *, secret_key: str | None = None, target: str | None = None
) -> None:
    """Upgrade to this version's head. A database that is ahead of this version is left as it
    is (with a warning) instead of failing with "Can't locate revision": migrations are
    forward-only and additive, so the previous release keeps running on the newer schema after
    a rollback (ops/sync.sh never restores the database automatically).

    [1.4.2] `secret_key` is handed to data migrations that encrypt / decrypt stored secrets
    (0012). `target` (`netmon migrate --to REV`) upgrades or downgrades to that revision."""
    cfg = _alembic_config()

    def _upgrade(sync_conn) -> None:  # type: ignore[no-untyped-def]
        cfg.attributes["connection"] = sync_conn
        cfg.attributes["secret_key"] = secret_key
        if target is not None:
            script = ScriptDirectory.from_config(cfg)
            current = current_revision(sync_conn)
            goal = script.get_revision(target)
            if goal is None:
                raise CommandError(f"unknown revision {target!r}")
            ancestors = (
                {r.revision for r in script.iterate_revisions(current, "base")}
                if current
                else set()
            )
            if current and goal.revision in ancestors and goal.revision != current:
                command.downgrade(cfg, goal.revision)
            else:
                command.upgrade(cfg, goal.revision)
            return
        ahead = newer_database_revision(sync_conn, cfg)
        if ahead is not None:
            head = ScriptDirectory.from_config(cfg).get_current_head()
            log.warning(
                "The database is at migration %s, which this version does not know (its head "
                "is %s): it was migrated by a newer release. Starting without migrating; "
                "deploy the newer release again, or restore a pre-deploy dump (ops/restore.sh).",
                ahead,
                head,
            )
            return
        command.upgrade(cfg, "head")

    async with engine.connect() as conn:
        await conn.run_sync(_upgrade)
        await conn.commit()
