"""add oidc role mapping and role_source (issue #18)

Revision ID: c5f8e2a71d9b
Revises: b4e7d1a92c6f
Create Date: 2026-08-19 21:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c5f8e2a71d9b"
down_revision: Union[str, Sequence[str], None] = "b4e7d1a92c6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    `users.role_source` backfills every existing row to `'local'`
    (server_default) — nothing pre-existing becomes retroactively
    OIDC-managed by this migration; only a future generic-OIDC login
    (issue #18) can set a row to `'oidc_mapped'`, and only for the user
    it just resolved. `oidc_provider_settings.role_claim_name` backfills
    to `'groups'`, matching the model's own default and having no
    observable effect until an admin also adds a role mapping row (this
    issue's #17 companion column, added alongside its own settings row
    with no prior data to reconcile). `oidc_role_mappings` is a
    brand-new table, no data to backfill.
    """
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("role_source", sa.String(length=16), nullable=False, server_default="local")
        )
        batch_op.create_check_constraint(
            "ck_user_role_source", "role_source IN ('local', 'oidc_mapped')"
        )
    with op.batch_alter_table("oidc_provider_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("role_claim_name", sa.String(length=255), nullable=False, server_default="groups")
        )
    op.create_table(
        "oidc_role_mappings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("claim_value", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "role IN ('admin', 'operator', 'reader', 'auditor')", name="ck_oidc_role_mapping_role"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("claim_value", name="uq_oidc_role_mapping_claim_value"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("oidc_role_mappings")
    with op.batch_alter_table("oidc_provider_settings", schema=None) as batch_op:
        batch_op.drop_column("role_claim_name")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("ck_user_role_source", type_="check")
        batch_op.drop_column("role_source")
