# playground-grc

A lightweight, internal ISMS/GRC application for operating an ISO 27001
program: frameworks, internal controls, risks, and an audit trail in one
place.

## What this is not

- Not a Vanta, Drata, Wiz, or Aikido replacement — see `docs/product-scope.md`
  for the full list of non-goals.
- Not a vulnerability scanner (Aikido owns that).
- Not an authentication system, multi-tenant platform, or certifying body.
  This software cannot grant ISO 27001 certification — only an accredited
  external certification body can do that.

## Stack

Python 3.12+, FastAPI, Jinja2 (server-rendered, no frontend build step),
SQLAlchemy + SQLite, pytest, Ruff. See `docs/architecture.md` for why.

## Local startup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env   # optional; defaults work out of the box

uvicorn app.main:app --reload
```

Visit http://127.0.0.1:8000 — the app seeds a small, clearly-labelled
example dataset on first run (see `app/seed.py`).

## Docker startup

```bash
docker build -t playground-grc .
docker run --rm -p 8000:8000 -v playground_grc_data:/data playground-grc
```

The container stores its SQLite database at `/data/grc.db` inside the
named volume `playground_grc_data`, so data survives container restarts
and rebuilds.

## Database location

Controlled by `GRC_DATABASE_PATH` (see `.env.example`):
- Local dev default: `./data/grc.db`
- Docker default: `/data/grc.db` (set in the `Dockerfile`, expects a
  mounted volume at `/data`)

No migration tool is wired in yet — schema is created via
`Base.metadata.create_all` on startup. See
`docs/decisions/architectural-decisions.md` for why, and what would
trigger introducing Alembic.

## Tests and lint

```bash
pytest                        # full suite
pytest tests/test_pages.py    # one file
pytest tests/test_pages.py::test_dashboard_loads  # one test

ruff check .                  # lint
ruff format .                 # format
```

## Current feature status

| Area | Status |
|------|--------|
| Frameworks / Requirements | Implemented (list, detail) |
| Internal Controls | Implemented (list, detail, map to requirements) |
| Risks | Implemented (structured register, list + create) |
| Audit Log | Implemented (real event history) |
| Policies | Placeholder — Google Drive is the source of truth |
| Evidence | Placeholder — metadata/snapshots planned, no object storage yet |
| Actions | Placeholder — Asana is the source of truth |
| Connectors | Placeholder — GitHub, AWS, Azure, Google Workspace, Asana planned |
| Trust Center | Placeholder — future read-only projection of public data |
| Vulnerabilities | Out of scope — owned by Aikido |

See `docs/product-scope.md` for the full status table and rationale.

## Docs

- [`docs/architecture.md`](docs/architecture.md) — how requests flow, key structural decisions
- [`docs/product-scope.md`](docs/product-scope.md) — in-scope vs. placeholder vs. externally-owned
- [`docs/domain/domain-model.md`](docs/domain/domain-model.md) — ISO 27001 domain research and the copyright boundary it observes
- [`docs/decisions/architectural-decisions.md`](docs/decisions/architectural-decisions.md) — why, for each major decision
- [`docs/worklog/`](docs/worklog/README.md) — change history

## Health check

`GET /health` returns `{"status": "ok"}`.
