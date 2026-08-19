# Issue #32: S3-compatible evidence artifact repository

**Date:** 2026-08-19
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds a miniGRC-owned evidence artifact repository: `EvidenceArtifact`
(logical identity) + `EvidenceArtifactVersion` (immutable, fully
event-sourced captured version, unlike #31's `PolicyVersion` — see
design doc §2 for why this domain makes the fuller event-sourcing
choice) with an S3-compatible object-storage backend
(`app/object_storage.py`, `app/evidence_repository.py`). See
`docs/superpowers/specs/2026-08-19-issue32-evidence-repository-design.md`
for the full design, repository-reality inventory, and explicit scope
boundaries (test/finding/period links deferred until #13 exists;
connector capture paths deferred until a real connector — #23-28 —
needs them; only manual upload is wired to a route in this slice).

## What changed

- `app/config.py` — `s3_endpoint_url`/`s3_bucket`/`s3_access_key_id`/
  `s3_secret_access_key`/`s3_region` settings + `evidence_repository_configured`
  property (mirrors `google_drive_configured`'s "all required fields or
  disabled" pattern). Plain, operator-set deployment config — same
  category as `DATABASE_URL`, not a DB-stored encrypted `Connection`.
- `app/models.py` — `EvidenceArtifact`, `EvidenceArtifactVersion` (event-
  sourced projection over the `"evidence_artifact_version"` DomainEvent
  aggregate), `ControlOccurrenceEvidenceArtifact` (mirrors
  `ControlOccurrenceEvidence` exactly — the "future general evidence-
  upload mechanism" #11's own docstring anticipated).
- `app/object_storage.py` (new) — S3 client construction (`boto3`,
  already a pinned dependency, reused as-is), stream/hash/bound/validate
  core mirroring `app/storage.py`'s local-storage pattern, content
  sniffing for pdf/docx/png/jpeg, path/key-injection-safe server-
  generated object keys.
- `app/evidence_repository.py` (new) — `capture_evidence_version`
  (idempotent by content hash; uploads to S3 *before* any DB write;
  cleans up the orphan S3 object if the subsequent event append fails)
  and `rebuild_evidence_artifact_version_projection`.
- `app/control_occurrences.py` — new `link_evidence_artifact_version`
  function + `ControlOccurrenceEvidenceArtifactLinked` event type,
  mirroring `link_evidence`/`ControlOccurrenceEvidenceLinked` exactly.
- `app/routers/evidence_artifacts.py` (new) — list/new/create/detail/
  upload-version/download/link-occurrence routes, `require_login` for
  reads, `require_write_access` for mutations (#37's convention).
- New migration `d81f6c3a9e07` — three brand-new tables, no data
  backfill (same category as #11's `ControlOccurrence`, unlike #31's
  `PolicyVersion` — #41's migration convention doesn't apply here).
- `pyproject.toml` — added `moto[s3]` as a dev dependency (an in-process,
  real S3-API-compatible test backend — see design doc §7 for why this
  satisfies the issue's "exercise a selected S3-compatible test
  backend" requirement without a live Docker/MinIO service container).

## A foundational bug found during adversarial testing, and why it was NOT fixed here

While adversarially testing "object-store failure produces no partial
authoritative mutation" (an explicit issue requirement), found that
`session.rollback()` does not always undo a `DomainEvent` row appended
via `app/events.py`'s `begin_nested()`-based SAVEPOINT mechanism — a
genuine, pre-existing SQLAlchemy/pysqlite interaction bug affecting
**every** event-sourced aggregate in this app (control occurrences,
#31's policy versions, this issue's evidence artifact versions), not
anything specific to #32.

**Reachability, checked directly**: this bug requires an earlier
`session.commit()` in the same session before the SAVEPOINT release —
no web route in this app ever commits mid-request (`app/deps.py::get_db`
commits exactly once, at the very end), and the only other
`session.commit()` call sites (`app/jobs.py`, `app/import_directory.py`)
never do event-sourced work. **The bug is latent, not currently
reachable by any real request or background job** — confirmed, not
assumed.

The standard SQLAlchemy-documented fix (disable pysqlite's
`isolation_level`, emit an explicit `BEGIN`/`BEGIN IMMEDIATE`) was
implemented, verified to fix the correctness bug, then **measured
directly** to reintroduce issue #55's exact concurrency regression —
"database is locked" failures at as few as 2 concurrent requests (down
from #55's baseline of ~25), because it holds a transaction-level lock
for every session's *entire* activity, including plain reads, instead
of only the brief window pysqlite's own implicit BEGIN uses immediately
before a write. That trade (severe availability regression for every
concurrent user, to close a currently-unreachable correctness gap) was
not acceptable to ship unilaterally. **Reverted**, and filed as
**issue #65** for a more surgical fix, with a ready-made, `xfail(strict=True)`
regression test (`tests/test_sqlite_savepoint_rollback.py`) so:

- the gap is not silently forgotten;
- a future fix attempt has an instant correctness check;
- if any future code path *does* introduce a mid-session commit before
  event-sourced work, the xfail test's existence (and this worklog)
  make the risk discoverable rather than hidden.

This required no follow-up change to #32's own code: `capture_evidence_version`'s
S3-side cleanup on failure is independent of this SQL-transaction issue
and works correctly regardless (tested,
`test_db_failure_after_successful_upload_cleans_up_the_orphan_object`
passes for real); only the DB-row-survives-rollback half of that
scenario is the deferred, `xfail`-marked gap
(`test_db_row_does_not_survive_rollback_after_projector_failure`).

## Test strategy and results

- **Unit** (`tests/test_object_storage.py`, 19 tests): hash/bound/
  validate/key-generation core, path/key-injection sanitization,
  content sniffing (pdf/png/jpeg accept/reject, plain-text formats
  trusted by extension), `build_s3_client` configured/not-configured.
- **Integration** (`tests/test_evidence_repository.py`, moto-backed, 9
  tests, 1 xfailed): first-version capture; byte-identical download
  round trip; duplicate-unchanged-capture idempotency (no new version/
  event, redundant temp file cleaned up without ever uploading);
  changed bytes create a new immutable version with the first version's
  bytes/hash unchanged; S3 upload failure leaves no DB row/event; S3
  orphan-object cleanup after a DB-side failure (real, passing); the
  DB-row-survives-rollback half of that same scenario (deferred,
  `xfail`); rebuild reproduces identical state; rebuild doesn't disturb
  an uninvolved second artifact.
- **Route/regression** (`tests/test_evidence_artifacts_routes.py`, 11
  tests): create+download round trip over real HTTP, new-version
  upload, duplicate-upload no-op, unsupported file type rejected,
  spoofed-extension rejected, link-to-occurrence (+ duplicate-link
  rejected), reader/auditor 403 on mutations with no state change,
  reader can still download, "not configured" 503.
- **Migration** (`tests/test_evidence_artifact_migration.py`, SQLite: 4
  tests; Postgres-gated: 1, skipped locally): tables created, unique
  constraint enforced, downgrade removes tables, upgrade/downgrade/
  upgrade round trip.
- **Headless UAT** (`tests/uat/test_evidence_artifacts.py`, required,
  not skippable): full manual-capture → download → second-version-
  capture (first version's bytes still verify) → link-to-occurrence
  journey over a real socket with a moto-backed S3; reader-403 check.
  `GRC_UAT_MODE=1 pytest -m uat tests/uat` — **8 passed, 1 skipped**
  (Postgres-gated SOC 2 lifecycle parametrize).
- **Full regression suite**: `pytest` — **684 passed, 4 skipped**
  (Postgres-gated), **9 deselected** (`uat` marker), **2 xfailed**
  (the deferred pysqlite/SAVEPOINT bug, both expected and `strict`).
- **Lint/format**: `ruff check .` and `ruff format --check .` — clean.
- **Concurrency regression check** (specifically re-verified after the
  `app/db.py` revert): `test_moderately_concurrent_writes_do_not_lock_the_database`
  — 5/5 clean runs, confirming #55's fix is fully intact.
- **Adversarial review** (`.agent/LOOP.md` §9, performed directly):
  confirmed reader/auditor cannot mutate (tested); confirmed idempotent
  capture prevents duplicate events/versions (tested, both directions);
  confirmed the upload-before-commit ordering prevents any DB row from
  ever referencing a non-existent S3 object on the *upload* failure
  path (tested); found and responsibly deferred the SAVEPOINT/rollback
  gap on the *reverse* failure path (see above); confirmed no secrets
  (`s3_secret_access_key`/`s3_access_key_id`) appear in any error
  message, log, or template; confirmed `s3_endpoint_url` is trusted
  deployment config, not per-request attacker input (documented
  reasoning in the design doc, same category as `DATABASE_URL`);
  confirmed no scope expansion beyond the design doc's explicit
  boundaries (no connector implementation, no test/finding/period
  links to domains that don't exist yet).

## Known deferred/untested paths

- Issue #65 (the pysqlite/SAVEPOINT rollback bug) — deliberately
  deferred; see above.
- Connector capture path (`source_type="connector"` is supported by
  `capture_evidence_version`'s signature but nothing calls it yet) —
  no real connector exists to wire up (#23-28).
- `linked_test_id`/`linked_finding_id`/`linked_period_id` — not added;
  those domains don't exist yet (#13/#30).
- PostgreSQL migration test was not run against a live PostgreSQL
  server in this sandbox (no local instance available); will run in
  CI's `test-postgres` job (now actually exercising every
  Postgres-gated test, per this session's earlier CI fix).
