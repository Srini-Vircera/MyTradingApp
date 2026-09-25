"""cycle steps (resumable trading cycles, M10)

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cycle_steps",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["trading_cycles.cycle_id"],
            name=op.f("fk_cycle_steps_cycle_id_trading_cycles"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cycle_steps")),
    )
    op.create_index(op.f("ix_cycle_steps_cycle_id"), "cycle_steps", ["cycle_id"], unique=False)
    op.execute(
        "CREATE TRIGGER cycle_steps_append_only BEFORE UPDATE OR DELETE ON cycle_steps "
        "FOR EACH ROW EXECUTE FUNCTION aq_reject_modification()"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_cycle_steps_cycle_id"), table_name="cycle_steps")
    op.drop_table("cycle_steps")
