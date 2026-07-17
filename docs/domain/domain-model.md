# Domain Model & Research Notes

## Copyright boundary (read first)

ISO/IEC 27001's normative clause and Annex A control text is copyrighted by
ISO/IEC and is not reproduced anywhere in this repository. Everything this
app seeds or stores as "requirement" or "control" content is either:

- a **placeholder** written for this repository (clearly flagged via
  `Framework.is_placeholder_content` and a UI notice on the framework
  detail page), or
- **user-supplied content** an organization enters after licensing its own
  copy of the standard.

Public, secondary-source overviews of the *structure* of ISO/IEC 27001:2022
(not its normative text) were used to shape the schema:

- ["ISO 27001 Annex A Controls Explained: All 93 Controls Overview"](https://www.glocertinternational.com/resources/articles/iso-27001-annex-a-controls-explained/)
- ["ISO 27001:2022 Annex A Controls List"](https://www.scrut.io/hub/iso-27001/iso-27001-controls)
- ["Understanding ISO/IEC 27001:2022 Annex A Controls"](https://pecb.com/en/article/understanding-isoiec-270012022-annex-a-controls) — PECB
- ["ISO 27001 Controls Explained: A Guide to Annex A"](https://secureframe.com/hub/iso-27001/controls) — Secureframe

This app does not grant, imply, or perform ISO 27001 certification.
Certification is issued only by an accredited external certification body.

## Structural summary used to design the schema

ISO/IEC 27001:2022 separates:

- **Clauses 4–10**: management-system requirements (context, leadership,
  planning, support, operation, performance evaluation, improvement) —
  process requirements for running an ISMS, not modeled as data in this PR.
- **Annex A**: 93 controls across 4 themes — Organizational (37),
  People (8), Physical (14), Technological (34) — replacing the 14-domain
  structure of the 2013 edition. An organization selects applicable
  controls into a Statement of Applicability (SoA).

The seeded sample catalogue in `app/seed.py` picks five representative
reference codes across these themes (e.g. `A.5.1`, `A.8.1`, `A.8.8`) purely
to exercise the framework→requirement→control relationships end to end. It
is not a full catalogue and is not meant to be one — see
`docs/product-scope.md` for the "import-ready structure, not manual
transcription" decision.

## Entities and relationships

```
User 1───* UserSession

Framework 1───* FrameworkRequirement 1───1 RequirementAssessment
                       │        │
                       │        └───* RequirementNote
                       │ *
                       ▼
              ControlRequirementMapping
                       ▲
                       │ *
                       │
InternalControl 1──────┘

Policy 1───* PolicyVersion   (versions are immutable; never overwritten)

Risk            (standalone register, no FK to controls yet)
AuditEvent      (standalone log, references other entities by id + type)
```

| Term | Definition used in this app | Modeled? |
|------|------------------------------|----------|
| User | A local login identity (email + password hash) | Yes — `User` |
| User session | A server-side record of one logged-in browser session | Yes — `UserSession` |
| Framework | A named compliance framework + version (e.g. "ISO/IEC 27001:2022") | Yes — `Framework` |
| Framework requirement | One requirement/clause/control reference within a framework | Yes — `FrameworkRequirement` |
| Requirement assessment | The organization's applicability/implementation-state/owner for one requirement | Yes — `RequirementAssessment` |
| Requirement note | Append-only note explaining a requirement's status or a decision | Yes — `RequirementNote` |
| Internal control | What the organization actually does to address one or more requirements | Yes — `InternalControl` |
| Requirement-to-control mapping | Many-to-many link between a control and the requirements it satisfies | Yes — `ControlRequirementMapping` |
| Control owner | Person/role accountable for a control | Yes — `InternalControl.owner` (plain string) |
| Control status | Where a control stands: not_started / in_progress / implemented / needs_review | Yes — `InternalControl.status` |
| Control review frequency | How often a control is expected to be reviewed | Yes — `InternalControl.review_frequency` |
| Policy | A governance document (e.g. an information security policy) | Yes — `Policy` |
| Policy version | A specific, immutable revision of a policy document | Yes — `PolicyVersion` |
| Evidence | Metadata proving a control operated (e.g. a screenshot, export, log excerpt) | Not modeled yet — placeholder page |
| Evidence snapshot | A point-in-time capture of evidence | Not modeled yet |
| Risk | A structured risk register entry: likelihood, impact, owner, status, treatment | Yes — `Risk` |
| Risk treatment | The plan to reduce/accept/transfer/avoid a risk | Partially — `Risk.treatment_plan` is free text; not a distinct entity yet |
| Exception | A time-boxed deviation from a control/policy | Not modeled yet |
| Corrective action | Work item remediating a finding | Not modeled yet — Asana is the source of truth |
| Audit event | Who changed what, when, for auditor-facing history | Yes — `AuditEvent` |
| Person | Shared identity reference (vendor admins, roster rows, MiniGRC users, Workspace Directory sync) | Yes — `Person` |
| Vendor/System | One system the org purchases or uses (GitHub, Slack, AWS, ...) — identity, access continuity, cost, contract/renewal, support | Yes — `VendorSystem` |
| Vendor user roster snapshot | Append-only, immutable capture of a vendor's reported user list at one point in time | Yes — `VendorUserSnapshot` / `VendorUserSnapshotRow` |
| Google Drive connection | One org-level, admin-managed, encrypted-at-rest OAuth connection used to read Drive file metadata/content | Yes — `GoogleDriveConnection` |
| Policy approval snapshot | Append-only mirror of one Google Drive Approvals API record for a PolicyVersion — optional, best-effort | Yes — `PolicyApprovalSnapshot` |
| AWS connection | Configuration (account label, expected account ID, optional AssumeRole ARN + encrypted external ID, regions) for one AWS account this app collects evidence from | Yes — `AwsConnection` |
| Evidence snapshot | Immutable, point-in-time capture of evidence from an external source (AWS CloudTrail/IAM today), mappable to requirements/controls | Yes — `EvidenceSnapshot` (+ `EvidenceRequirementMapping`/`EvidenceControlMapping`) |

## Why some entities were deferred

- **Evidence / Evidence snapshot**: needs an object-storage decision this
  PR intentionally punts on (see `docs/decisions/architectural-decisions.md`).
  Building the metadata table without a storage backend to point at would
  produce an empty, untested table.
- **Risk treatment / Exception / Corrective action** as distinct entities:
  each is a workflow (states, transitions, approvals) rather than a
  record. Modeling one workflow in isolation, before a second workflow
  exists to compare it against, risks guessing at a shape that won't fit.
  `Risk.treatment_plan` (free text) covers the PR's actual need: proving
  the risk register is usable.
- **Policy / Policy version** were deferred in the original foundation PR
  (Google Drive was treated as the source of truth) but are now modeled in
  this PR — see `docs/product-scope.md` for why local, versioned storage
  replaced the Drive-indexing plan.

## Requirement assessment model

`RequirementAssessment` is one-to-one with `FrameworkRequirement`, created
alongside it (`app/requirements.py::add_requirement`) so a requirement is
never in a state with no assessment row. Fields:

- `applicable`: `"yes"` or `"no"`. Marking `"no"` requires a `RequirementNote`
  explaining why, written in the same transaction as the assessment change.
- `implementation_state`: `not_started` / `in_progress` / `implemented`.
  Only meaningful when `applicable == "yes"`.
- `owner`, `last_reviewed_at`, `last_reviewed_by`: free-text owner and an
  optional "mark as reviewed" stamp, set from the assessment form.

Completion percentage (`app/progress.py::compute_progress`) is
`implemented applicable requirements / all applicable requirements`, with
an explicit `None` ("N/A") result — not a divide-by-zero — when a framework
has no applicable requirements yet.

## Policy model

`Policy` holds metadata (title, description, owner, status, effective/next
review dates). `PolicyVersion` rows are immutable and monotonically
numbered per policy (`UniqueConstraint(policy_id, version_number)`) — the
application never updates or deletes a version, only adds new ones. See
`docs/architecture.md` "Policy storage" for the upload/validation pipeline.

## Likelihood/impact scale

`Risk.likelihood` and `Risk.impact` are plain integers 1–5, and
`Risk.score` is their product (1–25). This is intentionally not a
configurable risk matrix (custom scales, weighted categories, matrix
color-coding) — the seeded data and tests only need a sortable, comparable
score. Revisit if a real risk assessment methodology requires more nuance.
