# Issue #47: PII retention/redaction reconciled with immutable compliance history

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature/architecture

## Summary

Resolves the tension the issue poses between miniGRC's immutable
event-backed compliance history and legitimate PII redaction needs —
by showing, through direct inspection, that the tension is much
narrower than assumed, then closing the real gap that inspection
found. See
`docs/superpowers/specs/2026-08-20-issue47-pii-retention-redaction-design.md`
for the full design.

## The central finding: `DomainEvent` never held PII to begin with

Grepped every `payload={...}` construction across every module that
calls `append_and_project` (control occurrences, policy lifecycle,
control-definition versions, evidence repository, control tests,
compliance scope, findings): **zero** instances of a raw PII value
embedded in a `DomainEvent` payload. Every person reference across the
entire event-sourced domain is a stable `*_person_id`/`actor_id`
foreign key, never a copied email or display name. `Person` and `User`
are already plain, ordinary mutable reference tables —
`Person.in_scope` already changes via a normal UPDATE +
`record_audit_event`, the exact pattern this issue needed, not a new
one.

**This means the issue's own anticipated "genuine tension" between
immutability and redaction does not actually exist at the `DomainEvent`
layer**, and no stop condition applies — redacting `Person.email`/
`display_name`/`external_id` in place, with `Person.id` staying
stable, is sufficient on its own; every historical `*_person_id`
reference (`ControlOccurrence.responsible_person_id`,
`InternalControl.owner_person_id`, etc.) keeps resolving correctly, now
showing the redacted values, with zero code changes to any
event-sourced domain.

## The real gap: ten plain-string columns freeze a login email verbatim

`AuditEvent.actor` and nine other plain `String` columns
(`PolicyVersion.uploader`, and eight `created_by`/`last_reviewed_by`
columns) freeze a `User.email` value at write time. `AuditEvent` is
confirmed **not** event-sourced — `app/events.py`'s immutability
guards are registered specifically against `DomainEvent`, not
globally; `AuditEvent`'s own docstring calls it an
application-written convenience log, never claimed as an immutable
compliance-event aggregate. Redacting these ten columns is therefore a
deliberate, scoped, audited mutation of already-plain-mutable
columns — not a violation of any stated architecture invariant.

## What changed

- `migrations/versions/bb48a6be0d80_...py`: nullable `pii_redacted_at`
  on both `Person` and `User` — makes redaction idempotent without
  inferring it from the email's own shape. No backfill needed (every
  existing row simply starts `NULL`, meaning "not yet redacted").
- `app/pii_redaction.py` (new):
  - `redact_person_pii(session, person, *, actor)` — requires
    `employment_status == "departed"`; redacts `email`/`display_name`/
    `external_id`; `Person.id` never changes.
  - `redact_user_pii(session, user, *, actor)` — requires
    `status == "disabled"`; redacts the login `email` on `User` and
    bulk-updates every one of the ten `_ACTOR_STRING_COLUMNS` entries
    that historically held that exact email to a stable pseudonym
    (`redacted-user-<id>@redacted.invalid`) that still embeds the real,
    resolvable `User.id` — "who did this" stays attributable to a
    specific account, just no longer to its actual email.
  - Both record their own action via `record_audit_event`
    (`action="redact_pii"`) — redaction is itself auditable, not
    silent.

## Test strategy and results

- **Unit/integration** (`tests/test_pii_redaction.py`, 9 tests):
  `redact_person_pii` rejects a non-departed person, redacts a departed
  one while `Person.id` stays stable, is idempotent, and — the issue's
  own named scenario — a `ControlOccurrence.responsible_person_id`
  generated *before* redaction still resolves correctly to the now-
  redacted `Person` row afterward. `redact_user_pii` rejects an active
  user, is idempotent, sweeps all ten real tables constructed with
  genuine valid rows (not mocks) and verifies each one's column now
  shows the pseudonym while a *different, unrelated* user's rows in
  the same tables are left untouched. A manual-occurrence-recording
  path (`record_occurrence_manually`, the other `ControlOccurrence`
  creation route) is also verified to keep resolving after redaction.
- **Full regression suite**: `pytest -q` — 1061 passed, 7 skipped
  (Postgres-gated), 21 deselected (`uat` marker), 2 xfailed (issue #65,
  pre-existing/unrelated), 0 failed. Delta from the pre-#47 baseline
  (1052, immediately post-#46) is exactly this issue's 9 new tests.
- **Lint/format**: clean (`ruff check .`, `ruff format --check .`,
  including the design doc's embedded code blocks).
- **SQLite/PostgreSQL equivalence**: the new columns are plain nullable
  `DateTime`; the bulk-redaction sweep uses ordinary SQLAlchemy Core
  `update()` statements — no backend-specific behavior introduced.
- **No headless UAT / no HTTP route**: no user-visible surface in this
  issue — backend service layer only, matching the connector epic's
  and #42's own established precedent; wiring an admin-facing "redact
  this departed person" action is a UI concern for whichever future
  issue defines the operator-facing surface.

## Evidence retention policy (documented, not implemented)

Per the issue's own explicit non-goal ("a documented evidence-retention
policy is sufficient"): `EvidenceArtifactVersion` (#32) has no PII
field — it's file metadata, not personal data, so evidence retention
is a storage-cost/time question structurally independent of PII
redaction. No deletion/expiry path exists today (`delete_object`'s
only call site is upload-failure cleanup). Policy: the hash/existence
record must never be deleted (a permanent historical compliance fact);
the bytes may legitimately be purged after an operator-defined
retention period in a future, separate feature — not built here, since
no concrete retention-period requirement exists yet to build it
against.

## Known deferred/untested paths

- No HTTP route/admin UI to trigger redaction interactively — matches
  the same backend-only precedent this session has established
  repeatedly; the mechanism and its tests prove it works, an operator-
  facing surface is a separate future concern.
- A theoretical, low-severity race exists if two concurrent calls to
  `redact_person_pii`/`redact_user_pii` for the same row both pass the
  `pii_redacted_at is None` check before either commits — the final
  data state is still correct/idempotent either way (same pseudonym
  either call produces), but the audit trail could gain one duplicate
  `redact_pii` entry. Not addressed with additional locking, given this
  is a rare, human-triggered admin action, not a hot path — matches
  the "smallest coherent mechanism" scope the issue itself asks for.
- No byte-level evidence purge job — documented policy only, per the
  issue's own explicit non-goal.
