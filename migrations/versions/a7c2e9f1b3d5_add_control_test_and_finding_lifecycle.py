"""add control test population/sample/test and finding lifecycle (issue #13)

Revision ID: a7c2e9f1b3d5
Revises: f3a1b6c2d4e8
Create Date: 2026-08-19 16:40:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c2e9f1b3d5"
down_revision: Union[str, Sequence[str], None] = "f3a1b6c2d4e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Nine brand-new tables, no pre-existing data to backfill (same category
    as #11's ControlOccurrence / #32's evidence_artifacts — see
    docs/superpowers/specs/2026-08-19-issue41-event-backfill-migration-convention.md
    §1 for why this doesn't need that convention).
    """
    op.create_table(
        "control_test_populations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("control_id", sa.String(length=32), nullable=False),
        sa.Column("control_period_id", sa.String(length=32), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("criteria_note", sa.Text(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["control_id"], ["internal_controls.id"]),
        sa.ForeignKeyConstraint(["control_period_id"], ["control_periods.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "control_test_population_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("population_id", sa.String(length=32), nullable=False),
        sa.Column("control_occurrence_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["population_id"], ["control_test_populations.id"]),
        sa.ForeignKeyConstraint(["control_occurrence_id"], ["control_occurrences.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "population_id", "control_occurrence_id", name="uq_population_item_occurrence"
        ),
    )
    op.create_table(
        "control_test_samples",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("population_id", sa.String(length=32), nullable=False),
        sa.Column("selection_method", sa.String(length=255), nullable=False),
        sa.Column("selection_rationale", sa.Text(), nullable=False),
        sa.Column("selected_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["population_id"], ["control_test_populations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "control_test_sample_items",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("sample_id", sa.String(length=32), nullable=False),
        sa.Column("population_item_id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["sample_id"], ["control_test_samples.id"]),
        sa.ForeignKeyConstraint(["population_item_id"], ["control_test_population_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sample_id", "population_item_id", name="uq_sample_item_population_item"),
    )
    op.create_table(
        "control_tests",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("control_id", sa.String(length=32), nullable=False),
        sa.Column("control_period_id", sa.String(length=32), nullable=True),
        sa.Column("sample_id", sa.String(length=32), nullable=True),
        sa.Column("procedure_description", sa.Text(), nullable=False),
        sa.Column("tester_person_id", sa.String(length=32), nullable=False),
        sa.Column("performed_at", sa.DateTime(), nullable=False),
        sa.Column("result", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("retest_of_test_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("result IN ('no_exceptions', 'exceptions_noted')", name="ck_control_test_result"),
        sa.ForeignKeyConstraint(["control_id"], ["internal_controls.id"]),
        sa.ForeignKeyConstraint(["control_period_id"], ["control_periods.id"]),
        sa.ForeignKeyConstraint(["sample_id"], ["control_test_samples.id"]),
        sa.ForeignKeyConstraint(["tester_person_id"], ["people.id"]),
        sa.ForeignKeyConstraint(["retest_of_test_id"], ["control_tests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "control_test_exceptions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("test_id", sa.String(length=32), nullable=False),
        sa.Column("sample_item_id", sa.String(length=32), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["test_id"], ["control_tests.id"]),
        sa.ForeignKeyConstraint(["sample_item_id"], ["control_test_sample_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "control_test_evidence_artifacts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("test_id", sa.String(length=32), nullable=False),
        sa.Column("evidence_artifact_version_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["test_id"], ["control_tests.id"]),
        sa.ForeignKeyConstraint(["evidence_artifact_version_id"], ["evidence_artifact_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("test_id", "evidence_artifact_version_id", name="uq_test_evidence_artifact"),
    )
    op.create_table(
        "findings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("control_id", sa.String(length=32), nullable=True),
        sa.Column("source_test_id", sa.String(length=32), nullable=True),
        sa.Column("owner_person_id", sa.String(length=32), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("closure_decision", sa.String(length=32), nullable=True),
        sa.Column("closure_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')", name="ck_finding_severity"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'remediating', 'retesting', 'closed')", name="ck_finding_status"
        ),
        sa.ForeignKeyConstraint(["control_id"], ["internal_controls.id"]),
        sa.ForeignKeyConstraint(["source_test_id"], ["control_tests.id"]),
        sa.ForeignKeyConstraint(["owner_person_id"], ["people.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "finding_remediation_updates",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("finding_id", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("linked_evidence_artifact_version_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"]),
        sa.ForeignKeyConstraint(
            ["linked_evidence_artifact_version_id"], ["evidence_artifact_versions.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "finding_retests",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("finding_id", sa.String(length=32), nullable=False),
        sa.Column("test_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["finding_id"], ["findings.id"]),
        sa.ForeignKeyConstraint(["test_id"], ["control_tests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("finding_retests")
    op.drop_table("finding_remediation_updates")
    op.drop_table("findings")
    op.drop_table("control_test_evidence_artifacts")
    op.drop_table("control_test_exceptions")
    op.drop_table("control_tests")
    op.drop_table("control_test_sample_items")
    op.drop_table("control_test_samples")
    op.drop_table("control_test_population_items")
    op.drop_table("control_test_populations")
