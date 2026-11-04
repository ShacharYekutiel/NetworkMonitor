"""Programmatic Alembic upgrade."""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from netmon.config import PACKAGE_DIR


def _alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(PACKAGE_DIR / "migrations"))
    return cfg


async def run_migrations(engine: AsyncEngine) -> None:
    cfg = _alembic_config()

    def _upgrade(sync_conn) -> None:  # type: ignore[no-untyped-def]
        cfg.attributes["connection"] = sync_conn
        command.upgrade(cfg, "head")

    async with engine.connect() as conn:
        await conn.run_sync(_upgrade)
        await conn.commit()
