"""add framework catalog_family and framework_adoptions (issue #43)

Revision ID: 7ea3c156037f
Revises: bb48a6be0d80
Create Date: 2026-08-20 09:31:19.568843

"""
import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7ea3c156037f"
down_revision: Union[str, Sequence[str], None] = "bb48a6be0d80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Explicit, hardcoded catalog_key -> catalog_family mapping (see
# docs/superpowers/specs/2026-08-20-issue43-catalog-version-pinning-design.md) —
# not a parsing heuristic. These are the only two catalog_key values
# app/framework_catalog.py::SYSTEM_CATALOGS has ever produced.
_CATALOG_KEY_TO_FAMILY = {
    "iso27001-2022-sample": "iso27001",
    "soc2-2017-sample": "soc2",
}


def _backfill_catalog_family_and_adoptions(connection) -> None:
    """Deterministic backfill: set catalog_family on the existing system
    catalog Framework rows, then auto-adopt each family that has no
    adoption yet — mirroring exactly what
    app/framework_catalog.py::reconcile_system_catalogs does for a
    brand-new deployment, so an upgraded existing deployment reaches the
    same state immediately rather than waiting for its next app restart.

    Never touches any ControlRequirementMapping/FrameworkRequirement row.
    """
    frameworks = sa.table(
        "frameworks",
        sa.column("id", sa.String),
        sa.column("catalog_key", sa.String),
        sa.column("catalog_family", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("updated_at", sa.DateTime),
    )
    framework_adoptions = sa.table(
        "framework_adoptions",
        sa.column("id", sa.String),
        sa.column("catalog_family", sa.String),
        sa.column("framework_id", sa.String),
        sa.column("previous_framework_id", sa.String),
        sa.column("adopted_at", sa.DateTime),
        sa.column("upgraded_at", sa.DateTime),
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

    for catalog_key, catalog_family in _CATALOG_KEY_TO_FAMILY.items():
        connection.execute(
            sa.update(frameworks)
            .where(frameworks.c.catalog_key == catalog_key)
            .values(catalog_family=catalog_family)
        )

    # framework_adoptions (the TABLE) is dropped entirely on downgrade,
    # so on a downgrade-then-upgrade cycle it is always empty here — the
    # projection row must always be re-inserted. The bootstrap
    # FrameworkAdoptionAdopted DomainEvent rows are immutable and never
    # deleted, so a repeat run must reuse the SAME aggregate_id (and
    # therefore the same FrameworkAdoption.id) an earlier run already
    # committed, recovered from that immutable event itself — same
    # idiom as 96fd3c35a6ae's InternalControlVersion backfill.
    existing_adopted_by_family: dict[str, tuple[str, dict, object]] = {}
    for row in connection.execute(
        sa.select(domain_events.c.aggregate_id, domain_events.c.payload_json, domain_events.c.occurred_at).where(
            domain_events.c.aggregate_type == "framework_adoption",
            domain_events.c.event_type == "FrameworkAdoptionAdopted",
            domain_events.c.actor_type == "migration",
        )
    ).fetchall():
        payload = json.loads(row.payload_json)
        existing_adopted_by_family[payload["catalog_family"]] = (row.aggregate_id, payload, row.occurred_at)

    for framework_row in connection.execute(
        sa.select(frameworks.c.id, frameworks.c.catalog_family, frameworks.c.updated_at).where(
            frameworks.c.catalog_family.in_(_CATALOG_KEY_TO_FAMILY.values())
        )
    ).fetchall():
        catalog_family = framework_row.catalog_family
        already_adopted = existing_adopted_by_family.get(catalog_family)
        if already_adopted is not None:
            adoption_id, adopted_payload, occurred_at = already_adopted
        else:
            adoption_id = uuid.uuid4().hex
            occurred_at = framework_row.updated_at
            adopted_payload = {"catalog_family": catalog_family, "framework_id": framework_row.id}
            connection.execute(
                sa.insert(domain_events).values(
                    id=uuid.uuid4().hex,
                    aggregate_type="framework_adoption",
                    aggregate_id=adoption_id,
                    aggregate_sequence=1,
                    event_type="FrameworkAdoptionAdopted",
                    schema_version=1,
                    payload_json=json.dumps(adopted_payload),
                    occurred_at=occurred_at,
                    recorded_at=occurred_at,
                    actor_type="migration",
                    actor_id=None,
                    correlation_id=None,
                    causation_id=None,
                    idempotency_key=None,
                )
            )

        connection.execute(
            sa.insert(framework_adoptions).values(
                id=adoption_id,
                catalog_family=catalog_family,
                framework_id=adopted_payload["framework_id"],
                previous_framework_id=None,
                adopted_at=occurred_at,
                upgraded_at=None,
            )
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "framework_adoptions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("catalog_family", sa.String(length=32), nullable=False),
        sa.Column("framework_id", sa.String(length=32), nullable=False),
        sa.Column("previous_framework_id", sa.String(length=32), nullable=True),
        sa.Column("adopted_at", sa.DateTime(), nullable=False),
        sa.Column("upgraded_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["framework_id"], ["frameworks.id"]),
        sa.ForeignKeyConstraint(["previous_framework_id"], ["frameworks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("catalog_family", name="uq_framework_adoption_catalog_family"),
    )
    op.add_column("frameworks", sa.Column("catalog_family", sa.String(length=32), nullable=True))

    _backfill_catalog_family_and_adoptions(op.get_bind())


def downgrade() -> None:
    """Downgrade schema.

    The backfilled DomainEvent rows are NOT deleted — immutable, and
    app/events.py's ORM guards reject deleting them anyway. A
    downgrade-then-upgrade cycle re-runs the same deterministic backfill
    (idempotent by construction via the catalog_family check above), not
    a second, duplicate set of bootstrap events.
    """
    op.drop_column("frameworks", "catalog_family")
    op.drop_table("framework_adoptions")
