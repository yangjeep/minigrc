# Issue #47: PII retention/redaction reconciled with immutable compliance history

## 1. The tension is real but much narrower than the issue assumes

Repository inspection (grepping every `payload={...}` construction
across every module that calls `append_and_project` — control
occurrences, policy lifecycle, control-definition versions, evidence
repository, control tests, compliance scope, findings) found **zero**
instances of a raw PII value (`Person.email`, `Person.display_name`)
embedded in a `DomainEvent` payload. Every reference to a person
across the entire event-sourced domain is a stable `*_person_id`
foreign key (`ControlOccurrence.responsible_person_id`,
`InternalControl.owner_person_id`, etc.) — never a copied string.
`actor_id` on every event is likewise always `User.id`, never an email.

**This means `DomainEvent` immutability and PII redaction do not
actually conflict for the event store itself.** `Person` (and `User`)
are already plain, ordinary mutable reference tables — `Person.in_scope`
already changes via a normal UPDATE + `record_audit_event`, the exact
same pattern this issue needs, not a new one. Redacting
`Person.email`/`display_name`/`external_id` in place, with `Person.id`
staying stable, automatically redacts what every historical
`*_person_id`-referencing view displays — no event, no projector
change, no new event type required.

**The real, narrower gap is `AuditEvent.actor` and nine other plain
`String` columns that freeze a `User.email` value verbatim at write
time**, confirmed by grep: `AuditEvent.actor`, `PolicyVersion.uploader`,
`ControlPeriod.created_by`, `ControlTestPopulation.created_by`,
`ControlTestSample.created_by`, `RequirementAssessment.last_reviewed_by`,
`Secret.created_by`, `ExternalConnection.created_by`, `Job.created_by`,
`ImportJob.created_by`. `AuditEvent` itself is confirmed **not**
event-sourced — `app/events.py`'s immutability guards
(`before_update`/`before_delete`/`do_orm_execute`) are registered
specifically against `DomainEvent`, not globally; `AuditEvent`'s own
docstring calls it "written by application code alongside a mutation,"
never claimed anywhere as an immutable compliance-event aggregate.
Redacting these columns is therefore a deliberate, scoped, audited
mutation of already-plain-mutable convenience/log tables — not a
violation of any stated architecture invariant. **This resolves the
issue's own "genuine tension" question: there is no unresolvable
conflict requiring a stop condition.** The tension the issue
anticipated was based on an assumption (PII embedded in event
payloads) that inspection disproves.

## 2. Scope: two separable redaction targets

1. **`Person` directory-entry redaction** (`redact_person_pii`) — the
   compliance-relevant identity `*_person_id` FKs reference. Requires
   `employment_status == "departed"` (an existing value in
   `EMPLOYMENT_STATUSES`, not a new one) — refuses to redact anyone not
   already marked departed, so this can never accidentally strip a
   current employee's contact details. Idempotent via a new
   `pii_redacted_at` column (nullable, `Person`) — a second call is a
   safe no-op, not a duplicate redaction.
2. **`User` login-identity redaction** (`redact_user_pii`) — the
   *login* email, which gets frozen verbatim into ten different plain
   `String` columns across the schema every time that user takes an
   action. Requires `status == "disabled"` (an existing value in
   `USER_STATUSES`) — refuses to redact an active login (which would
   both break their ability to sign in and rewrite audit attribution
   for someone still working). Also idempotent via a new
   `pii_redacted_at` column on `User`.

A real departed employee may be represented as a `Person`, a `User`
linked via `User.person_id`, or both — the two functions are separate
and composable rather than one that assumes a specific combination.

## 3. The ten-table actor-string registry

```python
_ACTOR_STRING_COLUMNS: tuple[tuple[type, str], ...] = (
    (AuditEvent, "actor"),
    (PolicyVersion, "uploader"),
    (ControlPeriod, "created_by"),
    (ControlTestPopulation, "created_by"),
    (ControlTestSample, "created_by"),
    (RequirementAssessment, "last_reviewed_by"),
    (Secret, "created_by"),
    (ExternalConnection, "created_by"),
    (Job, "created_by"),
    (ImportJob, "created_by"),
)
```

A declarative registry, not ten hand-written UPDATE statements — every
entry gets the identical treatment (`UPDATE <table> SET <column> =
new_pseudonym WHERE <column> = old_email`, via SQLAlchemy Core
`update()`, bypassing nothing since none of these are ORM-guarded).
Justified as a genuine abstraction, not a speculative one: ten real,
already-existing columns need the exact same operation today, not a
hypothetical future one.

