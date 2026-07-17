# Architecture

## Shape

A single Python process serving a server-rendered web app, backed by one
SQLite file plus a directory of uploaded policy documents. No frontend
build step, no queue, no separate API tier.

```
Browser
  │  HTML (Jinja2 templates), forms POST directly to routers
  ▼
FastAPI app (app/main.py)
  │  routers/*.py — one module per nav area
  ▼
SQLAlchemy session (app/db.py)  ──────────────┐
  ▼                                           ▼
SQLite file (GRC_DATA_DIR/grc.db)   Policy files (GRC_DATA_DIR/policies/<id>/<version>/)
```

## Request flow

1. `app/main.py::create_app()` builds the SQLAlchemy engine, runs
   `init_db()` (applies Alembic migrations up to `head`), seeds example
   data if the `frameworks` table is empty, then wires up routers, a
   Jinja2 `Environment`, and a CSRF-cookie middleware.
2. Each router function takes `db: Session = Depends(get_db)` (see
   `app/deps.py`) — one session per request, committed on success and
   rolled back on exception.
3. Every router except `auth`, `/health`, and static files depends on
   `require_login` (see `app/deps.py`), which reads the `session` cookie,
   looks up the hashed token in `user_sessions`, and redirects to `/login`
   if missing, expired, or revoked.
4. Every state-changing POST route depends on `verify_csrf`, which compares
   the hidden `csrf_token` form field against the `csrf_token` cookie set by
   the CSRF middleware (double-submit cookie pattern).
5. GET routes render a template via `request.app.state.templates`. POST
   routes mutate through the session, write an `AuditEvent` alongside the
   mutation (`app/audit.py`) in the same transaction, and redirect (303)
   back to a GET route with a one-shot flash message in the query string
   (`app/flash.py`) — plain server-rendered forms, no client-side state.
6. `app/main.py` registers an exception handler for `HTTPException` that
   renders `templates/error.html` for 4xx/5xx, and transparently redirects
   for 3xx (used by `require_login` to send unauthenticated requests to
   `/login` without a dedicated redirect object at every call site).

## Why this shape

- **One process, one SQLite file, one policy directory.** At this scale (one
  organization operating one ISMS program) a boring monolith is simpler to
  run, reason about, and back up than any distributed alternative. See
  `docs/decisions/architectural-decisions.md`.
- **Server-rendered Jinja2, not a JS frontend.** Filtering/searching the
  framework checklist is done with plain query-string GET parameters, not
  client-side state. No JS build step exists or is needed.
- **`app/routers/` mirrors the nav, one module per area.** Reading the nav
  in `app/main.py::NAV_ITEMS` tells you which router to open.
- **`placeholders.py` uses a catch-all `/{slug}` route** for the
  not-yet-built nav areas (Evidence, Actions, Connectors, Trust Center)
  instead of one router module per area. Because it's a catch-all, it is
  registered **last** in `create_app()` — anything registered after it
  would be silently shadowed.
- **No repository/service layer.** Routers call SQLAlchemy directly. With
  one process and one database, an extra layer here would only wrap the
  ORM without adding a real seam — see the "no generic abstractions"
  constraint in `CLAUDE.md`.
