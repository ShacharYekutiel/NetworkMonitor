"""Command line: netmon serve | migrate | reset-password <username>."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys

from netmon import __version__
from netmon.config import load_settings

H11_MAX_HEADER_BYTES = 65536  # [3] architecture § 13.5.1


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = load_settings()
    _setup_logging(settings.netmon_log_level)
    host = args.host or settings.netmon_host
    port = args.port or settings.netmon_port
    try:
        free = asyncio.run(lock_available(settings.async_database_url))
    except Exception:  # noqa: BLE001 - DB not up yet: the app's startup retry handles it
        free = True
    if not free:
        print(
            "error: another NetworkMonitor instance is already running against this database",
            file=sys.stderr,
        )
        return 3
    if settings.netmon_dev:
        uvicorn.run(
            "netmon.app:create_app",
            factory=True,
            host=host,
            port=port,
            reload=True,
            reload_dirs=[str(__import__("pathlib").Path(__file__).parent)],
            log_level=settings.netmon_log_level,
            ws_ping_interval=30,
            ws_ping_timeout=30,
            h11_max_incomplete_event_size=H11_MAX_HEADER_BYTES,
        )
        return 0
    from netmon.app import create_app

    app = create_app(settings)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=settings.netmon_log_level,
        ws_ping_interval=30,
        ws_ping_timeout=30,
        # X-Forwarded-* is interpreted by the app itself (NETMON_TRUST_PROXY); uvicorn's
        # rewriting would trust the spoofable leftmost X-Forwarded-For entry.
        proxy_headers=False,
        workers=1,
        # [3] Kerberos SSO: Negotiate headers of users in many groups are 10-40 KB
        h11_max_incomplete_event_size=H11_MAX_HEADER_BYTES,
    )
    server = uvicorn.Server(config)
    server.run()
    # uvicorn exits with code 3 itself when the lifespan startup fails (e.g. lock held)
    if not server.started:
        return 3
    return 0


async def lock_available(url: str) -> bool:
    """Pre-flight check of the single-instance advisory lock (clean exit message, no traceback).
    The real lock is taken during startup."""
    from sqlalchemy import text

    from netmon.db.engine import make_engine
    from netmon.runtime import ADVISORY_LOCK_ID

    engine = make_engine(url)
    try:
        async with engine.connect() as conn:
            got = (
                await conn.execute(
                    text("SELECT pg_try_advisory_lock(:id)"), {"id": ADVISORY_LOCK_ID}
                )
            ).scalar()
            if got:
                await conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": ADVISORY_LOCK_ID})
            await conn.commit()
            return bool(got)
    finally:
        await engine.dispose()


async def _migrate() -> int:
    from netmon.db import timescale
    from netmon.db.engine import make_engine, make_sessionmaker, wait_for_db
    from netmon.db.migrate import run_migrations
    from netmon.services import settings as settings_svc

    settings = load_settings()
    engine = make_engine(settings.async_database_url)
    try:
        await wait_for_db(engine)
        await run_migrations(engine)
        async with make_sessionmaker(engine)() as session:
            days = await settings_svc.get(session, "retention_days")
        enabled = await timescale.bootstrap(
            engine, int(days), disabled=settings.netmon_disable_timescale
        )
        print(f"Migrations applied. TimescaleDB: {'enabled' if enabled else 'not available'}")
    finally:
        await engine.dispose()
    return 0


async def _reset_password(username: str) -> int:
    from sqlalchemy import func, select

    from netmon.db.engine import make_engine, make_sessionmaker, wait_for_db
    from netmon.models import User
    from netmon.services.auth import hash_password

    settings = load_settings()
    engine = make_engine(settings.async_database_url)
    try:
        await wait_for_db(engine, timeout_s=10)
        async with make_sessionmaker(engine)() as session:
            user = (
                await session.execute(
                    select(User).where(func.lower(User.username) == username.lower())
                )
            ).scalar_one_or_none()
            if user is None:
                print(f"User {username!r} not found", file=sys.stderr)
                return 1
            password = secrets.token_urlsafe(12)
            user.password_hash = await hash_password(password)
            user.token_version += 1
            user.is_active = True
            await session.commit()
        print(f"New password for {user.username}: {password}")
    finally:
        await engine.dispose()
    return 0


def _run_cli(coro) -> int:  # type: ignore[no-untyped-def]
    try:
        return int(asyncio.run(coro))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        print(f"error: {first}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="netmon", description="NetworkMonitor")
    parser.add_argument("--version", action="version", version=f"netmon {__version__}")
    sub = parser.add_subparsers(dest="command")
    p_serve = sub.add_parser("serve", help="run the web server, poller and alert engine")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    sub.add_parser("migrate", help="apply database migrations and the Timescale bootstrap")
    p_reset = sub.add_parser("reset-password", help="set a new random password for a user")
    p_reset.add_argument("username")
    args = parser.parse_args(argv)
    if args.command == "serve":
        sys.exit(cmd_serve(args))
    if args.command == "migrate":
        _setup_logging("info")
        sys.exit(_run_cli(_migrate()))
    if args.command == "reset-password":
        _setup_logging("error")
        sys.exit(_run_cli(_reset_password(args.username)))
    parser.print_help()
    sys.exit(2)
