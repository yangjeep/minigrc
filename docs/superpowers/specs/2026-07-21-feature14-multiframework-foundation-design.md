# Feature 14 (slice 1/4): Multi-framework foundation — ISO 27001 + SOC 2

## Context

Feature 14 asks for full ISO 27001 + SOC 2 multi-framework support:
framework selection, unified controls, cross-framework mapping, framework-
specific assessment views, evidence reuse UI, migration/compat, and a large
e2e test matrix. That's several independent subsystems, so this spec covers
only the first, unblocking slice: **the ability for a tenant to have SOC 2
exist as data, enable/disable either framework, and scope SOC 2 to specific
Trust Services Categories (TSCs).** No UI labeling sweep, no cross-framework
requirement-mapping catalog, and no framework-specific readiness calculation
are in this slice — those are separate specs that depend on this one.

Existing schema already generalizes further than expected: `Framework`,
`FrameworkRequirement`, `InternalControl`, and `ControlRequirementMapping`
have no framework-specific FK or single-framework assumption anywhere — a
control can already map to requirements across multiple frameworks with
zero schema change. `Framework.is_active` already means "hide from active
workflows, keep data" per its existing docstring, which is exactly the
enable/disable semantics Feature 14 item 1 asks for.

## Scope of this slice

1. `Framework.code` — stable machine-readable slug.
2. `FrameworkRequirement.category` — generic category field (Annex A theme
   for ISO, TSC name for SOC2).
3. `FrameworkScope` — new table for category-level (TSC) in/out-of-scope
   toggle state.
4. SOC 2 seeded as a second `Framework`, disabled by default, with a
   handful of representative placeholder requirements per TSC.
5. Admin "Settings" page: enable/disable each framework (reusing
   `is_active`), and (when SOC2 is active) toggle its non-Security TSCs.
6. Migration: existing ISO-only installs get `code='iso27001'` backfilled,
   remain enabled, unaffected otherwise. SOC2 stays disabled until an admin
   opts in.

Out of scope for this slice (future specs): cross-framework requirement-to-
requirement mapping catalog; framework badges/filters across control
register, control details, checklist, assessments, evidence, CSV
import/export, reports, dashboards, search; framework-specific readiness
calculation; evidence-reuse UI; e2e browser test matrix (item 10 in the
parent feature).

## Data model

### `Framework.code` (new column)

```python
code: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
```

Set at seed time (`"iso27001"`, `"soc2"`). Nullable at the DB level (no
generic "add any framework" UI exists yet, so nothing else creates a
`Framework` row without a code in practice), but every framework this app
ships/seeds has one. App logic that needs to know "is this SOC2" or "what's
the default enabled framework" matches on `code`, never on `name`/`version`
text (which an admin might reasonably want to edit later without breaking
app logic).

### `FrameworkRequirement.category` (new column)

```python
category: Mapped[str] = mapped_column(String(64), default="")
```

`app/requirements.py::add_requirement` gets a `category: str = ""` keyword
argument, threaded through to the new column. Existing three call sites
(seed, manual add route, CSV import) are unaffected unless they choose to
pass it. Migration backfills the 5 existing seeded ISO placeholder rows
with their real Annex A theme (Organizational/People/Physical/
Technological) so category filtering has real data to exercise on both
frameworks from day one.

### `FrameworkScope` (new table)

```python
class FrameworkScope(Base):
    __tablename__ = "framework_scopes"
    __table_args__ = (
        UniqueConstraint("framework_id", "category", name="uq_framework_scope_category"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    framework_id: Mapped[str] = mapped_column(ForeignKey("frameworks.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    in_scope: Mapped[bool] = mapped_column(default=True)
    is_default: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    framework: Mapped[Framework] = relationship()
```

One row per (framework, category) that the org has a scope decision for.
Generic — not SOC2-specific — so a later slice could seed rows for ISO
Annex A themes too if theme-level scoping is ever wanted there. For this
slice, only SOC2 gets rows: `security` (`in_scope=True, is_default=True`),
`availability`, `processing_integrity`, `confidentiality`, `privacy` (all
`in_scope=False, is_default=False`).

**Security is not toggleable.** The admin route rejects an attempt to
change `in_scope` on the `security` category row for any framework whose
`code == "soc2"` (real SOC2 practice: the Common Criteria/Security section
is mandatory in every SOC2 report). The checkbox is rendered disabled
server-side (not just client-side) — the route itself re-checks and 400s
on a tampered POST.

## Seed data (`app/seed.py`)

- ISO framework: add `code="iso27001"`. Backfill each of its 5 requirement
  seed tuples with a `category` (Organizational/People/Physical/
  Technological per the real Annex A theme that reference code belongs to).
