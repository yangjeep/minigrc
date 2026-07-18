# MiniGRC

**A lightweight, self-hosted compliance workspace for startups and small teams.**

MiniGRC keeps the parts of a security and compliance program that need structure—requirements, controls, policy snapshots, evidence, risks, vendors, people, and audit history—in one place. It connects to the tools your team already uses instead of trying to replace them.

Google Drive can remain the source of truth for policy authoring and contracts. AWS remains the source of truth for cloud configuration. MiniGRC records the approved snapshots, relationships, review history, and evidence an auditor needs to follow.

## Why MiniGRC?

- **Start small.** Run one container and manage one organization's compliance program without buying an enterprise GRC platform.
- **Keep existing workflows.** Connect to Google Drive and AWS; import vendor rosters instead of rebuilding every source system.
- **Preserve history.** Policy versions, roster imports, evidence, assessment notes, and audit events are append-only or versioned where history matters.
- **Make ownership visible.** Track policy owners, control mappings, risk owners, vendor admins, renewal dates, and backup contacts.
- **Stay understandable.** Server-rendered pages, SQLite, local files, and a deliberately narrow feature set keep operation and recovery straightforward.

## What is included?

| Area | What MiniGRC does |
| --- | --- |
| Frameworks and requirements | Checklist, applicability/state/owner assessments, notes, history, manual entry, and CSV import |
| Policies | Immutable PDF/DOCX versions, review dates, Google Drive association, capture, and approval history |
| Controls, evidence, and risks | Internal controls mapped to requirements, immutable evidence snapshots, and a structured risk register |
| Vendors and systems | Access URL, shared-login flag, primary and backup admins, department, cost, contract location, renewal date, support contact, and operational warnings |
| Vendor access reviews | Bounded CSV roster imports with immutable snapshots, change detection, and internal-person matching |
| People | Internal directory with optional Google Workspace Directory sync |
| AWS evidence | Focused CloudTrail logging and IAM hygiene checks using the standard AWS credential chain or AssumeRole |
| Audit history | Authentication-protected history of meaningful changes and connector activity |

## What MiniGRC is not

MiniGRC is intentionally not a Vanta, Drata, Wiz, Aikido, or full enterprise GRC replacement. It is not a vulnerability scanner, CSPM, task tracker, document editor, multi-tenant SaaS platform, identity provider, or certification body. Its AWS connector performs a small set of evidence checks; it does not continuously scan or secure an AWS account.

See [Product Scope](docs/product-scope.md) for the detailed boundary and feature status.

## Quick start with Docker Compose

Requirements: Docker with Compose v2.

```bash
git clone https://github.com/yangjeep/minigrc.git
cd minigrc
cp .env.example .env
docker compose up -d --build
docker compose exec app python -m app.cli create-user --email you@example.com
```

The first CLI-created user becomes an administrator. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and sign in.

Useful commands:

```bash
docker compose logs -f app
docker compose exec app python -m app.cli migrate
docker compose exec app python -m app.cli promote-admin --email teammate@example.com
docker compose stop
```

All persistent data lives in the Docker volume `grc_data`, mounted at `/data`. Stopping or rebuilding the container does not remove it.

## Local development

Requirements: Python 3.12+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
python -m app.cli migrate
python -m app.cli create-user --email you@example.com
uvicorn app.main:app --reload
```

Run the checks with:

```bash
pytest
ruff check .
ruff format --check .
```

The application uses FastAPI, server-rendered Jinja templates, SQLAlchemy, SQLite, and Alembic. There is no frontend build step.

## Optional integrations

The application works without any external integration. Configuration is documented in [`.env.example`](.env.example).

### Google sign-in and Drive

Google OIDC can provide optional sign-in while local email/password remains available as break-glass access. Register `<GRC_PUBLIC_BASE_URL>/auth/google/callback` as the OAuth redirect URI. Sign-in can be limited to verified Google Workspace domains.

A separate, read-only Drive OAuth connection lets an administrator associate a policy with a Drive file and capture its current contents as an immutable MiniGRC policy version. Google Docs, Sheets, and Slides are exported to PDF. Drive approval sync and Google Workspace Directory sync are optional, best-effort features.

Stored refresh tokens are encrypted with `GRC_ENCRYPTION_KEY`.

### AWS evidence

Configure an AWS connection at `/connectors/aws`, then run checks in the UI or with:

```bash
python -m app.cli aws-run-checks
```

MiniGRC uses the standard AWS credential provider chain and supports optional AssumeRole. It never accepts or stores long-lived AWS access keys. Checks cover CloudTrail logging posture and basic IAM hygiene such as root MFA/keys, user MFA, access-key age, and password-policy presence.

### Vendor roster imports

On a vendor page, import a CSV containing:

```text
email,name,role,status,last_login_at
```

Each file is validated in full and bounded by configured size and row limits before any database write. A successful import creates an immutable snapshot and reports added users, removed users, role or status changes, new admins, and departed people who still retain access.

## Data, migrations, and backups

`GRC_DATA_DIR` defaults to `./data` locally and `/data` in Docker:

```text
GRC_DATA_DIR/
  grc.db
  policies/<policy-id>/<version>/document.<ext>
  tmp/
```

Database migrations run automatically when the container starts and can also be applied with `python -m app.cli migrate`.

A complete backup must include both the SQLite database and the `policies/` directory. For a live database, use SQLite's online backup command rather than copying `grc.db` directly:

```bash
docker compose exec app sqlite3 /data/grc.db ".backup /data/grc-backup.db"
docker cp <container>:/data/grc-backup.db ./grc-backup.db
```

## Documentation

- [Architecture](docs/architecture.md) — request flow and structural decisions
- [Product scope](docs/product-scope.md) — intended use, non-goals, and current feature status
- [Domain model](docs/domain/domain-model.md) — GRC concepts, relationships, and research boundary
- [Architectural decisions](docs/decisions/architectural-decisions.md) — rationale for major design choices
- [Worklog](docs/worklog/README.md) — implementation history

## Contributing

Issues and pull requests are welcome. Keep changes aligned with the project's startup-focused scope, include tests for behavior changes, and run `pytest`, `ruff check .`, and `ruff format --check .` before opening a pull request.

## Security

Do not report suspected vulnerabilities in a public issue. Until a private reporting policy is published, use GitHub's private vulnerability reporting for this repository if it is enabled, or contact the repository owner privately.

For a real deployment, put a TLS-terminating reverse proxy in front of MiniGRC and set `GRC_SESSION_COOKIE_SECURE=true`. Review [`.env.example`](.env.example) before exposing the application to a network. `GET /health` is intentionally unauthenticated; GRC data and policy downloads require authentication.

## License

MiniGRC is licensed under the [Apache License 2.0](LICENSE). You may use, modify, distribute, and commercially deploy it subject to the license terms. Apache-2.0 includes an explicit patent license and requires preservation of applicable copyright and license notices.

## Acknowledgements

MiniGRC is an independent project. Its focus on making compliance operations approachable for smaller teams shares common ground with projects such as [OpenGRC](https://github.com/LeeMangold/OpenGRC).
