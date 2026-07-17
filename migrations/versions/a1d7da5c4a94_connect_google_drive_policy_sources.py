"""connect google drive policy sources

Revision ID: a1d7da5c4a94
Revises: 1c2a027ce561
Create Date: 2026-07-17 18:20:53.144648

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1d7da5c4a94'
down_revision: Union[str, Sequence[str], None] = '1c2a027ce561'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'google_drive_connections',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('connected_by_user_id', sa.String(length=32), nullable=False),
        sa.Column('connected_at', sa.DateTime(), nullable=False),
        sa.Column('granted_scopes', sa.String(length=512), nullable=False),
        sa.Column('encrypted_refresh_token', sa.Text(), nullable=False),
        sa.Column('last_successful_sync_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_by_user_id', sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(['connected_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['revoked_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    # server_default backfills every pre-existing policy/version as "manual"
    # — the only source type that existed before this migration.
    op.add_column(
        'policies', sa.Column('source_type', sa.String(length=16), nullable=False, server_default='manual')
    )
    op.add_column('policies', sa.Column('drive_file_id', sa.String(length=255), nullable=True))
    op.add_column('policies', sa.Column('drive_web_url', sa.String(length=2048), nullable=True))
    op.add_column('policies', sa.Column('drive_mime_type', sa.String(length=128), nullable=True))
    op.add_column(
        'policies', sa.Column('drive_last_seen_revision_id', sa.String(length=255), nullable=True)
    )
    op.add_column('policies', sa.Column('drive_last_synced_at', sa.DateTime(), nullable=True))

    op.add_column(
        'policy_versions',
        sa.Column('source_type', sa.String(length=16), nullable=False, server_default='manual'),
    )
    op.add_column('policy_versions', sa.Column('source_file_id', sa.String(length=255), nullable=True))
    op.add_column('policy_versions', sa.Column('source_revision_id', sa.String(length=255), nullable=True))
    op.add_column('policy_versions', sa.Column('source_modified_at', sa.DateTime(), nullable=True))

    # captured_at is new provenance, not a rename of created_at — backfill
    # existing rows with their own created_at (the closest true answer to
    # "when was this version's content captured" for a manual upload).
    op.add_column('policy_versions', sa.Column('captured_at', sa.DateTime(), nullable=True))
    policy_versions = sa.table(
        'policy_versions', sa.column('captured_at', sa.DateTime()), sa.column('created_at', sa.DateTime())
    )
    op.execute(policy_versions.update().values(captured_at=policy_versions.c.created_at))
    with op.batch_alter_table('policy_versions', schema=None) as batch_op:
        batch_op.alter_column('captured_at', existing_type=sa.DateTime(), nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('policy_versions', 'captured_at')
    op.drop_column('policy_versions', 'source_modified_at')
    op.drop_column('policy_versions', 'source_revision_id')
    op.drop_column('policy_versions', 'source_file_id')
    op.drop_column('policy_versions', 'source_type')
    op.drop_column('policies', 'drive_last_synced_at')
    op.drop_column('policies', 'drive_last_seen_revision_id')
    op.drop_column('policies', 'drive_mime_type')
    op.drop_column('policies', 'drive_web_url')
    op.drop_column('policies', 'drive_file_id')
    op.drop_column('policies', 'source_type')
    op.drop_table('google_drive_connections')
