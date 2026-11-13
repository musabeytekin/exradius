"""tasks, runs, comments and people (design §4), and the install code of design §6

Revision ID: 0001
Revises:
"""

import secrets

import sqlalchemy as sa
from alembic import op

import exradius_desk.models

ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('counters',
    sa.Column('project', sa.String(length=100), nullable=False),
    sa.Column('last', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('project')
    )
    op.create_table('logins',
    sa.Column('state', sa.String(length=64), nullable=False),
    sa.Column('verifier', sa.String(length=128), nullable=False),
    sa.Column('nonce', sa.String(length=64), nullable=False),
    sa.Column('next', sa.String(length=2000), nullable=False),
    sa.Column('created_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('state')
    )
    op.create_table('meta',
    sa.Column('key', sa.String(length=50), nullable=False),
    sa.Column('value', sa.String(length=200), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('people',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('email_verified', sa.Boolean(), nullable=False),
    sa.Column('groups', sa.JSON(), nullable=False),
    sa.Column('first_seen_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('subject')
    )
    op.create_table('sessions',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.Column('created_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('expires_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('token_hash')
    )
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sessions_person_id'), ['person_id'], unique=False)

    op.create_table('tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project', sa.String(length=100), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('reporter_id', sa.Integer(), nullable=False),
    sa.Column('assignee_kind', sa.String(length=10), nullable=False),
    sa.Column('assignee_id', sa.Integer(), nullable=True),
    sa.Column('handed_over_by', sa.Integer(), nullable=True),
    sa.Column('runs', sa.Integer(), nullable=False),
    sa.Column('created_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('updated_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['assignee_id'], ['people.id'], ),
    sa.ForeignKeyConstraint(['handed_over_by'], ['people.id'], ),
    sa.ForeignKeyConstraint(['reporter_id'], ['people.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project', 'number')
    )
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tasks_project'), ['project'], unique=False)
        batch_op.create_index(batch_op.f('ix_tasks_updated_at'), ['updated_at'], unique=False)

    op.create_table('answerers',
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_id', 'person_id')
    )
    op.create_table('history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=True),
    sa.Column('field', sa.String(length=20), nullable=False),
    sa.Column('old', sa.Text(), nullable=True),
    sa.Column('new', sa.Text(), nullable=True),
    sa.Column('created_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('history', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_history_task_id'), ['task_id'], unique=False)

    op.create_table('runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('exradius_task_id', sa.String(length=255), nullable=False),
    sa.Column('state', sa.String(length=30), nullable=False),
    sa.Column('agent', sa.String(length=100), nullable=True),
    sa.Column('agent_name', sa.String(length=255), nullable=True),
    sa.Column('routing', sa.JSON(), nullable=True),
    sa.Column('handed_over_by', sa.Integer(), nullable=False),
    sa.Column('started_by_comment_id', sa.Integer(), nullable=True),
    sa.Column('retries', sa.Integer(), nullable=False),
    sa.Column('created_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.Column('ended_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('warnings', sa.JSON(), nullable=False),
    sa.Column('cancel_requested_by', sa.Integer(), nullable=True),
    sa.Column('cancel_requested_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['cancel_requested_by'], ['people.id'], ),
    sa.ForeignKeyConstraint(['handed_over_by'], ['people.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('exradius_task_id')
    )
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runs_task_id'), ['task_id'], unique=False)

    op.create_table('comments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('author_kind', sa.String(length=10), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=True),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('request_id', sa.String(length=100), nullable=True),
    sa.Column('answered_by_id', sa.Integer(), nullable=True),
    sa.Column('answers_request_id', sa.String(length=100), nullable=True),
    sa.Column('started_run_id', sa.Integer(), nullable=True),
    sa.Column('created_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['people.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('comments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_comments_request_id'), ['request_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_comments_task_id'), ['task_id'], unique=False)

    op.create_table('artifacts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('comment_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=255), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['comment_id'], ['comments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_artifacts_run_id'), ['run_id'], unique=False)

    op.create_table('attachments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('comment_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=255), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.Column('uploaded_by', sa.Integer(), nullable=False),
    sa.Column('created_at', exradius_desk.models.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['comment_id'], ['comments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploaded_by'], ['people.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('attachments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_attachments_task_id'), ['task_id'], unique=False)

    op.create_table('mentions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('comment_id', sa.Integer(), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=True),
    sa.Column('exradius', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['comment_id'], ['comments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('mentions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_mentions_comment_id'), ['comment_id'], unique=False)


    # Four random characters that keep task ids unique after this database is replaced.
    install = "".join(secrets.choice(ALPHABET) for _ in range(4))
    op.execute(sa.text("INSERT INTO meta (key, value) VALUES ('install', :value)").bindparams(value=install))


def downgrade() -> None:
    with op.batch_alter_table('mentions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mentions_comment_id'))

    op.drop_table('mentions')
    with op.batch_alter_table('attachments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_attachments_task_id'))

    op.drop_table('attachments')
    with op.batch_alter_table('artifacts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_artifacts_run_id'))

    op.drop_table('artifacts')
    with op.batch_alter_table('comments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_comments_task_id'))
        batch_op.drop_index(batch_op.f('ix_comments_request_id'))

    op.drop_table('comments')
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runs_task_id'))

    op.drop_table('runs')
    with op.batch_alter_table('history', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_history_task_id'))

    op.drop_table('history')
    op.drop_table('answerers')
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tasks_updated_at'))
        batch_op.drop_index(batch_op.f('ix_tasks_project'))

    op.drop_table('tasks')
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sessions_person_id'))

    op.drop_table('sessions')
    op.drop_table('people')
    op.drop_table('meta')
    op.drop_table('logins')
    op.drop_table('counters')
