# CLAUDE.md

Shared project instructions for Claude Code and other coding agents.

Read these imports before non-trivial work:

- @.agent/README.md
- @.agent/RULES.md
- @.agent/LOOP.md
- @.agent/TESTING.md
- @docs/product/minigrc-mvp-prd.md

Then read the GitHub issue/design for the specific task and inspect the current repository state before editing.

## What miniGRC is

miniGRC is a lightweight, self-hostable compliance operating system. The MVP is SOC 2 Type II-first while preserving ISO 27001 compatibility through a framework-neutral control/evidence core.

The product's primary question is:

> How far are we from audit-ready compliance, what is blocking us, and what should happen next?

Repository state is authoritative for what exists today. The PRD defines target product/architecture intent. GitHub issues define implementation slices. If they materially conflict, follow `.agent/LOOP.md` rather than silently choosing.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m app.cli migrate
python -m app.cli create-user --email you@example.com

uvicorn app.main:app --reload

pytest
pytest tests/test_pages.py
pytest tests/test_pages.py::test_dashboard_loads

ruff check .
ruff format .
ruff format --check .

alembic revision --autogenerate -m "describe change"
```

Use the repository's current PostgreSQL test/migration path whenever a change affects backend-neutral persistence or schema. Do not assume a SQLite-only test proves PostgreSQL semantics.

## Non-negotiable architecture summary

1. **Material compliance state is event-centric.** Immutable domain events are authoritative; canonical projections/read models are derived and rebuildable.
2. **Exactly one relational backend per deployment:** SQLite or PostgreSQL. Mixed/dual-database runtime modes are unsupported.
3. **PostgreSQL materialized views are disposable read optimization only**, never compliance truth.
4. **Approved/effective compliance history is immutable.** Later changes create new versions/events.
5. **File bytes belong in S3-compatible object storage; compliance meaning belongs in the DB/event model.** S3 versioning is not policy/control/evidence workflow versioning.
6. **Connectors produce normalized facts/artifacts with provenance, not compliance conclusions**, and never write projection tables directly.
7. **AI is advisory/drafting only.** It may observe, prioritize, nag, and pre-fill; it may not autonomously approve, attest, pass tests, accept risk, close findings, or fabricate evidence.
8. **One deployment = one organization for MVP.** Do not add SaaS multi-tenancy/org switching unless explicitly approved later.
9. **Schema changes go through Alembic** and must preserve supported SQLite/PostgreSQL semantics.
10. **Every change must preserve authorization, historical integrity, idempotency/replay behavior, secret handling, migration safety, and the applicable testing/UAT gates in `.agent/TESTING.md`.**

See `.agent/RULES.md` for the complete rules.

## Auth and security baseline

- Preserve secure server-side sessions and CSRF protection.
- Local break-glass administration remains available while generic OIDC is optional/being introduced.
- Authentication does not imply application authorization.
- Never commit secrets, `.env` files, database files, uploaded evidence/policies, raw OAuth tokens, API keys, or provider credentials.
- Never persist secrets in domain events, logs, exported audit packages, fixtures, or client-visible configuration readback.

## Execution order

Before writing code:

1. Read the imported agent files, testing contract, and PRD.
2. Read the target issue and parent/dependency issues.
3. Inspect current code/schema/migrations/tests/docs/recent merged work.
4. Verify assumptions against repository reality.
5. Produce/update a design first when the issue is architecture-sensitive or explicitly requires one.
6. Identify which test layers apply: unit, regression, integration, browser/E2E, Claude Desktop UAT.

After writing code:

1. Run targeted unit/regression tests.
2. Run full relevant regression suite.
3. Run applicable integration/backend tests.
4. Run `ruff check . && ruff format --check .`.
5. Run relevant SQLite/PostgreSQL migration/compatibility checks for persistence changes.
6. Run relevant security/dependency/secret checks available in the repo.
7. Exercise representative browser/E2E user flows for user-visible changes.
8. Perform the adversarial review in `.agent/LOOP.md`.
9. Prepare/execute the Claude Desktop UAT runbook required by `.agent/TESTING.md` when the feature is user-visible and ready for acceptance.
10. Any defect follows the regression-first bug-fix loop in `.agent/TESTING.md`; rerun affected integration/UAT scenarios after the fix.
11. Update required docs/worklog.
12. Commit logically, push the task branch, and create/update a draft PR unless the task contract says otherwise.
13. Do not merge without explicit authorization.

## Current layout

```text
app/                  FastAPI application/domain/persistence code
migrations/           Alembic migrations
tests/                automated tests
docs/                 product, architecture, design, decisions, worklogs
.agent/                coding-agent execution, testing, and UAT contract
CLAUDE.md              concise project-memory entrypoint
```

Do not rely on this abbreviated layout instead of inspecting the repository; it changes as the MVP evolves.

## Definition of done

A task is not complete until:

- required acceptance criteria are implemented;
- applicable unit tests cover new/changed logic;
- every bug fix has regression coverage unless an explicit documented exception is unavoidable;
- full relevant regression tests pass;
- applicable integration tests pass, including both SQLite/PostgreSQL when backend-neutral persistence changed;
- representative browser/E2E flows pass for user-visible changes;
- Claude Desktop UAT is PASS for release-ready user-visible work, or explicitly PENDING and therefore not release-complete;
- lint/format are clean;
- migrations/backfills are reviewed and verified where applicable;
- event/projection rebuild semantics are verified where applicable;
- authorization/security/integrity risks have been reviewed;
- UAT/test defects have completed the regression-first fix/retest workflow;
- documentation/worklog is current;
- no unrelated changes or secrets are in the diff;
- commits are pushed and the draft PR accurately reports unit, regression, integration, E2E, UAT, security, and bug-fix results separately.
