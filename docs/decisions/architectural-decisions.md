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

## 8. Local session authentication, not JWTs or a hosted identity provider

**Decision:** `app/models.py::User`/`UserSession`, `app/security.py`,
`app/deps.py::require_login`. Passwords hashed with `pwdlib[argon2]`.
Sessions are opaque `secrets.token_urlsafe(32)` tokens; only a SHA-256 hash
is stored server-side (`UserSession.token_hash`); the raw token lives only
in an `HttpOnly`/`SameSite=Lax` cookie.

**Rationale:** This app now handles potentially sensitive policy documents,
so "no auth" (decision #6, superseded) is no longer acceptable. A JWT would
either be stateless (can't revoke before expiry — wrong for logout) or need
a server-side denylist anyway, at which point a plain session table is
simpler and gives immediate revocation. A hosted identity provider (Clerk,
Auth0, Firebase, WorkOS, Keycloak, Authentik) would add an external runtime
dependency this self-hosted, single-organization tool doesn't need. All
authenticated users share the same permissions in this MVP — no RBAC yet;
revisit when a second concrete need for differentiated access exists.

**Supersedes:** Decision #6 ("No authentication or multi-tenancy") — the
multi-tenancy half of that decision still stands; the no-auth half does not.

## 9. CSRF via a double-submit cookie, independent of the login session

**Decision:** A `csrf_token` cookie is set for every request (even
unauthenticated ones) by middleware in `app/main.py`. Every state-changing
form includes a hidden `csrf_token` field; `app/deps.py::verify_csrf`
compares the two with `secrets.compare_digest`.

**Rationale:** Tying CSRF tokens to the login session would leave the login
form itself unprotected (no session exists yet at that point). A cookie
issued independent of login covers every form, including `/login`, with
one mechanism. `SameSite=Lax` on both cookies already blocks most
cross-site submission; the explicit token is defense in depth without
needing a signing secret.

## 10. Alembic migrations replace `Base.metadata.create_all`

**Decision:** `app/db.py::init_db` runs `alembic upgrade head`
programmatically (building an `alembic.config.Config` pointed at the
resolved database URL) instead of `Base.metadata.create_all`. One
migration (`migrations/versions/..._initial_mvp_schema.py`) represents the
full MVP schema.

**Rationale:** This PR turns the app from a throwaway skeleton into
something meant to hold real, persistent data (policy documents, audit
history). `create_all` cannot express a schema *change* against an
existing database with data in it — the trigger named in decision #2 (“the
first schema change after a real deployment has data in it”) is exactly
what this PR is doing. Using `init_db()` as the single call site for both
dev and Docker (via `python -m app.cli migrate`) keeps one
schema-initialization path rather than two competing ones.

**Supersedes:** Decision #2 ("No Alembic yet").

## 11. Policies are stored as local, versioned files — not indexed from Drive

**Decision:** `app/models.py::Policy`/`PolicyVersion`, `app/storage.py`.
Uploaded PDF/DOCX files are validated by content (not just extension),
written to a temp file while hashing, then atomically moved into
`GRC_DATA_DIR/policies/<policy id>/<version number>/`. Versions are never
overwritten or deleted by the application.

**Rationale:** The product requirement is auditable version history — "the
document an auditor reviewed is provably the same bytes as what's stored
today." A Drive-reference model (decision #5's original policy placeholder)
can't guarantee that, since the referenced document can change out from
under the review record. Local disk is sufficient at this app's target
scale (one organization); object storage is a reasonable future upgrade,
not a requirement for this PR — see `docs/product-scope.md`.

**Supersedes:** The policy portion of decision #5 ("Policies... are
placeholder pages, not empty tables") — Evidence, Actions, Connectors, and
Trust Center remain placeholders; Policies do not.

## 12. Binary admin authorization, not general RBAC

**Decision:** `User.role` is `"user"` or `"admin"` — nothing more granular.
The first user ever created via `python -m app.cli create-user` becomes
admin automatically; every subsequent user is `"user"` unless promoted with
`python -m app.cli promote-admin --email ...` (no password involved, so it
never needs to touch a credential). `app/deps.py::require_admin` gates
integration configuration, credential connections, manual syncs, and
destructive vendor operations only — every other authenticated route
remains available to any logged-in user, unchanged from decision #8.

**Rationale:** This PR introduces the app's first credential-adjacent
surfaces (Google OAuth tokens, AWS role ARNs) that genuinely need to be
restricted to a smaller set of people than "logged in at all," which
decision #8 explicitly named as the trigger for revisiting no-RBAC. A
second permission tier is the smallest change that satisfies that need — a
full permission matrix would be exactly the kind of generic abstraction
`CLAUDE.md` asks agents to avoid until a second concrete need for it
exists.

**Supersedes:** The "no RBAC" portion of decision #8 for credential/admin
surfaces specifically; GRC data access remains identical for every
authenticated user.

## 13. VendorSystem is one model, not separate Vendor/Application tables

**Decision:** `app/models.py::VendorSystem` represents one purchased/used
system end to end — identity, access continuity, cost, contract/renewal,
support — rather than a `Vendor` row plus a separate `Application` row
joined together.

**Rationale:** This branch's product boundary explicitly treats "GitHub",
"Slack", "AWS" as single real-world things a startup tracks, not a vendor
entity distinct from an application entity with its own lifecycle. Splitting
them would be exactly the kind of generic abstraction `CLAUDE.md` asks
agents to avoid until a second concrete need (e.g. one vendor selling
multiple distinct applications this org uses separately) actually appears.
Operational warnings (missing admin, contract missing, renewal approaching,
etc.) are computed in `app/vendor_flags.py` from live data at request time,
not stored — a stored flag would just be a cache that could drift stale.

## 14. Person is a shared identity table, not per-feature user references

**Decision:** `app/models.py::Person` is one row per human, optionally
referenced by `User.person_id`, `VendorSystem`'s admin/owner fields, and
(in a later commit on this branch) vendor roster snapshot rows — instead of
each feature area inventing its own "who is this" reference.

**Rationale:** Multiple upcoming features (vendor admin tracking, roster
import matching, optional Workspace Directory sync) all need to answer "is
this email a current employee?" A single shared table with an
`employment_status` answers that once. `employment_status` starts
`"unknown"`, not `"active"` — nothing has confirmed it until an explicit
source (manual edit or a directory sync) says so, and it's never inferred
or deleted from a missing sync record, only updated by explicit source
data — preserving history rather than guessing.

## 15. Vendor roster imports are append-only snapshots, validated wholesale

**Decision:** `app/vendor_roster_import.py::import_vendor_roster_snapshot`
parses and validates the entire CSV (bounded read via `app/uploads.py`,
row-count cap, per-row validation, duplicate-normalized-email rejection)
before writing a single row. A successful import always creates a *new*
`VendorUserSnapshot` — no update or delete route exists for a past
snapshot or its rows, mirroring the immutable `PolicyVersion` pattern from
decision #11.

**Rationale:** A vendor roster export is evidence of who had access at a
point in time; overwriting it would destroy exactly the history an
auditor needs. The all-or-nothing validation mirrors
`app/csv_import.py::import_requirements_csv`'s existing approach (now
sharing its bounded-read helper — see `app/uploads.py`) — a partially
imported roster is worse than a rejected one. Linking an imported row to a
`Person` (admin-only) only ever sets `matched_person_id`; the imported
columns stay exactly as reported, so the evidentiary record and the
organization's interpretation of it stay separately auditable.

