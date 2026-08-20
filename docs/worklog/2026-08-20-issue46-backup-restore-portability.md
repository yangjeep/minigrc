# Issue #46: Self-hosted backup/restore/portability procedure

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds a real, scriptable backup/restore/verify procedure for
self-hosted miniGRC covering the active relational backend (SQLite
snapshot or PostgreSQL dump, exactly one per #22's exclusive-backend
contract), the S3-compatible evidence object store (#32), and a
non-secret configuration checklist. See
`docs/superpowers/specs/2026-08-20-issue46-backup-restore-design.md`
for the full design and `docs/deployment/backup-restore.md` for the
operator-facing procedure.

## Repository-reality corrections found during inspection

- **Issue #9 has no merged runbook at all** — it's still open/
  unimplemented, contrary to what a naive reading of "distinct from
  #9" might suggest. This is the first backup/restore procedure this
  repository has, not a complement to an existing one.
- **#34's audit/PBC export is structurally incapable of serving as a
  backup** — period-scoped, secrets-stripped by design. Confirmed by
  reading `app/audit_package.py` directly rather than assuming.
- **No `scripts/` directory exists anywhere** — `app/cli.py`'s plain
  `argparse` subcommand pattern is the sole established convention for
  operational tooling; new commands belong there.
- **Seven `rebuild_*_projection` functions exist across the codebase,
  none exposed via the CLI** — genuinely new operational surface
  wired up here for `verify-restore`, not a duplicate of anything.

## What changed

- `app/backup.py` (new): `create_backup`, `restore_backup`,
  `verify_restore`, `BackupManifest`, `VerificationReport`.
  - Backup: SQLite via `sqlite3.Connection.backup()` (a live,
    WAL-safe snapshot, not a raw file copy); PostgreSQL via `pg_dump
    --format=custom`, gated on `shutil.which`. Evidence via paginated
    `list_objects_v2` + download + self-consistency hash. Config via
    an explicit allow-list of non-secret `Settings` fields plus a
    checklist of secret env var *names* (never values) that must be
    resupplied — `GRC_ENCRYPTION_KEY` always included.
  - Restore: reconstitutes the DB and re-uploads evidence objects;
    deliberately does not run migrations itself (that's `verify-
    restore`'s/the operator's `migrate` step, matching every other
    `app/cli.py` command's own convention).
  - Verify: real evidence-hash re-check against the *authoritative*
    `EvidenceArtifactVersion.sha256` (not the backup-time hash), all
    seven projection rebuilds in sequence with per-function error
    collection, and a real decryption attempt against one existing
    encrypted credential (if any) — proving the *correct* key was
    supplied, not merely a validly-shaped one.
- `app/cli.py`: new `backup --dest`, `restore --source`,
  `verify-restore` subcommands, following the existing `migrate`/
  `promote-admin` pattern exactly.
- `docs/deployment/backup-restore.md` (new): operator-facing
  procedure, explicit about the `GRC_ENCRYPTION_KEY` prerequisite and
  distinguishing this from #9/#34.

## Findings from self-review (adversarial pass on this issue's own new code)

Two real bugs surfaced while testing this module directly, both fixed
and both now regression-tested — see design doc §5.1:

1. **Wrong connection-string format passed to `pg_dump`/`pg_restore`**
   — `settings.database_url` is a SQLAlchemy URL
   (`postgresql+psycopg://...`); `pg_dump`/`pg_restore` speak plain
   libpq URLs and don't understand the `+psycopg` driver suffix. The
   command would have failed to parse its own connection string.
2. **Password exposure via command-line argument** — even after
   fixing (1), embedding the password directly in a `subprocess`
   argument is visible to any other user on the host with
   process-list access (`ps aux`, `/proc/<pid>/cmdline`). Fixed by
   `_libpq_url_and_env`: strip to a plain `postgresql://` URL via
   `sqlalchemy.engine.url.URL.create` — **not** `URL.set(password=None)`,
   which was directly tested and confirmed to silently leave the
   password in place (SQLAlchemy treats an explicit `None` there as
   "leave this field unchanged," not "clear it") — and pass the
   password via the `PGPASSWORD` environment variable instead, which
   requires higher host privilege to read (`/proc/<pid>/environ`) than
   a plain process listing. Regression:
   `test_libpq_url_strips_driver_suffix_and_moves_password_to_env`.

## Test strategy and results

- **Unit** (`tests/test_backup.py`, 8 tests): config-snapshot allow-list
  never includes a secret value regardless of which integrations are
  configured; manifest JSON round-trips; a missing `pg_dump` binary
  raises a clear `BackupError`; the libpq URL/password-env conversion
  is correct.
- **Integration** (real SQLite + `moto`-backed S3, matching this
  session's established `tests/test_connector_ingestion.py`/
  `tests/test_evidence_artifacts_routes.py` convention): a genuine
  seed → capture evidence + encrypted credential → backup → wipe DB
  file and delete the bucket object → restore → verify-restore cycle
  passes end to end (evidence hash verified, all 7 projections
  rebuild cleanly, decryption check `ok`); a wrong encryption key at
  restore time is caught by the decryption check (`failed`, not
  silently accepted); a corrupted restored evidence object is caught
  by the hash check.
- **Real CLI smoke test** (not moto — an actual `python -m app.cli
  backup`/`restore`/`verify-restore` subprocess cycle against a real
  local SQLite file, no evidence repository configured): confirmed the
  argparse wiring works end to end, not just the underlying functions.
- **Full regression suite**: `pytest -q` — 1052 passed, 7 skipped
  (Postgres-gated), 21 deselected (`uat` marker), 2 xfailed (issue #65,
  pre-existing/unrelated), 0 failed. Delta from the pre-#46 baseline
  (1044, immediately post-#42) is exactly this issue's 8 new tests.
- **Lint/format**: clean (`ruff check .`, `ruff format --check .`,
  including both new docs' embedded code blocks).
- **No headless UAT / no HTTP route**: this is operational CLI
  tooling, not a user-visible web feature — matches the same category
  as `migrate`/`aws-run-checks`, none of which have UAT coverage
  either.

## Known deferred/untested paths

- The live PostgreSQL `pg_dump`/`pg_restore` path is code-complete and
  unit-tested for its connection-string handling, but not exercised
  end-to-end against a real PostgreSQL server in this environment (no
  local Postgres/docker available) — documented in
  `docs/deployment/backup-restore.md` including the manual fallback if
  `pg_dump` isn't installed on the backup host.
- No automated/scheduled backup service — explicitly a non-goal per
  the issue ("documented, scriptable procedures are sufficient for
  MVP, consistent with the self-hosted, boring-monolith posture").
- `verify-restore`'s projection-rebuild check proves each rebuild
  completes without error, not a full before/after state-equality
  comparison across all seven domains — a lighter-weight but still
  real check, matching the issue's own "hash check and/or
  projection-rebuild equivalence" wording (either, not necessarily
  both at maximum strictness).
