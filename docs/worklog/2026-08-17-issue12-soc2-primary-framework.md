# Issue #12: SOC 2 as the primary framework experience, ISO 27001 preserved

**Date:** 2026-08-17
**Author:** Claude (agent)
**Type:** feat

## Summary

Adds SOC 2 as a second system framework catalog (Trust Services Criteria —
Security category only, placeholder content) alongside the existing ISO
27001 placeholder, made the default/primary experience for a fresh
deployment, while preserving ISO 27001 and the existing shared-control
architecture unchanged. Reconciles #12's execution prompt against actual
repository state: no "Feature 14" catalog-reconciliation subsystem exists
on `main`, and a much larger, twice-reviewed but never-shipped design for
this exact problem was found on `origin/yangjeep/dev-soc2-2` — this
implementation adopts the specific mechanisms it needs (concurrency-safe
reconciliation, safe legacy-framework identification, a demo-seed gate
that survives reconciliation always running) right-sized to #12's actual
acceptance criteria, and explicitly defers the larger category-scoping/
conflict-admin-UI/authorization-change scope that design also covers.

## Files Changed

- `app/models.py` — `Framework.catalog_key` (nullable, unique — identifies
  a system-catalog row independent of user-editable name/version) and
  `Framework.is_primary` (plain boolean, no enforced singleton).
- `app/framework_catalog.py` (new) — `SYSTEM_CATALOGS` (ISO + SOC 2
  placeholder definitions), `reconcile_system_catalogs` (idempotent,
  additive-only, concurrency-safe via savepoint-and-recover matching
  `app/events.py`'s pattern).
- `migrations/versions/a248573ccdfe_...` (new) — additive columns +
  unique constraint; count-checked backfill of the pre-existing seeded ISO
  framework's `catalog_key` (0 matches: no-op; 1: backfilled; >1:
  untouched + logged warning, never guessed).
- `app/seed.py` (rewritten) — demo-seed gate changed from
  `Framework`/`InternalControl` emptiness (both unsafe post-#12, see Bugs
  Found) to an `AuditEvent(entity_type="control", action="seed")` marker;
  looks up the canonical ISO/SOC 2 frameworks by `catalog_key` instead of
  creating its own; one demo control now mapped to both an ISO and a SOC 2
  requirement.
- `app/main.py` — wires `reconcile_system_catalogs` before `seed_if_empty`.
- `app/routers/frameworks.py`, `app/routers/dashboard.py` — list ordering
  `is_primary DESC, name`.
- `app/templates/frameworks/list.html`, `app/templates/dashboard.html`,
  `app/static/style.css` — "Primary" badge.
- `docs/architecture.md`, `docs/domain/domain-model.md` — new sections.
- `tests/test_framework_catalog.py`, `tests/test_framework_catalog_migration.py`
  (new); `tests/test_db_init.py`, `tests/test_postgres_compat.py` (updated).
- `docs/superpowers/specs/2026-08-17-issue12-soc2-primary-framework-design.md`
  (new) — full design, §1 repository-reality correction, §7 adopt/defer
  decision, §12 post-implementation review.

## Verification

- [x] Unit/regression tests: 20 new — `test_framework_catalog.py` (11:
      reconciliation idempotency, concurrency-race recovery, seed-gate
      safety, shared-control demonstration, `is_active=False`
      non-destructiveness) + `test_framework_catalog_migration.py` (5:
      0/1/2-match backfill, unique-constraint enforcement, migration
      round trip), plus 1 existing test updated
      (`test_app_startup_seeds_example_dataset`, now correctly expects 2
      frameworks).
- [x] Full suite: 507 passed, 1 skipped (Postgres-gated) on the first
      complete run after implementation; 513 passed, 1 skipped after the
      adversarial review's fixes (6 new tests added during that round).