## 16. Google OIDC login is separate from local login, not a replacement

**Decision:** `app/google_oidc.py` + `app/routers/google_oidc.py` add
`/auth/google/login` and `/auth/google/callback`, disabled (404) unless
`GRC_GOOGLE_OIDC_CLIENT_ID`/`GRC_GOOGLE_OIDC_CLIENT_SECRET`/
`GRC_PUBLIC_BASE_URL` are all configured. Local email/password login
(`app/routers/auth.py`) is untouched and remains fully usable regardless —
explicit break-glass access if Google sign-in is ever unavailable or
misconfigured. Session issuance itself (`start_user_session`, extracted
from `auth.py`) is shared between both paths; only how a `User` is
identified differs.

**Rationale:** This is deliberately *not* a "replace local auth with SSO"
change — see CLAUDE.md's break-glass requirement. `google.oauth2.
id_token.verify_oauth2_token` handles signature/issuer/audience/expiry
against Google's current signing keys; this app is responsible for nonce
(replay protection across the redirect), `email_verified`, and hosted-
domain (`hd`) checks, since the email suffix alone is not proof of
Workspace membership — `hd` is a verified claim inside the signed token,
the domain of `email` is not. A first-time Google sign-in creates a local
`User` row (linked to an existing `Person` by normalized email if one
exists) rather than requiring a separate SSO-account model — this app
still has exactly one `User` table regardless of how a session started.

**Non-goal:** SAML. OIDC covers Google Workspace sign-in with drastically
less protocol surface (no XML signing, no metadata exchange, no ACS
endpoint) for the one identity provider this app is scoped to support.

## 17. One org-level Google Drive connection, encrypted at rest, distinct from OIDC login

**Decision:** `app/models.py::GoogleDriveConnection` is an append-only
history table (like `PolicyVersion`) — connecting again always creates a
new row; the active connection is the most recent row with `revoked_at
IS NULL`. The refresh token is encrypted with Fernet
(`app/crypto.py`, requiring `GRC_ENCRYPTION_KEY`) before it's stored — the
plaintext token is never in the database, a log line, a template, or an
`AuditEvent.detail` string. Connect/disconnect/manual-sync-triggering
actions (policy Drive-link/capture) require `require_admin`. The OAuth
client credentials (`GRC_GOOGLE_DRIVE_CLIENT_ID/_SECRET`) are configured
separately from the OIDC login credentials
(`GRC_GOOGLE_OIDC_CLIENT_ID/_SECRET`) — see decision #16's non-negotiable
that Drive authorization stay distinct from OIDC authentication, even
when an operator points both at the same Google Cloud project.

**Rationale:** Unlike OIDC login (per-user, ephemeral session), the Drive
connection is a standing credential shared by the whole organization —
exactly the kind of secret CLAUDE.md requires admin-gating and encryption
for. Calling the Drive API v3 directly over HTTPS with `httpx` plus
`google.oauth2.credentials.Credentials`/`google.auth.transport.requests`
for the refresh-token grant (rather than adding
`google-api-python-client`'s discovery-document-based client) keeps the
dependency surface small and every call's shape explicit and easy to
mock in tests — this app only ever calls three or four fixed Drive v3
endpoints (`files.get`, `files.get?alt=media`, `files.export`,
`files.list` for revisions), not the full Drive API surface a generic
client would expose.

## 18. Policy/PolicyVersion source provenance without trusting Drive as archival storage

**Decision:** `Policy` gained `source_type`/`drive_*` fields;
`PolicyVersion` gained `source_type`/`source_file_id`/
`source_revision_id`/`source_modified_at`/`captured_at`. Capturing a Drive
file's content reuses `app/storage.py`'s existing validated
write-then-atomically-move pipeline (refactored into
`_save_policy_version`, shared by `save_policy_version_upload` and the new
`save_policy_version_from_bytes`) — same content-type validation, size
bound, SHA-256 hashing, and immutability guarantee as a manual upload.
`app/google_drive.py::parse_drive_file_id` only ever extracts a file ID
from user input; it never fetches the user-supplied value as a URL,
avoiding SSRF via a crafted "Drive link."

**Rationale:** The spec is explicit that Drive's revision history is not
guaranteed complete or permanent (Google's own documentation says so) —
so provenance fields are additive context ("what did Drive say this was
at capture time"), never a replacement for the locally stored,
content-hashed, immutable bytes that remain this app's actual evidence.
Google Docs/Sheets/Slides are exported to PDF (the one deterministic,
archival-appropriate format specified) rather than stored as
Google-proprietary formats with no independent viewer.

**Supersedes:** Nothing — extends decision #11 (local, versioned policy
storage) with an optional capture source; local storage remains
authoritative either way.

## 19. Drive Approvals is best-effort and never blocks a policy sync

**Decision:** `app/google_drive_approvals.py::fetch_approvals` raises a
single `ApprovalsUnavailableError` for any failure — missing scope, 403,
404, malformed response, or a tenant where the Approvals API doesn't
apply — and `app/routers/policies.py::sync_drive_approvals` catches it and
shows "Approval data unavailable" rather than surfacing an error that
looks like *this app* is broken. `PolicyApprovalSnapshot` rows are
append-only, deduplicated only on an exact-unchanged re-sync (matching
`external_approval_id` + `raw_payload_sha256`) — a real status change
(e.g. pending → approved) always creates a new row rather than mutating
the prior one, and is associated with the specific `PolicyVersion` it was
synced against.

**Rationale:** Google's own Approvals API documentation is explicit that
tenant availability, permissions, and behavior vary — this is a
genuinely optional capability, not a guaranteed one, and the spec for
this branch says so directly. Treating any failure as fatal would make
an unrelated, unsupported tenant configuration block ordinary policy
capture/sync work. This app never builds an internal approval workflow
or auto-approves/declines anything — it only mirrors what Drive already
recorded.

## 20. Optional Workspace Directory sync piggybacks on the Drive connection

**Decision:** `app/google_workspace_directory.py` requests the read-only
`admin.directory.user.readonly` scope as an *additional* scope on the
same Google Drive OAuth connection (`GRC_GOOGLE_WORKSPACE_DIRECTORY_ENABLED=true`
adds it to the consent screen), rather than a third separate OAuth flow.
`sync_directory_users` only creates/updates `Person` rows for users the
Directory API actually returns — it never deletes a `Person` missing from
a sync (e.g. a manually added contractor), preserving history per
decision #14.

**Rationale:** A third distinct OAuth connection for one small read-only
scope would be disproportionate complexity for what's explicitly framed
as "an optional extension of the Google integration, with a clean manual
fallback." Piggybacking keeps exactly one Drive-related credential to
manage, encrypt, and revoke, while still keeping the scope itself
optional and separately toggleable. If the scope isn't granted (not
enabled, or an admin declined it at consent), `Person` records stay fully
manual — nothing in `app/routers/people.py` depends on this sync having
ever run.

## 21. AWS evidence is two fixed check families, not a CSPM

**Decision:** `app/aws_connector.py` implements exactly two checks —
`check_cloudtrail` (at least one actively-logging trail; multi-region,
global-service-events, and log-file-validation as warnings, not
failures) and `check_iam` (root MFA/access keys, account password
policy presence, per-user MFA and access-key age) — using the standard
AWS credential provider chain with optional `AssumeRole`. No GuardDuty,
Security Hub, Config, S3/RDS/EBS posture, or general resource scanning.
A failed API call (`ClientError`/`BotoCoreError`) always produces
`status="unknown"`, never `"fail"` — this app cannot distinguish "the
control is broken" from "I don't have permission to check it," and
conflating the two would misrepresent posture to an auditor.

**Rationale:** The spec for this branch is explicit that this is "not a
CSPM" — CloudTrail logging posture and basic IAM hygiene are the two
check families named. `AwsConnection` never stores a long-lived AWS
access key (only an optional `role_arn` + encrypted `external_id` for
`AssumeRole`); ambient workload credentials are the default path,
matching how this app would actually run (ECS/EC2/Lambda instance role)
rather than requiring an operator to mint and rotate static keys just to
run evidence checks.

## 22. EvidenceSnapshot is a single shared, immutable table across sources

**Decision:** `app/models.py::EvidenceSnapshot` (with
`EvidenceRequirementMapping`/`EvidenceControlMapping` join tables) is the
one evidence table both AWS checks and any future evidence source write
to — no per-source evidence table. No edit/delete route exists; a
correction is always a new snapshot. `normalized_payload_json` is bounded
(20,000 characters) and never contains a raw credential; a
`raw_payload_sha256` lets an auditor confirm a snapshot's integrity
without this app needing to retain a full raw API response.

**Rationale:** The spec explicitly names `EvidenceSnapshot` as "a small
shared model...allowed because both Google and AWS will produce
evidence" — this is the one deliberate exception to "no generic
abstraction without a second caller" in this branch, called out because
two concrete sources (Drive approvals took its own dedicated
`PolicyApprovalSnapshot` table instead, since it's structurally
different: revision-scoped, policy-specific evidence vs. this table's
point-in-time infrastructure posture checks) genuinely need the same
immutable-snapshot-with-mappings shape.

## 23. Architecture pivot: platform/production scale supersedes decision #1's scale framing

**Decision:** miniGRC's target scale and architecture assumptions widen,
by explicit user request, to support: Postgres as a first-class supported
database alongside SQLite; a worker process for background jobs (imports,
connection tests, future evidence collection); Kubernetes/Helm as a
supported deployment target; an external-database connector interface for
read-only evidence/inventory collection from customer systems; AG Grid
Community replacing hand-rolled tables for register-like entities
(Frameworks, Controls, Risks, Assets). Decision #1's framing ("every
distributed-systems concern... would be pure overhead here") and the
single-tenant *scale* assumption in `docs/product-scope.md` are superseded
for these specific subsystems.

**What does not change:** single organization per deployment (no
`org_id`, no org switching — the multi-tenancy half of decision #6 still
stands), binary admin/user roles (decision #12), local session + Argon2
auth with optional Google OIDC (decisions #8/#16 — no JWT, no additional
hosted identity provider), Alembic migrations (decisions #2/#10), explicit
`AuditEvent` writes alongside mutations (decision #7), CSRF double-submit
cookie (decision #9), hex-UUID4 ids (decision #3), immutable
evidence/policy snapshot patterns (decisions #11/#15/#22). SQLite remains
fully supported for local development and small deployments — it is not
deprecated by adding Postgres support.

**Rationale:** This is a deliberate, explicit scale/deployment pivot, not
an auth/security/data-model pivot. Recorded here so a future agent
reviewing `CLAUDE.md`'s "boring monolith" framing or decision #1 doesn't
"fix" the codebase back toward the original single-process assumption
without realizing it was superseded on purpose. Each superseding
subsystem (Postgres, worker, connector interface, Helm chart, grid
framework) gets its own dedicated ADR entry as it ships, rather than this
entry pre-specifying implementation details it doesn't yet have.

**Supersedes:** The scale-framing portion of decision #1 and the
single-tenant *scale* assumption in `docs/product-scope.md`. Does not
supersede any auth, audit, CSRF, id-generation, or immutability decision
listed above.
