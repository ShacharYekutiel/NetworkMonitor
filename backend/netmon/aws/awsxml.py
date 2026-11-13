"""[4] Pure reader of AWS Query-protocol XML answers (STS; architecture § 15.1.3).

Hostile-input rules: no DTD and no entity declarations (`<!DOCTYPE` / `<!ENTITY` are refused
before the parser sees the text), a size cap (1 MiB in 4-0a), namespaces stripped. The stdlib
`xml.etree.ElementTree` parser does not fetch external entities; with the guard it never sees a
declaration at all."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

MAX_BYTES = 1024 * 1024


class XmlError(ValueError):
    pass


def _text(raw: bytes | str | None, max_bytes: int) -> str:
    if raw is None:
        raise XmlError("empty response")
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(data) > max_bytes:
        raise XmlError("response too large")
    text = data.decode("utf-8", "replace")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise XmlError("DTD / entity declarations are not accepted")
    return text


def parse(raw: bytes | str | None, *, max_bytes: int = MAX_BYTES) -> ET.Element:
    text = _text(raw, max_bytes)
    try:
        root = ET.fromstring(text)  # noqa: S314 - guarded above (no DTD / entities)
    except ET.ParseError as exc:
        raise XmlError(f"malformed XML ({exc})") from None
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _find(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    return el.text.strip()


def parse_error(raw: bytes | str | None) -> tuple[str | None, str]:
    """`<ErrorResponse><Error><Code/><Message/>` → (code, message). A body that is not an
    error document → (None, "")."""
    root = parse(raw)
    err = root if root.tag == "Error" else root.find(".//Error")
    if err is None:
        return None, ""
    return _find(err, "Code"), _find(err, "Message") or ""


def parse_assume_role(raw: bytes | str | None) -> dict[str, Any]:
    root = parse(raw)
    res = root.find(".//AssumeRoleResult")
    if res is None:
        raise XmlError("no AssumeRoleResult")
    out = {
        "access_key_id": _find(res, "Credentials/AccessKeyId"),
        "secret_access_key": _find(res, "Credentials/SecretAccessKey"),
        "session_token": _find(res, "Credentials/SessionToken"),
        "expiration": _find(res, "Credentials/Expiration"),
        "assumed_role_arn": _find(res, "AssumedRoleUser/Arn"),
        "assumed_role_id": _find(res, "AssumedRoleUser/AssumedRoleId"),
        "source_identity": _find(res, "SourceIdentity"),
    }
    if not out["access_key_id"] or not out["secret_access_key"]:
        raise XmlError("AssumeRoleResult without credentials")
    return out


def parse_caller_identity(raw: bytes | str | None) -> dict[str, str | None]:
    root = parse(raw)
    res = root.find(".//GetCallerIdentityResult")
    if res is None:
        raise XmlError("no GetCallerIdentityResult")
    return {
        "account": _find(res, "Account"),
        "arn": _find(res, "Arn"),
        "user_id": _find(res, "UserId"),
    }
