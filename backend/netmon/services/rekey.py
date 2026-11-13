"""[1.4.2] Re-encrypt every stored secret from one `secret.key` to another
(`netmon secrets rekey`, docs/production.md "[1.4.2] secret.key").

Registry of everything encrypted with `netmon.services.crypto`: the 1.4.2 secrets at rest
(netmon.services.secretbox) and the older ones (SSH credentials, LDAP bind password, Kerberos
keytab, OIDC client secret, assistant AWS secrets). One transaction: a value that the old key
cannot decrypt aborts the whole re-key (nothing changed).
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from netmon.services import crypto, secretbox

SSH_FIELDS = ("password", "private_key", "passphrase", "enable_password")
# settings key -> [(stored field, AAD)]
SETTINGS_FIELDS: dict[str, list[tuple[str, bytes]]] = {
    "ldap": [("bind_password_enc", b"ldap.bind_password")],
    "kerberos": [("keytab_enc", b"kerberos.keytab")],
    "oidc": [("client_secret_enc", b"oidc.client_secret")],
    "assistant": [
        ("secret_access_key_enc", b"assistant.secret_access_key"),
        ("session_token_enc", b"assistant.session_token"),
        ("ra_key_enc", b"assistant.ra_key"),
        ("ra_csr_key_enc", b"assistant.ra_csr_key"),
    ],
}


class RekeyError(RuntimeError):
    pass


def _re(old: str, new: str, value: Any, aad: bytes, where: str) -> str:
    try:
        plain = crypto.decrypt(old, value, aad)
    except crypto.DecryptError:
        raise RekeyError(
            f"{where} cannot be decrypted with the old key; nothing was changed"
        ) from None
    return crypto.encrypt(new, plain, aad)


def _fields(old: str, new: str, obj: Any, pairs: list[tuple[str, bytes]], where: str) -> Any:
    if not isinstance(obj, dict):
        return None
    out, changed = dict(obj), False
    for field, aad in pairs:
        if out.get(field):
            out[field] = _re(old, new, out[field], aad, f"{where}.{field}")
            changed = True
    return out if changed else None


def _box(old: str, new: str, obj: Any, fields: tuple[str, ...], owner: str) -> Any:
    return _fields(
        old, new, obj, [(f + secretbox.ENC, secretbox.aad(owner, f)) for f in fields], owner
    )


def _sealed(old: str, new: str, value: Any, owner: str) -> Any:
    if isinstance(value, dict) and secretbox.SEALED in value:
        aad = secretbox.aad(owner, "value")
        return {secretbox.SEALED: _re(old, new, value[secretbox.SEALED], aad, owner)}
    return None


def rekey_all(conn: Any, old: str, new: str) -> int:
    """Synchronous SQLAlchemy connection. Returns the number of rows rewritten."""
    n = 0

    def upd(sql: str, value: Any, **params: Any) -> None:
        nonlocal n
        conn.execute(text(sql), {"v": json.dumps(value), **params})
        n += 1

    for table, kind in (("groups", "group"), ("devices", "device")):
        rows = conn.execute(
            text(f"SELECT id, snmp_credentials, ssh_credentials FROM {table}")  # noqa: S608
        ).all()
        for r in rows:
            owner = secretbox.snmp_owner(kind, r.id)
            new_snmp = _box(old, new, r.snmp_credentials, secretbox.SNMP_FIELDS, owner)
            if new_snmp is not None:
                upd(
                    f"UPDATE {table} SET snmp_credentials = CAST(:v AS jsonb) WHERE id = :i",
                    new_snmp,
                    i=r.id,
                )  # noqa: S608, E501
            pairs = [(f"{f}_enc", f"ssh.{f}".encode()) for f in SSH_FIELDS]
            new_ssh = _fields(old, new, r.ssh_credentials, pairs, f"{kind}:{r.id}:ssh")
            if new_ssh is not None:
                upd(
                    f"UPDATE {table} SET ssh_credentials = CAST(:v AS jsonb) WHERE id = :i",
                    new_ssh,
                    i=r.id,
                )  # noqa: S608, E501
    for r in conn.execute(text("SELECT id, options FROM discovery_profiles")).all():
        owner = secretbox.profile_owner(r.id)
        v = _box(old, new, r.options, secretbox.DISCOVERY_OPTION_FIELDS, owner)
        if v is not None:
            upd(
                "UPDATE discovery_profiles SET options = CAST(:v AS jsonb) WHERE id = :i", v, i=r.id
            )  # noqa: E501
    for r in conn.execute(text("SELECT id, credentials FROM discovery_scans")).all():
        c = r.credentials
        if isinstance(c, list) and len(c) == 1:
            v = _sealed(old, new, c[0], secretbox.scan_owner(r.id))
            if v is not None:
                upd(
                    "UPDATE discovery_scans SET credentials = CAST(:v AS jsonb) WHERE id = :i",
                    [v],
                    i=r.id,
                )  # noqa: E501
    for r in conn.execute(text("SELECT id, type, config FROM notification_channels")).all():
        fields = secretbox.CHANNEL_FIELDS.get(r.type, ())
        v = _box(old, new, r.config, fields, secretbox.channel_owner(r.id))
        if v is not None:
            upd(
                "UPDATE notification_channels SET config = CAST(:v AS jsonb) WHERE id = :i",
                v,
                i=r.id,
            )  # noqa: E501
    for r in conn.execute(text("SELECT key, value FROM settings")).all():
        v: Any = None
        if r.key in SETTINGS_FIELDS:
            v = _fields(old, new, r.value, SETTINGS_FIELDS[r.key], f"settings:{r.key}")
        elif r.key == "smtp":
            v = _box(old, new, r.value, secretbox.SMTP_FIELDS, "settings:smtp")
        elif r.key == "trap_communities":
            v = _sealed(old, new, r.value, "settings:trap_communities")
        elif r.key == "trap_v3_users" and isinstance(r.value, list):
            users, changed = [], False
            for u in r.value:
                owner = secretbox.trap_user_owner(u.get("username") if isinstance(u, dict) else "")
                nu = _box(old, new, u, secretbox.TRAP_USER_FIELDS, owner)
                changed = changed or nu is not None
                users.append(nu if nu is not None else u)
            v = users if changed else None
        if v is not None:
            upd("UPDATE settings SET value = CAST(:v AS jsonb) WHERE key = :k", v, k=r.key)
    return n
