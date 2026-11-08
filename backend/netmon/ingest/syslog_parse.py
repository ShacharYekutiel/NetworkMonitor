"""[2b-3] Pure syslog parsing (data-model "Syslog parsing") and RFC 6587 TCP framing.

`parse_syslog(data, received_at, tz)` never raises for any input."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from netmon.ingest.limits import MAX_SYSLOG_MESSAGE, MAX_TCP_FRAME

FACILITY_DEFAULT = 1  # user
SEVERITY_DEFAULT = 5  # notice
BOM = "\ufeff"
REPLACEMENT = "\ufffd"


@dataclass
class SyslogRecord:
    facility: int
    severity: int
    format: str  # rfc5424 | rfc3164 | raw
    device_time: datetime | None
    hostname: str
    program: str
    pid: str
    msgid: str
    structured_data: str | None
    message: str
    truncated: bool = False


_PRI = re.compile(r"^<(\d{1,3})>")
_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    )
}
_TS3164 = re.compile(
    r"^(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (?P<day>[ \d]?\d) "
    r"(?:(?P<y1>\d{4}) )?(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})(?:\.(?P<frac>\d{1,6}))?"
    r"(?: (?P<y2>\d{4}))?(?: +|$)"
)
_COMWARE = re.compile(
    r"^%%(\d{2})?([A-Za-z0-9_-]+)/(\d)/([A-Za-z0-9_]+)(\([a-z]\))?:\s?(.*)$", re.DOTALL
)
_ARUBA = re.compile(r"^(\d{5}) ([^\s:\[]{1,48}):\s?(.*)$", re.DOTALL)
_TAG = re.compile(r"^([^\s:\[]{1,48})(\[([^\]]{1,128})\])?:\s?(.*)$", re.DOTALL)


def _zone(tz: str | tzinfo | None) -> tzinfo:
    if isinstance(tz, tzinfo):
        return tz
    if tz:
        try:
            return ZoneInfo(str(tz))
        except Exception:  # noqa: BLE001 - unknown zone: UTC
            return UTC
    return UTC


def clean_text(raw: bytes | str) -> str:
    """UTF-8 (errors replaced), NUL -> U+FFFD (PostgreSQL text cannot hold NUL)."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes | bytearray) else raw
    return text.replace("\x00", REPLACEMENT)


