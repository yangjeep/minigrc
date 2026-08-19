# Issue #30: Compliance scoping, SOC 2 program bootstrap, and guided onboarding

Status: implemented. See docs/worklog/2026-08-19-issue30-compliance-scope-onboarding.md
for the full verification account.

## 1. Repository-reality check

Before designing, the actual current state was inspected directly (not assumed from the PRD):

- **No scope/program model exists at all.** Genuinely greenfield — no `Organization`,
  `ComplianceScope`, `Program`, or similar table anywhere.
- `Framework.is_primary` (added by #12) already lets one framework be "the" primary
  experience, with no enforced singleton/toggle route. `reconcile_system_catalogs`
  (`app/framework_catalog.py`) unconditionally seeds a SOC 2 sample catalog with
  `is_primary=True` and an ISO 27001 sample catalog with `is_primary=False` on every
  startup — **so "a primary framework exists" is already true for every fresh
  deployment before any onboarding runs.** Onboarding does not need a "pick your
  framework" step; SOC 2-first is already the out-of-the-box default per PRD §4.1.
- The seeded SOC 2 catalog covers **only** the mandatory Security category (CC1.1-CC9.1,
  one representative code per series) — its own docstring says the four optional
  categories (Availability, Confidentiality, Processing Integrity, Privacy) are
  deliberately not seeded, to be "added via CSV import or the requirement form once
  your organization scopes them in." **`FrameworkRequirement` has no Trust Services
  Category column** — nothing tags which TSC a requirement belongs to.
- `ControlPeriod` is a **per-control-scoped** time window that gates `ControlOccurrence`
  generation (see its own docstring, `app/models.py:206-217`) — it is explicitly not an
  org-wide "audit/operating period." No org-wide operating-period concept exists today.
- `Person` and `VendorSystem` are real registers (identity/SaaS-vendor data respectively).
  No `Asset`/`Environment`/`Repository` model exists — grepped, zero matches.
- `TrustCenterSettings` / `GoogleOidcSettings` are the established one-row-singleton
  pattern (`app/trust_center.py::get_or_create_settings`: `select(...).limit(1)`,
  create-if-missing, no DB-level uniqueness constraint — acceptable at this app's
  concurrency profile for admin-configured, rarely-written singletons).
- `app/seed.py::seed_if_empty` creates 4 demo `InternalControl` rows (mapped to 6 of
  the 14 seeded sample requirements) + 2 demo `Risk` rows, unconditionally on every
  fresh deployment, gated only by a one-time `AuditEvent` marker — **not** a real
  onboarding flow (fixed content, no scope-awareness, no per-organization review step).
  It runs before this feature and is out of this issue's scope to change (RULES.md §11
  — keep diffs focused); this design accounts for its output already existing rather
  than assuming a truly empty `internal_controls` table.
- `app/routers/dashboard.py` computes readiness purely from
  applicable-vs-implemented `FrameworkRequirement`s, open risk counts, and policy
  due dates — it has no scope concept and no onboarding nudge today.
- `require_login`/`require_write_access`/`verify_csrf` (from `app/deps.py`) are the
  established RBAC pattern (router-level `require_login`, per-mutation-route
  `require_write_access` + `verify_csrf`).
- `app/events.py`'s `append_event`/`append_and_project`/`rebuild_projection` are
  unchanged since #31/#32; existing `aggregate_type` strings are lowercase-snake
  singular (`"policy"`, `"control_occurrence"`, `"evidence_artifact_version"`).

No prior design/worklog for #30, #14, or #33 exists — this is not reconciling against
an earlier partial attempt.

## 2. Scope decisions (where this design deliberately narrows or reshapes the issue wording)

The issue and PRD §5.1/§5.9 describe a broad scope model and a linear 9-step onboarding
wizard. Implementing that literally would require inventing content and models the
repository has no other need for yet, which CLAUDE.md and RULES.md both warn against.
Concrete decisions, and why:

1. **No new "framework selection" onboarding step.** A primary framework already
   exists in every deployment (see §1). Onboarding surfaces which framework is primary
   (informational) rather than asking the user to choose one from scratch.
2. **Trust Services Category selection is recorded as declared scope intent, not used
   to filter starter-control generation.** No requirement carries a TSC tag today, and
   only the mandatory Security category has seeded content. Storing the org's selected
   categories is still valuable — it documents audit intent and is the explicit signal
   the seeded catalog's own docstring anticipates ("add them via CSV import... once
   your organization scopes them in"). Filtering generated controls by TSC would
   require inventing a new per-requirement category taxonomy with no second caller
   yet — deferred; noted as a known gap below, not silently dropped.
