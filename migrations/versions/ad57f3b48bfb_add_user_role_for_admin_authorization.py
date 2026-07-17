"""add user role for admin authorization

Revision ID: ad57f3b48bfb
Revises: 647102981d1c
Create Date: 2026-07-17 17:53:59.048428

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ad57f3b48bfb'
down_revision: Union[str, Sequence[str], None] = '647102981d1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default backfills any existing row (including a pre-existing
    # sole/first user) as "user"; promote it via `python -m app.cli
    # promote-admin --email ...` after upgrading.
    op.add_column(
        'users', sa.Column('role', sa.String(length=16), nullable=False, server_default='user')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'role')
