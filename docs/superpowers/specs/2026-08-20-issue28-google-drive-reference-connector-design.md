# Issue #28: Google Drive as the reference evidence/file-capture connector

Parent epic: #23. Depends on #24, #25 (merged). Related: #21/#22, #26,
#27 (all merged).

## 1. Repository-reality check

Two Drive integrations already exist and must not be confused:

1. **Policy-document capture** (`app/routers/policies.py`'s
   `link_drive_file`/`drive-refresh`/`capture_drive_version`, pre-dating
   the connector platform): captures a Drive file as the next immutable
   `PolicyVersion` of a specific `Policy`, storing bytes on local disk
   via `app/storage.py`. This is compliance-*document lifecycle*
   (PRD §5.4: draft → review → approved → effective → superseded), a
   different domain concept from audit evidence, and is out of this
   issue's scope — rewriting it onto the evidence repository would be
   an unrelated, disproportionately risky refactor of a live, tested
   feature for no benefit `#28`'s acceptance criteria actually ask for.
2. **The evidence repository / S3-compatible object storage boundary**
   (`app/evidence_repository.py::capture_evidence_version`, issue #32,
   merged, event-sourced, idempotent-by-content-hash) — this is what
   `#28`'s acceptance criteria are actually shaped around ("captured
   evidence includes exact content hash and provider provenance,"
   "changed source content creates a new immutable capture/version
   fact"). `app/object_storage.py::capture_raw_bytes` was already
   built anticipating this ("stage in-memory bytes (a future connector
   capture) for an S3 commit").

**This issue wires Google Drive into (2) as a genuine `file.capture`
connector through the platform — a new, additional evidence-capture
path — and leaves (1) untouched.** Both reuse the same underlying OAuth
connection and provider client (`app/google_drive.py`, unchanged); the
platform layer is what's new.

Also already true: `app/connectors/adapters.py::google_drive_manifest()`
and `registry/registry.json`'s `google_drive` entry are placeholders —
declared capabilities/permissions, but nothing converts a real Drive
capture into a `ConnectorResult`/ingested fact yet. This issue makes
that real.

## 2. The `ConnectorInstance` / `GoogleDriveConnection` split (a decision, not an oversight)

Google Drive's real OAuth credential already lives in
`GoogleDriveConnection` — an append-only, org-level **singleton**
connection (one active row with `revoked_at IS NULL`), pre-dating and
architecturally distinct from `ConnectorInstance`'s per-instance
`secret_ref_json` model (built for AWS's multi-account-credential
shape). Forcing Drive's token into `ConnectorInstance.secret_ref_json`
would mean either migrating `GoogleDriveConnection` data (destructive,
touches a live, already-reviewed-for-leakage feature, wildly
disproportionate to "prove the reference connector") or creating a
`ConnectorInstance` whose `secret_ref_json` doesn't actually hold the
real secret (a lie about what that field means).

**Resolution**: `ConnectorInstance`/`ConnectorExecution` (#25) still
participate — that ledger is what "operates through the generic
connector platform" actually requires (health/execution/audit history,
idempotency-key ledger) — but the instance's `config_schema` stays
empty (already declared that way in `google_drive_manifest()`) and no
secret ever lives in `secret_ref_json` for it. The real credential
continues to live in `GoogleDriveConnection`, resolved by the caller
(a future route, or a test) and passed into the new execution function
as an already-resolved access token — this module never imports
`app.routers.google_drive` to avoid a service-layer-importing-a-router
dependency; the caller does that resolution and hands over
`access_token`/`connection`. Because `GoogleDriveConnection` is a
singleton, a new helper ensures at most one installed `ConnectorInstance`
for `connector_id="google_drive"` exists rather than letting duplicates
accumulate (unlike AWS, where multiple accounts are legitimate).

## 3. New module: `app/connectors/google_drive_capture.py`

```python
def get_or_create_google_drive_instance(
    session: Session, *, actor: User, encryption_key: str
) -> ConnectorInstance:
    """Idempotent: returns the existing google_drive ConnectorInstance
    if one exists, else installs exactly one via install_connector_instance.
    Drive is a singleton connection — unlike AWS, duplicates would be
    ambiguous about which execution ledger a capture belongs to."""


def run_google_drive_capture(
    session: Session,
    instance: ConnectorInstance,
    manifest: ConnectorManifest,
    *,
    drive_file_id: str,
    access_token: str,
    connection: GoogleDriveConnection,
    artifact: EvidenceArtifact,
    settings: Settings,
    idempotency_key: str,
    actor_id: str,
    triggered_by: str = "manual",
) -> ConnectorExecution:
    """The file.capture execution/ingestion boundary for Google Drive —
    the same shape run_connector_instance provides for JSON-fact
    capabilities, specialized for file bytes (run_connector_instance's
    generic connector_callable/_INGESTION_DISPATCH path deliberately
    excludes file.capture; see connector_lifecycle.py's own comment).
    Disabled instance and repeated idempotency_key are handled exactly
    like run_connector_instance. On any failure, rolls back and records
    a failure/error ConnectorExecution row — no partial evidence write
    survives a downstream failure, mirroring run_connector_instance's
    own atomicity fix from #25.
    """
```

