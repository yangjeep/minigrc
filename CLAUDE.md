# CLAUDE.md

Execution contract for coding agents working in this repository. Read this
file first, then `docs/architecture.md` and `docs/product-scope.md` before
making non-trivial changes.

## What this is

A lightweight, self-hosted ISMS/GRC tool for one organization operating one
internal compliance program (frameworks, policies, risks, audit trail). It
is **not** a Vanta/Drata/Wiz/Aikido competitor. See `docs/product-scope.md`
for explicit non-goals — read it before adding a feature area.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"       # install app + dev deps (pytest, ruff, httpx)

python -m app.cli migrate                        # apply DB migrations
python -m app.cli create-user --email you@example.com  # create your login

uvicorn app.main:app --reload # run dev server on http://127.0.0.1:8000

pytest                        # run all tests
pytest tests/test_pages.py    # run a single test file
pytest tests/test_pages.py::test_dashboard_loads  # run a single test

ruff check .                  # lint
ruff format .                 # format

alembic revision --autogenerate -m "describe change"  # new migration
```

## Hard constraints (non-negotiable)

1. **KISS over cleverness.** Boring monolith, one process, one SQLite file.
   No microservices, queues, background workers, or Kubernetes for this
   app's scale. If a constraint here blocks you, ask — don't route around it.
2. **No generic abstractions without a second caller.** No generic
   repository layer, no plugin/connector SDK, no requirement engine — until
   a second concrete use forces it. A future connector starts as one plain
   module (connection test + checks + evidence output), not a framework.
3. **Single-tenant, no multi-tenancy.** One deployment = one organization.
   No `org_id` column, no org switching, no RBAC beyond "logged in or not."
4. **IDs are 32-character hex UUID4 strings, not autoincrement ints, and
   not ULIDs** (see `app/models.py::new_id`) — they are not lexicographically
   sortable; use `created_at` where creation order matters.
5. **Every mutation that matters to an auditor writes an `AuditEvent`**
   (via `app/audit.py::record_audit_event`) in the same session/transaction
   as the mutation it describes.
6. **Never present placeholder/sample content as official ISO text.** Seed
   data and framework catalogues must be clearly labelled
   (`is_placeholder_content`, docs, UI notice) — see `docs/domain/domain-model.md`
   for the copyright boundary this observes. This app never claims to grant
   ISO certification.
7. **Schema changes go through Alembic**, not `Base.metadata.create_all`.
   Every schema change needs a migration (`alembic revision --autogenerate`),
   reviewed by hand before committing — see
   `docs/decisions/architectural-decisions.md`.
8. **Auth is local and simple**: session cookie + server-side session table,
   Argon2 password hashing, CSRF on every state-changing form. No JWTs, no
   hosted identity provider, no self-registration. See `app/security.py`,
   `app/deps.py`, `app/routers/auth.py`.
9. **All persistent data lives under `GRC_DATA_DIR`** (default `./data`):
   `grc.db`, `policies/<id>/<version>/`, `tmp/`. Never write persistent
   files anywhere else — see `app/storage.py`.
10. **Importing `app.main` must never touch the real `./data` directory.**
    The module-level `app` object is built lazily (see `app/main.py::__getattr__`)
    specifically so `tests/conftest.py` importing `create_app` doesn't create
    a real database. Tests must always pass an explicit `database_path`/
    `data_dir` to `create_app`.

## Execution order

**Before writing code:**
1. Read `docs/architecture.md` (layout, request flow, key decisions).
2. Read `docs/product-scope.md` (what's in scope for this app vs. owned by
   an external tool like Asana).
3. Check `docs/domain/domain-model.md` if the change touches frameworks,
   requirements, assessments, controls, mappings, or risks.

**After writing code:**
1. Run `pytest && ruff check . && ruff format --check .` — all must pass.
2. If the schema changed, generate an Alembic migration and check it by hand.
3. Add or update a worklog entry in `docs/worklog/YYYY-MM-DD-<slug>.md`
   (see `docs/worklog/README.md` for the template).
4. Commit with `<type>: <description>` (feat/fix/refactor/docs/test/chore).

## Definition of done

A task is not complete until:
- [ ] Tests pass (`pytest`) and cover the new behavior.
- [ ] Lint and format are clean (`ruff check .`, `ruff format --check .`).
- [ ] A worklog entry exists describing what changed and why.
- [ ] Docs (`docs/architecture.md`, `docs/product-scope.md`) are updated if
      the change alters the schema, layout, or scope they describe.
- [ ] No secrets, `.db` files, uploaded documents, or `.env` files are committed.

## Layout

```
app/
  main.py           # application factory, route wiring, middleware, error handlers
  config.py         # env-var settings (GRC_ prefix)
  db.py             # SQLAlchemy engine/session, init_db (runs Alembic)
  models.py         # ORM models — the domain schema lives here
  security.py       # password hashing, session tokens, CSRF tokens
  deps.py           # require_login, verify_csrf, get_db
  audit.py          # record_audit_event helper
  storage.py        # policy upload validation + immutable on-disk storage
  progress.py       # framework completion percentage
  requirements.py   # add_requirement (requirement + assessment together)
  csv_import.py     # framework requirement CSV import
  cli.py            # `python -m app.cli migrate|create-user`
  seed.py           # idempotent example dataset
  routers/          # one module per nav area
  templates/        # Jinja2, server-rendered
  static/           # plain CSS, no build step
migrations/         # Alembic environment + versions/
tests/
docs/
  architecture.md
  product-scope.md
  domain/domain-model.md
  decisions/architectural-decisions.md
  worklog/
```

## File roles

| Path | Role |
|------|------|
| `CLAUDE.md` (this file) | Binding execution contract — read first |
| `docs/architecture.md` | How requests flow through the app; key structural decisions |
| `docs/product-scope.md` | What's in scope now, what's a placeholder, what's owned externally |
| `docs/domain/domain-model.md` | ISO 27001 domain research and the copyright boundary it observes |
| `docs/decisions/architectural-decisions.md` | Why (Alembic, auth design, SQLite, etc.) |
| `docs/worklog/` | Append-only audit trail of changes, one file per unit of work |
