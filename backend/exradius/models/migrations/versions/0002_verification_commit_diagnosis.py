"""verification commit and diagnosis (docs/setup-and-verify.md §1.2, §1.3)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('project_verifications', sa.Column('commit', sa.String(length=64), nullable=True))
    op.add_column('project_verifications', sa.Column('diagnosis', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('project_verifications', 'diagnosis')
    op.drop_column('project_verifications', 'commit')
