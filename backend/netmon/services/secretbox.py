"""[1.4.2] Secrets at rest (data-model "[1.4.2] Secrets at rest").

SNMP communities and v3 keys, discovery extra credentials, the SMTP password, trap v3 user
keys, trap communities and the secret parts of notification channels get an AES-256-GCM
encrypted copy (`netmon.services.crypto`, key derived from `secret.key`).

Stored form: next to a secret field `f` of a JSON object, `f_enc` =
`crypto.encrypt(json.dumps(value))`. The AAD names the kind, the row and the field
(`netmon/secret/<owner>/<field>`, owner e.g. `device:12`, `channel:3`, `settings:smtp`), so a
ciphertext copied to another row or field does not decrypt.

Transition (one release): 1.4.1 must keep working on a database 1.4.2 has written, because
the automatic GitOps rollback starts the previous image on the migrated database. So by
default 1.4.2 keeps the plaintext field **in place and in sync** next to the encrypted copy
("plaintext copy" mode). The plaintext is removed:
  - by the next release's migration (after 1.4.2 is known good), or
  - earlier, on purpose: `netmon secrets remove-plaintext` (reversible with
    `netmon secrets restore-plaintext`, which a rollback to 1.4.1 then needs).
Without the plaintext copy, non-object values (trap community list, scan credential
snapshot) are stored as `{"_sealed": <ciphertext>}`.

Reading: a plaintext field that is present wins (it is either in sync, or newer: written by
1.4.1 after a rollback); otherwise the encrypted copy is decrypted. A ciphertext that cannot
be decrypted (`secret.key` replaced) reads as "not set" and is reported (`*_unreadable`),
never raised into the poller. The server re-synchronises the copies at every start.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable, Iterable
from typing import Any

from netmon.services import crypto

log = logging.getLogger("netmon.secrets")

ENC = "_enc"
SEALED = "_sealed"
STATE_KEY = "secrets_plaintext_removed"  # settings row (bool), written by the CLI

SNMP_FIELDS = ("community", "v3_auth_key", "v3_priv_key")
DISCOVERY_OPTION_FIELDS = ("extra_communities", "extra_v3")
TRAP_USER_FIELDS = ("auth_key", "priv_key")
SMTP_FIELDS = ("password",)
CHANNEL_FIELDS: dict[str, tuple[str, ...]] = {
    "telegram": ("bot_token",),
    "teams": ("url",),
    "webhook": ("url", "headers"),
}
SEALED_SETTINGS = ("smtp", "trap_communities", "trap_v3_users")

_KEY: str | None = None
_PLAINTEXT_COPY = True
_warned: set[str] = set()


def configure(secret_key: str | None, plaintext_copy: bool | None = None) -> None:
    global _KEY, _PLAINTEXT_COPY
    _KEY = secret_key
    if plaintext_copy is not None:
        _PLAINTEXT_COPY = bool(plaintext_copy)


def set_plaintext_copy(on: bool) -> None:
    global _PLAINTEXT_COPY
    _PLAINTEXT_COPY = bool(on)


def plaintext_copy() -> bool:
    return _PLAINTEXT_COPY


def aad(owner: str, field: str) -> bytes:
    return f"netmon/secret/{owner}/{field}".encode()


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _warn_once(owner: str) -> None:
    if owner not in _warned:
        _warned.add(owner)
        log.warning(
            "Stored secret of %s cannot be decrypted (secret.key replaced?); it is treated as "
            "not set until it is entered again",
            owner,
        )


def _enc(key: str, value: Any, owner: str, field: str) -> str:
    return crypto.encrypt(key, json.dumps(value, separators=(",", ":")), aad(owner, field))


def _dec(key: str | None, value: Any, owner: str, field: str) -> Any:
    if not key:
        raise crypto.DecryptError("no key")
    try:
        return json.loads(crypto.decrypt(key, value, aad(owner, field)))
    except (ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, crypto.DecryptError):
            raise
        raise crypto.DecryptError(str(exc)) from None


# ---------------------------------------------------------------- objects (explicit key)
def open_with(key: str | None, obj: Any, fields: Iterable[str], owner: str) -> tuple[Any, bool]:
    """Stored object -> (plain object without `*_enc`, unreadable)."""
    if not isinstance(obj, dict):
        return obj, False
    out = dict(obj)
    unreadable = False
    for f in fields:
        enc = out.pop(f + ENC, None)
        if f in out or not enc:
            continue  # the plaintext field wins (in sync, or newer: written by 1.4.1)
        try:
            out[f] = _dec(key, enc, owner, f)
        except crypto.DecryptError:
            out[f] = None
            unreadable = True
    return out, unreadable


def seal_with(key: str, obj: Any, fields: Iterable[str], owner: str, keep_plaintext: bool) -> Any:
    """Plain object -> stored object: every non-empty secret field gets its encrypted copy;
    the plaintext field is kept (`keep_plaintext`, the 1.4.2 default) or removed."""
    if not isinstance(obj, dict):
        return obj
    out = dict(obj)
    for f in fields:
        out.pop(f + ENC, None)
        value = out.get(f)
        if _empty(value):
            continue
        out[f + ENC] = _enc(key, value, owner, f)
        if not keep_plaintext:
            del out[f]
    return out


def in_form(
    key: str | None, obj: Any, fields: Iterable[str], owner: str, keep_plaintext: bool | None
) -> bool:
    """True when the stored object is already in the wanted form (`keep_plaintext` None =
    the 1.4.1 form: plaintext only, no encrypted copy). Avoids rewriting rows."""
    if not isinstance(obj, dict):
        return True
    plain, _ = open_with(key, obj, fields, owner)
    for f in fields:
        value = plain.get(f)
        has_enc = bool(obj.get(f + ENC))
        if keep_plaintext is None:
            if has_enc:
                return False
            continue
        if _empty(value):
            if has_enc and f in obj:
                return False  # cleared by an older release: drop the stale copy
            continue
        if not has_enc or (f in obj) != keep_plaintext:
            return False
        try:
            if _dec(key, obj[f + ENC], owner, f) != value:
                return False
        except crypto.DecryptError:
            return False
    return True


# ---------------------------------------------------------------- non-object values
def seal_value_with(key: str, value: Any, owner: str, keep_plaintext: bool) -> Any:
    if keep_plaintext or _empty(value) or (isinstance(value, dict) and SEALED in value):
        return value
    return {SEALED: _enc(key, value, owner, "value")}


def open_value_with(key: str | None, value: Any, owner: str, default: Any) -> tuple[Any, bool]:
    if not (isinstance(value, dict) and SEALED in value):
        return value, False
    try:
        return _dec(key, value[SEALED], owner, "value"), False
    except crypto.DecryptError:
        return copy.deepcopy(default), True


# ---------------------------------------------------------------- configured key
def seal(obj: Any, fields: Iterable[str], owner: str) -> Any:
    if obj is None or not _KEY:
        return obj
    return seal_with(_KEY, obj, fields, owner, _PLAINTEXT_COPY)


def open_ex(obj: Any, fields: Iterable[str], owner: str) -> tuple[Any, bool]:
    out, bad = open_with(_KEY, obj, fields, owner)
    if bad:
        _warn_once(owner)
    return out, bad


def open(obj: Any, fields: Iterable[str], owner: str) -> Any:  # noqa: A001 - module API
    return open_ex(obj, fields, owner)[0]


def seal_value(value: Any, owner: str) -> Any:
    if not _KEY:
        return value
    return seal_value_with(_KEY, value, owner, _PLAINTEXT_COPY)


def open_value(value: Any, owner: str, default: Any = None) -> Any:
    out, bad = open_value_with(_KEY, value, owner, default)
    if bad:
        _warn_once(owner)
    return out


def strip_fields(obj: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """The object without its secret fields: what a new row is inserted with before its id
    (part of the AAD) is known; the sealed object is written after the flush."""
    drop = set(fields)
    return {k: v for k, v in obj.items() if k not in drop}


# ---------------------------------------------------------------- kinds
def snmp_owner(kind: str, row_id: int | None) -> str:
    return f"{kind}:{row_id}"


def open_snmp(creds: Any, kind: str, row_id: int | None) -> Any:
    """`kind` "device" | "group"."""
    return open(creds, SNMP_FIELDS, snmp_owner(kind, row_id))


def seal_snmp(creds: Any, kind: str, row_id: int | None) -> Any:
    return seal(creds, SNMP_FIELDS, snmp_owner(kind, row_id))


def snmp_unreadable(creds: Any, kind: str, row_id: int | None) -> bool:
    if not isinstance(creds, dict) or not any(creds.get(f + ENC) for f in SNMP_FIELDS):
        return False
    return open_with(_KEY, creds, SNMP_FIELDS, snmp_owner(kind, row_id))[1]


def channel_owner(channel_id: int | None) -> str:
    return f"channel:{channel_id}"


def open_channel(type_: str, channel_id: int | None, config: Any) -> dict[str, Any]:
    out = open(dict(config or {}), CHANNEL_FIELDS.get(type_, ()), channel_owner(channel_id))
    return out if isinstance(out, dict) else {}


def seal_channel(type_: str, channel_id: int | None, config: dict[str, Any]) -> dict[str, Any]:
    return seal(config, CHANNEL_FIELDS.get(type_, ()), channel_owner(channel_id))


def strip_channel(type_: str, config: dict[str, Any]) -> dict[str, Any]:
    return strip_fields(config, CHANNEL_FIELDS.get(type_, ()))


def profile_owner(profile_id: int | None) -> str:
    return f"discovery_profile:{profile_id}"


def open_profile_options(profile_id: int | None, options: Any) -> dict[str, Any]:
    out = open(dict(options or {}), DISCOVERY_OPTION_FIELDS, profile_owner(profile_id))
    return out if isinstance(out, dict) else {}


def seal_profile_options(profile_id: int | None, options: dict[str, Any]) -> dict[str, Any]:
    return seal(options, DISCOVERY_OPTION_FIELDS, profile_owner(profile_id))


def scan_owner(scan_id: int | None) -> str:
    return f"discovery_scan:{scan_id}"


def _sealed_list(creds: Any) -> bool:
    return (
        isinstance(creds, list)
        and len(creds) == 1
        and isinstance(creds[0], dict)
        and SEALED in creds[0]
    )


def open_scan_credentials(scan_id: int | None, creds: Any) -> list[dict[str, Any]]:
    """`discovery_scans.credentials`: a list, or `[{"_sealed": ...}]` without the plaintext
    copy."""
    if _sealed_list(creds):
        return list(open_value(creds[0], scan_owner(scan_id), default=[]) or [])
    return list(creds or [])


def seal_scan_credentials(scan_id: int | None, creds: list[dict[str, Any]]) -> list[Any]:
    sealed = seal_value(creds, scan_owner(scan_id))
    return [sealed] if isinstance(sealed, dict) and SEALED in sealed else creds


def trap_user_owner(username: Any) -> str:
    return f"settings:trap_v3_users:{username}"


def open_setting(key: str, value: Any) -> Any:
    if key == "smtp" and isinstance(value, dict):
        return open(value, SMTP_FIELDS, "settings:smtp")
    if key == "trap_communities":
        return open_value(value, "settings:trap_communities", default=[])
    if key == "trap_v3_users" and isinstance(value, list):
        return [
            open(u, TRAP_USER_FIELDS, trap_user_owner(u.get("username")))
            if isinstance(u, dict)
            else u
            for u in value
        ]
    return value


def seal_setting(key: str, value: Any) -> Any:
    if key not in SEALED_SETTINGS or not _KEY:
        return value
    return _setting_to(_KEY, key, value, _PLAINTEXT_COPY)[0]


def _setting_to(
    key_: str | None, key: str, value: Any, keep_plaintext: bool | None
) -> tuple[Any, bool]:
    """Settings value -> (value in the wanted form, unreadable). `keep_plaintext` None = the
    1.4.1 form."""
    if key == "smtp" and isinstance(value, dict):
        plain, bad = open_with(key_, value, SMTP_FIELDS, "settings:smtp")
        if keep_plaintext is None:
            return plain, bad
        return seal_with(key_ or "", plain, SMTP_FIELDS, "settings:smtp", keep_plaintext), bad
    if key == "trap_communities":
        plain, bad = open_value_with(key_, value, "settings:trap_communities", [])
        if keep_plaintext is None:
            return plain, bad
        return seal_value_with(key_ or "", plain, "settings:trap_communities", keep_plaintext), bad
    if key == "trap_v3_users" and isinstance(value, list):
        out, bad = [], False
        for u in value:
            if not isinstance(u, dict):
                out.append(u)
                continue
            owner = trap_user_owner(u.get("username"))
            plain, unreadable = open_with(key_, u, TRAP_USER_FIELDS, owner)
            bad = bad or unreadable
            out.append(
                plain
                if keep_plaintext is None
                else seal_with(key_ or "", plain, TRAP_USER_FIELDS, owner, keep_plaintext)
            )
        return out, bad
    return value, False


def _setting_in_form(key_: str | None, key: str, value: Any, keep: bool | None) -> bool:
    if key == "smtp":
        return in_form(key_, value, SMTP_FIELDS, "settings:smtp", keep)
    if key == "trap_communities":
        sealed = isinstance(value, dict) and SEALED in value
        if keep is None or keep:
            return not sealed
        return sealed or _empty(value)
    if key == "trap_v3_users" and isinstance(value, list):
        return all(
            in_form(key_, u, TRAP_USER_FIELDS, trap_user_owner(u.get("username")), keep)
            for u in value
            if isinstance(u, dict)
        )
    return True


# ---------------------------------------------------------------- bulk (0012, CLI, startup)
class SecretsUnreadable(RuntimeError):
    pass


def _rows(conn: Any, sql: str) -> list[Any]:
    from sqlalchemy import text

    return list(conn.execute(text(sql)).all())


def _update(conn: Any, sql: str, **params: Any) -> None:
    from sqlalchemy import text

    conn.execute(text(sql), params)


def transform_all(
    conn: Any, key_source: str | Callable[[], str], keep_plaintext: bool | None
) -> dict[str, int]:
    """Bring every stored secret to one form, in place, on a synchronous SQLAlchemy
    connection (alembic / `run_sync`). Idempotent; rows already in that form are not
    rewritten.
      keep_plaintext=True   encrypted copy + plaintext in sync (1.4.2 default)
      keep_plaintext=False  encrypted only (`netmon secrets remove-plaintext`)
      keep_plaintext=None   plaintext only, the 1.4.1 form (0012 downgrade,
                            `netmon secrets restore-plaintext`)
    A value with no plaintext that cannot be decrypted with this key: raises
    SecretsUnreadable for the 1.4.1 form (nothing is written, the caller's transaction rolls
    back), else it is left as it is with a warning. `key_source` may be a callable: the key
    is read only when a value needs it (an empty database needs none)."""
    counts = {"snmp": 0, "discovery": 0, "channels": 0, "settings": 0}
    bad: list[str] = []
    resolved: list[str] = []

    def k() -> str:
        if not resolved:
            resolved.append(key_source() if callable(key_source) else str(key_source))
        return resolved[0]

    def has_secret(obj: Any, fields: tuple[str, ...]) -> bool:
        return isinstance(obj, dict) and any(
            obj.get(f + ENC) or not _empty(obj.get(f)) for f in fields
        )

    def conv(obj: Any, fields: tuple[str, ...], owner: str) -> Any:
        """The object in the wanted form, or None when it already is (or cannot be)."""
        if not has_secret(obj, fields):
            return None
        key = k()
        if in_form(key, obj, fields, owner, keep_plaintext):
            return None
        plain, unreadable = open_with(key, obj, fields, owner)
        if unreadable:
            bad.append(owner)
            return None  # keep the unreadable ciphertext (a later run with the right key)
        if keep_plaintext is None:
            return plain
        return seal_with(key, plain, fields, owner, keep_plaintext)

    def update(sql: str, value: Any, row_id: Any) -> None:
        _update(conn, sql, v=json.dumps(value), i=row_id)

    for table, kind in (("groups", "group"), ("devices", "device")):
        sql = f"SELECT id, snmp_credentials FROM {table} WHERE snmp_credentials IS NOT NULL"  # noqa: S608
        for r in _rows(conn, sql):
            new = conv(r.snmp_credentials, SNMP_FIELDS, snmp_owner(kind, r.id))
            if new is not None:
                update(
                    f"UPDATE {table} SET snmp_credentials = CAST(:v AS jsonb) WHERE id = :i",  # noqa: S608
                    new,
                    r.id,
                )
                counts["snmp"] += 1
    for r in _rows(conn, "SELECT id, options FROM discovery_profiles"):
        new = conv(r.options, DISCOVERY_OPTION_FIELDS, profile_owner(r.id))
        if new is not None:
            update(
                "UPDATE discovery_profiles SET options = CAST(:v AS jsonb) WHERE id = :i", new, r.id
            )
            counts["discovery"] += 1
    for r in _rows(conn, "SELECT id, credentials FROM discovery_scans"):
        creds = r.credentials
        sealed = _sealed_list(creds)
        new_creds: Any = None
        if keep_plaintext is False and creds and not sealed:
            new_creds = [seal_value_with(k(), creds, scan_owner(r.id), False)]
        elif keep_plaintext is not False and sealed:
            value, unreadable = open_value_with(k(), creds[0], scan_owner(r.id), [])
            if unreadable:
                bad.append(scan_owner(r.id))
            else:
                new_creds = value
        if new_creds is not None:
            update(
                "UPDATE discovery_scans SET credentials = CAST(:v AS jsonb) WHERE id = :i",
                new_creds,
                r.id,
            )
            counts["discovery"] += 1
    for r in _rows(conn, "SELECT id, type, config FROM notification_channels"):
        fields = CHANNEL_FIELDS.get(r.type)
        if not fields:
            continue
        new = conv(r.config, fields, channel_owner(r.id))
        if new is not None:
            update(
                "UPDATE notification_channels SET config = CAST(:v AS jsonb) WHERE id = :i",
                new,
                r.id,
            )
            counts["channels"] += 1
    for r in _rows(
        conn,
        "SELECT key, value FROM settings WHERE key IN ('smtp', 'trap_communities', "
        "'trap_v3_users')",
    ):
        value = r.value
        if _empty(value):
            continue
        key = k()
        if _setting_in_form(key, r.key, value, keep_plaintext):
            continue
        new, unreadable = _setting_to(key, r.key, value, keep_plaintext)
        if unreadable:
            bad.append(f"settings:{r.key}")
            continue
        if new != value:
            _update(
                conn,
                "UPDATE settings SET value = CAST(:v AS jsonb) WHERE key = :k",
                v=json.dumps(new),
                k=r.key,
            )
            counts["settings"] += 1
    if bad and keep_plaintext is None:
        raise SecretsUnreadable(
            f"{len(bad)} stored secret(s) cannot be decrypted with this secret.key "
            f"(first: {', '.join(bad[:5])}); nothing was changed"
        )
    if bad:
        log.warning(
            "%d stored secret(s) cannot be decrypted with this secret.key and have no "
            "plaintext copy (first: %s); they read as not set until entered again",
            len(bad),
            ", ".join(bad[:5]),
        )
    return counts
