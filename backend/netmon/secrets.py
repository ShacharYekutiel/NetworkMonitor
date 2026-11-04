"""Load or generate ${NETMON_DATA_DIR}/secret.key (the JWT signing key)."""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("netmon.secrets")


def windows_acl_command(path: Path, user: str | None) -> list[str]:
    # SYSTEM (S-1-5-18) and Administrators (S-1-5-32-544), plus the account running the app
    cmd = ["icacls", str(path), "/inheritance:r", "/grant:r", "*S-1-5-18:F", "*S-1-5-32-544:F"]
    if user:
        cmd.append(f"{user}:F")
    return cmd


def restrict_windows_acl(path: Path) -> bool:
    """Best effort: drop the inherited ProgramData ACL from secret.key."""
    user = os.environ.get("USERNAME")
    domain = os.environ.get("USERDOMAIN")
    account = f"{domain}\\{user}" if user and domain else user
    try:
        res = subprocess.run(
            windows_acl_command(path, account), capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Cannot restrict the ACL of %s: %s", path, exc)
        return False
    if res.returncode != 0:
        log.warning("Cannot restrict the ACL of %s (icacls exit %s)", path, res.returncode)
        return False
    return True


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
    if sys.platform.startswith("win"):
        restrict_windows_acl(path)
    log.info("Generated new secret key at %s", path)
    return value
