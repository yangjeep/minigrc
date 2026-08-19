"""add policy version approval lifecycle (issue #31)

Revision ID: c4e8a7f2b391
Revises: 96edf74d9219
Create Date: 2026-08-19 12:30:00.000000

"""

import json
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c4e8a7f2b391"
down_revision: Union[str, Sequence[str], None] = "96edf74d9219"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LIFECYCLE_STATUSES = ("draft", "in_review", "approved", "effective", "superseded", "withdrawn")


def _backfill_lifecycle(connection) -> None:
    """Deterministic, per-policy backfill (see docs/superpowers/specs/
    2026-08-19-issue31-policy-lifecycle-design.md §6):

    - the LATEST version of a policy whose status is "approved" or
      "retired" becomes "effective" (the best available signal from the
      pre-lifecycle model — there is no stronger one);
    - every NON-LATEST version becomes "superseded" (a newer capture
      existing at all is sufficient signal, independent of whether
      formal review ever happened under the old model);
    - the latest version of a "draft" policy stays "draft" (the column
      default — nothing to do).

    A matching bootstrap DomainEvent row (actor_type="migration") is
    inserted for every backfilled transition, so a later
    rebuild_policy_version_lifecycle_projection() reproduces this exact
    state instead of silently losing it — see app/policy_lifecycle.py.
    No event ever claims a human reviewed/approved anything: this is
    what keeps the backfill from fabricating a historical action.
    """
    policies = sa.table("policies", sa.column("id", sa.String), sa.column("status", sa.String))
    policy_versions = sa.table(
        "policy_versions",
        sa.column("id", sa.String),
        sa.column("policy_id", sa.String),
        sa.column("version_number", sa.Integer),
        sa.column("captured_at", sa.DateTime),
        sa.column("lifecycle_status", sa.String),
        sa.column("effective_at", sa.DateTime),
        sa.column("superseded_at", sa.DateTime),
        sa.column("superseded_by_version_id", sa.String),
    )
    domain_events = sa.table(
        "domain_events",
        sa.column("id", sa.String),
        sa.column("aggregate_type", sa.String),
        sa.column("aggregate_id", sa.String),
        sa.column("aggregate_sequence", sa.Integer),
        sa.column("event_type", sa.String),
        sa.column("schema_version", sa.Integer),
        sa.column("payload_json", sa.Text),
        sa.column("occurred_at", sa.DateTime),
        sa.column("recorded_at", sa.DateTime),
        sa.column("actor_type", sa.String),
        sa.column("actor_id", sa.String),
        sa.column("correlation_id", sa.String),
        sa.column("causation_id", sa.String),
        sa.column("idempotency_key", sa.String),
    )

    # A downgrade only drops the lifecycle columns/table this migration
    # adds — it deliberately never deletes the bootstrap DomainEvent rows
    # below (they are immutable; see this migration's downgrade
    # docstring). A downgrade-then-upgrade cycle therefore re-runs this
    # backfill against columns that are fresh/reset but against an event
    # store that may already hold last time's bootstrap events — skip
    # inserting a duplicate for any aggregate_id already represented,
    # while still always re-applying the column UPDATEs below (cheap and
    # idempotent) so the freshly-recreated columns end up correct either
    # way.
    already_backfilled_aggregate_ids = {
        row.aggregate_id
        for row in connection.execute(
            sa.select(domain_events.c.aggregate_id).where(
                domain_events.c.aggregate_type == "policy_version", domain_events.c.actor_type == "migration"
            )
        ).fetchall()
    }

    def _insert_event(*, aggregate_id: str, event_type: str, payload: dict, occurred_at) -> None:
        if aggregate_id in already_backfilled_aggregate_ids:
            return
        connection.execute(
            sa.insert(domain_events).values(
                id=uuid.uuid4().hex,
                aggregate_type="policy_version",
                aggregate_id=aggregate_id,
                aggregate_sequence=1,
                event_type=event_type,
                schema_version=1,
                payload_json=json.dumps(payload),
                occurred_at=occurred_at,
                recorded_at=occurred_at,
                actor_type="migration",
                actor_id=None,
                correlation_id=None,
                causation_id=None,
                idempotency_key=None,
            )
        )

    for policy_row in connection.execute(sa.select(policies.c.id, policies.c.status)).fetchall():
        versions = connection.execute(
            sa.select(
                policy_versions.c.id,
                policy_versions.c.version_number,
                policy_versions.c.captured_at,
            )
            .where(policy_versions.c.policy_id == policy_row.id)
            .order_by(policy_versions.c.version_number)
        ).fetchall()
        if not versions:
            continue

        for current, following in zip(versions, versions[1:]):
            connection.execute(
                sa.update(policy_versions)
                .where(policy_versions.c.id == current.id)
                .values(
                    lifecycle_status="superseded",
                    superseded_at=following.captured_at,
                    superseded_by_version_id=following.id,
                )
            )
            _insert_event(
                aggregate_id=current.id,
                event_type="PolicyVersionSuperseded",
                payload={"superseded_by_version_id": following.id},
                occurred_at=following.captured_at,
            )

        latest = versions[-1]
        if policy_row.status in ("approved", "retired"):
            connection.execute(
                sa.update(policy_versions)
                .where(policy_versions.c.id == latest.id)
                .values(lifecycle_status="effective", effective_at=latest.captured_at)
            )
            _insert_event(
                aggregate_id=latest.id,
                event_type="PolicyVersionMadeEffective",
                payload={},
                occurred_at=latest.captured_at,
            )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "policy_control_mappings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("policy_id", sa.String(length=32), nullable=False),
        sa.Column("control_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["control_id"], ["internal_controls.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["policies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_id", "control_id", name="uq_policy_control"),
    )

    op.add_column(
        "policy_versions",
        sa.Column("lifecycle_status", sa.String(length=16), nullable=False, server_default="draft"),
    )
    op.add_column("policy_versions", sa.Column("submitted_by", sa.String(length=32), nullable=True))
    op.add_column("policy_versions", sa.Column("submitted_at", sa.DateTime(), nullable=True))
    op.add_column("policy_versions", sa.Column("reviewed_by", sa.String(length=32), nullable=True))
    op.add_column("policy_versions", sa.Column("reviewed_at", sa.DateTime(), nullable=True))
    op.add_column("policy_versions", sa.Column("review_decision", sa.String(length=16), nullable=True))
    op.add_column(
        "policy_versions", sa.Column("review_comment", sa.Text(), nullable=False, server_default="")
    )
    op.add_column("policy_versions", sa.Column("effective_at", sa.DateTime(), nullable=True))
    op.add_column("policy_versions", sa.Column("superseded_at", sa.DateTime(), nullable=True))
    op.add_column(
        "policy_versions", sa.Column("superseded_by_version_id", sa.String(length=32), nullable=True)
    )
    op.add_column("policy_versions", sa.Column("withdrawn_at", sa.DateTime(), nullable=True))
    op.add_column(
        "policy_versions", sa.Column("withdrawn_reason", sa.Text(), nullable=False, server_default="")
    )

    _backfill_lifecycle(op.get_bind())

    # SQLite can't ALTER TABLE to add a CHECK constraint or a self-
    # referential FK directly — batch mode recreates the table under the
    # hood (harmless no-op wrapper on Postgres). Matches every prior
    # CHECK-constraint-on-existing-table precedent in this repo.
    with op.batch_alter_table("policy_versions", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_policy_version_superseded_by", "policy_versions", ["superseded_by_version_id"], ["id"]
        )
        batch_op.create_check_constraint(
            "ck_policy_version_lifecycle_status", f"lifecycle_status IN {_LIFECYCLE_STATUSES}"
        )

    # Partial unique index: at most one effective version per policy —
    # the real guard against a race between two concurrent requests each
    # making a different already-approved version effective (see
    # app/policy_lifecycle.py::make_version_effective's docstring and
    # app/models.py::PolicyVersion.__table_args__). Same pattern as
    # 2f6df407060b's uq_occurrence_control_due_no_period.
    op.create_index(
        "uq_policy_version_one_effective_per_policy",
        "policy_versions",
        ["policy_id"],
        unique=True,
        sqlite_where=sa.text("lifecycle_status = 'effective'"),
        postgresql_where=sa.text("lifecycle_status = 'effective'"),
    )


