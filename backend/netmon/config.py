"""Process configuration (env -> ${NETMON_DATA_DIR}/netmon.env -> defaults)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("netmon.config")

PACKAGE_DIR = Path(__file__).resolve().parent


def _is_docker() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("NETMON_IN_DOCKER") == "1"


def default_data_dir() -> Path:
    if _is_docker():
        return Path("/data")
    if sys.platform.startswith("win"):
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(base) / "NetworkMonitor"
    return Path("/var/lib/netmon")


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_data_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidate = default_data_dir()
    if _writable(candidate):
        return candidate
    fallback = Path("./data").resolve()
    log.warning("Data dir %s not writable, using %s", candidate, fallback)
    return fallback


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    database_url: str = Field(
        default="postgresql://netmon:netmon@localhost:5432/netmon", alias="DATABASE_URL"
    )
    netmon_data_dir: str | None = Field(default=None, alias="NETMON_DATA_DIR")
    netmon_db_password_file: str | None = Field(default=None, alias="NETMON_DB_PASSWORD_FILE")
    netmon_host: str = Field(default="0.0.0.0", alias="NETMON_HOST")
    netmon_port: int = Field(default=8080, alias="NETMON_PORT")
    netmon_log_level: Literal["debug", "info", "warning", "error"] = Field(
        default="info", alias="NETMON_LOG_LEVEL"
    )
    netmon_max_concurrent_checks: int = Field(default=100, alias="NETMON_MAX_CONCURRENT_CHECKS")
    netmon_frontend_dist: str | None = Field(default=None, alias="NETMON_FRONTEND_DIST")
    netmon_cookie_secure: str = Field(default="auto", alias="NETMON_COOKIE_SECURE")
    netmon_trust_proxy: bool = Field(default=False, alias="NETMON_TRUST_PROXY")
    netmon_dev: bool = Field(default=False, alias="NETMON_DEV")
    netmon_disable_timescale: bool = Field(default=False, alias="NETMON_DISABLE_TIMESCALE")
    netmon_telegram_api_base: str = Field(
        default="https://api.telegram.org", alias="NETMON_TELEGRAM_API_BASE"
    )
    netmon_discovery_concurrency: int = Field(
        default=64, ge=1, le=256, alias="NETMON_DISCOVERY_CONCURRENCY"
    )
    netmon_discovery_rate: int = Field(default=100, ge=1, le=1000, alias="NETMON_DISCOVERY_RATE")
    # [2b-1] QA only: enables PUT /api/test/clock. Never set by installers or compose.
    netmon_test_mode: bool = Field(default=False, alias="NETMON_TEST_MODE")

    # resolved values (filled by load_settings)
    data_dir: Path = Field(default=Path("./data"), exclude=True)

    @property
    def async_database_url(self) -> str:
        return normalize_db_url(self.database_url, self.netmon_db_password_file)

    @property
    def frontend_dist(self) -> Path:
        if self.netmon_frontend_dist:
            return Path(self.netmon_frontend_dist).expanduser().resolve()
        pkg_static = PACKAGE_DIR / "static"
        if (pkg_static / "index.html").is_file():
            return pkg_static
        return (PACKAGE_DIR.parent.parent / "frontend" / "dist").resolve()


def normalize_db_url(url: str, password_file: str | None = None) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgresql+psycopg2://") or url.startswith("postgresql+psycopg://"):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Cannot read NETMON_DB_PASSWORD_FILE: {exc}") from exc
        parts = urlsplit(url)
        netloc = parts.netloc
        userinfo, _, hostinfo = netloc.rpartition("@")
        user = userinfo.split(":", 1)[0] if userinfo else "netmon"
        netloc = f"{user}:{quote(password, safe='')}@{hostinfo}"
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def load_settings(**overrides: object) -> Settings:
    """Load settings with precedence env -> netmon.env -> defaults."""
    explicit_dir = os.environ.get("NETMON_DATA_DIR") or overrides.get("NETMON_DATA_DIR")
    data_dir = resolve_data_dir(str(explicit_dir) if explicit_dir else None)
    file_values = _read_env_file(data_dir / "netmon.env")
    merged: dict[str, object] = {}
    for key, value in file_values.items():
        if key.upper() not in {k.upper() for k in os.environ}:
            merged[key.upper()] = value
    merged.update({k: v for k, v in overrides.items()})
    settings = Settings(**merged)  # type: ignore[arg-type]
    settings.data_dir = data_dir
    return settings
