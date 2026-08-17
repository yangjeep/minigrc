# Issue #12: SOC 2 as the primary framework experience, ISO 27001 preserved

Status: revised after adversarial review — includes reconciliation against
prior unshipped design work (§1.3). Ready for implementation.

## 0. Scope and how this doc is organized

Issue #12's own execution prompt assumes a "Feature 14" system framework
catalog/reconciliation subsystem already exists and just needs a SOC 2
catalog "added to it." §1 is the repository-reality check (and, after
adversarial review, the reconciliation against real prior design work)
that this assumption required. Everything after §1 is the corrected
design, implemented in this issue's own scope.

## 1. Repository-reality correction (read first)

### 1.1 What #12's execution prompt assumes vs. what exists

**Claim:** "Inspect Feature 14's current framework catalog, reconciliation,
mapping, default-selection, requirement/checklist, and seed/backfill
behaviour before editing... Add SOC 2 as a system framework/catalog using
the existing reconciliation path. Reconciliation must remain idempotent
and additive-only."

**What actually exists on `main`, verified by direct inspection:** no
"Feature 14" code anywhere (`grep` across `app/`, `docs/superpowers/specs/`,
`docs/worklog/` returns nothing on `main`). `app/seed.py::seed_if_empty` is
the only startup-time framework bootstrap — a one-shot gate
(`if session.query(Framework).first() is not None: return False`) that
creates one placeholder ISO framework, 5 requirements, 4 demo
`InternalControl`s + mappings, and 2 demo `Risk`s as a single combined
"fresh-database demo dataset." No `catalog_key`/`is_system`-equivalent
column exists on `Framework`; `POST /frameworks/{id}/edit`
(`app/routers/frameworks.py:139-172`) lets any logged-in user rename/
re-version *any* framework today, including a would-be system-seeded one.
No primacy/default-framework concept exists anywhere.

### 1.2 A reusable additive-by-code precedent already exists

`app/csv_import.py::import_requirements_csv` computes `existing_codes =
{r.reference_code for r in framework.requirements}` and skips/reports
codes that already exist, then calls the shared
`app/requirements.py::add_requirement` for new ones — the exact
per-framework, additive-by-`reference_code` shape #12 needs, just not
wired to run automatically for system catalogs at startup.

### 1.3 Prior unshipped design work, found by adversarial review

