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
  There is no self-registration route.

## Policy storage

- `app/storage.py::save_policy_version_upload` never trusts the client
  filename as a path: it sanitizes it for *display* only, generates the
  on-disk path from server-controlled ids (`policy_id`, `version_number`),
  writes to `GRC_DATA_DIR/tmp/` first while hashing (SHA-256) and enforcing
  `GRC_MAX_UPLOAD_MB`, validates file *content* (PDF signature / DOCX
  zip + `word/document.xml`), then atomically `os.replace`s it into its
  final immutable version directory. Any failure removes the temp file.
- Downloads (`GET /policies/{id}/versions/{version_id}/download`) require
  login, stream via `FileResponse` with the stored `media_type`, a
  sanitized `Content-Disposition` filename, and `X-Content-Type-Options:
  nosniff`. `/data` is never mounted as a static directory.

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
