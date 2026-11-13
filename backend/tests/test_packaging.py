"""Every data file under netmon/ must be listed in pyproject package-data, or the Docker
image (a wheel install) misses it: phase 3b shipped without assistant/risk_rules.yaml and the
config-diff explanation failed with FileNotFoundError in the owner's office test."""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "netmon"


def test_all_package_data_is_declared() -> None:
    globs = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"][
        "package-data"
    ]["netmon"]
    missing = []
    for f in PKG.rglob("*"):
        if not f.is_file() or f.suffix in (".py", ".pyc") or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(PKG).as_posix()
        if rel.startswith("static/"):
            continue  # built SPA, globbed as static/**
        if not any(fnmatch.fnmatch(rel, g) for g in globs):
            missing.append(rel)
    assert missing == [], f"add to [tool.setuptools.package-data]: {missing}"
