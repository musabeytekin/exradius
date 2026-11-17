"""Independent artifact storage and upload limits.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("artifacts") as batch:
        batch.add_column(sa.Column("size", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("sha256", sa.String(64), nullable=True))
        batch.add_column(sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                                   server_default=sa.func.now()))
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("artifact_upload_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("artifact_upload_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("artifact_upload_count")
        batch.drop_column("artifact_upload_started_at")
    with op.batch_alter_table("artifacts") as batch:
        for column in ("created_at", "selected", "sha256", "size"):
            batch.drop_column(column)
