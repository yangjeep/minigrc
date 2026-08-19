"""add generic oidc provider settings and external identities (issue #17)

Revision ID: b4e7d1a92c6f
Revises: a7c2e9f1b3d5
Create Date: 2026-08-19 21:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b4e7d1a92c6f"
down_revision: Union[str, Sequence[str], None] = "a7c2e9f1b3d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Both tables are brand-new, no data to backfill (same category as
    #30/#32/#34 — no existing row anywhere implies a generic-OIDC
    configuration or a linked external identity).
    """
    op.create_table(
        "oidc_provider_settings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("secret_id", sa.String(length=32), nullable=True),
        sa.Column("allowed_domains", sa.Text(), nullable=False),
        sa.Column("auto_provision_enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["secret_id"], ["secrets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "external_identities",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("issuer", sa.String(length=500), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_external_identity_issuer_subject"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("external_identities")
    op.drop_table("oidc_provider_settings")
