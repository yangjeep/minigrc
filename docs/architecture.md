# Architecture

## Shape

A single Python process serving a server-rendered web app, backed by one
SQLite file. No frontend build step, no queue, no separate API tier.

```
Browser
  │  HTML (Jinja2 templates), forms POST directly to routers
  ▼
FastAPI app (app/main.py)
  │  routers/*.py — one module per nav area
  ▼
SQLAlchemy session (app/db.py)
  ▼
SQLite file (path from GRC_DATABASE_PATH)
```

## Request flow

1. `app/main.py::create_app()` builds the SQLAlchemy engine, runs
   `init_db()` (creates tables if missing via `Base.metadata.create_all`),
   seeds example data if the `frameworks` table is empty, then wires up
   routers and a Jinja2 `Environment`.
2. Each router function takes `db: Session = Depends(get_db)` (see
   `app/deps.py`) — one session per request, committed on success and
   rolled back on exception.
3. GET routes render a template via `request.app.state.templates`. POST
   routes mutate through the session, write an `AuditEvent` alongside the
   mutation (`app/audit.py`), and redirect (303) back to a GET route —
   plain server-rendered forms, no client-side state.
4. `app/main.py` registers 404/500 exception handlers that render
   `templates/error.html` instead of leaking a stack trace to the browser.

## Why this shape

- **One process, one SQLite file.** At this scale (a handful of internal
  users operating one ISMS program) a boring monolith is simpler to run,
  reason about, and back up than any distributed alternative. See
  `docs/decisions/architectural-decisions.md`.
- **Server-rendered Jinja2, not a JS frontend.** There's no interaction in
  this PR complex enough to need client-side state; plain forms + redirects
  are simpler to test and reason about. HTMX is deliberately not used yet —
  nothing here needs partial-page updates.
- **`app/routers/` mirrors the nav, one module per area.** Reading the nav
  in `app/main.py::NAV_ITEMS` tells you which router to open.
- **`placeholders.py` uses a catch-all `/{slug}` route** for the five
  not-yet-built nav areas (Policies, Evidence, Actions, Connectors, Trust
  Center) instead of five near-identical router modules. Because it's a
  catch-all, it is registered **last** in `create_app()` — anything
  registered after it would be silently shadowed. If you add a new
  concrete route, add it before the `placeholders.router` include, or add
  a regression test asserting the new route resolves correctly.
- **No repository/service layer.** Routers call SQLAlchemy directly. With
  one process and one database, an extra layer here would only wrap the
  ORM without adding a real seam — see the "no generic abstractions"
  constraint in `CLAUDE.md`.

## Persistence

- `GRC_DATABASE_PATH` (default `./data/grc.db`) points at a single SQLite
  file. `app/db.py::build_engine` creates the parent directory if missing.
- In Docker, mount a volume at `/data` (the Dockerfile sets
  `GRC_DATABASE_PATH=/data/grc.db`) so the database survives container
  restarts and rebuilds. See README.md for the exact `docker run` command.
- No Alembic yet — see `docs/decisions/architectural-decisions.md` for why,
  and what would trigger introducing it.

## Testing

- `tests/conftest.py` builds a fresh `FastAPI` app per test via
  `create_app(database_path=<tmp file>)`, so tests never touch the real
  database and don't interfere with each other.
- `tests/test_pages.py` hits every nav-visible route once, so a template or
  routing regression fails loudly.
- `tests/test_framework_control_relationship.py` exercises the
  requirement↔control many-to-many mapping directly against the ORM,
  independent of the HTTP layer.
