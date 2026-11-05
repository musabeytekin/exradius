"""model providers

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table('providers',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=50), nullable=False),
    sa.Column('auth_method', sa.String(length=50), nullable=False),
    sa.Column('credential_encrypted', sa.Text(), nullable=False),
    sa.Column('settings', JSON, nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('last_test', JSON, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_providers')),
    sa.UniqueConstraint('slug', name=op.f('uq_providers_slug'))
    )
    op.create_index('uq_providers_default', 'providers', ['is_default'], unique=True,
                    postgresql_where=sa.text('is_default'), sqlite_where=sa.text('is_default'))
    op.create_table('provider_tests',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('provider_id', sa.Uuid(), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('fingerprint', sa.String(length=64), nullable=False),
    sa.Column('worker_id', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.id'], name=op.f('fk_provider_tests_provider_id_providers'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_provider_tests'))
    )
    op.create_index(op.f('ix_provider_tests_provider_id'), 'provider_tests', ['provider_id'], unique=False)
    op.add_column('agents', sa.Column('provider_id', sa.Uuid(), nullable=True))
    op.create_index(op.f('ix_agents_provider_id'), 'agents', ['provider_id'], unique=False)
    op.create_foreign_key(op.f('fk_agents_provider_id_providers'), 'agents', 'providers', ['provider_id'], ['id'], ondelete='RESTRICT')


def downgrade() -> None:
    op.drop_constraint(op.f('fk_agents_provider_id_providers'), 'agents', type_='foreignkey')
    op.drop_index(op.f('ix_agents_provider_id'), table_name='agents')
    op.drop_column('agents', 'provider_id')
    op.drop_index(op.f('ix_provider_tests_provider_id'), table_name='provider_tests')
    op.drop_table('provider_tests')
    op.drop_index('uq_providers_default', table_name='providers')
    op.drop_table('providers')
