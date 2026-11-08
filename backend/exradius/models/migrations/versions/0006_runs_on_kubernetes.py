"""runs on Kubernetes (docs/kubernetes.md §19)

Run containers, run directories and worker ownership give way to the Run object; verify and
probe Runs get their own token; workers report the operator instead of a Docker daemon.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-08 23:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('runs') as batch:
        batch.drop_column('container_id')
        batch.drop_column('directory')
        batch.drop_column('worker_id')
        batch.add_column(sa.Column('run_object', sa.String(length=100), nullable=True))
        batch.add_column(sa.Column('delivery_warnings', JSON, nullable=True))
    op.execute("UPDATE runs SET waiting_for = NULL WHERE waiting_for = 'memory'")

    with op.batch_alter_table('project_verifications') as batch:
        batch.add_column(sa.Column('token_hash', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('tool_reports', JSON, nullable=True))
        batch.create_index('ix_project_verifications_token_hash', ['token_hash'])

    with op.batch_alter_table('provider_tests') as batch:
        batch.add_column(sa.Column('token_hash', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('report', JSON, nullable=True))

    with op.batch_alter_table('workers') as batch:
        batch.drop_column('docker_ok')
        batch.drop_column('docker_detail')
        batch.drop_column('images')
        batch.add_column(sa.Column('operator', JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('workers') as batch:
        batch.drop_column('operator')
        batch.add_column(sa.Column('images', JSON, nullable=False, server_default='[]'))
        batch.add_column(sa.Column('docker_detail', sa.Text(), nullable=True))
        batch.add_column(sa.Column('docker_ok', sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table('provider_tests') as batch:
        batch.drop_column('report')
        batch.drop_column('token_hash')
    with op.batch_alter_table('project_verifications') as batch:
        batch.drop_index('ix_project_verifications_token_hash')
        batch.drop_column('tool_reports')
        batch.drop_column('token_hash')
    with op.batch_alter_table('runs') as batch:
        batch.drop_column('delivery_warnings')
        batch.drop_column('run_object')
        batch.add_column(sa.Column('worker_id', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('directory', sa.Text(), nullable=True))
        batch.add_column(sa.Column('container_id', sa.String(length=100), nullable=True))
