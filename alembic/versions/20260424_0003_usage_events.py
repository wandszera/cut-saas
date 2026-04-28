"""add usage events

Revision ID: 20260424_0003
Revises: 20260424_0002
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260424_0003"
down_revision = "20260424_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_usage_events_idempotency_key"),
    )
    op.create_index(op.f("ix_usage_events_event_type"), "usage_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_usage_events_id"), "usage_events", ["id"], unique=False)
    op.create_index(op.f("ix_usage_events_job_id"), "usage_events", ["job_id"], unique=False)
    op.create_index(op.f("ix_usage_events_workspace_id"), "usage_events", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_usage_events_workspace_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_job_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_id"), table_name="usage_events")
    op.drop_index(op.f("ix_usage_events_event_type"), table_name="usage_events")
    op.drop_table("usage_events")
