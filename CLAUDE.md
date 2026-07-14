# CLAUDE.md

Execution contract for coding agents working in this repository. Read this
file first, then `docs/architecture.md` and `docs/product-scope.md` before
making non-trivial changes.

## What this is

A lightweight internal ISMS/GRC tool for operating our ISO 27001 program.
It is **not** a Vanta/Drata/Wiz/Aikido competitor. See `docs/product-scope.md`
for explicit non-goals — read it before adding a feature area.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"       # install app + dev deps (pytest, ruff, httpx)

uvicorn app.main:app --reload # run dev server on http://127.0.0.1:8000

pytest                        # run all tests
pytest tests/test_pages.py    # run a single test file
pytest tests/test_pages.py::test_dashboard_loads  # run a single test

ruff check .                  # lint
ruff format .                 # format
```

## Hard constraints (non-negotiable)

1. **KISS over cleverness.** Boring monolith, one process, one SQLite file.
   No microservices, queues, background workers, or Kubernetes for this
   app's scale. If a constraint here blocks you, ask — don't route around it.
2. **No generic abstractions without a second caller.** No generic
   repository layer, no plugin/connector SDK, no requirement engine — until
   a second concrete use forces it. A future connector starts as one plain
   module (connection test + checks + evidence output), not a framework.
3. **No auth, no multi-tenancy, in this phase.** Don't add login, sessions,
   roles, or an `org_id` column speculatively — this is a single-tenant
   internal tool until a task explicitly asks for auth.
4. **IDs are ULIDs/hex strings, not autoincrement ints** (see `app/models.py`).
5. **Every mutation that matters to an auditor writes an `AuditEvent`**
   (via `app/audit.py::record_audit_event`) in the same session/transaction
   as the mutation it describes.
6. **Never present placeholder/sample content as official ISO text.** Seed
   data and framework catalogues must be clearly labelled
   (`is_placeholder_content`, docs, UI notice) — see `docs/domain/domain-model.md`
   for the copyright boundary this observes. This app never claims to grant
   ISO certification.
7. **Alembic is not in use yet.** Schema changes go through
   `Base.metadata.create_all` in `app/db.py`. Introduce Alembic only when
   the schema has stabilized enough that hand-editing existing SQLite files
   in the field becomes the real alternative (see
   `docs/decisions/architectural-decisions.md`).

## Execution order

**Before writing code:**
1. Read `docs/architecture.md` (layout, request flow, key decisions).
2. Read `docs/product-scope.md` (what's in scope for this app vs. owned by
   an external tool like Google Drive, Asana, or Aikido).
3. Check `docs/domain/domain-model.md` if the change touches frameworks,
   requirements, controls, mappings, or risks.

**After writing code:**
1. Run `pytest && ruff check . && ruff format --check .` — all must pass.
2. Add or update a worklog entry in `docs/worklog/YYYY-MM-DD-<slug>.md`
   (see `docs/worklog/README.md` for the template).
3. Commit with `<type>: <description>` (feat/fix/refactor/docs/test/chore).

## Definition of done

A task is not complete until:
- [ ] Tests pass (`pytest`) and cover the new behavior.
- [ ] Lint and format are clean (`ruff check .`, `ruff format --check .`).
- [ ] A worklog entry exists describing what changed and why.
- [ ] Docs (`docs/architecture.md`, `docs/product-scope.md`) are updated if
      the change alters the schema, layout, or scope they describe.
- [ ] No secrets, `.db` files, or `.env` files are committed.

## Layout

```
app/
  main.py           # application factory, route wiring, error handlers
  config.py         # env-var settings (GRC_ prefix)
  db.py             # SQLAlchemy engine/session, init_db
  models.py         # ORM models — the domain schema lives here
  audit.py          # record_audit_event helper
  seed.py           # idempotent example dataset
  routers/          # one module per nav area
  templates/         # Jinja2, server-rendered
  static/           # plain CSS, no build step
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
| `docs/decisions/architectural-decisions.md` | Why (Alembic deferred, no auth yet, SQLite, etc.) |
| `docs/worklog/` | Append-only audit trail of changes, one file per unit of work |
