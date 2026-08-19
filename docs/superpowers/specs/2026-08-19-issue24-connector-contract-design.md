# Issue #24: Connector manifest, capability, runtime, and result contracts

Parent epic: #23. See also #25 (installation/config/secrets/health lifecycle),
#26 (registry/marketplace), #27 (security hardening), #28 (Google Drive
reference connector) — none implemented yet; this issue is contract-only.

## 1. Repository-reality check (inventory, per the issue's own required first step)

Three integrations already exist and, taken together, cover three
genuinely distinct capability shapes — the contract must fit all three,
not be designed against one and hoped to generalize:

- **Google Drive** (`app/google_drive.py`, `app/routers/google_drive.py`)
  — `file.capture`. OAuth2 authorization-code + refresh-token flow;
  `GoogleDriveConnection` is append-only (new row per connect,
  `revoked_at` on disconnect, never mutated in place); secret
  (`encrypted_refresh_token`) stored directly on the connection row via
  `app/crypto.py`, not the generic `Secret` table.
- **AWS CloudTrail/IAM** (`app/aws_connector.py`,
  `app/routers/aws_connector.py`) — `configuration.snapshot`. boto3 +
  ambient credentials/optional AssumeRole; `AwsConnection` is mutable,
  updated in place; `encrypted_external_id` same direct-crypto pattern.
  **`AwsCheckResult`** (`app/aws_connector.py:38-44`) is already an
  almost-generic result shape: `check_key`/`status`/`title`/`summary`/
  `normalized_payload`. **`build_evidence_snapshot`**
  (`app/aws_connector.py:263-280`) is already an almost-generic
  ingestion function turning that result into an `EvidenceSnapshot` row.
- **Google Workspace Directory** (`app/google_workspace_directory.py`)
  — `identity.population`. `sync_directory_users` upserts `Person` rows
  directly (`source`/`external_id`/`last_synced_at` fields on `Person`
  itself) — no evidence table involved at all. A third, genuinely
  different ingestion shape from the other two.

Two existing models already carry most of what a generic result/
provenance contract needs:

- **`EvidenceSnapshot`** (`app/models.py:1280+`) — `source_type`,
  `source_connection_id`, `check_key`, `status` (one of
  `EVIDENCE_STATUSES = ("pass", "fail", "warning", "unknown")`), `title`,
  `summary`, `collected_at`, `collector_version`,
  `normalized_payload_json` (bounded, secret-free by discipline),
  `raw_payload_sha256`. Explicitly append-only by convention (no edit/
  delete route), but **not** event-sourced through `app/events.py`.
