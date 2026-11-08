#!/usr/bin/env python3
"""QA oracle for the SSH simulator: the 7 shipped drivers' values (copied from docs/data-model.md § [3] Config
backup drivers) and a reference implementation of the normalisation rules (architecture.md §13.2.5).

    python tests/sim/ssh/expected.py            # (re)write expected/<personality>/<version>.txt
    python tests/sim/ssh/expected.py --check    # exit 1 when a file is stale

The expected files are what the backend must store for the simulator's configs with masking ON (the default):
volatile lines removed (ignore_lines), secrets replaced by `<secret removed>`, LF line endings, no trailing
whitespace, one final newline. Importable: DRIVERS, PERSONALITY_DRIVER, normalize(text, driver, mask=True),
expected(personality, version, hostname, mask=True).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SECRET = "<secret removed>"

DRIVERS: dict[str, dict] = {
    "hpe_comware": {
        "mode": "shell",
        "prompt": r"^(<[^<>\r\n]{1,64}>|\[[^\[\]\r\n]{1,64}\])\s*$",
        "setup": ["screen-length disable"],
        "commands": ["display current-configuration"],
        "ignore_lines": [],
        "secrets": [r"\b(?:cipher|hash)\s+(?!authentication-mode\b|privacy-mode\b)(\S+)",
                    r"^\s*snmp-agent community (?:(?:read|write)\s+)?(?:simple\s+|cipher\s+)?(\S+)",
                    r"\b(?:authentication-mode|privacy-mode)\s+\S+\s+(?!cipher\b|simple\b)(\S+)",
                    r"\b(?:key|password)\s+(?:\S+\s+)?(?:simple|cipher)\s+(\S+)"],
    },
    "hpe_arubaos_switch": {
        "mode": "shell",
        "prompt": r"^[\w.\-]{1,64}(\([\w\-]+\))?# ?$",
        "pre_prompt": [("Press any key to continue", "\n")],
        "setup": ["no page"],
        "commands": ["show running-config"],
        "ignore_lines": [r"^Running configuration:$"],
        "secrets": [r'^\s*password (?:manager|operator|port-access) .*?(?:sha1|sha256|plaintext) "?((?<=")[^"]+|[^"\s]+)',
                    r'\bpassword\b.*?\b(?:sha1|sha256|plaintext) "?((?<=")[^"]+|[^"\s]+)',
                    r'^\s*snmp-server community "([^"]+)"',
                    r'\bkey "?((?<=")[^"]+|[^"\s]+)',
                    r'\b(?:auth|priv)\s+(?:md5|sha\S*|des|3des|aes\S*)\s+"?((?<=")[^"]+|[^"\s]+)'],
    },
    "aruba_cx": {
        "mode": "shell",
        "prompt": r"^[\w.\-]{1,64}# ?$",
        "setup": ["no page"],
        "commands": ["show running-config"],
        "ignore_lines": [r"^Current configuration:$"],
        "secrets": [r"\bciphertext (\S+)",
                    r"\bplaintext (\S+)",
                    r"^\s*snmp-server community (\S+)"],
    },
    "checkpoint_gaia": {
        "mode": "exec",
        "commands": ["show configuration"],
        "fallback": {"show configuration": 'clish -c "show configuration"'},
        "ignore_lines": [r"^# Exported by \S+ on ", r"^# Configuration of \S+$"],
        "secrets": [r"password-hash (\S+)",
                    r"^set snmp community (\S+)",
                    r"^set expert-password-hash (\S+)",
                    r"\b(?:auth|privacy)-pass-(?:phrase|hash) (\S+)",
                    r"(?<![\w-])secret (\S+)",
                    r"^(?:add|set) aaa tacacs-servers .*?\bkey (\S+)"],
    },
    "cisco_ios": {
        "mode": "shell",
        "prompt": r"^[\w.\-:/]{1,64}[>#] ?$",
        "enable_when": r">\s*$",
        "setup": ["terminal length 0", "terminal width 0"],
        "commands": ["show running-config"],
        "ignore_lines": [r"^! (Last configuration change|NVRAM config last updated) at", r"^Building configuration",
                         r"^Current configuration : \d+ bytes", r"^ntp clock-period"],
        "secrets": [r"^\s*enable (?:secret|password)(?: \d)? (\S+)",
                    r"^\s*username \S+ .*?(?:secret|password)(?: \d)? (\S+)",
                    r"^\s*snmp-server community (\S+)",
                    r"^\s*snmp-server host \S+ (?:informs |traps )?(?:version (?:1|2c|3 \S+) )?(\S+)",
                    r"\bpre-shared-key(?: (?:local|remote))?(?: \d)? (\S+)",
                    r"\bkey(?:-string)?(?: \d+)?(?: md5)?(?: \d)? (\S+)",
                    r"(?<![\w-])password(?: \d)? (\S+)",
                    r"\bauth (?:md5|sha\S*) (\S+)",
                    r"\bpriv (?:des\S*|3des|aes(?: \d+)?) (\S+)"],
    },
    "fortigate": {
        "mode": "exec",
        "commands": ["show full-configuration"],
        "ignore_lines": [r"^#conf_file_ver=", r"^#buildno="],
        "secrets": [r"\bset [\w-]+ ENC (\S+)"],
    },
    "mikrotik_routeros": {
        "mode": "exec",
        "commands": ["/export"],
        "ignore_lines": [r"^# \S+ \d\d:\d\d:\d\d by RouterOS", r"^# \d{4}-\d\d-\d\d \d\d:\d\d:\d\d by RouterOS",
                         r"^# software id = "],
        "secrets": [r'\b[\w-]*(?:password|secret|passphrase|pre-shared-key|private-key|auth-key|psk)=("[^"]*"|\S+)'],
    },
}

PERSONALITY_DRIVER = {
    "comware": "hpe_comware", "arubaos_switch": "hpe_arubaos_switch", "aruba_cx": "aruba_cx",
    "checkpoint_clish": "checkpoint_gaia", "checkpoint_bash": "checkpoint_gaia", "cisco_ios": "cisco_ios",
    "fortigate": "fortigate", "mikrotik": "mikrotik_routeros",
}
PERSONALITY_CONFIG_DIR = {"checkpoint_clish": "checkpoint", "checkpoint_bash": "checkpoint"}

# Secret values present in the simulator configs: none of them may appear in a masked stored version.
FIXTURE_SECRETS = [
    "$c$3$Qk9NQkFSRElBX1JFQURfQ09NTVVOSVRZ", "$c$3$YXV0aEtleVNlY3JldA==", "$c$3$cHJpdktleVNlY3JldA==",
    "$c$3$cmFkaXVzU2hhcmVkU2VjcmV0", "$h$6$Wm9vbWVyYW5nV2luZG93", "$h$6$TmV0bW9uQmFja3Vw",       # comware
    "Pr1vateCommunity", "R4diusSecret", "8c6976e5b5410415bde908bd4dee15dfb167a9c8",
    "fe703d258c7ef5f50b71e06565a65aa07194907f",                                                  # arubaos
    "AQBapWdmY2NhdGhpc0lzQUNpcGhlcnRleHRWYWx1ZQ==", "AQBapZmFrZUJhY2t1cENpcGhlcnRleHQ=", "Cx5ecretComm",  # cx
    "$1$Rb2bVQ$Ks9q0m3Ge0Gyd3b4kM7Lz/", "Gw5nmpComm", "$6$rounds=10000$aBcDeFgHiJkLmNoP",
    "$6$rounds=10000$QrStUvWxYz012345",                                                          # checkpoint
    "$9$nhEmQVczB7dqsO", "$9$6Ux3jHw8rBp1wE", "$9$QW9OL3bAXuK7mE", "R0uterComm", "0822455D0A16544541",  # cisco
    "SH2xR3mpC8aWZqFdj9mVr1YeTEbm4fWqXoaFhhvMWl6pN0PvX0eY4AqAl8=", "SH2bmV0bW9uYmFja3VwSGFzaGVkUGFzc3dvcmRGb3J0aUdhdGU=",
    "M2Nk3x7Yq0aZ4b5c6d7e8f9g0h1i2j3k4l5m6n7o8p9q0r1s2t3u==",                                   # fortigate
]


def leaked_secrets(text: str) -> list[str]:
    return [s for s in FIXTURE_SECRETS if s in text]


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\x1b[=>78]")


def strip_overstrike(line: str) -> str:
    out: list[str] = []
    for ch in line:
        if ch == "\x08":
            if out:
                out.pop()
        else:
            out.append(ch)
    return "".join(out)


def normalize(text: str, driver: str, mask: bool = True) -> str:
    """Normalise a command's output (echo and trailing prompt already removed by the session engine)."""
    d = DRIVERS[driver]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = ANSI_RE.sub("", text)
    lines = [strip_overstrike(x).rstrip() for x in text.split("\n")]
    ign = [re.compile(p) for p in d["ignore_lines"]]
    lines = [x for x in lines if not any(r.search(x) for r in ign)]
    if mask:
        sec = [re.compile(p) for p in d["secrets"]]
        lines = [mask_line(x, sec) for x in lines]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def mask_line(line: str, regexes: list[re.Pattern]) -> str:
    """Every regex is matched against the ORIGINAL line; the union of the group-1 spans is replaced (so an
    already masked value is never masked again: the contract vector `snmp-agent community read cipher $c$3$xyz`
    -> `... cipher <secret removed>`, not `<secret removed> removed>` as a sequential re.sub would give)."""
    spans: list[tuple[int, int]] = []
    for r in regexes:
        for m in r.finditer(line):
            if m.lastindex and m.group(1) is not None and m.end(1) > m.start(1):
                spans.append((m.start(1), m.end(1)))
    if not spans:
        return line
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, pos = [], 0
    for a, b in merged:
        out.append(line[pos:a])
        out.append(SECRET)
        pos = b
    out.append(line[pos:])
    return "".join(out)


def expected(personality: str, version: str, hostname: str, mask: bool = True) -> str:
    from fake_device import load_personality, render_config  # noqa: PLC0415

    p = load_personality(personality)
    raw = render_config(p, version, hostname)
    return normalize(raw, PERSONALITY_DRIVER[personality], mask)


# The lab hostnames (fake_device.LAB) -> personality; the expected files are per lab device.
def lab_devices() -> list[tuple[str, str]]:
    from fake_device import LAB  # noqa: PLC0415

    return [(n, p) for n, p, _ip, flags in LAB if "huge" not in flags]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    stale = 0
    for name, pers in lab_devices():
        for v in ("v1", "v2"):
            for mask in (True, False):
                path = HERE / "expected" / name / f"{v}{'' if mask else '.unmasked'}.txt"
                text = expected(pers, v, name, mask)
                if mask and leaked_secrets(text):
                    print(f"CONTRACT GAP: {name} {v}: the driver regexes leave secrets unmasked: "
                          f"{leaked_secrets(text)}")
                if a.check:
                    if not path.exists() or path.read_text() != text:
                        print(f"stale: {path}")
                        stale += 1
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