def downgrade() -> None:
    """Downgrade schema.

    The backfilled DomainEvent rows are NOT deleted — DomainEvent rows
    are immutable and the ORM-level schema guards
    (app/events.py::_reject_domain_event_bulk_mutation) reject deleting
    them anyway; this repo never destructively rewrites history. A
    downgrade-then-upgrade cycle re-runs the same deterministic backfill
    against Policy.status/version data (idempotent by construction), not
    a second, duplicate set of bootstrap events, since the columns
    receiving them won't exist again until the next upgrade re-adds them.
    """
    op.drop_index("uq_policy_version_one_effective_per_policy", table_name="policy_versions")

    with op.batch_alter_table("policy_versions", schema=None) as batch_op:
        batch_op.drop_constraint("ck_policy_version_lifecycle_status", type_="check")
        batch_op.drop_constraint("fk_policy_version_superseded_by", type_="foreignkey")

    op.drop_column("policy_versions", "withdrawn_reason")
    op.drop_column("policy_versions", "withdrawn_at")
    op.drop_column("policy_versions", "superseded_by_version_id")
    op.drop_column("policy_versions", "superseded_at")
    op.drop_column("policy_versions", "effective_at")
    op.drop_column("policy_versions", "review_comment")
    op.drop_column("policy_versions", "review_decision")
    op.drop_column("policy_versions", "reviewed_at")
    op.drop_column("policy_versions", "reviewed_by")
    op.drop_column("policy_versions", "submitted_at")
    op.drop_column("policy_versions", "submitted_by")
    op.drop_column("policy_versions", "lifecycle_status")

    op.drop_table("policy_control_mappings")
