# Product Scope

## What this is

A lightweight, self-hosted ISMS/GRC application for one organization
operating one internal compliance program: a framework checklist with
notes and history, a local policy repository with versioning, a structured
risk register, internal controls, and an audit trail — in one place,
without duplicating tools that already do their job well.

## Explicit non-goals

- **Not a Vanta, Drata, Wiz, or Aikido replacement.** This app will never
  try to match their breadth. If a feature request looks like "build a
  general compliance automation platform," that's a signal to push back,
  not to scope it in.
- **Not a vulnerability scanner.** Aikido owns vulnerability management.
  This app may eventually surface Aikido's output as evidence, but it will
  never run scans itself.
- **Not a multi-tenant platform.** One deployment operates one
  organization's program. No org switching, no per-user roles beyond
  "logged in or not" in this MVP.
- **Not a hosted identity provider.** Login is local (email + password,
  Argon2-hashed, server-side sessions). No SSO, no OAuth, no
  self-registration — the first user is created via
  `python -m app.cli create-user`.
- **Not a document editor.** Policies are authored elsewhere (Word, Google
  Docs, etc.) and uploaded here as finished PDF/DOCX files for versioned
  storage and review tracking — this app never edits document content.
- **Not a task tracker.** Corrective actions and exceptions link to Asana
  tasks; Asana remains the system of record for task state.
- **Not an object store.** Policy documents are stored as local files under
  `GRC_DATA_DIR/policies/`, not in cloud object storage — sufficient for
  the single-instance, single-organization scale this app targets. Evidence
  *metadata* may live here in a future PR; large evidence files would still
  need an object-storage decision this PR does not make.
- **Not a certifying body.** This software cannot grant ISO 27001
  certification. Certification is performed by an accredited external
  certification body; this app only helps an organization operate and
  evidence its own ISMS.

## Feature area status

| Area | Status (this PR) | Source of truth |
|------|-------------------|------------------|
| Authentication | Implemented — local email/password, server-side sessions, optional Google OIDC login | Internal (this app), identity optionally asserted by Google |
| Frameworks / Requirements | Implemented — checklist, manual add, CSV import | Internal (this app), seeded with placeholder content |
| Requirement assessments & notes | Implemented — applicable/state/owner, append-only notes, audit history | Internal (this app) |
| Policies | Implemented — versioned PDF/DOCX repository, review dates, optional Google Drive source association + capture + approval history | Internal (this app), optionally sourced from Google Drive |
| Internal Controls | Implemented — list, detail, map to requirements | Internal (this app) |
| Risks | Implemented — structured register, validated bounds | Internal (this app) |
| Evidence | Implemented — immutable snapshots, maps to requirements/controls | Internal (this app), sourced from AWS today |
| AWS connector | Implemented — CloudTrail logging posture + basic IAM hygiene evidence, not a CSPM | External — AWS (ambient credentials or AssumeRole; never stores long-lived keys) |
| Audit Log | Implemented — real event history | Internal (this app) |
| People directory | Implemented — manual entries; optional Google Workspace Directory sync | Internal (this app), optionally synced from Google Workspace |
| Vendor/System register | Implemented — one record per purchased/used system, operational flags, renewals view | Internal (this app) |
| Vendor roster snapshots | Implemented — append-only CSV import per vendor, delta view, Person matching, admin-only linking | Internal (this app), sourced from the vendor's own export |
| Actions (corrective actions / exceptions) | Placeholder page only | External — Asana |
| Connectors (GitHub, Azure, Asana) | Placeholder page only | External systems; this app will store results |
| Trust Center | Admin implemented (settings, sections, publish/unpublish, preview) — public unauthenticated route not yet built | Internal — curated subset of this app's data |
| Vulnerabilities | Out of scope, intentionally | External — Aikido |

## Why controls and requirements are separate

A framework requirement is "what the standard asks for." An internal
control is "what we actually do." One control commonly satisfies several
requirements (e.g. one policy-publication control can satisfy several
Annex A organizational requirements), and the same requirement could
eventually be satisfied by more than one control. That's a many-to-many
relationship, modeled as `ControlRequirementMapping` — see
`docs/domain/domain-model.md` for the full research behind this. A user is
never forced to create an `InternalControl` just to check off a
requirement — the `RequirementAssessment` (applicable/state/owner) is
independent of any control mapping.

## Why policies are stored locally, not indexed from Google Drive

An earlier iteration of this app treated Google Drive as the source of
truth for policies and only planned to index/reference documents by ID.
This MVP instead stores uploaded PDF/DOCX files directly, because the
product requirement is specifically **versioned, auditable storage with
immutable history** (upload → review → new version → old versions still
downloadable) — a plain external reference wouldn't give an auditor
confidence that a "reviewed" version is the same bytes that were reviewed.
Object storage (S3-compatible) is a reasonable future upgrade once this
app needs to scale past local disk; it is out of scope for this PR (see
`docs/decisions/architectural-decisions.md`).

**Google Drive remains the source of truth for policy *authoring*.** A
later PR on `feat/startup-compliance-operations` added an *optional*
association between a `Policy` and a Drive file, plus a "Capture current
version" action that downloads/exports the file's current content through
the exact same validated storage pipeline as a manual upload. This is
metadata association and on-demand capture, not indexing-as-storage — the
locally captured, immutable `PolicyVersion` bytes remain the authoritative
record regardless of what happens to the Drive file afterward. Google
itself does not guarantee complete or permanent Drive revision history
(see the Drive Revisions API docs), so this app never relies on Drive as
archival storage.

## Architecture pivot (platform/production scale)

Starting on `feat/platform-trust-center-pivot`, miniGRC's target deployment
scale widens: Postgres, a worker process, Kubernetes/Helm, an external
database connector interface, and a Trust Center become first-class scope
— by explicit decision, not scope creep. See ADR #23 in
`docs/decisions/architectural-decisions.md` for exactly what changes and
what stays the same (single-tenant, binary admin/user roles, local session
auth remain unchanged).

## Next PR candidates

`feat/startup-compliance-operations` delivered evidence metadata + AWS
CloudTrail/IAM as the first real connector, Google Drive/OIDC/Workspace
Directory, admin authorization, People, and the Vendor/System register —
see that branch's worklog entries and ADRs #12–#20 for what shipped and
why. Remaining candidates, in rough priority order:

1. A second real connector (GitHub is still the lowest-friction next
   API), using the same "connection test + checks + evidence output"
   module shape as `app/aws_connector.py`.
2. Risk treatment as a distinct workflow (currently a free-text field on
   `Risk`) once a second risk-adjacent workflow (exceptions) makes the
   shared shape clear.
3. Object storage for policy files, once local-disk storage is a real
   constraint (multi-instance deployment, large file volume).
4. A route for a Google-created user (via OIDC) to set a local password,
   if break-glass access without Google is needed for that account.
