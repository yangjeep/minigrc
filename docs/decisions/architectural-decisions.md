# Architectural Decisions

Key decisions for playground-grc and their rationale. Append new decisions
here rather than editing past ones.

## 1. Boring monolith: one FastAPI process, one SQLite file

**Decision:** Single Python process, server-rendered Jinja2, SQLite via
SQLAlchemy. No microservices, no queue, no separate frontend build.

**Rationale:** The actual load is a handful of internal users operating one
ISMS program. Every distributed-systems concern (service boundaries,
network calls, eventual consistency) would be pure overhead here. See
`CLAUDE.md` constraint #1.

## 2. No Alembic yet

**Decision:** `app/db.py::init_db` uses `Base.metadata.create_all`. No
migration tool is wired in for this PR.

**Rationale:** The schema is five tables, all newly created, with no
production data to migrate. Alembic adds real value once there's a
deployed database whose schema must change without data loss — introduce
it at that point, not preemptively.

**Trigger to revisit:** The first schema change after a real deployment has
data in it.

## 3. IDs are hex-encoded UUID4 strings, not autoincrement integers

**Decision:** `app/models.py::new_id()` generates a 32-character hex string
per row, stored as the primary key.

**Rationale:** Stable across export/import and safe to reference from an
external system (e.g. a future connector or Trust Center export) without
collision risk. A true ULID (lexicographically sortable by creation time)
would be a small upgrade — deferred because nothing in this PR depends on
sort-by-id ordering (rows are ordered by `created_at` where order matters).

## 4. Requirement↔control mapping is many-to-many, not a foreign key

**Decision:** `ControlRequirementMapping` is a join table between
`InternalControl` and `FrameworkRequirement`.

**Rationale:** One control commonly satisfies multiple requirements, and
the reverse holds too as frameworks grow. A single FK on either side would
misrepresent real ISMS practice from the start. See
`docs/domain/domain-model.md`.

## 5. Evidence, Policies, Actions, Connectors, Trust Center are placeholder
   pages, not empty tables

**Decision:** These areas render a static status page (`app/routers/
placeholders.py`) instead of a modeled-but-unused database table.

**Rationale:** An empty table with no read or write path is worse than no
table — it implies a commitment the code doesn't back up, and it's the
kind of speculative schema `CLAUDE.md` explicitly asks agents to avoid.
Each placeholder states its intended source of truth so the next PR has a
clear starting point.

## 6. No authentication or multi-tenancy

**Decision:** No login, no sessions, no `org_id` column, no roles.

**Rationale:** Single internal deployment on a trusted network. Adding auth
speculatively would add real complexity (session management, password/SSO
integration, authorization checks on every route) with no current user of
that complexity. Revisit the moment this app is exposed beyond a trusted
internal network or needs to distinguish between users.

## 7. Audit events are written explicitly, not derived from an ORM hook

**Decision:** `app/audit.py::record_audit_event` is called explicitly
alongside each mutation that matters to an auditor (seeding, creating a
risk, mapping a control to a requirement).

**Rationale:** A generic "log every ORM flush" hook would capture noise
(e.g. internal housekeeping writes) and produce audit entries with
computer-generated, not human-readable, detail text. Explicit calls keep
the audit log meaningful to an auditor reading it directly.