An independent adversarial review of this design's first draft found real
prior art: `origin/yangjeep/dev-soc2-2` (three doc-only commits, branched
from an ancestor of current `main` that predates #21/#22/#40/#11) contains
`docs/superpowers/specs/2026-07-21-feature14-multiframework-foundation-design.md`
— a 1018-line, twice-adversarially-reviewed design for "Feature 14 slice
1/4: multi-framework foundation." It never shipped, was never merged, and
**no GitHub issue tracks its scope** (confirmed by searching all open/
closed issues for "multi-framework," "framework catalog," "trust services
categor(y/ies)," "Feature 14" — nothing matches; the closest is #43,
"organization-level framework/catalog version pinning," a separate,
narrower, not-yet-scheduled P2). This is very likely what #12's execution
prompt's "Feature 14" reference was actually pointing at — real, careful
prior design work that stalled before implementation, not a
fabricated/hallucinated dependency.

That design already correctly solved several problems this design's first
draft got wrong or left unaddressed: a seed-duplication bug from changing
`seed_if_empty`'s gate carelessly, a concurrency race in catalog
reconciliation under multiple app instances, an unsafe "table empty ==
fresh install" heuristic, and a legacy-ISO-framework identification
protocol for existing installations. It also designs substantially more
than #12 asks for: a `FrameworkCategory`/Trust-Services-Category scoping
model with a composite-FK ownership invariant, a persistent
`CatalogConflict` table with an admin acknowledgment UI, admin-gating
every framework CRUD route (a real authorization change from today's
"any logged-in user" behavior), and a `catalog_status`
(`seed_incomplete`/`ready`/`conflict`) gate on readiness-percentage
presentation.

**Decision, and why:** #12's actual GitHub acceptance criteria (§2 below)
do not require category/TSC scoping, a conflict-resolution admin UI,
authorization changes to existing framework routes, or completeness
gating — and #12's own non-goals explicitly rule out "broad UI redesign."
Building that full slice here would be exactly the "correct implementation
requires materially expanding MVP scope" condition `.agent/RULES.md` §12
asks to surface rather than decide silently. This design **adopts the
specific mechanisms** from that prior work needed to satisfy #12's own
"idempotent and additive-safe" / "no destructive migration" acceptance
criteria (concurrency-safe reconciliation, safe legacy-framework handling,
a demo-seed gate that survives reconciliation always running), each
right-sized to this smaller scope using patterns already established
elsewhere in this codebase rather than the larger design's new primitives
— and **explicitly defers** the category-scoping model, conflict admin UI,
authorization changes, and completeness gating as real, legitimate future
work, citing the source document, rather than silently dropping it or
silently building it unasked. See §7 for the itemized adopt/defer list and
rationale for each.

## 2. Gap analysis (required by #12)

| #12 requirement | Current state | Gap |
|---|---|---|
| SOC 2 available as a versioned system framework | No SOC 2 content anywhere | Need a placeholder SOC 2 catalog, same legal posture as the existing ISO placeholder |
| SOC 2 is the default/primary experience | No primacy concept exists | Minimal `is_primary` flag + list ordering, not a full onboarding wizard (#30, not yet built) |
| Idempotent, additive reconciliation | Only a one-shot whole-DB-empty seed | Real per-catalog reconciliation, safe under concurrency, built from the existing CSV-import additive pattern |
| Existing ISO-only DB upgrades safely | Mechanism doesn't exist yet | Migration backfill must identify the existing seeded row deterministically and fail safely (not fabricate a match) on ambiguity |
| Shared control → multiple framework requirements | `ControlRequirementMapping` already many-to-many, framework-agnostic (verified: no `framework_id` column on it at all) | No code gap; seed data must actually demonstrate it (round-2 review Finding 6) |
| Product mappings vs. official equivalence distinguishable | N/A | Explicit "product-authored" disclaimer, mirroring the ISO placeholder-content convention |
| Framework removal/disable without deleting shared history | `Framework.is_active` toggle already exists, never deletes anything, and has no FK relationship to `ControlOccurrence`/`ControlPeriod` at all (verified) | No code gap; needs a regression test locking this in |

## 3. SOC 2 Trust Services Criteria — structural facts (not reproduced text)

Researched from public, non-paywalled, secondary sources. The AICPA's own
TSC document and its official ISO crosswalk are both licensed/paywalled
and are **not** fetched or reproduced anywhere in this repo, matching
`docs/domain/domain-model.md`'s existing ISO 27001 copyright posture:

- Five Trust Services Categories. **Security** is mandatory for every
  SOC 2 report; **Availability**, **Processing Integrity**,
  **Confidentiality**, and **Privacy** are optional, scoped in per
  organization based on what it wants attested.
- Security's requirements are the 33 "Common Criteria" (CC-series): CC1
  Control Environment (5 sub-criteria), CC2 Communication and Information
  (3), CC3 Risk Assessment (4), CC4 Monitoring Activities (2), CC5 Control
  Activities (3), CC6 Logical and Physical Access Controls (8), CC7 System
  Operations (5), CC8 Change Management (1), CC9 Risk Mitigation (2).
- **Type I vs. Type II is a report-level distinction, not a criteria
  structure difference** — same criteria either way; Type I attests design
  at a point in time, Type II attests operation over a period. Confirms
  #11's control-period/occurrence model (already built and merged) is the
  correct, and only needed, mechanism for representing Type II — no
  separate schema is needed here for that distinction.
- A SOC 2 ↔ ISO 27001 crosswalk is standard industry practice, but the
  AICPA's own official crosswalk is licensed content. Any crosswalk this
  app ships must be labeled a **product-authored interpretation**, never
  official AICPA/ISO equivalence.

Sources (public secondary overviews of structure only):

- ["The 5 SOC 2 Trust Services Criteria Explained"](https://cloudsecurityalliance.org/blog/2023/10/05/the-5-soc-2-trust-services-criteria-explained) — Cloud Security Alliance
- ["SOC 2 Trust Services Criteria (TSC): A Guide"](https://www.cbh.com/insights/articles/soc-2-trust-services-criteria-guide/) — Cherry Bekaert
- ["SOC 2 Common Criteria List: CC-Series Explained"](https://www.compassitc.com/blog/soc-2-common-criteria-list-cc-series-explained) — CompassITC
- ["SOC 2 common criteria: What each CC control actually requires"](https://www.scrut.io/hub/soc-2/soc-2-common-criteria) — Scrut
- ["SOC 2 Type 1 vs Type 2: What's the Difference?"](https://secureframe.com/hub/soc-2/type-1-vs-type-2) — Secureframe
- ["SOC 2 Criteria Mapping to ISO 27001 Controls"](https://sprinto.com/blog/soc-2-criteria-mapping-to-iso-27001/) — Sprinto (crosswalk-convention example)

**Placeholder scope decision:** seed only the mandatory Security category
— one representative reference code per CC-series (9 total), matching the
ISO placeholder's own "representative codes, not a full catalogue"
precedent — as `is_placeholder_content=True`, paraphrased titles/summaries
(never AICPA text). Availability/Processing Integrity/Confidentiality/
Privacy are deliberately not seeded; an organization adds them via the
existing manual-add/CSV-import paths once it decides to scope them in.
This matches the sibling design's own §6 reasoning that ISO's partial
catalog has shipped and computed real readiness percentages without any
"incomplete" gate since the MVP — SOC 2's placeholder gets the same
treatment for consistency, not a new completeness-status concept (see
§7's deferred list for why a `catalog_status` gate is not built here).

## 4. Domain model changes

```python
class Framework(Base):
    ...
    catalog_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_primary: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (UniqueConstraint("catalog_key", name="uq_framework_catalog_key"),)
```

- `catalog_key`: stable identity for "this row is the canonical instance
  of system catalog X" — e.g. `"iso27001-2022-sample"`,
  `"soc2-2017-sample"`. **Nullable, unique.** NULL for every user-created
  framework; standard SQL NULL != NULL semantics mean any number of
  user-created (NULL) frameworks coexist without collision. Reconciliation
  looks frameworks up **by `catalog_key`, never by name/version** —
  immune to a user later renaming a system-seeded framework via the
  existing edit route. **Not exposed on any writable route or
  register-grid field** (verified: no route/FieldSpec anywhere accepts a
  `catalog_key` value from a client) — the only way a row gets a
  `catalog_key` is reconciliation creating it, or the migration backfill
  identifying it.
- `is_primary`: minimal default/primacy flag, plain boolean, no enforced
  singleton (an org could end up with zero or several — matches this
  repo's existing minimal-invariant posture, e.g.
  `InternalControl.owner_person_id` has no uniqueness enforcement either).
  Seeded `True` for SOC 2, `False` for ISO. No toggle route in this
  issue's scope (§7).

**Known, accepted, pre-existing limitation (not newly introduced by this
design):** nothing about `catalog_key`/`is_primary` changes today's
authorization on `POST /frameworks/{id}/edit` — any logged-in user can
still rename or deactivate the SOC 2 system framework exactly as any
framework has always been fully editable by any logged-in user. This is
unchanged existing behavior, not a regression this issue introduces; see
§7 for why admin-gating those routes is deliberately deferred rather than
silently added as a side effect of this issue.

## 5. Reconciliation mechanism

New module `app/framework_catalog.py`:

```python
@dataclass(frozen=True)
class CatalogRequirement:
    reference_code: str
    title: str
    summary: str
    display_order: int

@dataclass(frozen=True)
class SystemCatalog:
    catalog_key: str
    name: str
    version: str
    description: str
    is_primary: bool
    requirements: tuple[CatalogRequirement, ...]

SYSTEM_CATALOGS: tuple[SystemCatalog, ...] = (
    SystemCatalog(catalog_key="iso27001-2022-sample", is_primary=False, ...),
    SystemCatalog(catalog_key="soc2-2017-sample", is_primary=True, ...),
)

def reconcile_system_catalogs(session: Session) -> list[Framework]:
    """Find-or-create each SYSTEM_CATALOGS entry by catalog_key, then add
    any of its requirements missing by reference_code (reusing
    app/requirements.py::add_requirement). Never updates/removes an
    existing Framework or FrameworkRequirement row — additive only, safe
    to call on every startup, unconditionally, including under concurrent
    callers (see 5.1)."""
```

Called from `create_app` **before** `seed_if_empty`, unconditionally:

```python
with session_scope(session_factory) as session:
    reconcile_system_catalogs(session)  # every startup, idempotent, concurrency-safe
    if seed_if_empty(session):  # gate revised, see 5.2 — fixes a real bug found in review
        logger.info("seeded example dataset")
```

### 5.1 Concurrency safety (adopted from the prior design's §1, right-sized)

**The bug this closes:** a naive "query by `catalog_key`, insert if
absent" is only *sequentially* idempotent. Two app instances starting
concurrently against the same Postgres database (a realistic case for
this app's supported Kubernetes/multi-replica deployment target, per
`docs/architecture.md` and the Helm chart) can both observe "absent" before
either commits, and both attempt to `INSERT` the same `catalog_key` — one
succeeds, one raises `IntegrityError` inside the shared `session_scope`
block, which rolls back and re-raises, **crashing that instance's
startup**, not just skipping a redundant insert.

**The fix, matching this codebase's own already-established pattern**
(`app/events.py::_append_event_with_created_flag`, already reused for
#11's evidence-linking route and #50's delete-guard fix) rather than the
prior design's larger advisory-lock/`BEGIN IMMEDIATE`-with-bounded-retry
primary-serialization layer: wrap each catalog's create step in
`session.begin_nested()`, and on `IntegrityError`, let the savepoint
rollback and re-`SELECT` by `catalog_key` to pick up whichever instance's
insert actually won:

```python
def _find_or_create_catalog_framework(session: Session, catalog: SystemCatalog) -> Framework:
    existing = session.scalar(select(Framework).where(Framework.catalog_key == catalog.catalog_key))
    if existing is not None:
        return existing
    try:
        with session.begin_nested():
            framework = Framework(catalog_key=catalog.catalog_key, ...)
            session.add(framework)
            session.flush()
    except IntegrityError:
        return session.scalar(select(Framework).where(Framework.catalog_key == catalog.catalog_key))
    return framework
```

The same shape applies per-requirement (keyed on
`(framework_id, reference_code)`, the existing
`uq_requirement_framework_code` constraint). This prevents the crash — the
serious consequence — without adding a new locking primitive to the
codebase; the accepted, narrow, documented residual cost is that under
true concurrent first-ever creation, one instance may do one wasted
insert-then-refetch cycle, exactly once per catalog, ever (every
subsequent startup is a cheap idempotent read). This is a materially
narrower exposure than #22's own already-accepted idempotency-key race
(which can recur on every command, not once per catalog's lifetime) —
consistent with that precedent rather than a stricter bar applied
inconsistently.

### 5.2 Fixing the seed-duplication bug (review Finding 1)

**The bug:** the first draft of this design claimed `seed_if_empty` needed
no change beyond its gate, since it would "look up" the ISO framework's
requirements. It does not — current `app/seed.py` unconditionally
constructs its own `Framework(name="ISO/IEC 27001:2022...", ...)` object
and calls `add_requirement` against it directly, with no query. If
`reconcile_system_catalogs` runs first and creates the canonical,
`catalog_key`-tagged ISO framework, an unmodified `seed_if_empty` would
create a **second**, `catalog_key=NULL`, name/version-identical ISO
framework with its own 5 requirements, and map all 4 demo
`InternalControl`s to that orphan duplicate — on every fresh install,
silently defeating the entire point of this design.

**The fix:** `seed_if_empty` changes to look up the canonical ISO
framework by `catalog_key="iso27001-2022-sample"` and build its `by_code`
requirement-lookup dict from that framework's existing (already
reconciled) requirements, instead of constructing its own `Framework`/
`FrameworkRequirement` rows. This is a real, necessary code change to
`app/seed.py`, not a no-op.

### 5.3 Fixing the demo-seed gate (review Finding 3, right-sized)

**The bug this closes:** the first draft proposed changing `seed_if_empty`'s
gate from `Framework.first() is not None` to `InternalControl.first() is
not None`. `InternalControl` rows are user-deletable (`CONTROLS_REGISTER_
CONFIG` sets no `deletable=False`, and `RegisterConfig.deletable` defaults
`True`) — an org that deletes all 4 demo controls before adding real ones
would see them silently reinjected on the next restart. Separately, and
more urgently: leaving the gate as `Framework.first()` unmodified would
mean `seed_if_empty` **never runs at all**, on any install, once
`reconcile_system_catalogs` runs first and Framework is no longer ever
empty — demo data would stop being seeded entirely, a regression from
today's behavior.

**The fix, simpler than the prior design's new `DemoSeedState` table +
config-flag pair:** since this issue does not need to change *whether*
demo data is seeded by default (that's a bigger, separate product
decision the prior design's `GRC_SEED_DEMO_DATA` opt-in flag makes, out
of scope here — see §7), the gate only needs to become *accurate*, not
*reconfigurable*. `seed_if_empty` already writes
`record_audit_event(entity_type="control", action="seed", actor="system")`
for each demo control it creates (existing code, unchanged). Gating on
**that specific event's existence**
(`session.query(AuditEvent).filter_by(entity_type="control",
action="seed").first() is not None`) is immune to later control deletion
(an `AuditEvent` is never deleted by any route in this app) and immune to
`Framework` no longer ever being empty — reusing existing, already-written
history as the marker rather than introducing a new table for a
distinction (dataset versioning) this issue has no present need for.

### 5.4 Migration/backfill safety (review Finding 4)

Schema migration adds `catalog_key`/`is_primary` via `op.add_column` +
`op.batch_alter_table`'s `create_unique_constraint` (matching this repo's
existing precedent for adding a constraint to an existing table —
`8961da81a764_add_user_status_and_google_subject.py`'s shape — rather than
an inline inlined `unique=True` on `op.add_column`, which the reviewed
version of the migration must not do).

The backfill **must count matches before acting**, not blindly `UPDATE ...
WHERE name = ... AND version = ...`:

```python
candidates = conn.execute(
    sa.select(frameworks.c.id).where(
        frameworks.c.name == "ISO/IEC 27001:2022 Annex A (sample catalogue)",
        frameworks.c.version == "2022",
    )
).fetchall()
if len(candidates) == 1:
    conn.execute(
        sa.update(frameworks)
        .where(frameworks.c.id == candidates[0].id)
        .values(catalog_key="iso27001-2022-sample")
    )
# 0 matches: nothing to backfill, reconciliation creates a fresh
#            catalog_key-tagged row on next startup — safe, no-op.
# >1 matches: ambiguous: which row "is" the system catalog can't be
#             determined from a name/version match alone. Leave every
#             candidate's catalog_key NULL and log a warning naming all
#             matched ids — reconciliation then creates its own fresh
#             catalog_key-tagged row alongside the pre-existing,
#             unidentified one(s). This produces a visible duplicate in
#             this specific, narrow edge case, but it is a safe,
#             non-destructive outcome (no data loss, no incorrect merge,
#             no fabricated identity) per `.agent/RULES.md` §4's "fail
#             safely on ambiguity" — not silently resolved by a guess.
```

This is a smaller commitment than the prior design's full `CatalogConflict`
table + admin-acknowledgment workflow for the same ambiguity (§7) — a
logged warning is sufficient for #12's own acceptance criteria ("no
destructive migration"), which this satisfies; a persistent, actionable,
admin-facing conflict record is a real improvement but not a requirement
#12 states, and is deferred rather than built here.

## 6. Product-authored mapping disclaimer

The seeded SOC 2 catalog gets an `is_placeholder_content=True` framework,
identical disclaimer pattern to ISO's. Any shipped SOC2↔ISO crosswalk
content (not required by #12's minimum scope — none is built here) must
carry an explicit "product interpretation, not official AICPA/ISO
equivalence" note wherever displayed if a future issue adds one.

## 7. What's adopted vs. explicitly deferred, and why

| From the prior (`dev-soc2-2`) design | Adopted here? | Reasoning |
|---|---|---|
| Concurrency-safe reconciliation | **Adopted, right-sized** (§5.1) — savepoint+retry matching `app/events.py`'s existing pattern, not a new advisory-lock primitive | Required by #12's own "idempotent and additive-safe" criterion; the lighter mechanism already meets the actual bar (prevents crash) without introducing a locking primitive unused anywhere else in this codebase |
| Legacy-ISO ambiguity handling | **Adopted, right-sized** (§5.4) — count-then-backfill-or-warn, not a persistent `CatalogConflict` table + admin acknowledgment UI | Required by #12's "no destructive migration" criterion; a logged warning satisfies "fail safely," a full conflict-management subsystem is a larger, separate feature #12 doesn't ask for |
| Demo-seed decoupling from reconciliation | **Adopted, right-sized** (§5.3) — reuse existing `AuditEvent` history as the marker, no new table/config flag | Required simply to keep demo seeding working at all once Framework is never empty; doesn't need dataset-versioning or an opt-in/opt-out product decision this issue isn't making |
| `FrameworkCategory`/TSC scoping model + composite-FK ownership | **Deferred** | Not in #12's acceptance criteria; a real, legitimate future feature (SOC 2 category selection is a genuine product need) but its own design-sized addition — building it here would be exactly the scope-expansion `.agent/RULES.md` §12 asks to surface, not silently absorb |
| `CatalogConflict` table + admin acknowledgment UI | **Deferred** (replaced by §5.4's logged-warning fallback) | Same reasoning — real value, not required by #12, sized as its own future slice |
| Admin-gating framework create/edit routes | **Deferred, documented as a known limitation** (§4) | Would be an authorization behavior change affecting every framework, not just system ones — #12 doesn't ask for this and its own non-goals rule out broad changes; worth a future issue if it becomes a real product complaint, not decided as a side effect here |
| `catalog_status` (`seed_incomplete`/`ready`/`conflict`) readiness gating | **Deferred** | ISO's equally-partial placeholder catalog has shipped and computed real percentages since the MVP with no such gate; SOC 2 gets the same treatment for consistency rather than a new status concept invented for one framework |
| Register-grid per-row canonical-field lockdown | **Deferred** (no gap exists in this design's smaller scope — this issue doesn't add any generic-CRUD-exposed canonical fields) | N/A to a design that doesn't admin-gate/lock down framework rows in the first place |

**Note for whoever picks up the deferred items:** read
`docs/superpowers/specs/2026-07-21-feature14-multiframework-foundation-design.md`
on `origin/yangjeep/dev-soc2-2` first — it already solved these problems in
detail, twice-reviewed, before implementing them again from scratch. It
predates #21/#22/#40/#11 and will need re-reconciling against current
`main` (event-store availability, `ControlOccurrence`'s existence, this
issue's `catalog_key`/`is_primary` columns) before use, the same way this
document reconciled #12 against #22/#11 — but the mechanisms themselves
(concurrency, legacy-conflict decision table, category composite-FK,
mutability matrix) remain sound starting points, not something to
re-derive.

## 8. UI/route changes (minimal, per #12's own non-goals: "No broad UI redesign")

- `GET /frameworks` and the Dashboard's framework list: order by
  `is_primary DESC, name ASC` instead of `name` alone.
- A small "Primary" badge next to a primary framework's name on both
  pages — reuses the existing `<span class="badge ...">` pattern.
- No new routes for `is_primary`/`catalog_key` in this issue.

## 9. Non-goals reaffirmed from #12's own text

No full SOC 2 catalog beyond the Security/CC-series placeholder above; no
evidence/testing/findings lifecycle; no AI/BYOK; no SOC2-specific tables
(everything reuses `Framework`/`FrameworkRequirement`); no onboarding
wizard (#30); no readiness landing page (#33); no category/TSC scoping UI,
conflict-resolution UI, or framework-route authorization changes (§7).

## 10. Test strategy

- `reconcile_system_catalogs` idempotent under sequential re-runs (calling
  twice creates no duplicate frameworks/requirements).
- Concurrent creation race: two sessions/threads both attempt
  `_find_or_create_catalog_framework` for the same catalog_key against the
  same (SQLite, in-process-threaded) database; assert exactly one
  `Framework` row exists afterward and neither call raises.
- Clean database: one reconciliation pass creates both ISO and SOC 2 with
  correct `catalog_key`s and requirement counts.
- Existing ISO-only database (simulating a pre-#12 install, constructed
  with the exact old `seed_if_empty` shape): after the migration backfill,
  reconciliation recognizes the existing row by `catalog_key` and does not
  create a duplicate; SOC 2 is still added alongside it.
- Migration backfill ambiguity: construct 0, 1, and 2 pre-existing rows
  matching the literal name/version; assert 0 → no backfill, no error; 1 →
  backfilled correctly; 2 → neither backfilled, warning logged, no
  exception raised.
- `seed_if_empty` after `reconcile_system_catalogs` maps demo controls to
  the *canonical* ISO framework's requirements, not a duplicate.
- `seed_if_empty`'s revised gate: does not re-run after demo controls are
  deleted (the `AuditEvent` marker persists); does not run a second time
  on a second startup call.
- A demo control (from `app/seed.py`) is mapped to both an ISO requirement
  and a SOC 2 requirement — the actual "shared control across frameworks"
  demonstration the GitHub issue's own verification step asks for, not
  only a synthetic unit test.
- `Framework.is_active = False` on the SOC 2 framework does not delete its
  requirements, mappings, or any `ControlOccurrence`/evidence reachable
  through those mappings' controls.
- Migration: SQLite + Postgres, `upgrade head` → `downgrade` → `upgrade
  head`.
- Full existing framework/requirement/CSV-import/assessment regression
  suite stays green.

## 11. Risks, open questions, known limitations

- **No `is_primary` singleton enforcement** — deliberate, matches this
  repo's existing minimal-invariant posture elsewhere.
- **No admin UI to change which framework is primary** — seeded default
  only for this issue.
- **Any logged-in user can still rename/deactivate the SOC 2 system
  framework** — pre-existing behavior for every framework, not changed by
  this design; see §7 for why fixing this is deferred rather than bundled
  in here.
- **SOC 2 Security-category-only placeholder** — the other four
  categories are deliberately deferred to manual-add/CSV-import.
- **The `catalog_key` backfill only recognizes exactly one pre-existing
  match.** A renamed or duplicated legacy ISO row produces a visible,
  non-destructive duplicate rather than a guessed merge — see §5.4.
- **Substantial, more complete prior design work exists unshipped** (§1.3,
  §7) for the category-scoping/conflict-management/authorization concerns
  this issue defers — flagged prominently so it's found and reconciled
  properly next time, not re-discovered by accident or re-derived from
  scratch.

## 12. Post-implementation adversarial code review (2026-08-17)

An independent review agent (fresh context, full read of this doc) was
dispatched after implementation and a full green test run. It confirmed
the concurrency-safety mechanism (§5.1) is genuinely correct under test
(verified experimentally: `session.begin_nested()` flushes pre-existing
pending objects *before* issuing the SAVEPOINT, so an unrelated earlier
pending object is never at risk from a later savepoint's rollback; the
race-losing object cleanly reverts to `transient` state; `add_requirement`'s
internal flush happens before `RequirementAssessment` is ever created, so
a race loss cannot orphan one), confirmed the migration's 0/1/2-match
backfill logic correct on SQLite by independently reproducing all three
branches, confirmed no authorization/security issue, confirmed no scope
creep against §7's adopt/defer list, and confirmed `ControlRequirementMapping`'s
framework-agnostic claim. Four things were fixed as a result:

1. **(High)** `_find_or_create_catalog_framework`/`_find_or_create_catalog_requirement`'s
   `except IntegrityError` handlers didn't actually match the
   discriminate-before-recover shape they claimed to reuse from
   `app/events.py` — any `IntegrityError` (not just the intended unique-key
   collision) was silently treated as "a concurrent process won." Fixed:
   both now re-fetch by the intended key and `raise RuntimeError(...) from
   exc` if the row genuinely isn't there, matching `app/events.py`'s actual
   pattern instead of only its docstring description.
2. **(Medium)** Two new bare `assert`s (`app/seed.py`, and the framework
   path before the fix above) — silently stripped under `python -O`,
   unlike every other invariant-guard in this codebase, which raises.
   Replaced with `RuntimeError`.
3. **(Medium-High)** The design's own §10 test strategy committed to
   migration backfill and `is_active=False` non-destructiveness tests that
   weren't actually written. Added: `tests/test_framework_catalog_migration.py`
   (0/1/2-match backfill, unique-constraint enforcement, upgrade→downgrade→upgrade
   round trip, all via a programmatic `alembic.config.Config` rather than
   the bare CLI — see that file's docstring for why) and
   `test_deactivating_framework_does_not_delete_requirements_mappings_or_occurrences`
   in `tests/test_framework_catalog.py`.
4. **(Medium, flagged as a suspicion not a confirmed violation)** Several
   SOC 2 placeholder summaries (CC1.1, CC2.1, CC3.1) read as minimal
   word-swaps of the widely-quoted public COSO/AICPA criteria sentences
   ("entity"→"organization") rather than genuine structural paraphrases,
   unlike the ISO catalog's summaries. Rewritten (all nine, not only the
   three flagged, for a consistent safety margin) to describe each
   CC-series' actual subject matter in this repository's own words rather
   than lightly-reworded criterion sentences.
