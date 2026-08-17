"""add framework catalog key and primary flag

Revision ID: a248573ccdfe
Revises: 2f6df407060b
Create Date: 2026-08-17 19:27:45.700164

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a248573ccdfe"
down_revision: Union[str, Sequence[str], None] = "2f6df407060b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The exact, literal name/version app/seed.py has always used for its
# seeded ISO framework. Only used to identify a pre-existing row deployed
# before issue #12's reconciliation mechanism existed — never a fuzzy
# match. See docs/superpowers/specs/2026-08-17-issue12-soc2-primary-
# framework-design.md §5.4 for why this must count matches before acting.
_LEGACY_ISO_NAME = "ISO/IEC 27001:2022 Annex A (sample catalogue)"
_LEGACY_ISO_VERSION = "2022"
_LEGACY_ISO_CATALOG_KEY = "iso27001-2022-sample"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("frameworks", sa.Column("catalog_key", sa.String(length=64), nullable=True))
    # server_default backfills every existing framework row as non-primary
    # — no existing framework retroactively becomes "primary" by this
    # migration; app/framework_catalog.py's reconciliation is what sets
    # is_primary=True on the SOC 2 catalog going forward.
    op.add_column(
        "frameworks", sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    # SQLite can't ALTER TABLE to add a UNIQUE constraint directly — batch
    # mode recreates the table under the hood (harmless no-op wrapper on
    # Postgres). Matches 8961da81a764_add_user_status_and_google_subject.py.
    with op.batch_alter_table("frameworks", schema=None) as batch_op:
        batch_op.create_unique_constraint("uq_framework_catalog_key", ["catalog_key"])

    # Deterministic, count-checked backfill: identify the pre-existing
    # seeded ISO framework (if any) by its exact, known literal name and
    # version — never a heuristic/fuzzy match. Backfill only on exactly
    # one match; on zero matches there is nothing to backfill (a fresh
    # database, or one that never ran the old seed — reconciliation
    # creates a fresh catalog_key-tagged row on next startup); on more
    # than one match the identity is genuinely ambiguous, so none are
    # touched and a warning is printed — failing safely on ambiguity
    # rather than guessing, per .agent/RULES.md §4.
    connection = op.get_bind()
    frameworks = sa.table(
        "frameworks",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("version", sa.String),
        sa.column("catalog_key", sa.String),
    )
    candidates = connection.execute(
        sa.select(frameworks.c.id).where(
            frameworks.c.name == _LEGACY_ISO_NAME,
            frameworks.c.version == _LEGACY_ISO_VERSION,
        )
    ).fetchall()
    if len(candidates) == 1:
        connection.execute(
            sa.update(frameworks)
            .where(frameworks.c.id == candidates[0].id)
            .values(catalog_key=_LEGACY_ISO_CATALOG_KEY)
        )
    elif len(candidates) > 1:
        print(  # noqa: T201 - migration-time operator diagnostic, not application logging
            f"WARNING: {len(candidates)} existing frameworks match the legacy ISO "
            f"seed's exact name/version — ambiguous, none backfilled with "
            f"catalog_key. Matched framework ids: {[c.id for c in candidates]}. "
            f"app/framework_catalog.py will create a fresh, separately-identified "
            f"ISO catalog framework alongside these on next startup."
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("frameworks", schema=None) as batch_op:
        batch_op.drop_constraint("uq_framework_catalog_key", type_="unique")
    op.drop_column("frameworks", "is_primary")
    op.drop_column("frameworks", "catalog_key")
