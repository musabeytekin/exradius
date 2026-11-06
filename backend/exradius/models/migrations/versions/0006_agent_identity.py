"""agent identity and push permission

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('agents', sa.Column('identity_name', sa.String(length=255), nullable=True))
    op.add_column('agents', sa.Column('identity_email', sa.String(length=255), nullable=True))
    # Existing agents: their name, and a placeholder address the panel flags for editing.
    op.execute("UPDATE agents SET identity_name = name, identity_email = slug || '@exradius.invalid'")
    with op.batch_alter_table('agents') as batch:
        batch.alter_column('identity_name', existing_type=sa.String(length=255), nullable=False)
        batch.alter_column('identity_email', existing_type=sa.String(length=255), nullable=False)
    op.add_column('repositories', sa.Column('push_allowed', sa.Boolean(), server_default=sa.false(), nullable=False))
    with op.batch_alter_table('repositories') as batch:
        batch.alter_column('push_allowed', existing_type=sa.Boolean(), server_default=None)


def downgrade() -> None:
    op.drop_column('repositories', 'push_allowed')
    op.drop_column('agents', 'identity_email')
    op.drop_column('agents', 'identity_name')
