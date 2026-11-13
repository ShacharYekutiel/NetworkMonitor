"""Command line: netmon serve | migrate [--to REV] | secrets ... | reset-password <username>."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from pathlib import Path

from netmon import __version__
from netmon.config import load_settings

H11_MAX_HEADER_BYTES = 65536  # [3] architecture § 13.5.1


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)
    # [1.4.2] httpx / httpcore log request URLs (Telegram bot token, signed Teams / webhook
    # URLs) at INFO / DEBUG: never, whatever NETMON_LOG_LEVEL is
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


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


def _existing_secret_key(settings) -> str | None:  # type: ignore[no-untyped-def]
    """The server's secret.key, never created here (a new key would not open the database's
    encrypted values)."""
    path = settings.data_dir / "secret.key"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip() or None
    return None


async def _migrate(target: str | None = None) -> int:
    from netmon.db import timescale
    from netmon.db.engine import make_engine, make_sessionmaker, wait_for_db
    from netmon.db.migrate import db_revision, run_migrations
    from netmon.services import settings as settings_svc

    settings = load_settings()
    engine = make_engine(settings.async_database_url)
    try:
        await wait_for_db(engine)
        if target is not None:
            if not await lock_available(settings.async_database_url):
                print("error: stop the NetworkMonitor server first", file=sys.stderr)
                return 3
            await run_migrations(engine, secret_key=_existing_secret_key(settings), target=target)
            print(f"Database at migration {await db_revision(engine)}.")
            return 0
        await run_migrations(engine, secret_key=_existing_secret_key(settings))
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


async def _secrets(action: str, old_key_file: str | None = None) -> int:
    """[1.4.2] `netmon secrets status|remove-plaintext|restore-plaintext|rekey`."""
    from sqlalchemy import text

    from netmon.db.engine import make_engine, wait_for_db
    from netmon.db.migrate import db_revision
    from netmon.services import rekey, secretbox

    settings = load_settings()
    key = _existing_secret_key(settings)
    if not key:
        print(f"error: {settings.data_dir / 'secret.key'} not found", file=sys.stderr)
        return 1
    engine = make_engine(settings.async_database_url)
    try:
        await wait_for_db(engine, timeout_s=10)
        rev = await db_revision(engine)
        if rev != "0012":
            print(
                f"error: the database is at migration {rev}, this command needs 0012",
                file=sys.stderr,
            )
            return 1
        async with engine.connect() as conn:
            removed = (
                await conn.execute(
                    text("SELECT value FROM settings WHERE key = :k"), {"k": secretbox.STATE_KEY}
                )
            ).scalar() is True
        if action == "status":
            print(
                "Plaintext copies: "
                + ("removed (encrypted only)" if removed else "kept (1.4.1 can still read them)")
            )
            return 0
        if action in ("remove-plaintext", "restore-plaintext"):
            keep = action == "restore-plaintext"
            async with engine.begin() as conn:
                counts = await conn.run_sync(lambda c: secretbox.transform_all(c, key, keep))
                await conn.execute(
                    text(
                        "INSERT INTO settings (key, value) VALUES (:k, CAST(:v AS jsonb)) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                    ),
                    {"k": secretbox.STATE_KEY, "v": "false" if keep else "true"},
                )
            print(f"Done {counts}. Restart the server so new writes follow the same form.")
            return 0
        if action == "rekey":
            if not await lock_available(settings.async_database_url):
                print("error: stop the NetworkMonitor server first", file=sys.stderr)
                return 3
            import os
            import secrets as _secrets_mod

            path = settings.data_dir / "secret.key"
            if old_key_file:  # finish a re-key whose key file was already replaced
                old = Path(old_key_file).read_text(encoding="utf-8").strip()  # noqa: ASYNC240
                new = key
            else:
                old, new = key, _secrets_mod.token_urlsafe(64)
            async with engine.begin() as conn:
                n = await conn.run_sync(lambda c: rekey.rekey_all(c, old, new))
                if not old_key_file:
                    tmp = path.with_name("secret.key.new")
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    try:
                        os.write(fd, new.encode("ascii"))
                    finally:
                        os.close(fd)
            if not old_key_file:
                backup = path.with_name("secret.key.old")
                os.replace(path, backup)
                os.replace(path.with_name("secret.key.new"), path)
                print(
                    f"Re-encrypted {n} row(s); new secret.key written (old one: {backup}, "
                    "delete it once the server runs). Every user has to sign in again."
                )
            else:
                print(f"Re-encrypted {n} row(s) with the current secret.key.")
            return 0
    finally:
        await engine.dispose()
    print(f"error: unknown action {action}", file=sys.stderr)
    return 2


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
    p_mig = sub.add_parser("migrate", help="apply database migrations and the Timescale bootstrap")
    p_mig.add_argument(
        "--to",
        default=None,
        metavar="REV",
        help="[1.4.2] migrate up or DOWN to this revision (server stopped), e.g. 0011 "
        "before running an older release on this database",
    )
    p_sec = sub.add_parser("secrets", help="[1.4.2] stored secrets: plaintext copies, re-key")
    p_sec.add_argument(
        "action", choices=["status", "remove-plaintext", "restore-plaintext", "rekey"]
    )
    p_sec.add_argument(
        "--old-key-file",
        default=None,
        help="rekey: the previous secret.key, when the key file was already replaced",
    )
    p_reset = sub.add_parser("reset-password", help="set a new random password for a user")
    p_reset.add_argument("username")
    args = parser.parse_args(argv)
    if args.command == "serve":
        sys.exit(cmd_serve(args))
    if args.command == "migrate":
        _setup_logging("info")
        sys.exit(_run_cli(_migrate(args.to)))
    if args.command == "secrets":
        _setup_logging("warning")
        sys.exit(_run_cli(_secrets(args.action, args.old_key_file)))
    if args.command == "reset-password":
        _setup_logging("error")
        sys.exit(_run_cli(_reset_password(args.username)))
    parser.print_help()
    sys.exit(2)