- [x] `ruff check .` / `ruff format --check .` clean.
- [x] SQLite migration verified independently: `upgrade head` →
      `downgrade -1` → `upgrade head`, and all three backfill branches
      (0/1/2 matches), via a programmatic `alembic.config.Config`
      (discovered mid-session that bare `alembic` CLI commands ignore
      `GRC_DATABASE_PATH`/`GRC_DATA_DIR` — see
      `tests/test_framework_catalog_migration.py`'s docstring).
- [ ] PostgreSQL: `test_postgres_compat.py` extended with the new
      columns/reconciliation round trip; not run locally (no Postgres in
      this sandbox) — relies on CI's `test-postgres` job, same standing
      convention as every other assertion in that function.
- [x] Independent adversarial design review, then independent adversarial
      code review (fresh context each) — see Decisions below for what
      each found and how it was addressed.

## Decisions & Alternatives Rejected

- **Found real, unshipped prior design work mid-implementation**
  (`docs/superpowers/specs/2026-07-21-feature14-multiframework-foundation-design.md`
  on `origin/yangjeep/dev-soc2-2`) that solves this same problem in far
  more depth (Trust-Services-Category scoping, a `CatalogConflict`
  admin-acknowledgment workflow, admin-gating every framework route,
  catalog-completeness gating). No GitHub issue tracks that scope. Adopted
  only the mechanisms #12's own acceptance criteria actually require
  (concurrency safety, legacy-framework safety, demo-seed decoupling),
  right-sized using patterns already established in this codebase; the
  rest is documented as deliberately deferred, not silently dropped or
  silently built unasked — see design doc §1.3/§7.
- **First design draft had a real seed-duplication bug**: claimed
  `seed_if_empty` needed no change beyond its gate, but it actually
  constructs its own `Framework`/`FrameworkRequirement` rows unconditionally
  — left unfixed, every fresh install would get two indistinguishable ISO
  frameworks. Fixed: `seed_if_empty` now looks up the canonical,
  `catalog_key`-tagged framework instead of creating its own.
- **Demo-seed gate correctness**: neither `Framework` emptiness (never
  empty once reconciliation always runs — `seed_if_empty` would stop
  running at all) nor `InternalControl` emptiness (user-deletable — would
  silently reinject demo data after a deliberate deletion) is a safe
  one-shot marker. Fixed: gates on the existing, non-deletable
  `AuditEvent(entity_type="control", action="seed")` marker `seed_if_empty`
  already wrote, rather than introducing a new table/config flag for a
  distinction (dataset versioning) #12 doesn't need.
- **Migration backfill must count matches before acting**: a naive
  `UPDATE ... WHERE name = ... AND version = ...` could silently mismatch
  or fail unpredictably on ambiguity. Fixed: explicit 0/1/>1 count check,
  backfilling only on exactly one match, logging (never guessing) on
  ambiguity.
- **Adversarial code review found the concurrency-safety exception
  handling didn't match the pattern it claimed to reuse** — any
  `IntegrityError`, not just the intended unique-key collision, was
  treated as "a concurrent process won." Fixed to re-verify the actual
  cause (re-fetch by the intended key, `raise ... from exc` if genuinely
  absent) before recovering, matching `app/events.py`'s real
  discriminate-before-recover shape. Same review independently verified
  (by experiment, not just reading) that the underlying savepoint
  mechanics are correct: a losing insert's rollback cannot corrupt an
  earlier pending object, and a race loss cannot orphan a
  `RequirementAssessment`.
- **Adversarial review flagged three SOC 2 placeholder summaries as too
  close to public COSO/AICPA criteria wording** (minimal word-swaps
  rather than genuine paraphrase, unlike the ISO catalog's summaries).
  Rewrote all nine (not only the three flagged) for consistent structural
  distance, describing each CC-series' actual subject matter in this
  repository's own words.
- **Two bare `assert`s were flagged as the wrong tool** for a reachable
  invariant (silently stripped under `python -O`, inconsistent with every
  other invariant guard in this codebase). Replaced with `RuntimeError`.

## Bug found in CI (post-push)

**Symptom:** PR #54's `test` CI job failed — `alembic.util.exc.CommandError:
Path doesn't exist: /home/yangjeep/orca/workspaces/minigrc/epic/migrations`
— while the full suite had passed locally (513/513) immediately before
pushing, and `test-postgres`/`docker` both passed on the same commit.

**Root cause:** `tests/test_framework_catalog_migration.py` hardcoded
`PROJECT_ROOT = "/home/yangjeep/orca/workspaces/minigrc/epic"` — this
session's own sandbox path. It happened to be correct in every local run
(same sandbox), so nothing caught it before CI, whose checkout lives at a
different path.

**Fix:** `PROJECT_ROOT = Path(__file__).resolve().parent.parent`, matching
`app/db.py`'s own existing convention for the same value, instead of a
literal string. Grepped the full diff for any other `/home/yangjeep`
occurrence — none found.

**Verification:** reran `tests/test_framework_catalog_migration.py`
locally (5/5 pass) and `ruff check`/`format --check` (clean) after the
fix; pushed as a follow-up commit. CI on the new commit (eab2a1d): all
four checks green (docker, test, test-postgres, GitGuardian) — confirms
both the path fix and the Postgres-specific reconciliation/backfill
coverage added for #12.

## Known Gaps / Follow-ups

- PostgreSQL verification for this issue's schema/reconciliation has not
  been directly observed passing in this session (no local Postgres) —
  confirm the `test-postgres` CI job goes green on the pushed branch.
- No admin UI to change which framework is primary, no admin-gating of
  framework routes, no Trust-Services-Category scoping model, no
  persistent conflict-resolution UI for an ambiguous legacy-ISO backfill —
  all deliberately deferred; see design doc §7 for the full adopt/defer
  table and §1.3 for the prior design that should be read first before
  picking any of these up.
- SOC 2's placeholder catalog covers only the mandatory Security category
  (9 representative reference codes); the four optional categories
  (Availability, Processing Integrity, Confidentiality, Privacy) are not
  seeded — an organization adds them via the existing manual-add/CSV-import
  paths once it scopes them in.
