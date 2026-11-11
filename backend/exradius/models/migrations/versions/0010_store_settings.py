"""secret store settings (docs/config-repo.md §7)

``secret_stores.settings``: where an external store's values are (a Vault KV mount and
path), validated per kind. Never a credential. Internal stores have none.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Json = sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('secret_stores') as batch:
        batch.add_column(sa.Column('settings', Json, nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    with op.batch_alter_table('secret_stores') as batch:
        batch.drop_column('settings')
