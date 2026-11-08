#!/usr/bin/env python3
"""Regenerate backend/netmon/data/oui.tsv.gz from the IEEE MA-L listing (maintainers only).

    python backend/scripts/update_oui.py                  # download oui.csv from IEEE
    python backend/scripts/update_oui.py --input oui.csv  # use a local copy

Stdlib only (urllib, csv, gzip). The download honours HTTPS_PROXY (urllib) and SSL_CERT_FILE.
The output is deterministic (gzip mtime 0): UTF-8 text sorted by prefix, one line per MA-L
assignment `AABBCC<TAB>Organization Name`, after `#` header comments. Commit the output as is;
never edit it by hand. The application never downloads this file."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import re
import sys
import urllib.request
from pathlib import Path

SOURCE = "https://standards-oui.ieee.org/oui/oui.csv"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "netmon" / "data" / "oui.tsv.gz"
NAME_MAX = 64
_WS = re.compile(r"\s+")
_HEX6 = re.compile(r"^[0-9A-F]{6}$")


def parse_csv(text: str) -> dict[str, str]:
    """IEEE oui.csv (Registry, Assignment, Organization Name, Organization Address) -> {prefix:
    name} for MA-L rows. Names: whitespace collapsed, trimmed, cut to 64 characters."""
    out: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("Registry") or "").strip() != "MA-L":
            continue
        prefix = (row.get("Assignment") or "").strip().upper()
        name = _WS.sub(" ", row.get("Organization Name") or "").strip()[:NAME_MAX].strip()
        if _HEX6.match(prefix) and name and prefix not in out:
            out[prefix] = name
    return out


def render(entries: dict[str, str], retrieved: str, source: str = SOURCE) -> bytes:
    lines = [f"# source: {source}", f"# retrieved: {retrieved}", f"# entries: {len(entries)}"]
    lines += [f"{p}\t{entries[p]}" for p in sorted(entries)]
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(("\n".join(lines) + "\n").encode("utf-8"))
    return buf.getvalue()


def download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "NetworkMonitor update_oui.py"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - fixed https URL
        return resp.read().decode("utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", type=Path, help="a local oui.csv instead of downloading it")
    ap.add_argument("--output", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--source", default=SOURCE, help="source URL written in the header")
    ap.add_argument("--retrieved", default=dt.date.today().isoformat(), help="header date")
    args = ap.parse_args(argv)
    text = (
        args.input.read_text(encoding="utf-8", errors="replace")
        if args.input
        else download(args.source)
    )
    entries = parse_csv(text)
    if len(entries) < 1000:
        print(f"refusing to write: only {len(entries)} MA-L entries parsed", file=sys.stderr)
        return 1
    data = render(entries, args.retrieved, args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(f"{len(entries)} entries, {len(data)} bytes -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