def cut_utf8(text: str, limit: int) -> tuple[str, bool]:
    """Cut to at most `limit` UTF-8 bytes at a character boundary."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="ignore"), True


def _strip_tail(data: bytes) -> bytes:
    return data.rstrip(b"\n\r\x00")


def parse_syslog(
    data: bytes | str, received_at: datetime, tz: str | tzinfo | None = None
) -> SyslogRecord:
    raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else bytes(data)
    line = clean_text(_strip_tail(raw))
    m = _PRI.match(line)
    pri = int(m.group(1)) if m else -1
    if m is None or pri > 191:
        return _finish(
            SyslogRecord(
                FACILITY_DEFAULT, SEVERITY_DEFAULT, "raw", None, "", "", "", "", None, line
            )
        )
    facility, severity = pri // 8, pri % 8
    rest = line[m.end() :]
    if rest.startswith("1 "):
        rec = _parse_5424(rest[2:], facility, severity)
        if rec is not None:
            return _finish(rec)
    return _finish(_parse_3164(rest, facility, severity, received_at, _zone(tz)))


def _finish(rec: SyslogRecord) -> SyslogRecord:
    rec.message, cut = cut_utf8(rec.message, MAX_SYSLOG_MESSAGE)
    rec.truncated = rec.truncated or cut
    rec.hostname = rec.hostname[:255]
    rec.program = rec.program[:128]
    rec.pid = rec.pid[:128]
    rec.msgid = rec.msgid[:64]
    return rec


def _nil(v: str) -> str:
    return "" if v == "-" else v


def _parse_rfc3339(v: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(v.replace("z", "Z"))
    except ValueError:
        # more than 6 fractional digits
        mt = re.match(r"^(.*\.\d{6})\d+(.*)$", v)
        if not mt:
            return None
        try:
            dt = datetime.fromisoformat(mt.group(1) + mt.group(2))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


def _split_sd(s: str) -> tuple[str | None, str] | None:
    """-> (structured data or None, the rest after it), None when malformed."""
    if s.startswith("-"):
        if len(s) == 1:
            return None, ""
        if s[1] == " ":
            return None, s[2:]
        return None
    if not s.startswith("["):
        return None
    i = 0
    n = len(s)
    while i < n and s[i] == "[":
        j = i + 1
        while j < n:
            if s[j] == "\\" and j + 1 < n:
                j += 2
                continue
            if s[j] == "]":
                break
            j += 1
        if j >= n:
            return None
        i = j + 1
    sd = s[:i]
    rest = s[i:]
    if rest and not rest.startswith(" "):
        return None
    return sd, rest[1:] if rest else ""


def _parse_5424(s: str, facility: int, severity: int) -> SyslogRecord | None:
    parts = s.split(" ", 5)
    if len(parts) < 6:
        return None
    ts, host, app, procid, msgid, tail = parts
    device_time = None
    if ts != "-":
        device_time = _parse_rfc3339(ts)
        if device_time is None:
            return None
    sd_rest = _split_sd(tail)
    if sd_rest is None:
        return None
    sd, msg = sd_rest
    if msg.startswith(BOM):
        msg = msg[1:]
    return SyslogRecord(
        facility,
        severity,
        "rfc5424",
        device_time,
        _nil(host),
        _nil(app),
        _nil(procid),
        _nil(msgid),
        sd,
        msg,
    )


def _device_time_3164(m: re.Match[str], received_at: datetime, zone: tzinfo) -> datetime | None:
    try:
        month = _MONTHS[m.group("mon")]
        day = int(m.group("day").strip())
        h, mi, s = int(m.group("h")), int(m.group("m")), int(m.group("s"))
        frac = m.group("frac")
        micro = int((frac + "000000")[:6]) if frac else 0
        year_txt = m.group("y1") or m.group("y2")
        if year_txt:
            local = datetime(int(year_txt), month, day, h, mi, s, micro, tzinfo=zone)
            return local.astimezone(UTC)
        ref = received_at.astimezone(zone)
        local = datetime(ref.year, month, day, h, mi, s, micro, tzinfo=zone)
        if local.astimezone(UTC) > received_at.astimezone(UTC) + timedelta(days=1):
            local = datetime(ref.year - 1, month, day, h, mi, s, micro, tzinfo=zone)
        return local.astimezone(UTC)
    except (ValueError, OverflowError):  # e.g. Feb 30
        return None


def _parse_3164(
    s: str, facility: int, severity: int, received_at: datetime, zone: tzinfo
) -> SyslogRecord:
    device_time = None
    hostname = ""
    m = _TS3164.match(s)
    if m is not None:
        device_time = _device_time_3164(m, received_at, zone)
        s = s[m.end() :]
        token, sep, after = s.partition(" ")
        if (
            token
            and sep
            and not token.endswith(":")
            and not token.startswith("%%")
            and _ARUBA.match(s) is None
        ):
            hostname, s = token, after
    rec = SyslogRecord(facility, severity, "rfc3164", device_time, hostname, "", "", "", None, s)
    cm = _COMWARE.match(s)
    if cm is not None:
        rec.program, rec.msgid, rec.message = cm.group(2), cm.group(4), cm.group(6)
        return rec
    am = _ARUBA.match(s)
    if am is not None:
        rec.msgid, rec.program, rec.message = am.group(1), am.group(2), am.group(3)
        return rec
    tm = _TAG.match(s)
    if tm is not None:
        rec.program, rec.pid, rec.message = tm.group(1), tm.group(3) or "", tm.group(4)
    return rec


# ------------------------------------------------------------------ TCP framing (RFC 6587)
class FrameTooLarge(Exception):
    pass


class TcpFramer:
    """Per-connection frame splitter, decided per frame: a frame starting with a digit is
    octet-counted (`LEN SP MSG`, LEN <= 65536), otherwise LF-terminated (a trailing CR is
    stripped). `feed()` raises FrameTooLarge (the connection is then closed)."""

    def __init__(self, max_frame: int = MAX_TCP_FRAME) -> None:
        self.max_frame = max_frame
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out: list[bytes] = []
        while self.buf:
            first = self.buf[0]
            if first in b"\r\n":  # stray line ends between frames
                del self.buf[0]
                continue
            if 0x30 <= first <= 0x39:
                sp = -1
                for i in range(min(len(self.buf), 8)):
                    c = self.buf[i]
                    if c == 0x20:
                        sp = i
                        break
                    if not 0x30 <= c <= 0x39:
                        break
                else:
                    if len(self.buf) >= 8:
                        raise FrameTooLarge("octet count too long")
                    return out  # need more bytes
                if sp > 0:
                    length = int(self.buf[:sp])
                    if length > self.max_frame:
                        raise FrameTooLarge(f"frame of {length} bytes")
                    if len(self.buf) < sp + 1 + length:
                        return out
                    out.append(bytes(self.buf[sp + 1 : sp + 1 + length]))
                    del self.buf[: sp + 1 + length]
                    continue
                # digits not followed by a space: a newline-terminated frame
            nl = self.buf.find(b"\n")
            if nl < 0:
                if len(self.buf) > self.max_frame:
                    raise FrameTooLarge("line too long")
                return out
            if nl > self.max_frame:
                raise FrameTooLarge("line too long")
            frame = bytes(self.buf[:nl])
            del self.buf[: nl + 1]
            if frame.endswith(b"\r"):
                frame = frame[:-1]
            if frame:
                out.append(frame)
        return out
