"""control plane: jobs, uploads, dataset inventory, backtest summaries, runtime
configuration overlay, control state, audit events and worker heartbeats (PR #4)

Safety triggers: control_job_logs, runtime_config_changes and control_events are
append-only; a job in a terminal state (succeeded / failed / cancelled) can never
change state again, so a finished job can never be re-run by an update.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# frozen copy: later model changes must never alter what this revision does
APPEND_ONLY = ("control_job_logs", "runtime_config_changes", "control_events")


def upgrade() -> None:
    op.create_table(
        "control_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=160), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("detail_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("client", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_events")),
    )
    op.create_index(op.f("ix_control_events_action"), "control_events", ["action"], unique=False)
    op.create_index(op.f("ix_control_events_at"), "control_events", ["at"], unique=False)
    op.create_table(
        "control_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_type", sa.String(length=40), nullable=False),
        sa.Column("params_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("config_version", sa.String(length=64), nullable=True),
        sa.Column("retry_of", sa.String(length=36), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name=op.f("ck_control_jobs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_jobs")),
    )
    op.create_index(op.f("ix_control_jobs_job_type"), "control_jobs", ["job_type"], unique=False)
    op.create_index(
        "ix_control_jobs_status_requested", "control_jobs", ["status", "requested_at"], unique=False
    )
    op.create_table(
        "control_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_control_state")),
    )
    op.create_table(
        "data_uploads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("frequency", sa.String(length=8), nullable=False),
        sa.Column("filename_hint", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("preview_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_uploads")),
    )
    op.create_table(
        "dataset_inventory",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("frequency", sa.String(length=8), nullable=False),
        sa.Column("adjustment", sa.String(length=8), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_inventory")),
        sa.UniqueConstraint(
            "source", "symbol", "frequency", "adjustment", name="uq_dataset_inventory_series"
        ),
    )
    op.create_table(
        "runtime_config",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("overlay_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "strategy_overrides_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_runtime_config_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_config")),
    )
    op.create_table(
        "runtime_config_changes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("path", sa.String(length=160), nullable=False),
        sa.Column("old_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("new_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("config_version_before", sa.String(length=64), nullable=False),
        sa.Column("config_version_after", sa.String(length=64), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_config_changes")),
    )
    op.create_index(
        op.f("ix_runtime_config_changes_revision"),
        "runtime_config_changes",
        ["revision"],
        unique=False,
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_worker_heartbeats")),
    )
    op.create_table(
        "backtest_runs",
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("strategies", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("initial_capital", sa.Float(), nullable=False),
        sa.Column("summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report_path", sa.Text(), nullable=False),
        sa.Column("config_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["control_jobs.id"], name=op.f("fk_backtest_runs_job_id_control_jobs")
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_backtest_runs")),
    )
    op.create_table(
        "control_job_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=12), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["control_jobs.id"], name=op.f("fk_control_job_logs_job_id_control_jobs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_control_job_logs")),
    )
    op.create_index(
        op.f("ix_control_job_logs_job_id"), "control_job_logs", ["job_id"], unique=False
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION aq_reject_modification()"
        )
    op.execute(
        """
        CREATE FUNCTION aq_job_terminal_guard() RETURNS trigger AS $$
        BEGIN
            IF OLD.status IN ('succeeded', 'failed', 'cancelled')
               AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION 'control job % is %; a finished job never changes state',
                    OLD.id, OLD.status;
            END IF;
            IF OLD.status = 'running' AND NEW.status = 'queued' THEN
                RAISE EXCEPTION 'control job % is running; it cannot be re-queued', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER control_jobs_terminal_guard BEFORE UPDATE ON control_jobs "
        "FOR EACH ROW EXECUTE FUNCTION aq_job_terminal_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS control_jobs_terminal_guard ON control_jobs")
    op.execute("DROP FUNCTION IF EXISTS aq_job_terminal_guard()")
    op.drop_index(op.f("ix_control_job_logs_job_id"), table_name="control_job_logs")
    op.drop_table("control_job_logs")
    op.drop_table("backtest_runs")
    op.drop_table("worker_heartbeats")
    op.drop_index(op.f("ix_runtime_config_changes_revision"), table_name="runtime_config_changes")
    op.drop_table("runtime_config_changes")
    op.drop_table("runtime_config")
    op.drop_table("dataset_inventory")
    op.drop_table("data_uploads")
    op.drop_table("control_state")
    op.drop_index("ix_control_jobs_status_requested", table_name="control_jobs")
    op.drop_index(op.f("ix_control_jobs_job_type"), table_name="control_jobs")
    op.drop_table("control_jobs")
    op.drop_index(op.f("ix_control_events_at"), table_name="control_events")
    op.drop_index(op.f("ix_control_events_action"), table_name="control_events")
    op.drop_table("control_events")
