"""agent routing and bridges (docs/agent-routing.md §9, docs/integration-api.md §8)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-08 23:00:00
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
BRIDGE_NAME = re.compile(r"[a-z][a-z0-9-]*")


def upgrade() -> None:
    op.add_column('runs', sa.Column('routing', JSON, nullable=True))

    # Tokens are bound to a bridge (integration-api.md §5): an old token keeps working as
    # the bridge of its name when that is a valid bridge name, and is revoked otherwise.
    op.add_column('integration_tokens', sa.Column('bridge', sa.String(length=100), nullable=True))
    tokens = sa.table('integration_tokens', sa.column('id', sa.Uuid()), sa.column('name', sa.String()),
                      sa.column('bridge', sa.String()), sa.column('revoked_at', sa.DateTime(timezone=True)))
    bind = op.get_bind()
    for token_id, name, revoked_at in bind.execute(sa.select(tokens.c.id, tokens.c.name, tokens.c.revoked_at)).all():
        if BRIDGE_NAME.fullmatch(name):
            bind.execute(tokens.update().where(tokens.c.id == token_id).values(bridge=name))
        else:
            bind.execute(tokens.update().where(tokens.c.id == token_id).values(
                bridge='unbound', revoked_at=revoked_at or sa.func.now()))
    with op.batch_alter_table('integration_tokens') as batch:
        batch.alter_column('bridge', existing_type=sa.String(length=100), nullable=False)
        batch.create_index(op.f('ix_integration_tokens_bridge'), ['bridge'], unique=False)

    op.create_table('bridge_status',
    sa.Column('bridge', sa.String(length=100), nullable=False),
    sa.Column('report', JSON, nullable=False),
    sa.Column('reported_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('token_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['token_id'], ['integration_tokens.id'], name=op.f('fk_bridge_status_token_id_integration_tokens'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('bridge', name=op.f('pk_bridge_status'))
    )

    op.create_table('connection_tools',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('connection', sa.String(length=100), nullable=False),
    sa.Column('definition_hash', sa.String(length=64), nullable=False),
    sa.Column('tools', JSON, nullable=False),
    sa.Column('commit', sa.String(length=64), nullable=True),
    sa.Column('verification_id', sa.Uuid(), nullable=True),
    sa.Column('listed_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_connection_tools_project_id_projects'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['verification_id'], ['project_verifications.id'], name=op.f('fk_connection_tools_verification_id_project_verifications'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_connection_tools')),
    sa.UniqueConstraint('project_id', 'connection', name=op.f('uq_connection_tools_project_id'))
    )
    op.create_index(op.f('ix_connection_tools_project_id'), 'connection_tools', ['project_id'], unique=False)

    routing = op.create_table('routing_settings',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('openai_key_encrypted', sa.Text(), nullable=True),
    sa.Column('key_set_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_test', JSON, nullable=True),
    sa.CheckConstraint('id = 1', name=op.f('ck_routing_settings_single_row')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_routing_settings'))
    )
    op.bulk_insert(routing, [{'id': 1, 'openai_key_encrypted': None, 'key_set_at': None, 'last_test': None}])


def downgrade() -> None:
    op.drop_table('routing_settings')
    op.drop_index(op.f('ix_connection_tools_project_id'), table_name='connection_tools')
    op.drop_table('connection_tools')
    op.drop_table('bridge_status')
    with op.batch_alter_table('integration_tokens') as batch:
        batch.drop_index(op.f('ix_integration_tokens_bridge'))
        batch.drop_column('bridge')
    op.drop_column('runs', 'routing')
