# Initial GRC/ISMS application foundation

**Date:** 2026-07-11
**Author:** Claude (agent)
**Type:** feat

## Summary

Established the playground-grc repository as a lightweight internal
ISMS/GRC application skeleton for operating an ISO 27001 program: a
FastAPI + Jinja2 + SQLite monolith with frameworks, internal controls,
risks, and an audit log, plus honest placeholder pages for the areas
deliberately deferred (policies, evidence, actions, connectors, trust
center).

## Requested

Independently research the ISO 27001 ISMS domain, establish the repo,
build a small runnable skeleton (not a Vanta/Drata/Wiz/Aikido competitor),
document design decisions, verify it end to end, and open the initial PR
— without waiting for sign-off on routine technical decisions.

## Researched

- LeaseLab's existing agentic-development conventions
  (`~/ws/yangjeep/leaselab/CLAUDE.md`, `.agent/`, `docs/decisions/`,
  `.agent/WORKLOG/`) were read and adapted for this repo — the worklog
  format, decision-log format, and CLAUDE.md structure are derived from
  there, with LeaseLab-specific product/architecture content (event
  sourcing, Cloudflare Workers, `org_id` tenancy, Clerk auth) stripped out
  since none of it applies to this single-tenant Python monolith.
- ISO/IEC 27001:2022 structure (clauses 4-10 vs. Annex A's 93 controls
  across 4 themes) via public secondary sources — see
  `docs/domain/domain-model.md` for the full source list and the
  copyright boundary observed (no normative ISO text reproduced anywhere).

## Decisions made

See `docs/decisions/architectural-decisions.md` for the full list with
rationale. Highlights:
- Boring monolith: one FastAPI process, one SQLite file, server-rendered
  Jinja2, no frontend build step, no HTMX (nothing here needs it yet).
- No Alembic yet — `Base.metadata.create_all` until a real deployment has
  data to migrate.
- IDs are hex UUID4 strings, not autoincrement ints.
- Requirement↔control mapping is many-to-many via a join table.
- Policies/Evidence/Actions/Connectors/Trust Center are honest placeholder
  pages, not empty tables.
- No auth, no multi-tenancy, in this phase.
- Audit events are written explicitly alongside each meaningful mutation.

## Alternatives rejected

- A generic "requirement/control mapping engine" configurable per
  framework — rejected as speculative; the one relationship this PR needs
  is directly modeled instead (see `CLAUDE.md` constraint #2).
- Modeling Evidence/Policy/Exception/CorrectiveAction as tables now —
  rejected; each needs a workflow or storage decision this PR
  intentionally defers (see `docs/domain/domain-model.md`).
- HTMX for the control-mapping and risk-creation forms — rejected; plain
  POST + 303 redirect is simpler and sufficient; nothing here needs
  partial-page updates yet.
- docker compose — omitted; a single `docker run -v ...` command is
  enough for one service with no other local dependencies.

## Files changed

- `app/` — new FastAPI application package: `main.py` (factory, routing,
  error handlers), `config.py`, `db.py`, `models.py`, `audit.py`,
  `seed.py`, `deps.py`, `logging_config.py`, `routers/*.py` (dashboard,
  frameworks, controls, risks, audit_log, placeholders), `templates/*`,
  `static/style.css`
- `tests/` — `conftest.py`, `test_health.py`, `test_db_init.py`,
  `test_framework_control_relationship.py`, `test_pages.py`
- `pyproject.toml`, `.env.example`, `.gitignore`, `Dockerfile` — project
  packaging and container build
- `CLAUDE.md`, `docs/architecture.md`, `docs/product-scope.md`,
  `docs/domain/domain-model.md`, `docs/decisions/architectural-decisions.md`,
  `docs/worklog/README.md`, this entry — documentation
- `README.md` — rewritten with product purpose, non-goals, stack, startup,
  test/lint commands, feature status, doc links

## Verification

- [x] `pytest` — 16 passed.
- [x] `ruff check .` and `ruff format --check .` — clean.
- [x] `docker build -t playground-grc:test .` — succeeded.
- [x] Ran the container with `-v playground_grc_data_test:/data`; verified
      `/health`, `/`, `/frameworks`, `/controls`, `/risks`, `/audit-log`,
      and `/policies` all return 200, and the seeded framework content
      renders.
- [x] Manually exercised both POST flows against a running dev server: the
      control→requirement mapping form and the risk-creation form; both
      redirect correctly and the corresponding `AuditEvent` rows appear in
      `/audit-log`.
- [x] Created a risk via POST, restarted the container, and confirmed the
      risk was still present and the seed data was not duplicated
      (idempotent seeding verified across a real restart, not just a
      Python-level test).
- [x] Reviewed `git diff --stat` and confirmed no `.db`, `.env`, or cache
      files are staged.

## Known gaps / follow-ups

- No object storage, so Evidence remains a placeholder — see
  `docs/product-scope.md` "Next PR candidates" for the recommended order
  (Evidence metadata → first real connector → risk treatment workflow →
  policy index).
- `Risk.treatment_plan` is free text; a structured treatment/exception
  workflow is deferred until a second workflow exists to shape it against.
- IDs are UUID4 hex, not true ULIDs (not lexicographically sortable by
  creation time) — noted as a possible small upgrade in
  `docs/decisions/architectural-decisions.md`, not needed by anything in
  this PR.
- No CI workflow was added in this PR; tests/lint are run locally via the
  commands in `CLAUDE.md`/`README.md`.
