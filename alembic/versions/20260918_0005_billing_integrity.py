"""Track billing receipts and exact subscription cycles."""
from alembic import op
import sqlalchemy as sa

revision = "20260918_0005"
down_revision = "f1025a0a9c07"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("subscriptions", sa.Column("checkout_url", sa.String(), nullable=True))
    op.add_column("subscriptions", sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "billing_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "event_id", name="uq_billing_event_provider_id"),
    )


def downgrade():
    op.drop_table("billing_events")
    op.drop_column("subscriptions", "current_period_start")
    op.drop_column("subscriptions", "checkout_url")
