# Product Scope

## What this is

A lightweight, internal ISMS/GRC application for operating an ISO 27001
program: tracking frameworks, internal controls, risks, and an audit trail
in one place, without duplicating tools that already do their job well.

## Explicit non-goals

- **Not a Vanta, Drata, Wiz, or Aikido replacement.** This app will never
  try to match their breadth. If a feature request looks like "build a
  general compliance automation platform," that's a signal to push back,
  not to scope it in.
- **Not a vulnerability scanner.** Aikido owns vulnerability management.
  This app may eventually surface Aikido's output as evidence, but it will
  never run scans itself.
- **Not an auth/identity system.** No login, sessions, roles, or
  multi-tenancy in this phase. Single internal deployment, trusted network.
- **Not a document editor.** Policies live in Google Drive; this app will
  index/reference them, not replace Drive as the authoring tool.
- **Not a task tracker.** Corrective actions and exceptions link to Asana
  tasks; Asana remains the system of record for task state.
- **Not an object store.** Evidence *metadata* and snapshots will live here
  eventually; large evidence files will live in object storage in a future
  PR, not in this app's SQLite file.
- **Not a certifying body.** This software cannot grant ISO 27001
  certification. Certification is performed by an accredited external
  certification body; this app only helps an organization operate and
  evidence its own ISMS.

## Feature area status

| Area | Status (this PR) | Source of truth |
|------|-------------------|------------------|
| Frameworks / Requirements | Implemented — list, detail | Internal (this app), seeded with placeholder content |
| Internal Controls | Implemented — list, detail, map to requirements | Internal (this app) |
| Risks | Implemented — structured register, list + create | Internal (this app) |
| Audit Log | Implemented — real event history | Internal (this app) |
| Policies | Placeholder page only | External — Google Drive |
| Evidence | Placeholder page only | Internal metadata (future); large files in object storage (future) |
| Actions (corrective actions / exceptions) | Placeholder page only | External — Asana |
| Connectors (GitHub, AWS, Azure, Google Workspace, Asana) | Placeholder page only | External systems; this app will store results |
| Trust Center | Placeholder page only | Internal — curated subset of this app's data |
| Vulnerabilities | Out of scope, intentionally | External — Aikido |

## Why controls and requirements are separate

A framework requirement is "what the standard asks for." An internal
control is "what we actually do." One control commonly satisfies several
requirements (e.g. one policy-publication control can satisfy several
Annex A organizational requirements), and the same requirement could
eventually be satisfied by more than one control. That's a many-to-many
relationship, modeled as `ControlRequirementMapping` — see
`docs/domain/domain-model.md` for the full research behind this.

## Next PR candidates

In rough priority order, based on what this foundation makes buildable
next:
1. Evidence metadata + snapshot model, linked to a control.
2. First real connector (most likely GitHub, since it's the lowest-friction
   API to authenticate against) using the plain "connection test + checks +
   evidence output" module shape described in `CLAUDE.md`.
3. Risk treatment as a distinct workflow (currently a free-text field on
   `Risk`) once a second risk-adjacent workflow (exceptions) makes the
   shared shape clear.
4. Policy index referencing Google Drive documents by ID, with version/review
   tracking layered on top rather than re-hosting documents.
