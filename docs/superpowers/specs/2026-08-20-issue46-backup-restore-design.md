# Issue #46: Self-hosted backup/restore/portability procedure

## 1. Repository-reality check

- **Issue #9 ("Production deployment and operational readiness") has no
  merged runbook at all** — it is still open/unimplemented. The only
  backup/restore-adjacent doc content anywhere in the repository is one
  incidental sentence in `docs/deployment/kubernetes.md` about
  operating a production-grade Postgres. This issue is not "avoiding
  duplication with #9's existing runbook" (there isn't one yet) — it is
  the first backup/restore procedure this repository has, scoped for
  the general self-hosted case. If/when #9 lands a PaaS-specific
  runbook, it should reference this one for the general mechanism and
  add only its platform-specific specifics on top.
- **Issue #34's audit/PBC export (`app/audit_package.py`,
  `generate_audit_package`) is confirmed structurally incapable of
  serving as a backup**: it is period-scoped (filters to one
  `ComplianceScope` audit period), explicitly excludes all
  secrets/tokens/credentials, and its own docstring states this is
  deliberate. A real restore needs the opposite: everything, not one
  period, and the encryption key path documented (never silently
  stripped).
- **`app/cli.py` is a plain `argparse` module** — one subcommand per
  operator action, no `scripts/` directory exists anywhere in the
  repository. New backup/restore/verify commands belong there as new
  subcommands, matching `migrate`/`create-user`/`aws-run-checks`'s
  existing shape exactly (resolve settings → build engine → do the
  thing → print a plain, credential-free status line).
- **`GRC_ENCRYPTION_KEY` is the one field that makes or breaks a
  restore.** Every encrypted secret at rest — `GoogleDriveConnection.
  encrypted_refresh_token`, every `Secret` row with `kind="encrypted"`
  (AWS external IDs, connector secrets) — becomes permanently
  undecryptable ciphertext if this key is lost, and it must be the
  *exact same* key used at backup time, not merely "a" valid Fernet
  key. It must never be written into the backup bundle itself (bundling
  the key with the data it protects defeats the point of separating
  them) — it is operator-held, out-of-band, documented as a
  prerequisite, never captured by `create_backup`.
- **No "list objects under a prefix" helper exists in
  `app/object_storage.py`** — a backup needs to call boto3's own
  paginated `list_objects_v2` directly.
- **Seven `rebuild_*_projection` functions exist across the codebase**
  (policy lifecycle #31, control occurrences #11, control-definition
  versions #42, evidence artifact versions #32, control tests,
  compliance scope, findings) — all built on the shared
  `app/events.py::rebuild_projection`, but **none are exposed via the
  CLI today**, only ever called from tests. This is exactly the
  integrity-verification primitive issue #46 needs for its "projection
  rebuild reproduces expected state" acceptance criterion — wiring them
  into a new `verify-restore` command is new operational surface, not
  a duplicate of anything existing.

## 2. Scope: what "backup" actually captures

Three things, matching the issue's own required behavior, each backed
up **independently reasoned about** rather than as one opaque blob:

1. **The active relational backend** — exactly one of:
   - SQLite: a live, WAL-safe snapshot via Python's
     `sqlite3.Connection.backup()` API (not a raw file copy, which
     risks capturing a mid-write, inconsistent file if the app is
     running concurrently — `.backup()` is SQLite's own documented
     mechanism for a consistent live copy).
   - PostgreSQL: `pg_dump --format=custom`, shelled out via
     `subprocess`, gated on `shutil.which("pg_dump")` — if missing, a
     clear `BackupError` pointing at the documented manual procedure
     rather than a bare `FileNotFoundError`.
