# MVP: auth, framework checklist, versioned policies, Alembic, Docker Compose

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** feat

## Summary

Turned the initial GRC/ISMS skeleton (PR #1) into a usable single-organization
MVP: local session authentication, a real framework checklist (requirement
assessments, append-only notes, CSV import), a versioned local policy
repository (PDF/DOCX upload with content validation), Alembic migrations
replacing `Base.metadata.create_all`, SQLite integrity hardening, Docker
Compose, a CI workflow, and a comprehensive test suite.

## Requested

Continue PR #1 on `feat/initial-grc-foundation`, implement the explicit MVP
scope from the task brief (auth, checklist, policies, migrations, Docker
Compose, tests, docs) under the existing KISS/no-speculative-abstraction
constraints, verify end to end, and update the PR — without waiting for
sign-off on routine technical decisions.

## Repository state found

The existing foundation (frameworks, requirements, internal controls,
control↔requirement mapping, risk register, audit log, placeholder pages)
was a working skeleton with no auth, no policy storage, no requirement
assessment/note model, and schema creation via `create_all`. 16 tests
passed; `ruff` was clean. See the prior worklog entry
(`2026-07-11-initial-grc-foundation.md`) for that baseline.

## Design decisions

Full rationale in `docs/decisions/architectural-decisions.md` (new
decisions #8–#11, appended, prior decisions preserved and marked
superseded where applicable). Highlights:

- **Auth:** local email/password (Argon2 via `pwdlib`), opaque
  `secrets.token_urlsafe` session tokens hashed (SHA-256) before storage,
  `HttpOnly`/`SameSite=Lax` cookie, server-side expiry + revocation, no
  JWTs, no hosted identity provider, no self-registration. First user via
  `python -m app.cli create-user`.
- **CSRF:** double-submit cookie, issued independent of the login session
  (covers the login form itself, not just authenticated forms).
- **Migrations:** Alembic, one initial migration capturing the full MVP
  schema. `app/db.py::init_db` runs `alembic upgrade head` programmatically
  against whichever database path the caller resolves — one schema-init
  path for dev, tests, and Docker.
- **Policy storage:** local, versioned files under `GRC_DATA_DIR/policies/`,
  not a Google-Drive-indexing model — see decision #11 for why versioned
  local storage better serves the audit-history requirement.
- **Requirement assessment model:** `RequirementAssessment` (one per
  requirement, created alongside it), `RequirementNote` (append-only).
  Completion % = implemented / applicable, with an explicit "N/A" (not a
  divide-by-zero) when no applicable requirements exist yet.
- **Flash messages:** encoded in the redirect query string
  (`app/flash.py`) rather than a session-backed flash store — this app
  already redirects after every POST, so this is the simplest mechanism
  that needs no additional storage.
- **SQLite correctness:** `PRAGMA foreign_keys=ON`, `busy_timeout=5000`,
  `journal_mode=WAL` on every connection; CHECK constraints for risk
  likelihood/impact bounds and non-blank titles; unique constraints for
  `(framework_id, reference_code)` and `(control_id, requirement_id)`.

## Bugs found and fixed along the way

- **`app/main.py` built a real `FastAPI` app (and touched the real
  `./data/grc.db`) merely on import**, via a module-level `app =
  create_app()`. Since `tests/conftest.py` imports `create_app` from this
  module, every test run was silently creating/touching the developer's
  real database directory. Fixed with a lazy `__getattr__` (PEP 562) that
  only builds the default app when something asks for `app.main.app` (as
  `uvicorn app.main:app` does), never on plain import.
- **Policy file storage always resolved to the real default `GRC_DATA_DIR`
  (`./data`)**, even in tests, because `create_app`'s `database_path`
  override wasn't reflected in `app.state.settings.data_dir`. Fixed by
  having `create_app` accept an optional `data_dir` too and derive one from
  `database_path` when only that's given, then store a `Settings` copy
  with both overridden — `tests/conftest.py`'s `tmp_path`-based database
  now keeps policy uploads contained to the test's temp directory too.

## Files changed

- `app/models.py` — `User`, `UserSession`, `Policy`, `PolicyVersion`,
  `RequirementAssessment`, `RequirementNote`; CHECK/UNIQUE constraints on
  `Risk`, `FrameworkRequirement`, `ControlRequirementMapping`;
  `Framework.is_active`; `FrameworkRequirement.display_order`.
- `app/security.py` (new) — password hashing, session tokens, CSRF tokens.
- `app/deps.py` — `require_login`, `verify_csrf`.
- `app/routers/auth.py` (new) — login/logout.
- `app/cli.py` (new) — `migrate`, `create-user` subcommands.
- `app/storage.py` (new) — validated, atomic policy file upload/storage.
- `app/routers/policies.py` (new) — policy CRUD, version upload, download.
- `app/requirements.py`, `app/csv_import.py`, `app/progress.py` (new) —
  requirement+assessment creation, CSV import, completion percentage.
- `app/routers/frameworks.py` — rewritten: checklist UX, requirement
  detail/notes/assessment routes, CSV import, framework/requirement admin.
- `app/routers/{dashboard,controls,risks,audit_log,placeholders}.py` —
  `require_login`, CSRF on POSTs, idempotent duplicate-mapping handling,
  server-side risk validation, real dashboard metrics.
- `app/flash.py` (new), `app/main.py` — CSRF middleware, lazy `app`,
  `data_dir` override, generic HTTPException→redirect/error-page handler.
- `app/config.py` — `data_dir`, `max_upload_mb`, `session_ttl_hours`,
  `session_cookie_secure`.
- `app/db.py` — SQLite pragmas, `init_db` now runs Alembic.
- `migrations/` (new) — Alembic environment + one initial migration.
- Templates: `login.html` (new), `policies/*.html` (new),
  `frameworks/{new,edit,requirement_new,requirement_detail}.html` (new),
  `frameworks/{list,detail}.html`, `dashboard.html`, `base.html` rewritten;
  minor rebrand (`playground-grc` → `minigrc`) in remaining templates.
- `Dockerfile` — non-root user, copies `migrations/`/`alembic.ini`,
  healthcheck, `python -m app.cli migrate && uvicorn ...` startup.
- `compose.yaml` (new) — one service, one named volume at `/data`, healthcheck.
- `.github/workflows/ci.yml` (new) — lint, format check, pytest, Docker build,
  `docker compose config`.
- `.env.example` — `GRC_DATA_DIR`, upload size, session TTL, cookie-secure flag.
- `pyproject.toml` — `pwdlib[argon2]`, `alembic`; excludes `migrations/versions`
  from Ruff (autogenerated code isn't hand-style-checked).
- `tests/` — `conftest.py` (`logged_in_client`, `test_user` fixtures),
  `test_auth.py`, `test_policies.py`, `test_requirements.py`, `test_risks.py`,
  `test_sqlite_integrity.py`, `test_cli.py` (all new); `test_pages.py`,
  `test_db_init.py`, `test_framework_control_relationship.py` updated.
- `CLAUDE.md`, `README.md`, `docs/architecture.md`, `docs/product-scope.md`,
  `docs/domain/domain-model.md`, `docs/decisions/architectural-decisions.md`
  — updated for the new scope (see "Design decisions" above).

## Verification

- [x] `pytest` — 64 passed.
- [x] `ruff check .` and `ruff format --check .` — clean.
- [x] Fresh SQLite database created from Alembic migrations only (no
      `create_all` anywhere) — verified via `test_db_init.py` and a manual
      `build_engine` + `init_db` run against an empty file.
- [x] Confirmed importing `app.main` does not create or modify the real
      `./data` directory (manual check + the fix described above).
- [x] Full application walkthrough via FastAPI's `TestClient` against real
      HTTP routes (not just unit-level calls): login, framework CSV import,
      requirement assessment + note history, PDF/DOCX upload (valid and
      spoofed), version upload/download, path-traversal filename handling,
      CSRF rejection, unauthenticated-access redirects.
- [x] Restart-persistence equivalent: built the app twice
      (`create_app(data_dir=...)`) against the same on-disk `data_dir`,
      confirming the created user, policy record, and uploaded file all
      survive a fresh process/app instance — the same guarantee a container
      restart needs, exercised without Docker.
- [x] `docker compose config` — validates cleanly (one service, one named
      volume `grc_data` at `/data`, healthcheck, `.env` optional).
- [ ] `docker build` / `docker compose up` — **blocked in this sandbox**:
      the Docker Desktop Linux VM's registry pulls hang indefinitely (even
      for `hello-world`), while host-level `curl` reaches
      `registry-1.docker.io` immediately — a sandbox network restriction on
      the VM's egress, not a Dockerfile/compose problem. `Dockerfile` and
      `compose.yaml` were reviewed by hand and validated with
      `docker compose config`; an actual image build/run should be done by
      a maintainer with unrestricted Docker network access before merging,
      or the next agent run in an environment where Docker Hub is reachable
      from the Docker VM.

## Known gaps / follow-ups

- No RBAC — all authenticated users share the same permissions.
- Evidence, Actions, Connectors, Trust Center remain placeholders (per
  `docs/product-scope.md`).
- No automated backup service — manual procedure documented in `README.md`.
- No reverse proxy / TLS termination included — documented as a deployment
  requirement, not built in this PR.
- `Risk.treatment_plan` remains free text (unchanged from the prior PR).
- IDs remain UUID4 hex, not true ULIDs (unchanged; still not needed).
