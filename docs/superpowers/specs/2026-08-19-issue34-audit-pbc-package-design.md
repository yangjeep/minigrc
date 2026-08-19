# Issue #34: Defensible SOC 2 audit/PBC package export

Status: implemented. See docs/worklog/2026-08-19-issue34-audit-pbc-package.md
for the full verification account.

## 1. Repository-reality check

- `ComplianceScope.audit_period_starts_on`/`audit_period_ends_on`
  (#30) is the org-wide, singleton declared audit period — the natural
  "the period this package covers" concept. `ControlPeriod` (#11) is a
  different thing: a framework-neutral, potentially-multiple, per-
  control scheduling window that gates occurrence generation. A PBC
  package covers "the audit," not one control's scheduling window, so
  it exports against `ComplianceScope`'s period, not `ControlPeriod`.
- No existing multi-file export/manifest/zip-generation precedent exists
  for outbound data (`zipfile` is only used for docx content-sniffing in
  `app/storage.py`/`app/object_storage.py`). `app/import_directory.py`
  does write a small `manifest.json` for *imported* files — a naming/
  shape precedent to follow, not reusable code (it's for the opposite
  direction).
- The single-file download pattern to mirror exactly:
  `app/routers/evidence_artifacts.py::download_evidence_artifact_version`
  (`build_s3_client` → `download_object` → catch `ObjectStorageError` →
  `Response(content=..., media_type=..., headers={"Content-Disposition":
  ...})`) and `app/routers/policies.py::download_policy_version`
  (`policy_version_path(data_dir, ...)` → `os.path.isfile` check →
  `FileResponse`). A package export just needs to do both of these in a
  loop and zip the results — no new retrieval mechanism.
- `download_object` raises `ObjectStorageError` (a safe-to-display,
  credential-free exception) on any S3 failure — exactly the "fail
  clearly, don't produce a misleading complete package" signal this
  issue wants; the package generator must abort the whole export on the
  first one, not silently skip the missing item.
- `PolicyVersion` already carries everything needed to answer "what was
  effective when": `effective_at`, `superseded_at`,
  `superseded_by_version_id`, `lifecycle_status`. No new tracking needed.
- Confirmed the natural query path for this export never touches
  `Secret`, `GoogleDriveConnection.encrypted_refresh_token`,
  `AwsConnection.encrypted_external_id`, or either `secret_id` FK
  (`GoogleOidcSettings`, `ExternalConnection`) — "no secrets in the
  package" is a discipline (never join against those tables), not a
  filtering problem against data that would otherwise be in scope.
- `app/routers/audit_log.py` (`require_admin` router-level) is this
  repo's existing convention for "bulk/sensitive data leaves the system"
  — the package export route follows it.
- Genuinely greenfield — no existing design/worklog for "pbc",
  "audit package", "export", or "manifest" (export direction) anywhere.

## 2. Scope decisions

1. **Export against the current `ComplianceScope`'s declared audit
   period.** Fails clearly (no export possible) if no scope is defined
   yet — matches #30's own "scope incomplete" stage; there is nothing
   coherent to export before a period exists.
2. **No server-side persistence of generated package bytes.** The issue
   asks for "reproducible from authoritative state," not "miniGRC keeps
   a copy of every export ever made" — an auditor receiving the
   downloaded file is responsible for retaining their own copy, the same
   way a downloaded evidence file or policy PDF already works today. A
   `record_audit_event` entry (who generated a package, when, and the
   manifest's own SHA-256) is written instead — enough accountability
   trail without adding a new S3-object-per-export storage requirement
   this issue's acceptance criteria doesn't actually require.
3. **Scoping rule for what's *inside* the package**, applied once,
   consistently:
   - Frameworks/requirements/controls/policies are **org-wide, not
     period-filtered** — a control or policy exists once per
     deployment (RULES.md §1.11), it is not itself a period-scoped
     object, unlike an occurrence or a test.
   - `ControlOccurrence` rows are included when `due_at` **or**
     `performed_at` falls within `[audit_period_starts_on,
     audit_period_ends_on]`.
   - `ControlTest` rows are included when `performed_at` falls within
     the same window (a test is inherently a point-in-time activity,
     unlike a control's design).
   - `PolicyVersion`s included per policy: every version whose
     `effective_at <= period_end` and (`superseded_at is None` or
     `superseded_at >= period_start`) — i.e. every version that was
     effective at any point overlapping the period, which can correctly
     be more than one if a policy changed mid-period.
   - `Finding` rows are included when tied to an in-period `ControlTest`
     (`source_test_id`) **or** when the finding's own `created_at` falls
     within the period **or** it was still open (`status != "closed"`)
     as of `audit_period_ends_on` — an auditor needs visibility into
     unresolved issues discovered before the period that were still
     open during it, not only ones opened inside the window.
   Every rule is documented here rather than left implicit, since "what
   counts as in-period" is exactly the kind of judgment call an auditor
   would otherwise have to guess at from the export alone.
4. **Fail the whole export, not partially, on any missing/corrupt
   evidence or policy file.** One unreadable object aborts generation
   with a clear error identifying which artifact failed — a package
   silently missing one file while claiming to be complete is worse
   than no package at all (the issue's own explicit requirement).
5. **`require_admin`**, matching the existing "bulk/sensitive export"
   convention (`app/routers/audit_log.py`) — generating a package that
   bundles evidence, findings, and policy content is squarely that
   category, distinct from the ordinary `require_write_access` used for
   single-object mutations elsewhere in this app.
6. **No auditor-assurance language anywhere in the manifest or the
   route.** The manifest states facts (what was in scope, what evidence
   exists, what tests found) and explicitly disclaims that it is not an
   auditor's opinion or certification — matching RULES.md §1.10/PRD
   §4.6's AI-advisory-only spirit applied here to automated packaging
   in general: the tool assembles facts, it does not pronounce assurance.

## 3. Model

No new table. A plain module, `app/audit_package.py`, matching the
shape of `app/readiness.py`/`app/vendor_flags.py` (compute-and-return,
nothing persisted):

```python
class AuditPackageError(RuntimeError):
    """A safe-to-display reason package generation failed (e.g. a
    missing/corrupt evidence object, or no compliance scope defined) —
    never includes credential material."""


def generate_audit_package(session, settings, *, actor_email) -> bytes:
    """Returns the zip file's bytes. Raises AuditPackageError and writes
    nothing if any required artifact cannot be retrieved/verified."""
```

Package structure (inside the zip):

```text
manifest.json
scope.json
frameworks/<framework_id>.json
controls.json
occurrences.json
control_tests.json
findings.json
policies/<policy_id>/version-<n>-metadata.json
policies/<policy_id>/version-<n>-<original_filename>
evidence/<artifact_id>/<version_id>-<original_filename>
```

`manifest.json` lists every other file in the package with its SHA-256
(computed at packaging time — independently re-verifiable by an
auditor), a `disclaimer` field stating the package is a factual export,
not an assurance opinion, and package-level metadata (generated_at,
generated_by, audit_period_starts_on/ends_on, service_description).

## 4. Routes

`app/routers/audit_package.py` (new), `require_admin` router-level:

- `GET /admin/audit-package` — a small page describing the current
  scope's declared period and a "Generate and download" button (no
  separate confirmation step needed; generation is read-only/side-
  effect-free beyond the one audit-log entry).
- `POST /admin/audit-package` — calls `generate_audit_package`; on
  success, streams the zip back (`Response`, `application/zip`,
  `Content-Disposition: attachment`); on `AuditPackageError`, redirects
  back with a clear error flash (never a raw 500) — matching the "fail
  clearly, not silently" requirement being visible to the operator too,
  not only encoded in an exception type.

## 5. Testing strategy

- Unit (`app/audit_package.py`): each in-period/out-of-period boundary
  (occurrence/test/finding inclusion rules above); no-scope-defined
  failure; a policy version selection spanning a mid-period
  supersession (both versions included); manifest hash re-verification
  against the embedded files; a later policy/control edit after
  generation does not change what an *already-generated* package
  contained (proven by generating twice around an edit and diffing).
- Integration: a missing S3 evidence object aborts the whole export with
  `AuditPackageError`, no partial zip returned; a policy version whose
  local file is missing does the same.
- Routes: `require_admin` (non-admin roles rejected); a real generate-
  and-download round trip; the failure path renders a flash, not a 500.
- Headless UAT (required): build a representative period (a control, an
  occurrence, a policy with an effective version, a control test with
  an exception, evidence, and a finding with remediation), generate the
  package, and independently re-verify the manifest's hashes against
  the embedded file bytes over a real socket.

## 6. Known deferred/untested paths

- No server-side package history/retention (§2.2) — only an audit-log
  entry recording that a generation happened and its manifest hash.
- No auditor-facing delivery/portal (explicit MVP non-goal, PRD §9).
- No support for exporting a *past*, no-longer-current scope revision as
  its own separate audit period — the export always uses the current
  `ComplianceScope` row's declared period (its event history exists for
  #45's future point-in-time reconstruction work, not duplicated here).
- **Person identities are never resolved to a name/email — only opaque
  `*_person_id` values are included** (found during adversarial review,
  §9). An auditor working only from the package cannot map "who
  performed/tested/owns this" to a human name without separate access to
  miniGRC's own People page. This is a deliberate minimization, not an
  oversight: resolving names would add real PII to a document meant to
  leave the system, and the PRD/issue give no explicit instruction that
  it should. Revisit if real auditor usage shows this is needed —
  joining `Person.display_name`/`email` in per identifier is a small,
  additive change if a future issue asks for it.
