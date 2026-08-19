# Issue #41: convention for migrating an existing mutable domain onto the event store

Status: implemented and proven — see §4. No new code required; this
document formalizes and generalizes a convention #31 already
implements and #22's own design doc already authorized.

## 1. Repository-reality check

- #21/#22 (event store foundation) explicitly deferred this: "Existing
  data migration into event-backed truth belongs to a follow-up issue...
  Document the migration strategy/gap for follow-up." No issue owned it
  until #41 filed the gap.
- **#41's own premise needs a correction**: it names #11 (control
  operations) as a candidate "real domain" alongside #31. #11 never
  actually needed this — `ControlOccurrence` was a brand-new table
  introduced by #22 itself, with no pre-existing mutable rows to
  migrate. **#31 (policy-version lifecycle) is the only domain that has
  actually done this** — its migration (`c4e8a7f2b391`) backfills
  bootstrap `DomainEvent` rows for pre-existing `PolicyVersion` rows.
  This document formalizes the convention from that real implementation
  rather than building a second, synthetic example.
- `docs/superpowers/specs/2026-08-15-issue22-event-store-design.md` §5
  already establishes the foundational rule, before #41 was even filed:
  `recorded_at` is unconditionally the append-time `utcnow()`;
  `occurred_at` defaults to `recorded_at` but "a caller may only pass a
  different `occurred_at` for genuine backdating (e.g. a migration/
  import fact)," and doing so requires `actor_type="migration"`, never
  `"user"`.
- **#41's issue text narrows this further than #22 §5 already allows**:
  it says a bootstrap event's `occurred_at` should be "left unset/equal
  to `recorded_at` since the real historical `occurred_at` is
  unknowable" — stated as if that's always true. It is not: #31's
  migration backfills `occurred_at` from each `PolicyVersion`'s own
  `captured_at` column (a real, already-recorded timestamp for when
  that version was captured) — a genuine signal, not a fabrication, and
  a strictly more historically accurate choice than pretending the
  migration's run time is when the fact "happened." #22 §5 already
  permits exactly this ("genuine backdating"); #41's text simply
  described the no-signal-available case as if it were the only case.
  This document adopts the fuller rule #22 §5 already authorized, with
  #31 as the proof it works correctly.

## 2. The convention

When an existing mutable domain gains an event-backed aggregate for
some slice of its state (as #31 did for `PolicyVersion`'s lifecycle
columns, leaving its file-capture columns as a separately-justified
plain fact — see that design doc §2/§4 for why not everything on a row
needs to be event-sourced):

1. **Bootstrap events are real `DomainEvent` rows**, inserted by the
   Alembic migration itself (via `op.execute`/Core `sa.insert`, not
   through the ORM/`append_event` — migrations in this repo do not
   import `app/` modules), for every pre-existing mutable row that
   needs an initial event history.
2. **`actor_type = "migration"`, `actor_id = NULL`, always.** Never
   `"user"`, even when a plausible human actor could be guessed from
   other columns (e.g. `PolicyVersion.uploader`) — a bootstrap fact is
   not a real-time human action and must never be presented as one.
3. **`occurred_at` uses the best available real proxy timestamp already
   recorded on the row being migrated, if one exists; falls back to the
   migration's own run time (equal to `recorded_at`) only when no such
   signal exists.** This is the corrected/generalized rule (see §1) —
   #22 §5 already permits genuine backdating for migration facts; this
   is what "genuine" means in practice: use real recorded data, don't
   invent a plausible-sounding one, and don't pretend "now" is more
   accurate than a real timestamp you already have.
