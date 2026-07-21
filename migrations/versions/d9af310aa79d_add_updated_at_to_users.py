"""add updated_at to users

Revision ID: d9af310aa79d
Revises: b8373eb767c4
Create Date: 2026-07-20 19:57:23.714365

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d9af310aa79d"
down_revision: Union[str, Sequence[str], None] = "b8373eb767c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add nullable, backfill from created_at (closer to reality than "now"
    # for existing rows, and avoids a non-constant column default — SQLite
    # rejects ALTER TABLE ADD COLUMN ... DEFAULT CURRENT_TIMESTAMP), then
    # tighten to NOT NULL. The app-level default=utcnow/onupdate=utcnow
    # (app/models.py::User) takes over for every insert/update from here on.
    op.add_column("users", sa.Column("updated_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE users SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "updated_at")