Steps inside the try block (all using existing, unmodified functions):

1. `get_file_metadata(drive_file_id, access_token=access_token)` →
   `DriveFileMetadata`.
2. Compute `extension_of(captured_filename(metadata))` and reject
   up front if it's not in `object_storage.ALLOWED_EVIDENCE_MEDIA_TYPES`
   — the same allow-list #32's manual-upload path already enforces,
   applied here to close the "malformed/oversized files" testing
   requirement for content arriving from an external source rather than
   a browser upload.
3. `list_revisions(...)` (best-effort, already tolerant of failure) to
   resolve `source_modified_at` for `metadata.current_revision_id` —
   identical to `policies.py::capture_drive_version`'s own existing
   revision-matching logic, reused rather than reinvented.
4. `download_file_content(metadata, access_token=access_token, max_bytes=settings.max_upload_bytes)`.
5. `capture_raw_bytes(content, max_bytes=settings.max_upload_bytes)` →
   staged tmp file + sha256 + byte_size.
6. `validate_evidence_content(tmp_path, extension)` — content-sniffs
   the extensions with a real magic-byte signature (pdf/docx/png/jpg),
   same defense-in-depth #32's manual path already has, applied to
   Drive's declared MIME type the same way it's applied to a browser
   upload's declared filename — neither is trusted on claim alone.
