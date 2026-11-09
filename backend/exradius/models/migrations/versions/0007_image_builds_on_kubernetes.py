"""image builds as Build objects (docs/kubernetes.md §12)

A row per Build object: its name, the pushed digest, the context tar in the object store and
the build pod's token for it.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-09 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('image_builds') as batch:
        batch.add_column(sa.Column('build_object', sa.String(length=253), nullable=True))
        batch.add_column(sa.Column('digest', sa.String(length=100), nullable=True))
        batch.add_column(sa.Column('context_key', sa.String(length=255), nullable=True))
        batch.add_column(sa.Column('token_hash', sa.String(length=64), nullable=True))
        batch.create_index('ix_image_builds_token_hash', ['token_hash'])


def downgrade() -> None:
    with op.batch_alter_table('image_builds') as batch:
        batch.drop_index('ix_image_builds_token_hash')
        batch.drop_column('token_hash')
        batch.drop_column('context_key')
        batch.drop_column('digest')
        batch.drop_column('build_object')