- **`EvidenceArtifactVersion`** (`app/models.py:1385+`, issue #32) —
  genuinely event-sourced; already carries `source_type`,
  `source_connection_id`, `source_object_id`, `source_revision_id`,
  `source_modified_at`, `linked_evidence_snapshot_id`. Ingested through
  the existing authorized command **`capture_evidence_version`**
  (`app/evidence_repository.py:93+`) — idempotent by content hash,
  uploads bytes before any event/DB row, deletes the orphaned upload on
  a failed append. This is already exactly the "authorized core command"
  boundary RULES.md §9 requires for connector-originated facts.

No generic "manifest"/"capability"/"connector" vocabulary exists
anywhere in the repository today (confirmed by a repository-wide search)
— this issue is genuinely new scaffolding, not formalizing something
partially built. Two different secret-storage conventions already
coexist (the generic `Secret` table vs. a direct encrypted column on a
connection row); reconciling that is out of scope here (#25/#27's
concern, if ever) — the contract's `ConfigFieldSpec.secret` flag only
*declares* which config fields are secret-shaped, agnostic to how a
future runtime actually persists them.

## 2. Scope decisions

1. **The manifest is a plain, versioned Python object, not a database
   table.** Connector *installation* state (#25) and a *registry*
   (#26) don't exist yet; a DB-backed manifest would be premature. A
   manifest is a frozen dataclass a connector module declares at import
   time — the same "declarative Python object, not a DB row" shape this
   codebase already uses for `RegisterConfig`/`FieldSpec`
   (`app/registers/config.py`).
2. **Capability vocabulary is validated against the three real shapes
   above, not invented speculatively.** `file.capture`,
   `configuration.snapshot`, and `identity.population` each get a real,
   tested ingestion function in this slice. `evidence.collect`,
   `identity.groups`, `asset.inventory`, `vulnerability.findings`, and
   `notification.send` are declared in the vocabulary (so manifests can
   reference them and #28/future connectors aren't blocked on a second
   contract revision) but get no ingestion function yet — matching the
   issue's own "do not freeze names without validating against concrete
   use cases."
3. **Two result shapes, not one forced-generic shape.** A JSON-fact
   result (`ConnectorResult` — `configuration.snapshot`,
   `identity.population`, and future fact-shaped capabilities) and a
   file-bytes result (`file.capture`, which inherently needs
   tmp_path/media_type/byte_size, not a JSON payload) are genuinely
   different shapes. Forcing file bytes through a `normalized_payload`
   dict would be a false unification. What *does* unify across both is
   **provenance** — a single `ConnectorProvenance` dataclass
   (`source_connection_id`, `source_object_id`, `source_revision_id`,
   `source_modified_at`, `collected_at`, `collector_version`) feeds both
   ingestion paths. `collected_at` is caller-supplied, never
   `datetime.now()` inside the dataclass, so results stay deterministic/
   testable.
4. **Ingestion functions are the only path into domain state; adapters
   prove the contract without rewiring live routes.** New generalized
   ingestion functions (`app/connectors/ingestion.py`) are exercised
   through **adapter functions** (`app/connectors/adapters.py`) that
   feed them realistic Drive/AWS/Directory-shaped data and assert the
   correct `EvidenceSnapshot`/`EvidenceArtifactVersion`/`Person` rows
   result. The **live production routes**
   (`app/routers/aws_connector.py`, `app/routers/google_drive.py`,
   whatever calls `sync_directory_users`) are **not** rewired to call
   through the new contract in this slice — that is a larger, separate
   retrofit with real regression risk on already-shipped integrations,
   and the issue's own non-goals say not to implement Drive provider
   logic here / not to build the marketplace here. "Model Google Drive
   file capture... using the same contract" is satisfied by a real,
   tested adapter proving the shape fits — not by migrating the live
   route. Documented explicitly as a deferred follow-up (§8), not a
   silently narrower interpretation.
5. **Malformed-input and overclaiming defenses, validated by tests, not
   just structurally implied**: `validate_manifest` rejects an unknown
   capability key, empty `capabilities`/`connector_id`/`version`, a
   malformed version string, or a config-schema field-name collision.
   `check_compatibility` rejects a manifest outside
   `[min_core_contract_version, max_core_contract_version]` against the
   current `CORE_CONTRACT_VERSION` constant. `validate_result` rejects
   a result whose `capability` isn't in the manifest's own declared
   `capabilities` (capability overclaiming), a `status` outside
   `EVIDENCE_STATUSES`, an oversized `normalized_payload` (reusing
   AWS's existing `MAX_NORMALIZED_PAYLOAD_CHARS` bound), and — as
   defense-in-depth, not the primary control — a `normalized_payload`
   containing an obviously secret-shaped key (`token`/`secret`/
   `password`/`refresh_token`/etc.). The primary control remains "a
   connector must never put secret material in a normalized payload in
   the first place," matching existing AWS/Drive discipline; the scan
   is a safety net, not a substitute.
6. **Secrets stay out of the contract entirely.** `ConfigFieldSpec.secret`
   only *declares* a field as secret-shaped for a future runtime (#25)
   to route through an encrypted-storage mechanism — the manifest/result
   contract itself never carries a resolved secret value.

## 3. Contract (`app/connectors/`, new package)

```python
# app/connectors/manifest.py
CORE_CONTRACT_VERSION = 1

CAPABILITIES = {
    "file.capture": "Capture a specific file's bytes/metadata as durable evidence.",
    "evidence.collect": "Collect a fact/check result as evidence (generic).",
    "configuration.snapshot": "Snapshot a provider configuration/posture check.",
    "identity.population": "Sync a population of identities (e.g. directory users).",
    "identity.groups": "Sync group/role membership for identities.",
    "asset.inventory": "Enumerate provider-managed assets.",
    "vulnerability.findings": "Report vulnerability/security findings.",
    "notification.send": "Send an outbound notification.",
}


class ConnectorContractError(ValueError):
    """A manifest/result violates the connector contract."""


@dataclass(frozen=True)
class ConfigFieldSpec:
    name: str
    type: str  # "str" | "bool" | "int"
    required: bool = True
    secret: bool = False


@dataclass(frozen=True)
class ConnectorManifest:
    connector_id: str
    display_name: str
    publisher: str
    version: str  # semver
    capabilities: tuple[str, ...]
    required_permissions: tuple[str, ...] = ()
    supported_auth_methods: tuple[str, ...] = ()
    config_schema: tuple[ConfigFieldSpec, ...] = ()
    min_core_contract_version: int = CORE_CONTRACT_VERSION
    max_core_contract_version: int | None = None


def validate_manifest(manifest: ConnectorManifest) -> None: ...
def check_compatibility(manifest, core_contract_version: int = CORE_CONTRACT_VERSION) -> None: ...
```

```python
# app/connectors/result.py
@dataclass(frozen=True)
class ConnectorProvenance:
    source_connection_id: str | None
    collected_at: datetime.datetime
    source_object_id: str | None = None
    source_revision_id: str | None = None
    source_modified_at: datetime.datetime | None = None
    collector_version: str = "1"


@dataclass(frozen=True)
class ConnectorResult:
    capability: str
    check_key: str
    status: str  # one of app.models.EVIDENCE_STATUSES
    title: str
    summary: str
    provenance: ConnectorProvenance
    normalized_payload: dict = field(default_factory=dict)


def validate_result(result: ConnectorResult, manifest: ConnectorManifest) -> None: ...
```

```python
# app/connectors/ingestion.py — the only path from a ConnectorResult into domain state
def ingest_configuration_snapshot(session, result: ConnectorResult, manifest) -> EvidenceSnapshot: ...
def ingest_identity_population(session, result: ConnectorResult, manifest) -> dict: ...
def ingest_file_capture(session, artifact, *, provenance: ConnectorProvenance, manifest, **capture_kwargs):
    ...
    # thin wrapper around app.evidence_repository.capture_evidence_version
```

## 4. Adapters proving the contract (no live-route rewiring — see §2.4)

`app/connectors/adapters.py`:

- `aws_manifest()` — capability `configuration.snapshot`; config schema
  mirrors `AwsConnection` (`account_label`, `expected_account_id`,
  `role_arn`, `external_id` [secret], `regions`).
- `aws_check_result_to_connector_result(check: AwsCheckResult, *, connection_id, collected_at) -> ConnectorResult`
  — adapts the existing, unmodified `AwsCheckResult` into the generic
  envelope.
- `google_drive_manifest()` — capability `file.capture`; config schema
  reflects the OAuth grant shape (no per-instance secret beyond the
  existing connection-level refresh token).
- `workspace_directory_manifest()` — capability `identity.population`.
- `directory_users_to_connector_result(users: list[DirectoryUser], *, connection_id, collected_at) -> ConnectorResult`
  — adapts the existing, unmodified `DirectoryUser` list into the
  generic envelope's `normalized_payload["users"]`.

## 5. Security review (adversarial, at design time)

- **Capability overclaiming**: `validate_result` rejects any result
  whose `capability` isn't in the manifest's declared set.
- **Malformed manifest**: `validate_manifest` rejects unknown
  capabilities, empty required fields, malformed version strings,
  duplicate config-field names.
- **Version-confusion/downgrade**: `check_compatibility` is a pure
  function with explicit min/max bounds — no implicit "latest wins."
- **Secret leakage**: config-schema `secret` flag is declarative only;
  `validate_result`'s secret-shaped-key scan on `normalized_payload` is
  defense-in-depth; ingestion functions never accept or forward a
  resolved secret value.
- **Provider data injection**: `normalized_payload` size-bounded
  (reusing AWS's existing 20,000-char bound); `identity.population`
  ingestion normalizes email the same way `sync_directory_users`
  already does before using it as a lookup key.
- **Compliance-conclusion leakage**: ingestion functions only ever
  produce `EvidenceSnapshot`/`EvidenceArtifactVersion`/`Person`
  population rows — none of them touch `ControlTest`, `Finding`, or any
  control-effectiveness table, matching RULES.md §9 exactly.

## 6. Test strategy

- **Manifest validation**: valid manifest passes; unknown capability,
  empty capabilities/connector_id/version, malformed version string,
  duplicate config-field name all rejected with a clear
  `ConnectorContractError`.
- **Compatibility**: manifest within bounds accepted; below
  `min_core_contract_version` and above `max_core_contract_version`
  (when set) both rejected.
- **Result validation**: valid result passes; capability-overclaiming
  result rejected; invalid status rejected; oversized payload rejected;
  a payload containing an obviously secret-shaped key rejected.
- **Ingestion — configuration.snapshot**: a `ConnectorResult` adapted
  from a real `AwsCheckResult` shape produces an `EvidenceSnapshot` row
  with correct `source_type`/`check_key`/`status`/hash — same shape
  `build_evidence_snapshot` already produces today, proving no
  behavioral regression for the (unmodified, not-yet-rewired) AWS route.
- **Ingestion — identity.population**: a `ConnectorResult` adapted from
  a real `DirectoryUser` list produces the same create/update counts
  and `Person` field values `sync_directory_users` already produces —
  including the "never deletes a missing person" invariant.
- **Ingestion — file.capture**: `ingest_file_capture` correctly forwards
  provenance into `capture_evidence_version` and preserves its existing
  idempotent-by-hash behavior (moto-backed S3, mirroring #32's test
  pattern).
- **Adapters**: `aws_check_result_to_connector_result` and
  `directory_users_to_connector_result` round-trip real input shapes
  without loss.
- Documentation-only for the manifest/contract shape itself (no
  route/UI/UAT layer — no user-visible surface changes in this issue).

## 7. Definition of done

- One versioned, provider-neutral connector contract exists.
- Capabilities describe provider functionality, not compliance outcomes.
- Normalized results carry durable provenance metadata.
- Secrets are excluded from the result/manifest contracts.
- Google Drive (`file.capture`) and a non-file shape (AWS
  `configuration.snapshot`, plus Workspace Directory
  `identity.population` as a second proof point) fit the same
  abstraction, proven by tests.
- Connector outputs cannot directly become authoritative compliance
  conclusions (no ingestion function touches a control/finding table).

## 8. Known deferred/untested paths

- Live production routes (`app/routers/aws_connector.py`,
  `app/routers/google_drive.py`, the Workspace Directory sync entry
  point) are **not** rewired to call through the new contract in this
  slice — see §2.4. A future issue should retrofit them once the
  contract has proven stable, so real regression risk on shipped
  integrations isn't taken on speculatively.
- No connector installation/configuration/health-lifecycle persistence
  (#25) — the manifest is a Python object with no DB-backed install
  state yet.
- No registry/marketplace discovery (#26).
- No package/signature verification or permission-review workflow (#27).
- No new provider logic for Google Drive or any other connector (#28) —
  this issue only defines and proves the contract against what already
  exists.
