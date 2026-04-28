"""add job locks

Revision ID: 20260424_0002
Revises: 20260424_0001
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260424_0002"
down_revision = "20260424_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("locked_by", sa.String(), nullable=True))
    op.create_index(op.f("ix_jobs_locked_at"), "jobs", ["locked_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_jobs_locked_at"), table_name="jobs")
    op.drop_column("jobs", "locked_by")
    op.drop_column("jobs", "locked_at")
