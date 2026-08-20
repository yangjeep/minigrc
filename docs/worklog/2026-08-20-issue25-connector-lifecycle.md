# Issue #25: Connector installation, configuration, secrets, health, and execution lifecycle

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Summary

Builds the core lifecycle for installing, configuring, enabling/
disabling, testing, and executing connectors on top of #24's manifest/
capability/result contract, without letting connector runtime state
become compliance truth. Backend service layer only, matching #24's
own accepted scope — see
`docs/superpowers/specs/2026-08-20-issue25-connector-lifecycle-design.md`
for the full design and repository-reality grounding.

## What changed

- `app/models.py` — `ConnectorInstance` (installed/configured instance,
  mutable like `AwsConnection`) and `ConnectorExecution` (append-only
  execution/idempotency ledger, real FK to `ConnectorInstance`).
  Migration `d3a8f61c9e27`.
- `app/connector_lifecycle.py` (new) — `install_connector_instance`,
  `update_connector_instance_config`, `set_connector_instance_enabled`,
  `remove_connector_instance`, `resolve_connector_config`,
  `test_connector_instance`, and the execution/ingestion boundary
  `run_connector_instance`.

## Test strategy and results

- **Lifecycle** (`tests/test_connector_lifecycle.py`, 18 tests): install
  validates the manifest and stores a config secret without ever
  putting the plaintext in `config_json`/`secret_ref_json`/an audit
  detail; rejects a missing required config or secret field; rejects
  an incompatible manifest before creating anything; update changes
  config in place and **keeps the existing secret when a blank value
  is submitted** (a genuine bug caught while writing this test — the
  update path was reusing install's "secret is always required" check,
  which would have forced re-entering an unchanged credential on every
  edit); enable/disable toggles and audits; remove deletes the instance
  **and its own execution history** but leaves historical evidence
  (`EvidenceSnapshot`) completely untouched — proven with an explicit
  assertion, not just by code-reading argument.
- **Config resolution**: merges non-secret and just-in-time-resolved
  secret values correctly; a broken encryption key degrades to `None`
  for that field rather than crashing or returning garbage as if valid.
- **Connection test**: success and failure paths both update
  `last_test_*` without raising past the function.
- **Execution — fake reference connector** (test-only, lives in this
  file per RULES.md's "no speculative infrastructure" — see design doc
  §8): disabled instance rejected before any execution row is created;
  success ingests an `EvidenceSnapshot` and records a success
  execution; a **repeated** idempotency key returns the existing
  execution and does not re-ingest; a **different** key genuinely runs
  again; a provider failure (the connector callable raises) records a
  failure execution with no ingestion; a capability-overclaiming or
  unregistered-capability result records an "error" execution with no
  ingestion; a **genuine partial-write-then-downstream-failure**
  scenario (an ingestion function that successfully adds an
  `EvidenceSnapshot` and then raises) is rolled back completely —
  proven with a monkeypatched ingestion function, not just by
  code-reading argument.
- **Full regression suite**: `pytest -q` — **972 passed, 6 skipped**
  (Postgres-gated), **21 deselected** (`uat` marker), **2 xfailed**
  (issue #65, pre-existing/unrelated), **0 failed**. Delta from the
  pre-#25 baseline (954) is exactly this issue's 18 new tests.
- **Lint/format**: clean (including the design doc's embedded Python
  code blocks).
- **No headless UAT / no HTTP route**: no user-visible surface in this
  issue, matching #24's own accepted precedent.
- **Migration**: clean `alembic upgrade head`; `alembic check` reports
  no model/migration drift.

## Defects found and fixed during adversarial review / self-testing

1. **`update_connector_instance_config` rejected a legitimate "leave
   the secret unchanged" update** — it reused install's validation,
   which requires every required secret field to be present in the
   *current* call's `secret_values`, with no way to say "I'm not
   changing this one." Fixed by threading the instance's *existing*
   secret field names into validation so a blank value on an update is
   correctly treated as "keep existing," matching every other
   secret-holding settings form in this app.
2. **`remove_connector_instance` raised a foreign-key violation** when
   an instance had any execution history — `ConnectorExecution` has a
   real FK to `ConnectorInstance` (deliberately, for referential
   integrity of the operational ledger), which the original design
   note ("removal deletes only the instance row") didn't account for.
   Fixed by deleting the instance's own execution rows first —
   execution history is operational/runtime state, not compliance
   evidence (RULES.md §1), so this is safe; the actual evidence tables
   (`EvidenceSnapshot`/`EvidenceArtifactVersion`/`Person`) are untouched
   either way, since their `source_connection_id` is a plain string,
   not an FK.
3. **A capability-overclaiming/malformed result was misclassified as
   "failure" instead of "error"** — the exception-type check in
   `run_connector_instance`'s except block only recognized
   `ConnectorLifecycleError`, not #24's own `ConnectorContractError`.
   Fixed by checking both.
4. **A genuine atomicity gap**: the original except-block handling
   only called `session.flush()` before writing the failure execution
   row — if an ingestion function had *already* written a partial row
   to the session before raising, that partial write would still be
   pending and would ride along with the caller's eventual commit,
   silently defeating the "failure must not partially mutate compliance
   state" requirement. Fixed by calling `session.rollback()` first,
   before ever writing the failure execution row — verified with a
   dedicated test that monkeypatches the ingestion dispatch table with
   a function that writes an `EvidenceSnapshot` and then raises,
   proving the snapshot never survives.
5. **A pytest name collision**: importing the production function
   `test_connector_instance` directly into a test module made pytest
   try to collect it as a test itself (`fixture 'session' not found`).
   Fixed by aliasing the import (`as check_connector_instance`) in the
   test file — the production name itself is accurate and was kept.

## Known deferred/untested paths

See design doc §8: no HTTP route or admin UI (deferred to whichever
issue defines the operator-facing surface — likely alongside #26's
registry/marketplace); no scheduled/background execution (no existing
scheduler pattern to reuse, and none invented speculatively); no
package/signature verification (#27).
