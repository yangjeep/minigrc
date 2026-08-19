# Issue #32: S3-compatible evidence artifact repository

Status: implemented. See docs/worklog/2026-08-19-issue32-evidence-repository.md
for the full verification account, including a foundational,
pre-existing (latent, not #32-specific) SQLite transaction bug found
during adversarial testing and deliberately deferred rather than fixed
unsafely — see that worklog and issue #65.

## 1. Repository-reality check

- **No S3/object-storage abstraction exists today.** `app/storage.py`
  is 100% local-filesystem, used only by `Policy`/`PolicyVersion`
  (stream to a temp file under `GRC_DATA_DIR/tmp`, hash + validate by
  content sniffing (not extension), atomically `os.replace` into a
  path built entirely from server-generated ids — the filename is
  never part of the path). This is the pattern to mirror for S3 (temp
  file, hash, validate, *then* commit), not code to reuse directly.
- **`EvidenceSnapshot`** (`app/models.py:959-984`) is a deliberately
  bytes-free, normalized *fact* snapshot ("this app needing to retain
  the raw payload itself" is explicitly what its docstring says it
  avoids) — `raw_payload_sha256` hashes a payload that is never itself
  stored. Its only creation call site today is
  `app/aws_connector.py`'s `build_evidence_snapshot`, for CloudTrail/
  IAM API responses that are not files at all. It is not "two
  competing models" with what #32 needs — it answers "what did a
  connector observe," a genuinely different question from "here is the
  durable file that constitutes evidence." Nothing about it changes in
  this issue; #32 adds a new, complementary capability alongside it.
- **`ControlOccurrenceEvidence`** (`app/models.py:305-...`) already
  links a `ControlOccurrence` to an `EvidenceSnapshot`, and its own
  docstring anticipates this issue directly: *"Scoped to the AWS-
  connector-sourced EvidenceSnapshot only... #13 will likely need its
  own link to a future general evidence-upload mechanism."* #32 is
  that mechanism for the one domain that already exists
  (`ControlOccurrence`, from #11); tests/findings/periods stay
  deferred per the issue's own "where corresponding domains exist"
  qualifier — #13 hasn't landed.
- **No S3/MinIO config exists anywhere** in `app/config.py` — the
  `google_drive_configured`-style "all required fields or disabled"
  property (config.py:84-91) is the precedent to follow for a new
  `evidence_repository_configured` property.
- **`boto3>=1.35`** is already a pinned dependency
  (`pyproject.toml:20`), used today by `app/aws_connector.py` to *read*
  AWS as a source. Its S3 client natively supports any S3-compatible
  self-hosted target (MinIO, etc.) via `endpoint_url` — no new library
  needed for the runtime code.
- **No `moto` (S3 test double) dependency exists yet** — needed for
  the always-on test suite (see §7).
- **Event-sourcing precedent to mirror**: `app/events.py` (#22) +
  `app/policy_lifecycle.py` (#31)'s shape — plain module, event
  type constants, projector dict, `append_and_project`/
  `rebuild_projection`. Also directly applies **#41's now-formalized
  migration convention** if this ever needs a legacy-data bootstrap —
  it doesn't, since this is a brand-new table with no pre-existing
  rows (same as #11's `ControlOccurrence`, not like #31's
  `PolicyVersion`).
- **PRD §5.5 exact required facts**: "logical artifact identity;
  immutable captured versions; content hash; source/provider
  provenance; captured/collected timestamps; source revision/version
  metadata when available; actor/connector execution correlation;
  links to control occurrences, tests, findings, and periods." Minimum
  paths: "manual upload; connector capture; version/history view;
  download/export; content hash verification; source provenance."
- **PRD §6.5 verbatim**: "File bytes use an S3-compatible backend
  abstraction. Self-hosted deployments should be able to point this at
  an appropriate compatible implementation. The DB/event store remains
  authoritative for artifact identity, provenance, compliance
  versioning, lifecycle, and relationships." And: "S3 object versioning,
  if enabled, is infrastructure protection and not the compliance-
  version model."

## 2. Scope decision: event-source the capture itself, unlike #31

#31 deliberately kept `PolicyVersion` row creation as a plain insert
(file-capture facts are provenance-bearing but not event-sourced,
matching `EvidenceSnapshot`'s own precedent). #32's issue text asks for
something stronger: *"event-back material artifact/version/provenance
facts."* This is a brand-new table with no pre-existing rows (unlike
`PolicyVersion`), so there is no reason not to make the capture itself
the authoritative event — matching `ControlOccurrence`'s fully-
event-sourced shape (#11) more than `PolicyVersion`'s hybrid one.

`EvidenceArtifact` (the logical identity — title/description, a
lightweight container) is a plain row, not event-sourced — same
category as `Policy` itself. `EvidenceArtifactVersion` (one immutable
captured version) is a canonical projection over an
`"evidence_artifact_version"` DomainEvent aggregate — the row does not
exist independent of its capture event.

## 3. Model

```python
class EvidenceArtifact(Base):
    __tablename__ = "evidence_artifacts"
    id, title, description, created_at, updated_at
    versions: relationship, order_by version_number desc
    @property latest_version

class EvidenceArtifactVersion(Base):
    __tablename__ = "evidence_artifact_versions"
    id  # == aggregate_id, no default=new_id (matches ControlOccurrence)
    artifact_id: FK(evidence_artifacts.id)
    version_number: int
    UniqueConstraint(artifact_id, version_number)

    object_key: str            # server-generated, never client input
    media_type, byte_size, sha256

    source_type: str           # "manual" | connector-specific, e.g. "aws_cloudtrail"
    source_connection_id: str | None   # FK-shaped id, mirrors EvidenceSnapshot's own column name/meaning
    source_object_id: str | None
    source_revision_id: str | None
    source_modified_at: datetime | None
    linked_evidence_snapshot_id: str | None  # FK(evidence_snapshots.id), nullable
      # optional: a connector capturing a real file MAY also have
      # created an EvidenceSnapshot fact for the same collection run;
      # this is how the two complementary models connect when both exist,
      # never required.

    captured_at: datetime      # occurred_at from the event
    actor_type, actor_id       # who/what triggered the capture

    original_filename: str     # display-only, sanitized, never a path component
```

New join table `ControlOccurrenceEvidenceArtifact` (mirrors
`ControlOccurrenceEvidence` exactly): `occurrence_id` FK,
`evidence_artifact_version_id` FK,
`UniqueConstraint(occurrence_id, evidence_artifact_version_id)`.
Populated by a new `ControlOccurrenceEvidenceArtifactLinked` event on
the existing `"control_occurrence"` aggregate (`app/control_occurrences.py`),
mirroring `link_evidence`/`ControlOccurrenceEvidenceLinked` exactly —
the smallest possible addition to already-existing, already-tested
code, not a new subsystem.

No `linked_test_id`/`linked_finding_id`/`linked_period_id` columns —
those domains don't exist yet (#13); adding nullable FKs to nothing
would be speculative infrastructure RULES.md warns against. Noted as
explicit deferred work for whichever of #13/#30 lands next.

## 4. Object storage abstraction (`app/object_storage.py`)

Mirrors `app/storage.py`'s shape:

- `build_s3_client(settings) -> boto3 S3 client`: constructed with
  `endpoint_url=settings.s3_endpoint_url`,
  `aws_access_key_id=settings.s3_access_key_id`,
  `aws_secret_access_key=settings.s3_secret_access_key`,
  `region_name=settings.s3_region`. Raises if
  `not settings.evidence_repository_configured` — routes check this
  first and return a clear "not configured" error rather than
  constructing a client against empty credentials.
- `capture_evidence_bytes(chunks, *, max_bytes) -> (tmp_path, sha256, byte_size)`:
  same stream-to-temp-file-while-hashing-and-bounding core as
  `app/storage.py::_save_policy_version`, factored out so both a
  browser `UploadFile` and in-memory bytes (future connector captures)
  share it — same reasoning #32's issue text gives for "manual and
  connector capture paths can converge on the same repository
  boundary."
- `upload_object(client, *, bucket, object_key, tmp_path, media_type) -> None`:
  a single `put_object` from the already-hashed, already-size-bounded
  temp file (the file is small enough — `max_upload_mb` default 25MB —
  that true S3 multipart upload is unnecessary complexity; a single
  `put_object` is one HTTP request). Object key convention:
  `evidence/<artifact_id>/<version_id>/<extension-only-suffix>` —
  entirely server-generated ids, matching the path/key-injection
  protection `app/storage.py::sanitize_original_filename` already
  established (the original filename is *never* part of the key,
  only kept for display/`Content-Disposition` on download).
- `download_object(client, *, bucket, object_key) -> bytes` (or a
  streaming body for the route to pipe through) for the download
  route — the app **proxies bytes through itself**, never a presigned-
  URL redirect: keeps authorization centralized in the app (matching
  `download_policy_version`'s existing `FileResponse` shape) and
  doesn't require a self-hosted MinIO endpoint to be reachable from an
  end user's browser (it usually is only reachable from the app
  itself, e.g. an internal Docker network — a presigned redirect would
  break that deployment shape entirely).
- **Ordering for "no partial authoritative mutation" (issue
  requirement)**: upload to S3 *fully completes and is verified*
  before any `DomainEvent`/DB row is ever created. If the S3 `put_object`
  fails, nothing is written to the relational side at all — no
  orphaned row referencing a non-existent object. This mirrors
  `_save_policy_version`'s own ordering (write bytes fully, *then*
  return `StoredUpload` for the caller to persist metadata from).
  Symmetrically, if the object was uploaded but the subsequent
  `append_and_project` call fails for any reason, the object is
  deleted from S3 before the exception propagates (best-effort cleanup,
  same category as `_save_policy_version`'s temp-file cleanup on
  failure) — an unreferenced orphan object in the bucket is a
  containable, non-authoritative leftover; a DB row/event referencing
  a missing object would be a correctness violation and is what this
  ordering is designed to prevent.

## 5. Config (`app/config.py`)

```python
s3_endpoint_url: str = ""
s3_bucket: str = ""
s3_access_key_id: str = ""
s3_secret_access_key: str = ""
s3_region: str = "us-east-1"  # boto3 requires some region even for non-AWS targets


@property
def evidence_repository_configured(self) -> bool:
    return bool(
        self.s3_endpoint_url and self.s3_bucket and self.s3_access_key_id and self.s3_secret_access_key
    )
```

Same category as `DATABASE_URL`/`google_oidc_client_id` — plain,
operator-set deployment configuration via environment variables at
startup, not a runtime-configurable, DB-stored, encrypted `Connection`
row like the AWS/Drive *connectors*. Object storage is deployment
infrastructure the whole app depends on once evidence capture is used,
analogous to the database choice, not an optional third-party
integration an admin toggles at runtime through a UI.

**SSRF note** (RULES.md's "treat configurable external URLs as
security boundaries"): `s3_endpoint_url` is operator-set deployment
configuration, not attacker-reachable per-request user input — the
same trust category as `DATABASE_URL` or `google_oidc_client_id`, which
this repo already treats as trusted deployment config rather than a
per-request SSRF surface. No additional runtime validation is added
beyond what any other deployment-level endpoint setting already gets;
this is called out explicitly here so the reasoning is visible rather
than silently assumed.

## 6. Routes (new `app/routers/evidence_artifacts.py`)

Mirrors `app/routers/policies.py`'s shape and #37's RBAC convention
(router-level `require_login`, per-mutation `require_write_access`):

- `GET /evidence-artifacts` — list
- `GET /evidence-artifacts/new`, `POST /evidence-artifacts` — create
  artifact + first version (manual upload)
- `GET /evidence-artifacts/{id}` — detail + version history
- `POST /evidence-artifacts/{id}/versions` — upload a new version
- `GET /evidence-artifacts/{id}/versions/{version_id}/download` —
  proxied download (never a redirect — see §4)
- `POST /evidence-artifacts/{id}/versions/{version_id}/link-occurrence`
  (form: `occurrence_id`) — the new
  `ControlOccurrenceEvidenceArtifact` link

Every route returns a clear, safe error (not a raw 500) when
`not settings.evidence_repository_configured` — "no automatic test/
control pass from upload/capture" also means capture must fail
*visibly* when misconfigured, never silently no-op.

No delete route — immutable history, matching every other captured-
artifact precedent in this repo (`PolicyVersion`, `EvidenceSnapshot`).

## 7. Testing strategy

- **`moto`** (new dev dependency) provides a real, in-process
  S3-API-compatible backend — satisfies the issue's "exercise a
  selected S3-compatible test backend" requirement without a live
  Docker/MinIO service container in CI (avoiding the CI-complexity/
  image-CMD issues a real `minio/minio` service container would add,
  per direct inspection of how service containers are configured in
  `.github/workflows/ci.yml`). Always-on in the default `pytest` run —
  no environment-variable gating needed, unlike Postgres, since `moto`
  needs no external service.
- **Unit**: `app/object_storage.py`'s hash/bound/validate core in
  isolation (mirrors `app/storage.py`'s own existing test coverage
  shape).
- **Integration** (`moto`-backed): manual upload → download round trip
  byte-identical; duplicate-unchanged-bytes capture is idempotent (no
  new version/event); changed bytes create a new immutable version;
  first version's bytes/hash unchanged after a second version is
  captured; S3 failure (mocked) during upload leaves no DB row/event;
  a DB-side failure after a successful S3 upload cleans up the orphan
  object.
- **Authorization**: reader/auditor 403 on upload/link routes, no
  state change (extends #37's established pattern).
- **Path/key-injection**: a malicious filename (`../../etc/passwd`,
  null bytes, etc.) never appears in the object key, only in the
  sanitized display filename — mirrors `app/storage.py`'s own existing
  test coverage shape for `sanitize_original_filename`.
- **Secret leakage**: `s3_secret_access_key`/`s3_access_key_id` never
  appear in logs, error messages, or any client-visible response.
- **Rebuild**: `rebuild_evidence_artifact_version_projection` reproduces
  identical state from events (mirrors #31/#11's own rebuild tests).
- **Migration/backend**: SQLite + Postgres-gated (this session's fixed
  `-k postgres` CI selection — see the CI worklog from earlier this
  session) schema/constraint tests.
- **Headless UAT**: manual upload → download; capture a second version
  → first version's hash/bytes still verify; link an artifact version
  to a control occurrence.