- New SOC2 `Framework`: `code="soc2"`, `is_active=False`,
  `is_placeholder_content=True`, description explicitly framing SOC 2 as
  "an audit/reporting framework, not a certification" (mirrors the ISO
  framework's existing non-certification framing).
- SOC2 requirements — placeholder reference codes only, paraphrased
  titles/summaries, same discipline as ISO:
  - Security: `CC6.1` (Logical access controls), `CC7.1` (System monitoring)
  - Availability: `A1.1` (Capacity and availability monitoring)
  - Processing Integrity: `PI1.1` (Processing accuracy and completeness)
  - Confidentiality: `C1.1` (Confidential information identification and protection)
  - Privacy: `P1.1` (Notice and communication of privacy practices)
- 5 `FrameworkScope` rows for the SOC2 framework as specified above.
- `AuditEvent` rows for the SOC2 framework/requirement seed, matching the
  existing pattern for ISO.

## Copyright boundary (`docs/domain/domain-model.md`)

Add a paragraph mirroring the existing ISO/IEC one: AICPA's Trust Services
Criteria normative text is copyrighted and not reproduced here; seeded SOC2
titles/summaries are placeholders authored for this repository or
user-supplied after the organization licenses its own copy. This app does
not perform or imply a SOC 2 audit opinion — only an accredited CPA firm
can issue one.

## Admin route: `app/routers/admin_settings.py`

New router module (one file per admin sub-area, per existing convention).
Registered nav item: `("Settings", "/admin/settings")` added to
`ADMIN_NAV_ITEMS` in `app/main.py` — a slot already anticipated in that
list's comment.

- `GET /admin/settings` — `require_admin`-gated. Renders both frameworks
  with an enable/disable control, and (only for the SOC2 framework, and
  only when it's active) its 5 `FrameworkScope` rows as checkboxes with
  Security shown checked and disabled.
- `POST /admin/settings/frameworks/{framework_id}/toggle` — `require_admin`
  + `verify_csrf`. Flips `Framework.is_active`. Rejected with a flash error
  (no mutation) if this would disable the last remaining active framework.
  Writes an `AuditEvent` (`entity_type="framework"`, action
  `"enabled"`/`"disabled"`).
- `POST /admin/settings/scope/{scope_id}/toggle` — `require_admin` +
  `verify_csrf`. Flips `FrameworkScope.in_scope`, stamps `updated_by_user_id`.
  Rejected (400) if the target row's `category == "security"` and its
  framework's `code == "soc2"`. Writes an `AuditEvent`
  (`entity_type="framework_scope"`).

## Migration (Alembic)

Single revision:
1. Add `frameworks.code` (nullable, unique index).
2. Add `framework_requirements.category` (not null, server default `''`).
3. Create `framework_scopes` table.
4. Data migration: `UPDATE frameworks SET code = 'iso27001' WHERE code IS NULL`
   — safe because every pre-existing installation seeded exactly one
   framework (the ISO placeholder catalogue) before this feature existed.
   No backfill of `category` values in the migration itself (existing rows
   get `''` from the column default); the seed-data backfill above only
   affects fresh seeds, not already-migrated installs — an already-running
   install's existing 5 ISO placeholder rows are left with `category=''`
   rather than rewritten by a migration guessing at content matches. This
   is a deliberate compromise documented in the worklog: acceptable because
   this slice doesn't yet have any UI that depends on category being
   populated for already-installed data.

## Tests

- `tests/test_admin_settings.py` (new): non-admin gets 403 on both GET and
  POST routes; admin can view the page; toggling the only active framework
  off is rejected with no DB change and no audit event; toggling SOC2 on
  works and its scope checkboxes appear; toggling a non-Security TSC on/off
  works and writes an audit event; toggling Security is rejected with no
  DB change even via a raw POST bypassing the disabled checkbox.
- `tests/test_requirements.py`: extend to cover `add_requirement(...,
  category=...)` persists correctly; existing calls without `category`
  still default to `""`.
- `tests/test_pages.py`: add `/admin/settings` to the routes-render sweep.
- Migration test: apply migration against a pre-feature-shaped SQLite file
  (or exercise via `alembic upgrade head` from base in the existing
  migration test pattern) and assert `code='iso27001'` lands on the one
  pre-existing framework row, and it stays `is_active=True`.
- `app/seed.py` idempotency test (extend existing coverage): re-running
  `seed_if_empty` a second time is still a no-op; SOC2 framework exists
  with `is_active=False` after first seed.

## Explicitly deferred

- Cross-framework requirement↔requirement mapping catalog (confidence,
  system-vs-user-provided, notes) — next spec.
- Framework badges/filters in control register, control details,
  checklist, assessments, evidence, CSV import/export, reports, dashboards,
  search — next spec, depends on `category`/`code` existing.
- Framework-specific readiness/assessment views and computation — next
  spec, depends on the mapping catalog.
- Evidence reuse UI clarity — next spec.
- Full e2e browser scenario matrix (parent feature item 10) — spread
  across the specs above as each becomes testable end-to-end.
