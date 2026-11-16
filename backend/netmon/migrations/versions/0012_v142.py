"""[1.4.2] Secrets at rest, transition step.

Adds an AES-256-GCM encrypted copy (`<field>_enc`, AAD with the row identity) next to every
secret that earlier releases stored only as plaintext: SNMP communities and v3 keys of groups /
devices, discovery profile extra credentials, the secret parts of notification channels
(Telegram bot token, Teams URL, webhook URL and headers), the SMTP password and trap v3 user
keys (data-model "[1.4.2] Migration 0012"). No schema change.

The plaintext stays in place and in sync for this release: the automatic GitOps rollback
starts 1.4.1 on this database (1.4.1 starts on a newer revision without migrating) and 1.4.1
reads only the plaintext. The next release removes it; an admin can remove it earlier with
`netmon secrets remove-plaintext`.

Also masks, one-way, discovery extra communities and webhook URLs that audit rows written
before 1.4.2 hold in clear.

Idempotent. `downgrade()` returns to the 1.4.1 form: plaintext restored where only the
encrypted copy exists (after `remove-plaintext`), `*_enc` removed; it stops with an error,
changing nothing, when such a value cannot be decrypted with this `secret.key`.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-13
"""

from __future__ import annotations

import logging

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

log = logging.getLogger("netmon.migrations")


def _secret_key() -> str:
    key = op.get_context().config.attributes.get("secret_key")
    if key:
        return str(key)
    # alembic CLI / `netmon migrate`: the server's own key file
    from netmon.config import load_settings

    path = load_settings().data_dir / "secret.key"
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise RuntimeError(f"migration 0012 needs the server's secret.key ({path} not found)")


# Audit rows written before 1.4.2 could hold discovery extra communities and webhook URLs in
# clear (the audit masks them from 1.4.2 on): mask the values that are set, keep null / [].
_SCRUB_KEYS = ("options.extra_communities", "config.url")
_SCRUB = """
UPDATE audit_log SET changes = jsonb_set(changes, ARRAY[:k], (
    SELECT jsonb_agg(CASE WHEN e = 'null'::jsonb OR e = '[]'::jsonb OR e = '""'::jsonb
                          OR e = '"***"'::jsonb THEN e ELSE '"***"'::jsonb END ORDER BY n)
    FROM jsonb_array_elements(changes -> :k) WITH ORDINALITY AS t(e, n)))
WHERE changes ? :k AND jsonb_typeof(changes -> :k) = 'array'
  AND (:k <> 'config.url' OR entity_type = 'notification_channel')
  AND EXISTS (SELECT 1 FROM jsonb_array_elements(changes -> :k) e
              WHERE e NOT IN ('null'::jsonb, '[]'::jsonb, '""'::jsonb, '"***"'::jsonb))
"""


def _scrub_audit() -> None:
    from sqlalchemy import text

    bind = op.get_bind()
    exists = bind.execute(text("SELECT to_regclass('public.audit_log') IS NOT NULL")).scalar()
    if not exists:
        return
    op.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_immutable")
    for key in _SCRUB_KEYS:
        bind.execute(text(_SCRUB), {"k": key})
    op.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_immutable")


def upgrade() -> None:
    from netmon.services import secretbox

    _scrub_audit()  # one-way (not restored by downgrade)

    # the key is read only when a stored value needs it (an empty database needs none)
    counts = secretbox.transform_all(op.get_bind(), _secret_key, keep_plaintext=True)
    log.info("Migration 0012: encrypted copies of stored secrets added %s", counts)


def downgrade() -> None:
    from netmon.services import secretbox

    counts = secretbox.transform_all(op.get_bind(), _secret_key, keep_plaintext=None)
    op.execute(f"DELETE FROM settings WHERE key = '{secretbox.STATE_KEY}'")
    log.info("Migration 0012 downgrade: secrets back in the 1.4.1 form %s", counts)