The pseudonym (`f"redacted-user-{user.id}@redacted.invalid"`) embeds
the real, stable, never-deleted `User.id` — deliberately **not**
anonymous. An authorized admin can still resolve "this action was
taken by account `<id>`, whose PII has since been redacted," matching
the issue's own "preserve their stable identity reference" requirement
exactly, while no longer exposing the actual email anywhere these
columns are displayed.

## 4. What is NOT touched

- No `DomainEvent` row is ever mutated, deleted, or specially
  redacted — there was never PII in one to redact (§1).
- No `*_person_id`/`actor_id` foreign key or ID column is ever
  changed — every reference keeps resolving to the same row, now
  showing redacted display values.
- `ControlOccurrence`/every other event-sourced projection needs zero
  code changes — they already resolve `Person`/`User` display data by
  ID at read time, never by a denormalized copy.

## 5. Evidence retention policy (documented only, per the issue's own non-goals)

`EvidenceArtifactVersion` (#32) has no PII field — it's file metadata
(`object_key`, `sha256`, `media_type`, `byte_size`, provenance), not
personal data. Evidence retention is a storage-cost/time question,
structurally independent of PII redaction, and no deletion/expiry path
exists today (`delete_object`'s only call site is upload-failure
cleanup, not retention). Policy, documented rather than implemented
(matching the issue's own "a documented evidence-retention policy is
sufficient" non-goal):

- The **hash and existence record** of a captured evidence version
  (`EvidenceArtifactVersion` row: `sha256`, `object_key`,
  `original_filename`, provenance, capture timestamp) is a historical
  compliance fact and must never be deleted, matching every other
  event-sourced record's immutability.
- The **bytes** in S3-compatible storage may legitimately be purged
  after a retention period an operator defines (not specified by this
  issue, since no such period exists in the PRD) — this is a future,
  separate feature (a real "purge expired evidence bytes" job would
  need a `bytes_purged_at` marker distinct from the row's own
  existence, exactly the "tombstone" shape the issue anticipates) —
  not built here, since no concrete retention-period requirement
  exists yet to build it against (RULES.md: no speculative
  infrastructure without a concrete use).

## 6. Security review

- Redaction requires the same authorization boundary as every other
  admin mutation in this codebase (a caller-supplied `actor`, recorded
  via `record_audit_event` exactly like `Person.in_scope`'s existing
  edit routes) — this module does not introduce a new authorization
  concept, and does not itself add an HTTP route (backend service
  layer only, matching #24-28/#42's own established precedent; wiring
  an admin-facing "redact this departed person" button is a UI concern
  for whichever future issue defines the operator-facing surface).
- **Could redaction be abused to hide a compliance-relevant fact
  rather than genuine non-material PII?** No — only `email`/
  `display_name`/`external_id` are ever changed; every material
  compliance fact (`employment_status` itself, `*_person_id`
  references, every `DomainEvent`, every projection's own compliance
  columns) is untouched. The `employment_status == "departed"` /
  `status == "disabled"` gates mean redaction can only ever apply to
  someone already recorded as no longer active — it cannot be used to
  retroactively obscure who performed a *current* action.
- The redaction action itself is always recorded via
  `record_audit_event` (`action="redact_pii"`) — redaction is
  auditable, not silent.

## 7. Test strategy

- `redact_person_pii` rejects a non-departed person; is idempotent
  (second call is a safe no-op); redacts email/display_name/
  external_id while `Person.id` stays stable; a `ControlOccurrence`
  still resolves `responsible_person_id` to the (now redacted) row
  without any change to the occurrence itself.
- `redact_user_pii` rejects a non-disabled user; is idempotent;
  redacts the login email on `User` and, verified per table, every one
  of the ten `_ACTOR_STRING_COLUMNS` entries that previously held that
  exact email — while every `*_id`/`actor_id` column referencing that
  user remains unchanged.
- The redaction action itself is recorded via `AuditEvent`.
- SQLite/PostgreSQL equivalence for the new `pii_redacted_at` columns
  (a plain nullable `DateTime`, no backend-specific behavior).

## 8. Definition of done

- A documented policy exists reconciling immutable `DomainEvent`
  history with PII redaction — resolved by showing the events never
  held PII to begin with, not by inventing a new mechanism to
  route around them.
- A concrete, tested redaction path exists for both the compliance-
  relevant `Person` directory and the ten plain-string columns that
  freeze a departed `User`'s login email.
- No `DomainEvent` row is ever mutated or deleted by this mechanism.
- An evidence-retention policy is documented, distinguishing
  byte-level purge (future, undefined retention period) from
  historical hash/existence preservation (permanent).
