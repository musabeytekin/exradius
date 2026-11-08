"""config hook, directory state and integration tokens (docs/integration-api.md §6)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-08 22:00:00
"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('hook_token', sa.String(length=64), nullable=True))
    op.add_column('projects', sa.Column('hook_last_at', sa.DateTime(timezone=True), nullable=True))
    projects = sa.table('projects', sa.column('id', sa.Uuid()), sa.column('hook_token', sa.String()))
    bind = op.get_bind()
    for (project_id,) in bind.execute(sa.select(projects.c.id)).all():
        bind.execute(
            projects.update().where(projects.c.id == project_id).values(hook_token=secrets.token_urlsafe(32))
        )
    with op.batch_alter_table('projects') as batch:
        batch.alter_column('hook_token', existing_type=sa.String(length=64), nullable=False)
        batch.create_unique_constraint(op.f('uq_projects_hook_token'), ['hook_token'])

    directory_state = op.create_table('directory_state',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('directory', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('id = 1', name=op.f('ck_directory_state_single_row')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_directory_state'))
    )
    # The one row, so that the check can lock it (docs/integration-api.md §3).
    op.bulk_insert(directory_state, [{'id': 1, 'directory': None, 'published_at': None}])

    op.create_table('integration_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_integration_tokens')),
    sa.UniqueConstraint('name', name=op.f('uq_integration_tokens_name')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_integration_tokens_token_hash'))
    )


def downgrade() -> None:
    op.drop_table('integration_tokens')
    op.drop_table('directory_state')
    with op.batch_alter_table('projects') as batch:
        batch.drop_constraint(op.f('uq_projects_hook_token'), type_='unique')
        batch.drop_column('hook_last_at')
        batch.drop_column('hook_token')
