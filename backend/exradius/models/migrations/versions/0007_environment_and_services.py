"""environment section notes, services and model choice

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-07 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.add_column('agents', sa.Column('model', sa.Text(), nullable=True))
    op.add_column('projects', sa.Column('notes', sa.Text(), server_default='', nullable=False))
    op.add_column('provider_tests', sa.Column('model', sa.Text(), nullable=True))
    op.create_table(
        'project_services',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('project_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=63), nullable=False),
        sa.Column('image', sa.String(length=255), nullable=False),
        sa.Column('env', JSON, nullable=False),
        sa.Column('ports', JSON, nullable=False),
        sa.Column('ready', JSON, nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_project_services_project_id_projects'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_project_services')),
        sa.UniqueConstraint('project_id', 'name', name=op.f('uq_project_services_project_id')),
    )
    op.create_index(op.f('ix_project_services_project_id'), 'project_services', ['project_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_project_services_project_id'), table_name='project_services')
    op.drop_table('project_services')
    op.drop_column('provider_tests', 'model')
    op.drop_column('projects', 'notes')
    op.drop_column('agents', 'model')
