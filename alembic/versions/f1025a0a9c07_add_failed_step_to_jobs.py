"""add failed_step to jobs

Revision ID: f1025a0a9c07
Revises: e0daa4bbc0ad
Create Date: 2026-05-30 18:05:17.180804
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1025a0a9c07'
down_revision: Union[str, None] = 'e0daa4bbc0ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('failed_step', sa.String(), nullable=True))
    op.add_column('jobs', sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'failed_at')
    op.drop_column('jobs', 'failed_step')
