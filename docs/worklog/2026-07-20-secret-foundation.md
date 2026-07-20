# Feature 5: shared secret/credential foundation

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Fifth phase of the platform pivot (umbrella issue #5, PR #6), second of
the "checkpoint 1" group. Adds a generic `Secret` model and
create/resolve service, reusing `app/crypto.py`'s existing Fernet
encryption rather than introducing new cryptography — foundation for
Feature 6's external database connections.

## Files Changed

- `app/models.py` — `Secret` model (`kind` = `encrypted` | `env_ref`,
  mutually exclusive via a `CHECK` constraint), `SECRET_KINDS`.
- `app/secrets.py` — `create_encrypted_secret`, `create_env_ref_secret`,
  `resolve_secret`, `SecretNotResolvableError`.
- `migrations/versions/75f0a90a40fa_add_secrets_table.py`.
- `tests/test_secrets.py` — 9 tests: encrypt/decrypt round trip, fails
  safely without a key, env-ref resolution + missing-var error, `__repr__`
  never leaks ciphertext/env value, name uniqueness, audit event written
  without the value in its `detail` text.

## Verification

- [x] Tests pass (`pytest` — 245 passed, 1 skipped)
- [x] Lint/format clean
- [x] No UI/API surface yet — this is a backend-only foundation feature
      (as scoped: "shared secrets and credential foundation" before
      external connections). No browser verification applicable.

## Decisions & Alternatives Rejected

- Reused `app/crypto.py` (Fernet, `GRC_ENCRYPTION_KEY`) rather than a new
  encryption scheme — same reasoning as the existing `GoogleDriveConnection`
  refresh-token storage.
- `kind` enforced mutually-exclusive-fields via a single `CHECK`
  constraint rather than two nullable columns with only convention
  keeping them consistent — the DB itself now rejects a row that's
  neither purely `encrypted` nor purely `env_ref`.
- No API/router built in this feature — `Secret`/`app/secrets.py` are
  consumed directly by Feature 6's connection model; a feature that only
  offers `resolve_secret()` server-side has no request surface to
  restrict, so building admin CRUD routes ahead of a real caller would
  be exactly the premature abstraction `CLAUDE.md` asks agents to avoid.

## Known Gaps / Follow-ups

- No key-rotation tooling yet (re-encrypt all `Secret.ciphertext` rows
  under a new key) — deferred until a real rotation need exists;
  documented as a known limitation per the architecture checkpoint.
- `resolve_secret` takes `key` as an explicit parameter rather than
  reading `Settings` itself, keeping the module dependency-free of
  `app.config` — Feature 6's connection-test code is expected to pass
  `settings.encryption_key` through.