7. `build_s3_client(settings)`.
8. Build `ConnectorProvenance(source_connection_id=connection.id, collected_at=<now>, source_object_id=metadata.file_id, source_revision_id=metadata.current_revision_id, source_modified_at=<from step 3>)`.
9. `ingest_file_capture(...)` (#24, unmodified) → `capture_evidence_version`
   (#32, unmodified) → `(version, created)`. `source_type` on the
   resulting `EvidenceArtifactVersion` becomes `"google_drive"` (the
   manifest's `connector_id`) — matching the existing, already-merged
   convention `ingest_file_capture` uses for every capability, verified
   against `tests/test_connector_ingestion.py::test_file_capture_ingestion_forwards_provenance`.

On success, record a `ConnectorExecution(status="success", ...)` and
update `instance.last_success_at` — identical bookkeeping to
`run_connector_instance`.

## 4. Idempotency and versioning (two layers, both already built)

- **`ConnectorExecution.idempotency_key`** (per invocation attempt):
  prevents re-calling Drive's API and re-attempting a capture on a
  request retry — identical mechanism `run_connector_instance` already
  uses.
- **`capture_evidence_version`'s content-hash idempotency** (per
  artifact): re-capturing byte-identical content against the same
  `EvidenceArtifact` is already a safe no-op (`(existing, False)`,
  #32) — no new logic needed. A *changed* Drive revision naturally
  produces different bytes → a different sha256 → a genuinely new,
  immutable `EvidenceArtifactVersion`, directly satisfying "changed
  source content creates a new immutable capture/version fact"
  without this issue adding any explicit revision-comparison logic of
  its own.

### 4.1 Finding from self-review: a real repeated-capture bug, fixed before shipping

Testing a realistic "capture the same artifact twice in one session"
sequence (exactly the changed-revision scenario §4 describes) reproduced
a genuine `IntegrityError` on the second capture: `UniqueConstraint
("artifact_id", "version_number")` violation, because both calls
computed the same `next_version_number`. Root cause: this app's session
factory uses `expire_on_commit=False` (`app/db.py`), so a long-lived
`artifact` object's `.versions` relationship — which
`capture_evidence_version` reads via `artifact.latest_version` to
compute the next version number — does not automatically reflect a
version inserted by an *earlier call in the same session* unless
something re-triggers a load. `tests/test_evidence_repository.py`'s own
existing tests already work around this by calling
`session.refresh(artifact)` between repeated captures — an established,
if easy-to-miss, caller responsibility.

Fixed by having `run_google_drive_capture` call `session.refresh(artifact)`
itself before calling `ingest_file_capture`, rather than requiring every
future caller (a batch job iterating several Drive files, a route, a
test) to remember this. This is the one part of this issue's own new
code that touches a *behavior*, not just wiring — everything else in §3
is a direct, unmodified call into existing functions. Regression:
`test_changed_revision_creates_new_immutable_version` (fails without the
fix, passes with it).

### 4.2 Finding from self-review: SSRF guard applied defensively

`run_google_drive_capture` originally passed its `drive_file_id`
parameter straight into `get_file_metadata`/`download_file_content`/
`list_revisions`, which build Drive API request URLs from it. The
existing policy-Drive routes are safe because they always call
`parse_drive_file_id` (SSRF-safe: never fetches the given value as a
URL, only extracts/validates an ID) on a caller-submitted value before
it reaches those functions — but this issue doesn't add the HTTP route
that would normally be that enforcement point, so nothing yet
guaranteed a future caller would remember to validate first. Fixed by
calling `parse_drive_file_id` at the top of `run_google_drive_capture`
itself. Regression:
`test_malformed_drive_file_id_rejected_before_any_drive_call`.

## 5. Historical immutability

`EvidenceArtifactVersion.source_connection_id` is a plain
`String(32)`, **not a foreign key** (confirmed in `models.py` — same
"evidence tables use a plain string, not a FK" pattern #25's
`remove_connector_instance` docstring already establishes for
`ConnectorExecution`). Consequently:

- Removing the `ConnectorInstance` (via #25's existing
  `remove_connector_instance`) never touches previously captured
  `EvidenceArtifactVersion` rows.
- Revoking/disconnecting `GoogleDriveConnection` (existing, unchanged
  disconnect flow) never touches them either — the captured bytes and
  their provenance in S3/the event log are permanent regardless of
  what happens to the connection or instance afterward.
- Neither path is new to this issue; both are pre-existing guarantees
  this issue relies on rather than re-implements.

## 6. Security review

- **SSRF**: `parse_drive_file_id` (existing, unchanged) already
  guarantees a user-pasted value is never fetched as a URL — only used
  to build miniGRC's own Google API request. This issue never adds a
  new configurable-URL surface.
- **Secret handling**: unchanged — the refresh token stays encrypted in
  `GoogleDriveConnection.encrypted_refresh_token`, resolved just-in-time
  by the existing, already-leakage-tested
  `get_access_token_for_active_connection`. This issue never logs or
  persists an access/refresh token.
- **Malicious file names/MIME metadata**: closed by reusing #32's
  extension allow-list + magic-byte content-sniffing (§3 steps 2, 6) —
  a Drive file claiming to be a PDF that isn't one is rejected exactly
  like a manually-uploaded one would be.
- **Oversized files**: `download_file_content`'s existing streaming
  size-cap (raises before buffering past `max_bytes`) plus
  `capture_raw_bytes`'s own `max_bytes` bound — same limit enforced
  twice, at the provider-download layer and the staging layer.
- **Capability boundary**: `ingest_file_capture` already refuses to run
  against a manifest that doesn't declare `file.capture` (#24) — this
  issue's manifest (`google_drive_manifest()`) already declares exactly
  that capability and nothing else.
- **No new provider permissions**: still `drive.readonly` only,
  unchanged from the existing OAuth flow — no write scope is ever
  requested.

## 7. Test strategy

- **Unit**: extension-allow-list rejection for an unsupported Drive
  file type; `source_modified_at` resolution matching/no-matching
  revision (mirrors `policies.py`'s existing logic, tested fresh for
  this module).
- **Integration** (mocking Drive's HTTP layer the same way
  `tests/test_google_drive.py` already does — `unittest.mock.patch` on
  the names this new module imports directly — and real `moto`-backed
  S3, matching `tests/test_connector_ingestion.py`'s existing `s3`
  fixture):
  - binary file capture and Google-native-doc export-to-PDF capture;
  - same-revision/content retry is idempotent (no new
    `EvidenceArtifactVersion`, `created=False`);
  - a changed revision (different bytes) creates a new, immutable
    version while the prior one is untouched;
  - disabled instance rejected before any Drive call;
  - repeated `idempotency_key` returns the existing execution without
    re-invoking Drive;
  - a Drive error (`GoogleDriveError`, e.g. file not found/access
    denied) is recorded as a failed execution without ingesting
    anything, and doesn't touch the instance's `last_success_at`;
  - malformed/oversized file rejected before any S3 upload is
    attempted;
  - removing the `ConnectorInstance` afterward leaves the captured
    `EvidenceArtifactVersion` fully intact and downloadable.
- **No HTTP route/UI** in this slice — same accepted backend-only
  precedent #24/#25/#26/#27 established; this issue's own execution
  prompt only asks to "demonstrate... install/configure/test/capture
  through #24/#25," which the integration tests above do directly.

## 8. Definition of done

- Google Drive has a real (not placeholder) `file.capture` execution
  path through `ConnectorInstance`/`ConnectorExecution` and
  `ingest_file_capture`/`capture_evidence_version`.
- Captured evidence carries exact content hash and Drive provenance
  (file ID, revision ID where available, modified time where
  available).
- A changed Drive revision produces a new immutable version; an
  unchanged one is a safe no-op.
- Removing the connector instance or disconnecting Drive never deletes
  previously captured evidence.
- No new provider permissions, no new configurable-URL/SSRF surface, no
  secret logged/persisted beyond the existing encrypted storage.
- The pre-existing policy-document Drive capture path is untouched.
