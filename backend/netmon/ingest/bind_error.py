"""[2b-3] Human-readable reason when a receiver cannot listen (§12.2.1).

asyncio sometimes reports a bind failure without the OS reason (e.g. "could not bind to
local_addr"). The admin needs the real cause, typically "[Errno 98] Address already in use"
(Linux) or "[WinError 10048] ..." (Windows, often the SNMP Trap service on UDP 162), so a
plain socket bind on the same address is tried to read the operating system's message."""

from __future__ import annotations

import socket


def bind_reason(exc: BaseException, host: str, port: int, udp: bool) -> str:
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, OSError) and exc.errno is not None:
        return text  # already "[Errno N] ..." / "[WinError N] ..."
    if isinstance(exc, TimeoutError):
        return f"timed out binding to {host}:{port}"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    kind = socket.SOCK_DGRAM if udp else socket.SOCK_STREAM
    try:
        with socket.socket(family, kind) as s:
            s.bind((host, port))
    except OSError as probe:
        return f"{text} ({probe})"
    return text