2. **The S3-compatible evidence object store (#32)** — every object
   under the `evidence/` prefix, listed via paginated
   `list_objects_v2` (the store's own key layout,
   `evidence/<artifact_id>/<version_id>/content.<ext>`, needs no
   database lookup to enumerate), downloaded to
   `<dest_dir>/evidence/...`, each recorded in the manifest with its
   own observed sha256 (self-consistency: did what was downloaded hash
   to what was recorded, at backup time).
3. **Non-secret configuration**, as a checklist, not a value dump: a
   snapshot of every non-secret `Settings` field (`app_env`,
   `log_level`, `public_base_url`, `s3_bucket`/`s3_region`/
   `s3_endpoint_url`, `oidc_issuer`, `oidc_display_name`, etc.) plus
   boolean flags for which secret-backed integrations were configured
   (`google_oidc_configured`, `oidc_configured`,
   `google_drive_configured`, `evidence_repository_configured`) — so
   an operator restoring knows *what* to re-supply from their own
   secret store, without the backup ever holding the secret *values*.
   `database_url`/`encryption_key`/every `*_client_secret`/
   `s3_access_key_id`/`s3_secret_access_key` are never included, even
   masked — their *presence as a requirement* is recorded, never their
   value.

`BackupManifest` (a plain dataclass, JSON-serialized as
`manifest.json` in the backup directory) ties these three together:
`created_at`, `backend`, `db_backup_filename`, evidence object
count/total bytes, the config snapshot, and
`required_env_vars_not_included` (a checklist of names, e.g.
`GRC_ENCRYPTION_KEY`, `GRC_S3_ACCESS_KEY_ID`, whichever `*_client_secret`
fields were non-empty at backup time).

## 3. Scope: restore

`restore_backup(settings, backup_dir)`:

1. Restore the DB backend matching `manifest.backend` to
   `settings.resolved_engine_target` — SQLite: copy the snapshot file
   into place; PostgreSQL: `pg_restore --clean --if-exists`, same
   `shutil.which` gate as backup.
2. Re-upload every backed-up evidence object into the (freshly
   configured, possibly empty) bucket via the existing
   `object_storage.upload_object`.
3. Print the manifest's `required_env_vars_not_included` checklist as
   an explicit, actionable reminder — restore does not and cannot
   verify these were supplied correctly; that is `verify-restore`'s
   job (§4).

Deliberately **not done by `restore_backup` itself**: running
migrations. The restored DB is already at whatever schema revision it
was backed up at; bringing it forward to the currently-deployed
app version is the same `init_db`/`migrate` step every other
`app/cli.py` command already does first — `verify-restore`'s CLI
wrapper does this before verifying, matching that established
convention, rather than `restore_backup` silently mutating schema as a
side effect of what should be a pure data-restore operation.

## 4. Scope: verify-restore (the integrity-verification step)

`verify_restore(session_factory, settings) -> VerificationReport`,
run against the *already-restored-and-migrated* instance:

1. **Evidence hash check**: query every `EvidenceArtifactVersion`,
   download its `object_key` from the (now-restored) bucket, re-hash,
   compare to the *authoritative* `EvidenceArtifactVersion.sha256` —
   not the backup-time hash recorded in the manifest, since the whole
   point is proving the restored bytes match what the database
   actually claims, independent of what backup-time observed.
2. **Projection rebuild equivalence**: call all seven
   `rebuild_*_projection` functions in sequence, collecting exceptions
   per-function rather than aborting on the first one — a genuinely
   new operational entry point for existing, already-tested rebuild
   logic, proving the restored event store round-trips cleanly through
   every domain that has one.
3. **Decryption sanity check**: if any `Secret` row with
   `kind="encrypted"` or `GoogleDriveConnection.encrypted_refresh_token`
   exists, attempt to decrypt one with the *currently configured*
   `settings.encryption_key`. Success proves the operator supplied the
   *correct* key post-restore (not just *a* validly-shaped one) —
   directly answering #46's own "secret/credential handling must be
   explicit" requirement with a real, executable check rather than a
   documentation-only promise. `"no_encrypted_data_found"` is a valid,
   non-failing outcome (a fresh/small deployment may have none yet).

## 5. Security review

- `encryption_key` is never read into, written to, or logged by any
  function in this module — `create_backup` never touches it at all;
  `verify_restore`'s decryption check calls `app.crypto.decrypt`
  exactly the way every other consumer does, and only ever logs
  success/failure, never the key or the decrypted plaintext.
- The non-secret config snapshot is built from an explicit allow-list
  of field names, not "every `Settings` field except a deny-list" —
  a newly-added secret-shaped `Settings` field in the future is
  invisible to the snapshot by default rather than silently included.
- `pg_dump`/`pg_restore` invocations pass the connection URL as a
  `subprocess` argument, not through a shell (`shell=False`, argument
  list, not a formatted string) — no shell-injection surface from a
  URL containing special characters.
- Backup/restore are pure Python-callable functions independent of the
  CLI's argparse layer, matching the codebase's existing separation
  (`app/control_occurrences.py`'s domain functions vs.
  `app/cli.py`'s thin wrappers) — authorization for who may *invoke*
  the CLI is an operational/host-access concern (same trust boundary
  every other `app/cli.py` command already has, e.g. `promote-admin`),
  not something this module re-implements.

### 5.1 Finding from self-review, fixed before shipping

The initial implementation passed `settings.database_url` straight to
`pg_dump`/`pg_restore` as a command-line argument. Two real problems,
found by actually running it, not just reading it: (1) `database_url`
is a *SQLAlchemy* URL (e.g. `postgresql+psycopg://user:pass@host/db`)
— `pg_dump`/`pg_restore` speak plain libpq URLs and don't understand
the `+psycopg` driver suffix, so the command would have failed to
parse its own connection string; (2) even once fixed to strip the
suffix, embedding the password directly in a command-line argument is
visible to any other user on the same host with process-list access
(`ps aux`, `/proc/<pid>/cmdline`) — a real, if narrow, credential-
exposure surface. Fixed by `_libpq_url_and_env`: convert to a plain
`postgresql://` URL via `sqlalchemy.engine.url.URL.create` (not
`URL.set(password=None)`, which — confirmed by direct testing — treats
an explicit `None` as "leave this field unchanged," not "clear it,"
and silently leaves the password in the rendered string) and pass the
password via the `PGPASSWORD` environment variable instead, which is
only visible via `/proc/<pid>/environ` — requiring higher host
privilege than a plain process listing. Regression:
`test_libpq_url_strips_driver_suffix_and_moves_password_to_env`.

## 6. Test strategy

- Unit: config-snapshot allow-list excludes every secret-shaped field
  even when set; manifest JSON round-trips; a missing `pg_dump`/
  `pg_restore` binary raises a clear `BackupError`, not a bare OS
  exception.
- Integration (SQLite + real `moto`-backed S3, matching this session's
  established `tests/test_connector_ingestion.py`/
  `tests/test_evidence_artifacts_routes.py` convention): a genuine
  create → capture evidence → back up → wipe DB file and bucket →
  restore → verify-restore cycle, proving the evidence hash check
  passes, all seven projection rebuilds complete without error, and
  the decryption sanity check succeeds against real encrypted data
  (a seeded `GoogleDriveConnection`).
- A deliberately corrupted restored evidence object is caught by the
  hash-check step (proving the check isn't a no-op).
- A wrong `encryption_key` at restore time is caught by the decryption
  sanity check, not silently accepted.

## 7. Definition of done

- `python -m app.cli backup --dest <dir>` produces a manifest plus DB
  snapshot plus evidence mirror, with no secret values ever written to
  it.
- `python -m app.cli restore --source <dir>` reconstitutes a working
  instance's DB and evidence store, printing the required-env-vars
  checklist.
- `python -m app.cli verify-restore` proves evidence integrity,
  projection-rebuild equivalence, and correct-encryption-key
  possession, all as real executable checks.
- A full SQLite backup→restore→verify cycle is locally reproducible
  and tested; the PostgreSQL path is documented and code-complete,
  verified by CI's live-PostgreSQL job.
- `docs/deployment/backup-restore.md` documents the procedure for an
  operator, explicit about the `GRC_ENCRYPTION_KEY` prerequisite.
