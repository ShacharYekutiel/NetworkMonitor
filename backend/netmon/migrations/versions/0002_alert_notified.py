"""Remember which (rule, sensor) pairs had a problem notification sent.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-04
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE alert_notified (
            rule_id integer NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
            sensor_id integer NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
            status text NOT NULL CHECK (status IN ('down','warning')),
            time timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (rule_id, sensor_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS alert_notified")
