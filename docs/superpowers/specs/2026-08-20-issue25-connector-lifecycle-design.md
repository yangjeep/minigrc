# Issue #25: Connector installation, configuration, secrets, health, and execution lifecycle

Parent epic: #23. Depends on #24 (merged) — this issue builds the
lifecycle *around* #24's manifest/capability/result contract; it does
not change that contract.

## 1. Repository-reality check

- `app/secrets.py` (`create_encrypted_secret`/`resolve_secret`) is the
  generic, already-shipped secret-storage mechanism — never returns a
  plaintext value through a serializer. #24's
  `ConfigFieldSpec.secret` flag exists precisely so this issue can
  route secret-shaped config fields through it.
- `EvidenceSnapshot.source_connection_id` and
  `EvidenceArtifactVersion.source_connection_id` are **plain strings,
  not foreign keys** to any connection/instance table (confirmed by
  reading the model columns directly). This means removing a
  connector instance can never cascade into or orphan already-captured
  evidence — "historical evidence survives removal" is already true by
  construction, not something this issue needs to engineer.
- `python -m app.cli aws_run_checks` is an existing, accepted precedent
  for "invoke a connector via a direct backend call, no HTTP route
  required" — issue #24 itself shipped with no route/UI at all and was
  accepted on that basis. This issue follows the same discipline: a
  backend service layer, proven by tests including one fake reference
  connector run end to end, with no new HTTP route or admin page in
  this slice. The epic's own non-goal ("do not implement marketplace
  browsing... in this issue") reserves operator-facing browsing/install
  UI for #26; wiring this lifecycle to a concrete UI makes more sense
  once #26 defines what that UI actually needs to show.
- `capture_evidence_version`'s own docstring already states the
  established convention: domain functions append to the session but
  the **caller** commits/rolls back — this issue's execution boundary
  follows the same convention rather than introducing a nested
  SAVEPOINT (this codebase has a documented, deliberately-deferred
  residual gap around nested-rollback-after-an-earlier-same-session-
  commit; not reaching for that mechanism here avoids it entirely).

## 2. Scope decisions

1. **Backend service layer only, no HTTP route/UI, no headless UAT
   required in this slice** — same justification #24 already
   established. `app/connector_lifecycle.py` (new) is exercised by
   direct tests, including one deterministic fake reference connector
   run through install -> configure -> test -> execute -> ingest, per
   the issue's own verification wording.
2. **Two new tables**: `ConnectorInstance` (one configured instance of
   a manifest-declared connector — mutable, updated in place, matching
   `AwsConnection`'s existing "plain configuration" shape rather than
   `GoogleDriveConnection`'s append-only OAuth-grant shape, since a
   connector instance's config is ordinary settings, not a credential-
   grant history) and `ConnectorExecution` (append-only execution/
   idempotency ledger).
3. **Secrets**: `ConnectorInstance.config_json` holds only non-secret
   config values. For every `ConfigFieldSpec` the manifest marks
   `secret=True`, the value is stored via `create_encrypted_secret` and
   only its `secret_id` is kept, in `secret_ref_json` (`{field_name:
   secret_id}`) — never the plaintext, matching the existing
   `GoogleOidcSettings`/`OidcProviderSettings` precedent of "secret_id
   column, never a readback."
4. **Idempotency via an explicit key, not silent dedup-by-content.**
   `ConnectorExecution` is unique on `(connector_instance_id,
   idempotency_key)`. A caller (a future route, CLI command, or
   scheduler) is responsible for supplying a stable key per logical
   "run this now" intent (e.g. a fresh token per rendered confirmation
   page, mirroring the CSRF-token convention already used everywhere
   else in this app) — re-submitting the same key returns the existing
   execution unchanged rather than re-invoking the connector or
   re-ingesting a fact. This is deliberately simpler than the
   `file.capture` capability's own content-hash idempotency (already
   handled, unchanged, by `capture_evidence_version`) — it exists for
   the capabilities that don't have an intrinsic content hash to dedupe
   on (`configuration.snapshot`, `identity.population`).
