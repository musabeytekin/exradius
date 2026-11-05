"""worker runtime bundles replace the runner image check

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06 00:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    # Heartbeat rows are rewritten every 15 s; old ones carry nothing worth keeping.
    op.execute("DELETE FROM workers")
    op.add_column('workers', sa.Column('runtimes', JSON, nullable=False))
    op.drop_column('workers', 'runner_image_ok')
    op.drop_column('workers', 'runner_image')


def downgrade() -> None:
    op.execute("DELETE FROM workers")
    op.add_column('workers', sa.Column('runner_image', sa.String(length=255), nullable=False))
    op.add_column('workers', sa.Column('runner_image_ok', sa.Boolean(), nullable=False))
    op.drop_column('workers', 'runtimes')
