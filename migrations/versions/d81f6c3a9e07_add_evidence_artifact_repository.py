"""add evidence artifact repository (issue #32)

Revision ID: d81f6c3a9e07
Revises: c4e8a7f2b391
Create Date: 2026-08-19 14:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d81f6c3a9e07"
down_revision: Union[str, Sequence[str], None] = "c4e8a7f2b391"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Three brand-new tables, no pre-existing data to backfill (same
    category as #11's ControlOccurrence — see
    docs/superpowers/specs/2026-08-19-issue41-event-backfill-migration-convention.md
    §1 for why this doesn't need that convention).
    """
    op.create_table(
        "evidence_artifacts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "evidence_artifact_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("artifact_id", sa.String(length=32), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_connection_id", sa.String(length=32), nullable=True),
        sa.Column("source_object_id", sa.String(length=255), nullable=True),
        sa.Column("source_revision_id", sa.String(length=255), nullable=True),
        sa.Column("source_modified_at", sa.DateTime(), nullable=True),
        sa.Column("linked_evidence_snapshot_id", sa.String(length=32), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("actor_type", sa.String(length=30), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["artifact_id"], ["evidence_artifacts.id"]),
        sa.ForeignKeyConstraint(["linked_evidence_snapshot_id"], ["evidence_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", "version_number", name="uq_evidence_artifact_version"),
    )
    op.create_table(
        "control_occurrence_evidence_artifacts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("occurrence_id", sa.String(length=32), nullable=False),
        sa.Column("evidence_artifact_version_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["occurrence_id"], ["control_occurrences.id"]),
        sa.ForeignKeyConstraint(["evidence_artifact_version_id"], ["evidence_artifact_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "occurrence_id", "evidence_artifact_version_id", name="uq_occurrence_evidence_artifact"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("control_occurrence_evidence_artifacts")
    op.drop_table("evidence_artifact_versions")
    op.drop_table("evidence_artifacts")
