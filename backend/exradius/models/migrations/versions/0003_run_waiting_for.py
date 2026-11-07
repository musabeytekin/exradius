"""runs.waiting_for (docs/run-environment.md §4)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-07 22:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('waiting_for', sa.String(length=30), nullable=True))


def downgrade() -> None:
    op.drop_column('runs', 'waiting_for')
