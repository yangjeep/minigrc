# Backup, restore, and portability

This is the general self-hosted backup/restore procedure for miniGRC,
covering either supported relational backend (SQLite or PostgreSQL,
exactly one per deployment), the S3-compatible evidence object store,
and application configuration.

Distinct from two other things that sound similar but are not backups:

- **The audit/PBC package export** (`Settings > Audit Package`, or
  `app.audit_package.generate_audit_package`) is a curated,
  auditor-facing bundle scoped to one compliance period. It deliberately
  excludes provider secrets/tokens and most org-wide history outside
  that period — the opposite of what a real backup needs.
- **A managed-PaaS deployment runbook** (issue #9, not yet written at
  the time this doc was added) will cover platform-specific operational
  concerns for one particular managed-Postgres hosting target. This
  document is the general self-hosted procedure; a future PaaS runbook
  should reference it rather than duplicate it.

## The one thing you must protect separately: `GRC_ENCRYPTION_KEY`

Every credential miniGRC stores encrypted at rest — Google Drive
refresh tokens, connector secrets, anything else through
`app/secrets.py` — is decrypted using this key. **The backup procedure
below never writes this key's value anywhere.** If you lose it, every
encrypted credential in a restored backup becomes permanently
undecryptable ciphertext — the database and evidence bytes restore
fine, but nothing that was encrypted with the old key will decrypt with
a different one.

Store `GRC_ENCRYPTION_KEY` in your own secret manager, separately from
the database/evidence backup artifacts this procedure produces. Losing
the database backup is recoverable if you still have a recent one and
the key. Losing the key is not recoverable for anything encrypted with
it.

## What gets backed up

Run:

```bash
python -m app.cli backup --dest /path/to/backup-2026-08-20
```

This produces, inside that directory:

- `manifest.json` — created-at timestamp, which backend was backed up,
  the evidence object count/total bytes, a non-secret configuration
  snapshot, and a checklist of environment variable *names* (never
  values) you must resupply before restoring — always includes
  `GRC_ENCRYPTION_KEY`, plus whichever of `GRC_S3_ACCESS_KEY_ID`/
  `GRC_S3_SECRET_ACCESS_KEY`/OIDC or Drive client secrets were
  configured.
- `database.sqlite3` (SQLite deployments) — a live, WAL-safe snapshot
  taken via SQLite's own backup API, safe to run against a running
  instance.
- `database.pgdump` (PostgreSQL deployments) — a `pg_dump --format=custom`
  dump. Requires `pg_dump` on `PATH`; if it's missing, `backup` fails
  with a clear error rather than a bare exception. If you're backing
  up from a host that doesn't have `pg_dump` installed, run
  `pg_dump --format=custom --file=database.pgdump "$DATABASE_URL"`
  manually from a host that does, then assemble the rest of the backup
  directory around it.
- `evidence/...` — every object from the S3-compatible evidence bucket,
  mirrored under the same key layout it's stored under
  (`evidence/<artifact_id>/<version_id>/content.<ext>`), skipped
  entirely if no evidence repository is configured.

No provider secret, client secret, `GRC_ENCRYPTION_KEY`, or S3
credential value is ever written into this directory. Treat the backup
directory itself as containing sensitive data anyway (evidence content
and database contents are real compliance data) — just not
*credentials*.

## Restoring

On the target instance, with the destination backend already
configured (fresh SQLite path, or an empty PostgreSQL database, and —
if evidence is included — an empty or existing S3-compatible bucket
configured via `GRC_S3_*`):

```bash
python -m app.cli restore --source /path/to/backup-2026-08-20
python -m app.cli migrate
python -m app.cli verify-restore
```

`restore` reconstitutes the database file/dump and re-uploads every
evidence object; it deliberately does not run migrations itself (the
restored database is at whatever schema revision it was backed up at)
— run `migrate` next to bring it up to the currently-deployed app
version, exactly like every other `app/cli.py` command already
expects.

Before running `restore`, ensure every environment variable
`manifest.json` listed under `required_env_vars_not_included` is set to
its **original** value — especially `GRC_ENCRYPTION_KEY`. `restore`
cannot verify this for you; `verify-restore` is what actually proves it.

## Verifying a restore

```bash
python -m app.cli verify-restore
```

Runs three real, executable checks against the restored-and-migrated
instance:

1. **Evidence integrity** — downloads every `EvidenceArtifactVersion`'s
   object from the bucket and re-hashes it against the *database's own*
   `sha256` column (not the backup-time hash) — proving the restored
   bytes are exactly what the database claims they should be.
2. **Projection rebuild equivalence** — calls every domain's
   `rebuild_*_projection` function (policy lifecycle, control
   occurrences, control-definition versions, evidence artifact
   versions, control tests, compliance scope, findings) and reports any
   failure — proving the event store round-trips cleanly.
3. **Encryption key check** — attempts to decrypt one real encrypted
   credential (if any exist) with the currently-configured
   `GRC_ENCRYPTION_KEY`. `ok` means the correct key was supplied;
   `failed` means the wrong key is set — fix this before doing anything
   else; `no_encrypted_data_found` is a normal, passing outcome for a
   deployment with no encrypted credentials yet.

Exit code `1` and a non-empty error section mean at least one check
failed — do not consider the restore complete until this command
reports `verify-restore passed.`

## Cross-backend migration is out of scope

Restoring a SQLite backup to a PostgreSQL deployment (or vice versa) is
not supported by this procedure — miniGRC's exclusive-backend contract
means a deployment commits to one backend, and this procedure restores
into the same backend it backed up from. A SQLite-to-PostgreSQL cutover
is a separate, explicit offline migration, not covered here.
