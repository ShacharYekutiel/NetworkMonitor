"""[1.3.1] Named enum value maps: shared code tables with localized texts (en/he).

An `snmp_custom` enum sensor may set `value_map_ref` to one of `NAMED_VALUE_MAPS`; its own
`value_map` entries (if any) override single codes. The check writes the status message in the
server's `default_language` (as the notifications); the UI renders the table in the viewer's
language from `GET /api/sensor-types` (`value_maps`). Texts live only here, so sensors that use a
named map pick up corrected texts without being re-created."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LANGS = ("en", "he")


@dataclass(frozen=True)
class NamedCode:
    status: str  # up | warning | down
    mib: str  # the MIB enumeration name (technical detail)
    text: dict[str, str]  # {en, he}


@dataclass(frozen=True)
class NamedValueMap:
    title: dict[str, str]
    codes: dict[int, NamedCode] = field(default_factory=dict)
    unknown: dict[str, str] = field(default_factory=dict)  # "{code}" placeholder


def _c(status: str, mib: str, en: str, he: str) -> NamedCode:
    return NamedCode(status, mib, {"en": en, "he": he})


# HH3C-ENTITY-EXT-MIB hh3cEntityExtErrorStatus (1.3.6.1.4.1.25506.2.6.1.1.1.1.19). Every value
# but normal(2) is a fault (status down, as the template's value_map_default).
HH3C_ERROR_STATUS = NamedValueMap(
    title={"en": "HPE Comware hardware fault codes", "he": "קודי תקלת חומרה של HPE Comware"},
    codes={
        1: _c("down", "notSupported", "Status not supported", "המצב אינו נתמך"),
        2: _c("up", "normal", "Normal", "תקין"),
        3: _c("down", "postFailure", "Power-on self-test failed", "בדיקת חומרה נכשלה בהפעלה"),
        4: _c("down", "entityAbsent", "Not present", "הרכיב חסר"),
        11: _c("down", "poeError", "PoE fault", "תקלת PoE"),
        21: _c("down", "stackError", "Stack fault", "תקלת מחסנית"),
        22: _c("down", "stackPortBlocked", "Stack port blocked", "פורט מחסנית חסום"),
        23: _c("down", "stackPortFailed", "Stack port failed", "פורט מחסנית נכשל"),
        31: _c("down", "sfpRecvError", "SFP receive fault", "תקלת קליטה ב-SFP"),
        32: _c("down", "sfpSendError", "SFP transmit fault", "תקלת שידור ב-SFP"),
        33: _c("down", "sfpBothError", "SFP receive and transmit fault", "תקלת קליטה ושידור ב-SFP"),
        41: _c("down", "fanError", "Fan fault", "מאוורר תקול"),
        51: _c("down", "psuError", "Power supply fault", "ספק כוח תקול"),
        61: _c(
            "down", "rpsError", "Redundant power supply (RPS) fault", "תקלת ספק כוח חיצוני (RPS)"
        ),
        71: _c("down", "moduleFaulty", "Module fault", "מודול תקול"),
        81: _c("down", "sensorError", "Sensor fault", "חיישן תקול"),
        91: _c("down", "hardwareFaulty", "Hardware fault", "תקלת חומרה"),
    },
    unknown={"en": "Unknown fault code ({code})", "he": "קוד תקלה לא מוכר ({code})"},
)

NAMED_VALUE_MAPS: dict[str, NamedValueMap] = {"hh3c_error_status": HH3C_ERROR_STATUS}


def _lang(lang: str | None) -> str:
    return lang if lang in LANGS else "en"


def resolve(ref: str, code: int, lang: str | None) -> tuple[str | None, str, bool]:
    """(status or None = not in the table, message text, known). The text of a known code is
    "<text> (<mib>, <code>)"; an unknown code gives the table's unknown text."""
    named = NAMED_VALUE_MAPS.get(ref)
    lg = _lang(lang)
    if named is None:
        return None, f"unexpected value ({code})", False
    hit = named.codes.get(code)
    if hit is None:
        return None, named.unknown[lg].replace("{code}", str(code)), False
    return hit.status, f"{hit.text[lg]} ({hit.mib}, {code})", True


def api_value_maps() -> dict[str, Any]:
    """`GET /api/sensor-types` `value_maps`: {name: {title, codes: [{value, status, mib, text}],
    unknown}} (texts as {en, he})."""
    return {
        name: {
            "title": dict(m.title),
            "codes": [
                {"value": str(code), "status": c.status, "mib": c.mib, "text": dict(c.text)}
                for code, c in sorted(m.codes.items())
            ],
            "unknown": dict(m.unknown),
        }
        for name, m in NAMED_VALUE_MAPS.items()
    }