5. **Atomicity without a nested SAVEPOINT.** `run_connector_instance`
   only ever writes an ingestion row (`EvidenceSnapshot`/`Person`/
   `EvidenceArtifactVersion`) *before* writing the success
   `ConnectorExecution` row, and never catches an exception from the
   connector callable, `validate_result`, or an `ingest_*` call except
   to translate it into a `ConnectorExecution(status="failure")` row
   written on an otherwise-untouched session — if that translation
   itself fails (e.g. a DB error), the whole thing propagates and the
   caller's own transaction rollback (matching every other domain
   function in this codebase) removes any partial writes. No connector
   failure can result in a half-ingested fact reaching a committed
   state.
6. **Removal deletes only the `ConnectorInstance` row.** Since
   `source_connection_id` on every ingestion table is a plain string
   (not an FK — see §1), no cascade/orphan handling is needed; already-
   captured evidence is untouched by construction.
7. **No scheduler integration in this slice.** The issue says "using
   existing scheduler patterns *where appropriate*" — this deployment
   has no connector currently running on a schedule (confirmed: neither
   Drive nor AWS syncs go through the `Job`/`ImportJob` tables today),
   so there is no existing scheduled-execution pattern to reuse, and
   inventing one speculatively would be exactly the "do not build
   before there is demand" the epic itself warns against. Manual/
   programmatic invocation only; a future issue can add scheduling once
   a concrete need exists.

## 3. Model

```python
class ConnectorInstance(Base):
    """One configured instance of a manifest-declared connector (#24).
    Mutable, updated in place (like AwsConnection) — plain deployment
    configuration, not a credential-grant history."""

    __tablename__ = "connector_instances"
    id: str
    connector_id: str  # matches a ConnectorManifest.connector_id — not a DB FK
    manifest_version: str  # the manifest version configured against
    display_name: str
    enabled: bool = False
    config_json: str  # non-secret config field values, JSON dict
    secret_ref_json: str  # {field_name: secret_id}, JSON dict — never plaintext
    last_test_at: datetime | None
    last_test_status: str | None  # "success" | "failure"
    last_test_error: str
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error_summary: str
    created_by_user_id: str  # FK users
    created_at, updated_at


class ConnectorExecution(Base):
    """One invocation attempt — execution history and idempotency
    ledger. Append-only."""

    __tablename__ = "connector_executions"
    __table_args__ = (UniqueConstraint("connector_instance_id", "idempotency_key"),)
    id: str
    connector_instance_id: str  # FK
    idempotency_key: str
    status: str  # "success" | "failure" | "error"
    result_summary: str
    error_summary: str
    triggered_by: str  # "manual" | "system"
    actor_id: str | None
    started_at: datetime
    finished_at: datetime | None
```

## 4. Service layer (`app/connector_lifecycle.py`, new)

