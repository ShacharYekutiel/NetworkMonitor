"""Load or generate ${NETMON_DATA_DIR}/secret.key (the JWT signing key)."""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("netmon.secrets")


def load_or_create_secret(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "secret.key"
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_urlsafe(64)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, value.encode("ascii"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows ACLs
        pass
    log.info("Generated new secret key at %s", path)
    return value