- **Auth is a plain session table, not JWTs.** A JWT would need to be either
  stateless (can't be revoked before expiry) or backed by a server-side
  denylist (all the complexity of a session table plus JWT parsing). A
  session table alone is simpler and gives immediate revocation on logout.
- **CSRF is a double-submit cookie, not a signed token.** The CSRF cookie is
  independent of the login session (set for every request, even
  unauthenticated ones), so the same mechanism protects the login form
  itself, not just authenticated forms.

## Persistence

- `GRC_DATA_DIR` (default `./data`) is the single directory all persistent
  data lives under:
  - `GRC_DATA_DIR/grc.db` — the SQLite database (override the exact file
    with `GRC_DATABASE_PATH` if needed).
  - `GRC_DATA_DIR/policies/<policy id>/<version number>/document.<ext>` —
    immutable uploaded policy versions (see `app/storage.py`).
  - `GRC_DATA_DIR/tmp/` — scratch space for in-progress uploads; a file only
    ever leaves this directory via an atomic rename into its final
    `policies/...` path, and is deleted on any validation failure.
- `app/db.py::build_engine` creates the parent directory if missing, and
  sets `PRAGMA foreign_keys = ON`, `PRAGMA busy_timeout = 5000`, and
  `PRAGMA journal_mode = WAL` on every new connection.
- In Docker, mount one named volume at `/data` (`compose.yaml` does this) so
  the database and policy files survive container restarts and rebuilds.
- Schema is managed by Alembic (`migrations/`) — `init_db()` runs
  `alembic upgrade head` programmatically against whichever database path
  the caller resolved, so dev, tests, and Docker all go through the exact
  same code path. See `docs/decisions/architectural-decisions.md`.

## Authentication

- `app/models.py::User` / `UserSession`, `app/security.py`,
  `app/deps.py::require_login`, `app/routers/auth.py`.
- Passwords hashed with `pwdlib[argon2]`. Sessions are opaque
  `secrets.token_urlsafe(32)` values; only a SHA-256 hash is stored
  server-side, the raw token lives solely in an `HttpOnly`, `SameSite=Lax`
  cookie (`Secure` when `GRC_SESSION_COOKIE_SECURE=true`).
- `require_login` rejects missing, unknown, revoked, or expired sessions by
  redirecting to `/login`. Logout revokes the specific `UserSession` row.
- The first user is created with `python -m app.cli create-user --email …`
  (prompts for a password, never takes one as a CLI argument or env var).
  There is no self-registration route. That first user automatically
  becomes `admin`; later users default to `user`
  (`python -m app.cli promote-admin --email …` grants admin to an
  existing user, no password involved — see "Admin authorization" below).
- Optional Google OIDC login (`app/google_oidc.py`,
  `app/routers/google_oidc.py`) is a second way to establish the same
  `User`/session — disabled (404) unless `GRC_GOOGLE_OIDC_CLIENT_ID`/
  `_CLIENT_SECRET`/`GRC_PUBLIC_BASE_URL` are all set. Session issuance
  itself (`app/routers/auth.py::start_user_session`) is shared between
  local login and OIDC login.

## Admin authorization

- `app/models.py::User.role` is `"user"` or `"admin"` — not general RBAC.
  `app/deps.py::require_admin` (built on `require_login`) gates
  integration configuration, credential connections, manual syncs, and
  admin-designated destructive vendor operations only. Every other
  authenticated route is identical for every logged-in user.
- See ADR #12 in `docs/decisions/architectural-decisions.md`.

## Policy storage

- `app/storage.py`'s write/validate/store core (`_save_policy_version`) is
  shared by two entry points: `save_policy_version_upload` (browser
  upload) and `save_policy_version_from_bytes` (Google Drive capture).
  Neither trusts the client/Drive filename as a path: it's sanitized for
  *display* only, the on-disk path is generated from server-controlled
  ids (`policy_id`, `version_number`), content is written to
  `GRC_DATA_DIR/tmp/` first while hashing (SHA-256) and enforcing
  `GRC_MAX_UPLOAD_MB`, validated by *content* (PDF signature / DOCX
  zip + `word/document.xml`), then atomically `os.replace`d into its
  final immutable version directory. Any failure removes the temp file.
- Downloads (`GET /policies/{id}/versions/{version_id}/download`) require
  login, stream via `FileResponse` with the stored `media_type`, a
  sanitized `Content-Disposition` filename, and `X-Content-Type-Options:
  nosniff`. `/data` is never mounted as a static directory.
- Optional Google Drive source: a `Policy` can be linked to a Drive file
  (`app/routers/policies.py`, admin-only) and an admin can "Capture
  current version" — content flows through the exact same validated
  pipeline above. Drive is never treated as archival storage; the locally
  captured, hashed `PolicyVersion` is authoritative regardless of what
  happens to the Drive file afterward. See ADRs #17/#18.

## Framework checklist

- `app/models.py::RequirementAssessment` (one per `FrameworkRequirement`,
  created alongside it by `app/requirements.py::add_requirement`) holds
  `applicable`, `implementation_state`, `owner`, `last_reviewed_at/by`.
- `app/models.py::RequirementNote` is append-only — no update/delete route
  exists; corrections are new notes.
- `app/progress.py::compute_progress` computes the completion percentage as
  `implemented / applicable` (with a `None`/"N/A" result, not a
  divide-by-zero, when there are no applicable requirements).
- Marking a requirement not-applicable requires a note in the same POST,
  and the assessment update + note + audit event are written in one
  session/transaction (see `app/routers/frameworks.py::update_assessment`).
- `app/csv_import.py::import_requirements_csv` validates every row before
  writing anything — a malformed file changes nothing.

## People and Vendor/System register

- `app/models.py::Person` is a shared identity reference (vendor admins,
  roster rows, `User.person_id`, optional Workspace Directory sync).
  `employment_status` starts `"unknown"` and only changes on explicit
  source data — never inferred, never deleted on a missing sync record.
- `app/models.py::VendorSystem` is one model per purchased/used system
  (not separate Vendor/Application tables). Money is integer minor units
  + a single `billing_frequency`; `annualized_cost_minor` is always
  computed (`app/models.py::VendorSystem.annualized_cost_minor`), never a
  second manually-entered field that could disagree with the first.
  `app/vendor_flags.py::compute_flags` computes operational warnings
  (missing admin, contract missing, renewal approaching...) from live
  data at request time — never a stored, driftable flag.
- `app/models.py::VendorUserSnapshot`/`VendorUserSnapshotRow`: append-only
  vendor roster CSV import (`app/vendor_roster_import.py`), validated
  wholesale before anything is written, sharing the bounded-read helper
  (`app/uploads.py`) with the framework CSV importer. Delta-vs-previous
  and Person-matching happen at read time in
  `app/routers/vendor_systems.py`.
- See ADRs #13–#15.

## Google Drive Approvals and Workspace Directory (optional)

- `app/google_drive_approvals.py::fetch_approvals` mirrors Drive's
  Approvals API into `PolicyApprovalSnapshot` (append-only, associated
  with a specific `PolicyVersion`). Any failure (missing scope, 403/404,
  unsupported tenant) is caught by the caller and shown as "Approval data
  unavailable" — never a failed sync.
- `app/google_workspace_directory.py` requests one additional read-only
  scope (`admin.directory.user.readonly`) on the *same* Drive OAuth
  connection (`GRC_GOOGLE_WORKSPACE_DIRECTORY_ENABLED=true`) rather than a
  third OAuth flow, and updates `Person.employment_status` — never
  deletes a `Person` missing from a sync.
- See ADRs #19/#20.

## Evidence and the AWS connector

- `app/models.py::EvidenceSnapshot` is one shared, immutable table for
  evidence from any source (AWS today) — explicitly named in this
  branch's spec as the one deliberate exception to "no shared abstraction
  without a second caller," since two concrete sources need the same
  point-in-time-snapshot-with-mappings shape. No edit/delete route;
  mappable to framework requirements or internal controls
  (`EvidenceRequirementMapping`/`EvidenceControlMapping`).
- `app/aws_connector.py` collects exactly two evidence families —
  CloudTrail logging posture and basic IAM hygiene — via the standard AWS
  credential provider chain (ambient workload credentials preferred),
  with optional `AssumeRole`. Never accepts/stores long-lived AWS access
  keys; a failed AWS API call always produces `status="unknown"`, never
  `"fail"`. `app/models.py::AwsConnection` holds configuration
  (encrypted `external_id`) and is updated in place — unlike
  `GoogleDriveConnection`, it isn't an OAuth grant with a revocation
  history to preserve.
- Admin-only: connection settings, "Test connection", "Run checks now"
  (`app/routers/aws_connector.py`), and the equivalent
  `python -m app.cli aws-run-checks`. Evidence itself (`/evidence`) is
  viewable by any authenticated user.
- See ADRs #21/#22.

## Testing

- `tests/conftest.py` builds a fresh `FastAPI` app per test via
  `create_app(database_path=<tmp file>)`; `create_app` derives an isolated
  `data_dir` from that path (see `CLAUDE.md` constraint #10) so tests never
  touch the real `./data` directory or `./data/policies`.
- `logged_in_client` (in `tests/conftest.py`) logs in a real test user
  through the actual `/login` route (extracting the CSRF token from the
  rendered HTML), so authenticated tests exercise the real auth path.
- `tests/test_pages.py` hits every nav-visible route once, so a template or
  routing regression fails loudly.
- `tests/test_auth.py`, `tests/test_policies.py`, `tests/test_requirements.py`,
  `tests/test_risks.py`, `tests/test_sqlite_integrity.py` cover
  authentication, policy uploads/downloads, the framework checklist
  (assessments/notes/import), risk validation, and SQLite-level integrity
  (foreign keys, CHECK constraints) respectively.
- `tests/test_admin.py`, `tests/test_people.py`, `tests/test_vendor_systems.py`,
  `tests/test_vendor_roster.py`, `tests/test_google_oidc.py`,
  `tests/test_google_drive.py`, `tests/test_drive_approvals_and_directory.py`,
  `tests/test_aws_connector.py` cover admin authorization, the People
  directory, the Vendor/System register, vendor roster snapshots, Google
  OIDC login, the Google Drive connector, Drive Approvals + Workspace
  Directory sync, and the AWS connector + Evidence respectively. External
  calls (Google, AWS) are always mocked/stubbed — see each file's use of
  `unittest.mock.patch` or `botocore.stub.Stubber`.
