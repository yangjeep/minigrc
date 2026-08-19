"""add compliance scope and in_scope flags (issue #30)

Revision ID: f3a1b6c2d4e8
Revises: d81f6c3a9e07
Create Date: 2026-08-19 15:50:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3a1b6c2d4e8"
down_revision: Union[str, Sequence[str], None] = "d81f6c3a9e07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    `compliance_scopes` is a brand-new table, no data to backfill (same
    category as #32's evidence_artifacts — see
    docs/superpowers/specs/2026-08-19-issue41-event-backfill-migration-convention.md
    §1). `in_scope` on `people`/`vendor_systems` backfills every existing
    row as False via server_default (sa.false()) — matching
    a248573ccdfe_add_framework_catalog_key_and_primary_.py's is_primary
    precedent: no existing person/vendor system retroactively becomes
    "in scope" by this migration.
    """
    op.create_table(
        "compliance_scopes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("service_description", sa.Text(), nullable=False),
        sa.Column("trust_service_categories", sa.String(length=255), nullable=False),
        sa.Column("audit_period_starts_on", sa.Date(), nullable=True),
        sa.Column("audit_period_ends_on", sa.Date(), nullable=True),
        sa.Column("data_categories", sa.Text(), nullable=False),
        sa.Column("locations", sa.Text(), nullable=False),
        sa.Column("exclusions", sa.Text(), nullable=False),
        sa.Column("exclusions_rationale", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("people", sa.Column("in_scope", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column(
        "vendor_systems", sa.Column("in_scope", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("vendor_systems", "in_scope")
    op.drop_column("people", "in_scope")
    op.drop_table("compliance_scopes")
