"""secret stores bound to projects, and their change log (docs/config-repo.md §7)

``secret_store_projects``: the projects the admin allowed to use a store; a config naming a
store that does not allow its project is not runnable. ``secret_store_changes``: an
append-only log of what was done to each store, never a value.

So that no working install stops running, each project is allowed for the store its cached
snapshot names, when that store exists; the log records it as done by ``migration``.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11 12:00:00
"""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

Json = sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'secret_store_projects',
        sa.Column('store_id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('allowed_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['store_id'], ['secret_stores.id'],
                                name=op.f('fk_secret_store_projects_store_id_secret_stores'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'],
                                name=op.f('fk_secret_store_projects_project_id_projects'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('store_id', 'project_id', name=op.f('pk_secret_store_projects')),
    )
    op.create_index(op.f('ix_secret_store_projects_project_id'), 'secret_store_projects', ['project_id'])
    op.create_table(
        'secret_store_changes',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('actor', sa.String(length=100), nullable=False),
        sa.Column('store_id', sa.Uuid(), nullable=False),
        sa.Column('store', sa.String(length=63), nullable=False),
        sa.Column('action', sa.String(length=30), nullable=False),
        sa.Column('key', sa.String(length=255), nullable=True),
        sa.Column('project', sa.String(length=100), nullable=True),
        sa.Column('detail', Json, nullable=True),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_secret_store_changes')),
    )
    op.create_index(op.f('ix_secret_store_changes_store_id'), 'secret_store_changes', ['store_id'])

    # Allow each project for the store its cached snapshot names (the file read last).
    bind = op.get_bind()
    stores = {name: store_id for store_id, name in bind.execute(sa.text('SELECT id, name FROM secret_stores'))}
    bindings = sa.table('secret_store_projects', sa.column('store_id', sa.Uuid()), sa.column('project_id', sa.Uuid()),
                        sa.column('allowed_at', sa.DateTime(timezone=True)))
    changes = sa.table('secret_store_changes', sa.column('at', sa.DateTime(timezone=True)), sa.column('actor', sa.String()),
                       sa.column('store_id', sa.Uuid()), sa.column('store', sa.String()), sa.column('action', sa.String()),
                       sa.column('project', sa.String()))
    now = datetime.now(UTC)
    for project_id, slug, snapshot in bind.execute(sa.text('SELECT id, slug, snapshot FROM projects')):
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        name = (snapshot or {}).get('store') if isinstance(snapshot, dict) else None
        store_id = stores.get(name) if name else None
        if store_id is None:
            continue
        project_id = project_id if isinstance(project_id, uuid.UUID) else uuid.UUID(str(project_id))
        store_id = store_id if isinstance(store_id, uuid.UUID) else uuid.UUID(str(store_id))
        op.bulk_insert(bindings, [{'store_id': store_id, 'project_id': project_id, 'allowed_at': now}])
        op.bulk_insert(changes, [{'at': now, 'actor': 'migration', 'store_id': store_id, 'store': name,
                                  'action': 'project_allowed', 'project': slug}])


def downgrade() -> None:
    op.drop_index(op.f('ix_secret_store_changes_store_id'), table_name='secret_store_changes')
    op.drop_table('secret_store_changes')
    op.drop_index(op.f('ix_secret_store_projects_project_id'), table_name='secret_store_projects')
    op.drop_table('secret_store_projects')