4. **`aggregate_sequence` starts at 1** for a bootstrap event — safe
   specifically because the migration is the *first* time that
   aggregate type/id pair can have any event at all (the columns/table
   the event projects onto didn't exist before this same migration).
5. **The backfill must be idempotent.** A downgrade of the schema
   change does not (and must not) delete the bootstrap `DomainEvent`
   rows it created — `DomainEvent` rows are immutable by construction
   (`app/events.py`'s `before_update`/`before_delete`/bulk-mutation
   guards) and this repo never destructively rewrites history. That
   means a later re-upgrade could re-run the same backfill against an
   event store that already holds last time's bootstrap events for the
   same aggregate ids — the migration must detect and skip re-inserting
   a duplicate for any aggregate already represented, while still
   safely re-applying the (idempotent) projection-column `UPDATE`s so a
   freshly-recreated column set ends up correct either way. See #31's
   migration (`c4e8a7f2b391::_backfill_lifecycle`'s
   `already_backfilled_aggregate_ids` check) for the concrete pattern —
   this was found and fixed via a real upgrade→downgrade→upgrade round
   trip test, not designed in the abstract.
6. **Real, subsequent mutations go through `append_event`/
   `append_and_project` normally**, with `actor_type="user"` and a real
   `actor_id`, exactly like any event-backed command that was never a
   migration concern in the first place. A bootstrap event only ever
   covers the *starting* state at migration time; everything after that
   point is ordinary event-sourced history.
7. **Existing mutable rows are never destructively rewritten.** The
   migration is additive: new `domain_events` rows referencing existing
   entity ids, plus new nullable projection columns/tables on the
   existing row if the event-backed slice needs its own state (as
   `PolicyVersion.lifecycle_status` etc. did for #31) — never a rewrite
   of the pre-existing columns' meaning or values beyond the new
   columns' own backfill.

## 3. What this does NOT require

- Migrating every existing mutable domain in one pass — this is a
  per-domain decision made when (and only when) that domain's state
  actually becomes event-backed, matching #22's own "smallest coherent
  slice" discipline.
- Removing `AuditEvent`/`app/audit.py::record_audit_event` — the
  administrative, human-readable audit trail continues to coexist
  alongside real domain events for event-backed domains, and remains
  the only mechanism for domains that haven't (yet, or ever need to)
  become event-backed.
- Inventing a plausible human actor for a fact whose real actor is
  unknown. `actor_type="migration"` is the honest answer, not a
  workaround to avoid using it.

## 4. Proof: #31's policy-version lifecycle migration

`migrations/versions/c4e8a7f2b391_add_policy_version_lifecycle.py`
already satisfies every acceptance criterion below, verified by
`tests/test_policy_lifecycle_migration.py` (merged in #31, PR #62):

- **Convention documented and followed**: latest version of an
  `approved`/`retired` policy backfills to `"effective"` with
  `occurred_at` = that version's real `captured_at`; every non-latest
  version backfills to `"superseded"` with `occurred_at` = the
  superseding version's real `captured_at`; both insert a matching
  `DomainEvent` with `actor_type="migration"`.
- **Idempotent / safe to re-run**:
  `test_upgrade_downgrade_upgrade_round_trip_does_not_duplicate_events`
  proves a downgrade-then-upgrade cycle does not insert a duplicate
  bootstrap event for the same aggregate.
- **Projection-rebuild equivalence**:
  `tests/test_policy_lifecycle.py::test_rebuild_policy_version_lifecycle_projection_reproduces_state`
  proves `rebuild_policy_version_lifecycle_projection` (which resets
  every `PolicyVersion`'s lifecycle columns and replays only real
  `DomainEvent` rows, bootstrap and live alike, with no special-casing)
  reproduces identical state — the bootstrap events are not a special
  case the rebuild contract has to accommodate; they are ordinary
  events by the time replay sees them.
- **Bootstrap events unambiguously distinguishable**:
  `test_backfill_inserts_bootstrap_events_marked_as_migration` asserts
  `actor_type == "migration"` on every backfilled row; any reporting/
  audit-trail view built later can filter on this the same way it would
  filter `actor_type == "user"` vs `"system"`.
- **No destructive rewrite**: the migration only adds columns/a table/
  constraints/an index and inserts new `domain_events` rows; no
  existing `policies`/`policy_versions` column value is altered outside
  the new lifecycle columns' own backfill.

## 5. Guidance for the next domain (e.g. #42's control-definition lifecycle)

When a future issue extends event-backing to another existing mutable
domain (`InternalControl` definitions, per #42, is the next likely
candidate — now unblocked since #31 merged):

- Follow §2's seven rules directly; there is no reason to re-derive
  them.
- Look for a real, already-recorded timestamp on the row being migrated
  before falling back to migration run time for `occurred_at` — most
  domains in this repo already have a `created_at`/`updated_at` or a
  domain-specific timestamp (e.g. `captured_at`) that is a better proxy
  than "now."
- Write the idempotency and rebuild-equivalence tests *as part of* that
  issue's own migration test file, mirroring
  `tests/test_policy_lifecycle_migration.py` directly — do not treat
  this document as requiring a separate migration or PR of its own.
