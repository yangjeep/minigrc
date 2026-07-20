"""harden job and trust center section constraints and indexes

Revision ID: 363c1c5fe38b
Revises: 86f108a23aed
Create Date: 2026-07-20 13:36:30.335004

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "363c1c5fe38b"
down_revision: Union[str, Sequence[str], None] = "86f108a23aed"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index("ix_jobs_status_available_at", "jobs", ["status", "available_at"], unique=False)
    op.create_index("ix_jobs_status_claimed_at", "jobs", ["status", "claimed_at"], unique=False)
    op.create_index(
        "ix_trust_center_sections_visibility_status",
        "trust_center_sections",
        ["visibility", "status"],
        unique=False,
    )
    # SQLite can't ALTER a table to add a CHECK constraint directly —
    # batch mode recreates the table under the hood (copy-and-move strategy).
    with op.batch_alter_table("trust_center_sections", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_trust_center_section_visibility", "visibility IN ('public', 'restricted', 'internal')"
        )
        batch_op.create_check_constraint("ck_trust_center_section_status", "status IN ('draft', 'published')")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("trust_center_sections", schema=None) as batch_op:
        batch_op.drop_constraint("ck_trust_center_section_status", type_="check")
        batch_op.drop_constraint("ck_trust_center_section_visibility", type_="check")
    op.drop_index("ix_trust_center_sections_visibility_status", table_name="trust_center_sections")
    op.drop_index("ix_jobs_status_claimed_at", table_name="jobs")
    op.drop_index("ix_jobs_status_available_at", table_name="jobs")
