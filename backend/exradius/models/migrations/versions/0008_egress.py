"""the egress proxy (docs/kubernetes.md §10, §11)

``egress_records``: every request of a run, verification or probe through the egress proxy.
``runs.provider_id``: the provider resolved when the Run was created, whose credential the
proxy's model route uses (no pod holds it any more).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'egress_records',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False),
        sa.Column('object_id', sa.Uuid(), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('phase', sa.String(length=10), nullable=False),
        sa.Column('route', sa.String(length=10), nullable=False),
        sa.Column('method', sa.String(length=16), nullable=True),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('path', sa.Text(), nullable=True),
        sa.Column('allowed', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('status', sa.Integer(), nullable=True),
        sa.Column('bytes_sent', sa.BigInteger(), nullable=False),
        sa.Column('bytes_received', sa.BigInteger(), nullable=False),
        sa.Column('duration_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_egress_records')),
    )
    op.create_index('ix_egress_records_object_id_at', 'egress_records', ['object_id', 'at'])
    with op.batch_alter_table('runs') as batch:
        batch.add_column(sa.Column('provider_id', sa.Uuid(), nullable=True))
        batch.create_foreign_key(op.f('fk_runs_provider_id_providers'), 'providers', ['provider_id'], ['id'],
                                 ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('runs') as batch:
        batch.drop_constraint(op.f('fk_runs_provider_id_providers'), type_='foreignkey')
        batch.drop_column('provider_id')
    op.drop_index('ix_egress_records_object_id_at', table_name='egress_records')
    op.drop_table('egress_records')
