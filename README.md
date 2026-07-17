# minigrc

A lightweight, self-hosted ISMS/GRC application for one organization
operating one internal compliance program: a framework checklist (with
notes and history), a versioned policy repository, internal controls, a
structured risk register, and an audit trail — in one place.

## What this is not

- Not a Vanta, Drata, Wiz, or Aikido replacement — see `docs/product-scope.md`
  for the full list of non-goals.
- Not a vulnerability scanner (Aikido owns that).
- Not a multi-tenant platform or a certifying body. This software cannot
  grant ISO 27001 certification — only an accredited external certification
  body can do that.

## Stack

Python 3.12+, FastAPI, Jinja2 (server-rendered, no frontend build step),
SQLAlchemy + SQLite + Alembic, pwdlib[argon2], pytest, Ruff. See
`docs/architecture.md` for why.

## Local startup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env                              # optional; defaults work out of the box
python -m app.cli migrate                         # apply database migrations
python -m app.cli create-user --email you@example.com  # create your login (prompts for a password)

uvicorn app.main:app --reload
```

Visit http://127.0.0.1:8000, sign in, and you'll see a small,
clearly-labelled example framework (see `app/seed.py`).

## Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec app python -m app.cli create-user --email you@example.com
```

Then visit http://127.0.0.1:8000.

Other useful commands:

```bash
docker compose logs -f app     # tail logs
docker compose stop            # stop without removing the volume
docker compose exec app pytest # run tests inside the container
```

All persistent data (the SQLite database and uploaded policy files) lives
in the named volume `grc_data`, mounted at `/data` — it survives container
restarts and rebuilds.

## Data layout

Controlled by `GRC_DATA_DIR` (default `./data` locally, `/data` in Docker):

```
GRC_DATA_DIR/
  grc.db                                  # SQLite database
  policies/<policy id>/<version>/document.<ext>  # immutable policy versions
  tmp/                                    # scratch space during upload; never left behind
```

Override the exact database file with `GRC_DATABASE_PATH` if you need the
database somewhere other than `GRC_DATA_DIR/grc.db`. See `.env.example` for
all settings.

## Migrations

Schema changes go through Alembic (`migrations/`), applied via:

```bash
python -m app.cli migrate          # local
docker compose exec app python -m app.cli migrate  # Docker (also runs automatically on container start)
```

To add a schema change: edit `app/models.py`, then
`alembic revision --autogenerate -m "describe the change"`, review the
generated file by hand, and commit it alongside the model change.

## Backups

A complete backup is the entire `GRC_DATA_DIR` directory (both the database
and the policy files) — copying only `grc.db` loses uploaded documents.

The database is SQLite in WAL mode; copying the live file while the app is
running can capture an inconsistent snapshot. Take a consistent backup with:

```bash
docker compose exec app sqlite3 /data/grc.db ".backup /data/grc-backup.db"
docker cp <container>:/data/grc-backup.db ./grc-backup.db
tar czf policies-backup.tar.gz -C ./data policies   # if run locally, adjust for a Docker volume
```

(`.backup` uses SQLite's online backup API, safe to run against a live
database — unlike copying the file directly.)

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
| Authentication | Implemented — local email/password login, server-side sessions |
| Frameworks / Requirements | Implemented — checklist, manual add, CSV import |
| Requirement assessments & notes | Implemented — applicable/state/owner, append-only notes, audit history |
| Policies | Implemented — versioned PDF/DOCX repository with review dates |
| Internal Controls | Implemented (list, detail, map to requirements) |
| Risks | Implemented (structured register, validated bounds) |
| Audit Log | Implemented (real event history) |
| Evidence | Placeholder — metadata/snapshots planned, no object storage yet |
| Actions | Placeholder — Asana is the source of truth |
| Connectors | Placeholder — GitHub, AWS, Azure, Google Workspace, Asana planned |
| Trust Center | Placeholder — future read-only projection of public data |
| Vulnerabilities | Out of scope — owned by Aikido |

See `docs/product-scope.md` for the full status table and rationale.

## Security notes

- Passwords are hashed with Argon2 (`pwdlib`); sessions are opaque tokens,
  hashed (SHA-256) before storage, expire, and can be revoked (logout).
- Every state-changing form is CSRF-protected (double-submit cookie).
- Policy downloads and every GRC data page require authentication;
  `/health`, `/login`, and static assets do not.
- This app does not terminate TLS itself — **put a reverse proxy (nginx,
  Caddy, Traefik) in front of it in any real deployment** and set
  `GRC_SESSION_COOKIE_SECURE=true` once it's served over HTTPS. No reverse
  proxy container is included in this PR.
- Operational backup is a manual, documented procedure (see above) — there
  is no automated backup service.

## Docs

- [`docs/architecture.md`](docs/architecture.md) — how requests flow, key structural decisions
- [`docs/product-scope.md`](docs/product-scope.md) — in-scope vs. placeholder vs. externally-owned
- [`docs/domain/domain-model.md`](docs/domain/domain-model.md) — domain model, ISO 27001 research, and the copyright boundary it observes
- [`docs/decisions/architectural-decisions.md`](docs/decisions/architectural-decisions.md) — why, for each major decision
- [`docs/worklog/`](docs/worklog/README.md) — change history

## Health check

`GET /health` returns `{"status": "ok"}` — no authentication required.