- `install_connector_instance(session, manifest, *, display_name, config, secret_values, actor) -> ConnectorInstance` — validates the manifest (#24's `validate_manifest`/`check_compatibility`) and `config` against `manifest.config_schema` (required fields present; secret-flagged fields come from `secret_values`, never `config`), stores accordingly, records an audit event.
- `update_connector_instance_config(session, instance, manifest, *, config, secret_values, actor)` — same validation, updates in place.
- `set_connector_instance_enabled(session, instance, enabled, *, actor)`.
- `remove_connector_instance(session, instance, *, actor)` — deletes only the instance row (§2.6).
- `resolve_connector_config(session, instance, *, key) -> dict` — merges non-secret config with just-in-time-resolved secret values, for a connector callable's own use only; never returned to a client.
- `test_connector_instance(session, instance, manifest, test_callable, *, actor) -> bool` — invokes `test_callable(config)`, records `last_test_*`, never raises past this function.
- `run_connector_instance(session, instance, manifest, connector_callable, *, idempotency_key, triggered_by="manual", actor_id=None) -> ConnectorExecution` — the execution/ingestion boundary (§2.5): disabled check, idempotency-key short-circuit, invoke, validate via #24's `validate_result`, dispatch to the matching `app.connectors.ingestion` function by `result.capability`, write the execution row, update instance health fields.

## 5. Security review (adversarial, at design time)

- **Secret leakage**: `resolve_connector_config`'s return value is
  documented as connector-callable-only; no function in this module
  returns a resolved secret to any caller that isn't the connector
  invocation itself. `remove_connector_instance` doesn't touch
  `secrets` rows directly (existing `Secret` lifecycle is untouched;
  an orphaned secret after instance removal is the same accepted
  residue other secret-holding rows already leave — not a new gap).
- **Partial mutation on failure**: covered by §2.5's atomicity
  argument; tested explicitly by simulating an ingestion failure and
  asserting a rollback leaves no partial `EvidenceSnapshot`.
- **Retry/idempotency**: a repeated `idempotency_key` returns the
  existing execution without re-invoking the connector or re-ingesting.
- **Disabled connector cannot execute**: `run_connector_instance`
  checks `instance.enabled` before ever invoking the connector
  callable.
- **Historical evidence survives removal**: by construction (§1) — a
  regression test still proves it explicitly.
- **Connector result validated before ingestion**: `run_connector_instance`
  always calls `validate_result` (capability-overclaiming, status
  vocabulary, payload size, secret-shaped-key scan) before any
  `ingest_*` call — a malicious/malformed result never reaches domain
  state.

## 6. Test strategy

- **Lifecycle**: install validates manifest/config, stores secrets
  correctly (never plaintext in `config_json`), rejects a missing
  required config field; update changes config in place; enable/
  disable toggles execution eligibility; remove deletes only the
  instance row and **historical evidence survives it** (explicit test).
- **Config resolution**: `resolve_connector_config` merges non-secret
  and just-in-time-resolved secret values correctly; a broken
  encryption key degrades safely (matches the existing
  `GoogleOidcSettings`/`OidcProviderSettings` "not usable" pattern,
  never a crash).
- **Connection test**: success and failure paths both update `last_test_*`
  without raising.
- **Execution — success path (fake reference connector,
  `configuration.snapshot`)**: disabled instance is rejected; first
  execution ingests an `EvidenceSnapshot` and records a success
  execution row; a repeated call with the **same** idempotency key
  returns the existing execution and does **not** ingest a second
  snapshot; a **different** idempotency key genuinely runs again
  (proving the dedup is key-scoped, not connector-instance-scoped).
- **Execution — provider failure**: the connector callable raises ->
  a failure execution row is recorded, `last_failure_at`/
  `last_error_summary` update, **no** `EvidenceSnapshot` row is
  created.
- **Execution — malformed/overclaiming result**: `validate_result`
  rejects it -> an error execution row is recorded, no ingestion.
- **Execution — atomicity under a simulated ingestion failure**:
  forces an exception after the ingest call but before the execution
  row commits; asserts a caller-level rollback leaves **zero** partial
  rows (no `EvidenceSnapshot`, no execution row) — proving "failure
  must not partially mutate compliance state" concretely, not just by
  code-reading argument.
- **Malformed/incompatible connector rejection**: installing against
  an incompatible manifest version is rejected before any row is
  created.
- **Security**: no plaintext secret ever appears in `config_json`, an
  audit event detail, or `ConnectorExecution.result_summary`/
  `error_summary`.

## 7. Definition of done

- Connectors can be installed/configured/enabled/disabled/removed
  safely (proven by tests, not yet by a UI).
- Secrets are protected and never normally readable after save.
- Execution history and health state are available (`ConnectorExecution`,
  `ConnectorInstance.last_*` fields).
- Failed/retried executions cannot duplicate authoritative facts
  silently.
- Connector outputs enter core only after #24's contract validation
  and this issue's authorized `run_connector_instance` boundary.
- Historical captured evidence survives connector disable/removal.

## 8. Known deferred/untested paths

- No HTTP route or admin UI to install/configure/run a connector
  interactively — a future issue (likely alongside #26's registry/
  marketplace browsing, which needs one anyway) should add it.
- No scheduled/background execution (§2.7) — manual/programmatic
  invocation only until a concrete scheduling need exists.
- No package/signature verification (#27).
- The fake reference connector used for end-to-end proof lives in the
  test suite only, not under `app/` — it has no product value and
  RULES.md's "no speculative infrastructure" applies to shipped code,
  not test fixtures.
