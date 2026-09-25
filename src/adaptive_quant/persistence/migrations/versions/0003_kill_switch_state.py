"""shared kill-switch state (multi-service deployments, M13)

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kill_switch_state",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("engaged", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_kill_switch_state_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kill_switch_state")),
    )
    # No row is inserted: an absent row reads as "never released" (engaged).


def downgrade() -> None:
    op.drop_table("kill_switch_state")
