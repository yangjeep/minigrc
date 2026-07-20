"""add user status and google subject

Revision ID: 8961da81a764
Revises: 363c1c5fe38b
Create Date: 2026-07-20 15:08:42.839861

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8961da81a764"
down_revision: Union[str, Sequence[str], None] = "363c1c5fe38b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default backfills every existing user row as "active" — no
    # existing session/login is retroactively disabled by this migration.
    op.add_column("users", sa.Column("status", sa.String(length=16), nullable=False, server_default="active"))
    op.add_column("users", sa.Column("google_subject", sa.String(length=255), nullable=True))
    # SQLite can't ALTER a table to add a CHECK constraint directly —
    # batch mode recreates the table under the hood.
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_user_google_subject", ["google_subject"])
        batch_op.create_check_constraint("ck_user_role", "role IN ('user', 'admin')")
        batch_op.create_check_constraint("ck_user_status", "status IN ('active', 'disabled', 'pending')")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("ck_user_status", type_="check")
        batch_op.drop_constraint("ck_user_role", type_="check")
        batch_op.drop_constraint("uq_user_google_subject", type_="unique")
    op.drop_column("users", "google_subject")
    op.drop_column("users", "status")
