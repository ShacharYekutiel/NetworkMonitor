"""[premium] Signed-in browsers ("sessions" on the My account page).

`user_sessions`: one row per login session (the `sid` claim of the session cookie), for
`GET /api/auth/sessions` and `DELETE /api/auth/sessions/{id}`. The row is a registry, not the
credential: a session stays valid by the 1.4.1 rules (signed cookie, `token_version`, not in
`revoked_sessions`, absolute lifetime). A row is written the first time a session is seen
after this migration (so sessions issued before it keep working and simply appear in the
list on their next request) and its `last_seen_at` at most once a minute.

`public_id` is the identifier the API exposes: random, unrelated to the cookie or the `sid`.

Plain DDL, no hypertable: identical in TimescaleDB and plain PostgreSQL mode. Idempotent.
`downgrade()` drops the table (nothing else reads it; every session keeps working).

Revision ID: 0014 (data-model "[premium] Migration 0014").
Revises: 0013
Create Date: 2026-11-17
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE IF NOT EXISTS user_sessions (
        id bigserial PRIMARY KEY,
        public_id varchar(32) NOT NULL UNIQUE,
        sid varchar(64) NOT NULL UNIQUE,
        user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_version integer NOT NULL,
        created_at timestamptz NOT NULL,
        last_seen_at timestamptz NULL,
        ip varchar(64) NULL,
        user_agent varchar(256) NULL
    )""",
    "CREATE INDEX IF NOT EXISTS user_sessions_user_idx ON user_sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS user_sessions_seen_idx ON user_sessions (last_seen_at)",
]

DOWNGRADE = ["DROP TABLE IF EXISTS user_sessions"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
