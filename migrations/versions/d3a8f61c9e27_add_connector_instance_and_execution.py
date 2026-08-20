"""add connector instance and execution lifecycle (issue #25)

Revision ID: d3a8f61c9e27
Revises: c5f8e2a71d9b
Create Date: 2026-08-20 01:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d3a8f61c9e27"
down_revision: Union[str, Sequence[str], None] = "c5f8e2a71d9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Both tables are brand-new (issue #25's connector installation/
    execution lifecycle, built on #24's manifest/result contract) — no
    data to backfill.
    """
    op.create_table(
        "connector_instances",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("connector_id", sa.String(length=128), nullable=False),
        sa.Column("manifest_version", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("secret_ref_json", sa.Text(), nullable=False),
        sa.Column("last_test_at", sa.DateTime(), nullable=True),
        sa.Column("last_test_status", sa.String(length=16), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_summary", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "connector_executions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("connector_instance_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("result_summary", sa.Text(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=False),
        sa.Column("triggered_by", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('success', 'failure', 'error')", name="ck_connector_execution_status"
        ),
        sa.CheckConstraint(
            "triggered_by IN ('manual', 'system')", name="ck_connector_execution_triggered_by"
        ),
        sa.ForeignKeyConstraint(["connector_instance_id"], ["connector_instances.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connector_instance_id", "idempotency_key", name="uq_connector_execution_instance_key"
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("connector_executions")
    op.drop_table("connector_instances")
