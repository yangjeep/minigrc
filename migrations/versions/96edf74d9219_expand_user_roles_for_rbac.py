"""expand user roles for RBAC (issue #37)

Revision ID: 96edf74d9219
Revises: a248573ccdfe
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "96edf74d9219"
down_revision: Union[str, Sequence[str], None] = "a248573ccdfe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Renames the binary role's non-admin value ("user") to "operator" and
    widens the CHECK constraint to admit "reader"/"auditor". Deterministic
    1:1 rename, additive only: every existing "user" row becomes "operator"
    (same write privileges it already had); every existing "admin" row is
    untouched. Must drop the old constraint before the data backfill —
    `role IN ('user','admin')` would reject 'operator' if the UPDATE ran
    first.
    """
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("ck_user_role", type_="check")

    op.execute(sa.text("UPDATE users SET role = 'operator' WHERE role = 'user'"))

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_user_role", "role IN ('admin', 'operator', 'reader', 'auditor')"
        )


def downgrade() -> None:
    """Downgrade schema.

    Symmetric rename back ("operator" -> "user"). Data-loss-risk warning:
    any row created or changed to "reader"/"auditor" after the upgrade has
    no historical equivalent under the old binary role model. This
    downgrade maps both back to "user" (the closest prior semantic — least
    surprising default, not a guess at which of the two the operator
    intended) rather than inventing a distinction the old schema never
    had; if that is not acceptable for a given deployment, resolve those
    rows manually before downgrading.
    """
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("ck_user_role", type_="check")

    op.execute(sa.text("UPDATE users SET role = 'user' WHERE role IN ('operator', 'reader', 'auditor')"))

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_check_constraint("ck_user_role", "role IN ('user', 'admin')")