3. **No new "systems/assets/environments" or "repository/cloud environment" model.**
   The issue's own execution prompt requires integrating with existing People/Vendor
   data rather than duplicating it, and no `Asset`/`Environment` model exists to
   integrate with. A boolean `in_scope` column is added directly to the existing
   `Person` and `VendorSystem` tables instead of a new join table — a join table would
   imply multiple concurrent scopes, which contradicts the one-org-per-deployment MVP
   invariant (RULES.md §1.11). "Repositories/cloud environments" and "locations/data
   categories" are recorded as free-text fields on the scope record itself (informational,
   not modeled as new relational entities) rather than inventing register tables with
   no second use case yet.
4. **`in_scope` on Person/VendorSystem is an ordinary mutable register field, not a
   separately event-sourced fact.** Every existing edit route for both models already
   calls `record_audit_event` on update (`app/routers/people.py:90,123,170`,
   `app/routers/vendor_systems.py:213,301,395,427`) — adding one more editable field
   to an already-audited edit form is consistent with how every other mutable
   attribute on these registers works (`employment_status`, `lifecycle_status`, etc.).
   RULES.md §3 distinguishes an audit log from event-backed truth specifically for
   facts *whose historical occurrence is itself the compliance conclusion* (e.g. "this
   control was tested and passed"). Whether a given person/system is currently flagged
   in-scope is operational bookkeeping of the same kind as the other fields already on
   these rows, not a new class of fact — it does not get its own aggregate.
5. **The scope declaration itself (categories, audit period, service description,
   exclusions) IS event-sourced** — this is the one thing the issue explicitly calls
   out as needing to be historically defensible ("integrate with #21 event semantics"),
   and it is genuinely the kind of fact an auditor would ask "what did you declare as
   in-scope during period X." Modeled like `ControlOccurrence`: one long-lived
   aggregate (`aggregate_type="compliance_scope"`, one row, `aggregate_id` fixed at
   creation) whose current-state projection (`ComplianceScope`) is updated in place by
   each event, not like `EvidenceArtifactVersion`/`PolicyVersion` where every change
   creates a whole new browsable row. Nothing in this issue or the PRD asks users to
   browse/compare specific past scope revisions (that is #45's "auditor-facing
   historical timeline" — explicitly a separate future issue) — it only requires that
   the change history exist and be reconstructable from events, which an append-only
   log already satisfies without building point-in-time UI now.
6. **No starter *policy* content.** PRD §5.9 step 6 ("review required starter
   policies") has no existing content pipeline to draw on — `Policy`/`PolicyVersion`
   (#31) support the lifecycle but no placeholder policy text catalog exists anywhere
   in this repository, unlike frameworks which have a real (if sample) catalog.
   Fabricating placeholder policy documents here risks being mistaken for real
   guidance with no validated need. **Deferred, not implemented in this slice** —
   listed under Known deferred/untested paths.
7. **"Configure evidence connectors" and "assign owners" are informational/soft
   onboarding signals, not hard gates.** The PRD explicitly frames the whole flow as
   guided, not gated ("For an active program, it should show... prioritized next
   actions" — not "must complete step N before step N+1"). Every step reads real
   authoritative state and is independently actionable in any order; nothing blocks
   navigation.

## 3. Onboarding step derivation (must be facts, not new mutable progress state)

RULES.md §3 explicitly says not to event-source (or invent new persisted state for)
"transient" bookkeeping without a concrete compliance reason. A mutable
"current onboarding step" field would drift from reality (e.g. a user could complete
prerequisites through the ordinary controls/frameworks pages without ever touching
`/onboarding`). Every step below is therefore a pure read over existing authoritative
tables — nothing new is persisted to track "done-ness":

| Step | Complete when | Source |
|---|---|---|
| Define compliance scope | `ComplianceScope` row exists | new, this issue |
| Identify people/systems in scope | at least one `Person.in_scope` or `VendorSystem.in_scope` is true | new column, this issue |
| Generate starter controls | every `FrameworkRequirement` under the primary `Framework` has ≥1 `ControlRequirementMapping` | existing tables |
| Assign control owners | every `InternalControl` has a non-empty `owner` or `owner_person_id` | existing tables |
| Configure an evidence source | `settings.evidence_repository_configured` (#32) OR any `GoogleDriveConnection`/`AwsConnection`/`ExternalConnection` row exists | existing tables/config |
| Start an operating period | any `ControlPeriod.status == "active"` exists | existing table |

Each step also carries a direct link to the existing route that resolves it (PRD §5.8:
"direct links to resolve the underlying authoritative objects") — frameworks/people/
vendor-systems/controls/connections/control-periods pages already exist; onboarding
does not duplicate their forms except for the new scope-definition form itself.

## 4. Model

```python
TRUST_SERVICE_CATEGORIES = ("security", "availability", "confidentiality", "processing_integrity", "privacy")


class ComplianceScope(Base):
    __tablename__ = "compliance_scopes"
    id: str  # generated by the domain function, not default=new_id — matches the
    # event-sourced-aggregate-id convention (ControlOccurrence, #11)
    service_description: str = ""
    trust_service_categories: str = ""  # comma-separated subset of TRUST_SERVICE_CATEGORIES;
    # "security" always included (mandatory in every SOC 2 report)
    audit_period_starts_on: date | None
    audit_period_ends_on: date | None
    data_categories: str = ""
    locations: str = ""
    exclusions: str = ""
    exclusions_rationale: str = ""
    revision: int = 1  # bumped on every revise; mirrors aggregate_sequence for readability
    created_at: datetime
    updated_at: datetime
```

Person/VendorSystem gain one column each: `in_scope: Mapped[bool] = mapped_column(default=False)`.

Event types on `aggregate_type="compliance_scope"`, `aggregate_id=<the one ComplianceScope.id>`:
`ComplianceScopeDefined` (sequence 1, creates the row) and `ComplianceScopeRevised`
(sequence 2+, updates it in place) — payload is the full field set each time (not a
diff), matching #31/#32's precedent of self-contained event payloads that don't
require replaying prior events to interpret.

## 5. Starter-control generation

`generate_starter_controls_for_framework(session, framework, *, actor) -> list[InternalControl]`:
for every `FrameworkRequirement` under the given framework with zero existing
`ControlRequirementMapping` rows, create one placeholder `InternalControl`
(`status="not_started"`, empty `owner`, name/description clearly derived from the
requirement, e.g. `"Starter control for {reference_code}: {title}"`) plus the mapping.
Idempotent by construction — a requirement that already has any mapping (from the
demo seed, a CSV import, a prior onboarding run, or manual creation) is left alone, so
re-running the action after partial coverage only fills the remaining gap. Never sets
`status` to anything implying effectiveness (RULES.md §1.10/PRD §4.6 — AI/automation,
and by extension bootstrap tooling, must not fabricate an effectiveness conclusion).
`InternalControl` itself stays a plain mutable row here, matching #11's existing
choice not to event-source control *design* (only `ControlOccurrence` *performance*
facts are event-sourced) — this generator does not introduce a new precedent, it
reuses the exact plain-row + `record_audit_event` shape `reconcile_system_catalogs`/
`seed_if_empty` already use for the same kind of bootstrap action.

## 6. Routes

`app/routers/onboarding.py`, `require_login` at router level:

- `GET /onboarding` — checklist (table above) with links to resolve each gap, plus the
  current scope summary if defined.
- `GET /onboarding/scope` — view/edit form (create or revise, same form; label changes
  based on whether a scope already exists).
- `POST /onboarding/scope` (`require_write_access`, `verify_csrf`) — calls
  `define_or_revise_scope`; validates `trust_service_categories` against the allow-list
  server-side (never trusts client-supplied category strings) and that
  `audit_period_ends_on >= audit_period_starts_on` when both are given.
- `POST /onboarding/generate-starter-controls` (`require_write_access`, `verify_csrf`)
  — calls the generator against the primary framework; flashes how many controls were
  created (0 is a valid, non-error outcome — "already fully covered").

`Person`/`VendorSystem` edit forms/routes gain the `in_scope` checkbox — a field
addition to existing routes, not new routes; existing `record_audit_event` calls on
those edit routes already cover it.

`app/routers/dashboard.py` gains a small "N/6 onboarding steps complete — continue
setup" banner (computed via the same `compute_onboarding_steps` used by `/onboarding`)
when incomplete, linking to `/onboarding`. This is intentionally minimal — the full
readiness-landing-page rebuild is issue #33's job, not this one's.

## 7. Testing strategy

- Unit: `trust_service_categories` allow-list validation (rejects unknown values,
  always includes "security"), audit-period ordering validation, revise-vs-define
  branching, starter-control-generation idempotency (second call creates nothing),
  and that generation coexists correctly with `seed_if_empty`'s pre-existing partial
  coverage (i.e., only fills the actual gap, not a full re-seed).
- Integration: event append + projection update atomicity; `rebuild_compliance_scope_projection`
  reproduces identical state from the event log alone.
- Routes: RBAC (reader/auditor blocked from mutation), CSRF, scope define→revise
  round trip preserves history (old event payloads still readable), in_scope toggle on
  Person/VendorSystem edit forms.
- Migration: SQLite + PostgreSQL-gated, upgrade/downgrade/upgrade round trip, new
  columns/table created and dropped correctly.
- Headless UAT (required): fresh-deployment journey — define scope → mark a person
  in-scope → generate starter controls → assign an owner → start an operating period →
  confirm the dashboard onboarding banner reflects the change at each step.

## 8. Known deferred/untested paths (stated up front, not discovered after the fact)

- No starter *policy* content catalog (§2 point 6) — PRD §5.9 step 6 is not implemented.
- Trust Services Category selection does not filter starter-control generation (§2
  point 2) — no per-requirement category data exists to filter by.
- No "repositories/cloud environments" relational model — recorded as free text only.
- `seed_if_empty`'s demo content is unchanged and continues to run on every fresh
  deployment; this issue does not gate or remove it (out of this issue's scope).
