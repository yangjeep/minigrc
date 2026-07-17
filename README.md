# minigrc

A lightweight compliance index and evidence ledger for startups. minigrc
connects the systems a startup already uses instead of replacing them:
Google Drive remains the source of truth for policy authoring and
contracts, AWS remains the source of truth for cloud configuration, Asana
remains the source of truth for remediation. minigrc stores relationships,
approved snapshots, access reviews, evidence, risks, and audit history —
a framework checklist (with notes and history), a versioned policy
repository, a vendor/system register with roster snapshots, an internal
people directory, a structured risk register, and an audit trail — in one
place.

## What this is not

- Not a Vanta, Drata, Wiz, or Aikido replacement — see `docs/product-scope.md`
  for the full list of non-goals.
- Not a vulnerability scanner (Aikido owns that) or a CSPM (the AWS
  connector is two fixed evidence checks, not general cloud scanning).
- Not a multi-tenant platform or a certifying body. This software cannot
  grant ISO 27001 certification — only an accredited external certification
  body can do that.
- Not a generalized RBAC platform — a binary admin/user distinction only,
  gating credential and integration surfaces specifically.

## Stack

Python 3.12+, FastAPI, Jinja2 (server-rendered, no frontend build step),
SQLAlchemy + SQLite + Alembic, pwdlib[argon2], google-auth, boto3,
cryptography, pytest, Ruff. See `docs/architecture.md` for why.

## Local startup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env                              # optional; defaults work out of the box
python -m app.cli migrate                         # apply database migrations
python -m app.cli create-user --email you@example.com  # create your login (prompts for a password)
# ^ the first user created this way automatically becomes admin.
# To promote a later or pre-existing user: python -m app.cli promote-admin --email you@example.com

uvicorn app.main:app --reload
```

Visit http://127.0.0.1:8000, sign in, and you'll see a small,
clearly-labelled example framework (see `app/seed.py`).

## Optional integrations

All of these are off by default — the app is fully usable with none of
them configured. Each is enabled by setting its env vars in `.env`; see
`.env.example` for the exact variable names and generation commands.

### Google OIDC login

Sign in with Google as an alternative to local email/password (which
always remains available as break-glass access). Requires an OAuth 2.0
Client ID (Web application) in Google Cloud Console, with
`<GRC_PUBLIC_BASE_URL>/auth/google/callback` registered as a redirect
URI. Optionally restrict sign-in to specific Workspace domains via the
verified `hd` claim (never the email suffix).

### Google Drive connector

An admin connects one org-level, read-only Drive OAuth grant at
`/connectors/google-drive` (distinct client credentials from OIDC login
above, even if pointed at the same Google Cloud project). Once
connected, an admin can associate a `Policy` with a Drive file and
"Capture current version" — downloads/exports the file's current content
through the same validated storage pipeline as a manual upload. Google
Docs/Sheets/Slides export to PDF. The refresh token is encrypted at rest
(`GRC_ENCRYPTION_KEY`, a Fernet key — generate with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
Drive Approvals ("Sync Drive approvals" on a policy page) and optional
Google Workspace Directory sync (`GRC_GOOGLE_WORKSPACE_DIRECTORY_ENABLED=true`,
piggybacks on the same Drive connection) are both best-effort — Google
does not guarantee either is available for every tenant.

### AWS connector

An admin configures one AWS connection at `/connectors/aws` (account
label, expected account ID, optional `AssumeRole` ARN + external ID,
regions) then runs "Run checks now" (or `python -m app.cli aws-run-checks`,
suitable for an external cron). Uses the standard AWS credential provider
chain — ambient workload credentials (an ECS/EC2/Lambda instance role)
are preferred; this app never accepts or stores long-lived AWS access
keys. Collects exactly two evidence families: CloudTrail logging posture
and basic IAM hygiene (root MFA/keys, per-user MFA/access-key age,
password policy presence) — not a CSPM. Results land on `/evidence`,
mappable to framework requirements or internal controls.

### Vendor roster CSV import

On a vendor's page ("User roster"), import a CSV with columns
`email,name,role,status,last_login_at`. Every import is validated
wholesale (bounded size and row count) before anything is written, and
creates a new, immutable snapshot — never overwrites a prior one. Shows
what changed since the last snapshot (added/removed/role changes/status
changes/new admins) and flags departed/suspended people still appearing
in the roster.

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
| Authentication | Implemented — local email/password, server-side sessions, optional Google OIDC login |
| Admin authorization | Implemented — binary admin/user role, gates credential/integration surfaces only |
| Frameworks / Requirements | Implemented — checklist, manual add, CSV import |
| Requirement assessments & notes | Implemented — applicable/state/owner, append-only notes, audit history |
| Policies | Implemented — versioned PDF/DOCX repository, review dates, optional Google Drive source + capture + approvals |
| Internal Controls | Implemented (list, detail, map to requirements) |
| Risks | Implemented (structured register, validated bounds) |
| Evidence | Implemented — immutable snapshots, maps to requirements/controls |
| People directory | Implemented — manual entries, optional Google Workspace Directory sync |
| Vendor/System register | Implemented — one record per system, operational flags, renewals view |
| Vendor roster snapshots | Implemented — append-only CSV import, delta view, Person matching |
| Audit Log | Implemented (real event history) |
| Connectors: Google Drive | Implemented — policy source, approvals, Workspace Directory |
| Connectors: AWS | Implemented — CloudTrail + IAM evidence (not a CSPM) |
| Connectors: GitHub, Azure, Asana | Placeholder |
| Actions | Placeholder — Asana is the source of truth |
| Trust Center | Placeholder — future read-only projection of public data |
| Vulnerabilities | Out of scope — owned by Aikido |

See `docs/product-scope.md` for the full status table and rationale.

## Security notes

- Passwords are hashed with Argon2 (`pwdlib`); sessions are opaque tokens,
  hashed (SHA-256) before storage, expire, and can be revoked (logout).
  Local email/password login always remains available as break-glass
  access, even when Google OIDC is enabled.
- Every state-changing form is CSRF-protected (double-submit cookie),
  including the OAuth `state` parameter used for Google sign-in/Drive
  connect (compared with `secrets.compare_digest`).
- Policy downloads and every GRC data page require authentication;
  `/health`, `/login`, and static assets do not. Integration
  configuration, credential connections, and manual syncs additionally
  require the `admin` role.
- OAuth refresh tokens (Google Drive) and AWS `AssumeRole` external IDs
  are encrypted at rest with Fernet (`GRC_ENCRYPTION_KEY`) — never stored,
  logged, or displayed in plaintext. This app never accepts or stores
  long-lived AWS access keys.
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
