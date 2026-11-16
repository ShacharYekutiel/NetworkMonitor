"""[1.4.1] Security hardening: server-side logout.

`revoked_sessions`: the ids (`sid` claim of the session cookie) of login sessions ended by
`POST /api/auth/logout`. A session cookie whose `sid` is listed is refused, so a copied cookie
no longer survives the logout, while the user's other browsers stay signed in. Rows are kept
until no session with that id can still be alive (`expires_at`, 31 days) and then removed.

Plain PostgreSQL only (data-model "[1.4.1] Migration 0011").

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-13
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

UPGRADE = [
    """
    CREATE TABLE IF NOT EXISTS revoked_sessions (
        sid varchar(64) PRIMARY KEY,
        user_id integer NULL REFERENCES users(id) ON DELETE CASCADE,
        revoked_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS revoked_sessions_expires_idx ON revoked_sessions (expires_at)",
]

DOWNGRADE = ["DROP TABLE IF EXISTS revoked_sessions"]


def upgrade() -> None:
    for stmt in UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE:
        op.execute(stmt)
