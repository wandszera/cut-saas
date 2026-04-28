"""add subscriptions

Revision ID: 20260424_0004
Revises: 20260424_0003
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260424_0004"
down_revision = "20260424_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_customer_id", sa.String(), nullable=True),
        sa.Column("provider_subscription_id", sa.String(), nullable=True),
        sa.Column("provider_checkout_id", sa.String(), nullable=True),
        sa.Column("plan_slug", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_subscriptions_id"), "subscriptions", ["id"], unique=False)
    op.create_index(op.f("ix_subscriptions_plan_slug"), "subscriptions", ["plan_slug"], unique=False)
    op.create_index(op.f("ix_subscriptions_provider_checkout_id"), "subscriptions", ["provider_checkout_id"], unique=True)
    op.create_index(op.f("ix_subscriptions_provider_customer_id"), "subscriptions", ["provider_customer_id"], unique=False)
    op.create_index(op.f("ix_subscriptions_provider_subscription_id"), "subscriptions", ["provider_subscription_id"], unique=False)
    op.create_index(op.f("ix_subscriptions_status"), "subscriptions", ["status"], unique=False)
    op.create_index(op.f("ix_subscriptions_workspace_id"), "subscriptions", ["workspace_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_subscriptions_workspace_id"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_status"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_provider_subscription_id"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_provider_customer_id"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_provider_checkout_id"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_plan_slug"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_id"), table_name="subscriptions")
    op.drop_table("subscriptions")
