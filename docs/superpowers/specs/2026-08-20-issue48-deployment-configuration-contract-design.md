# Issue #48: unified self-hosted deployment/configuration contract

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** documentation

## Goal

Tie the already-tracked configuration pieces — DB backend (#22), evidence
storage backend (#32), auth mode (#16-20), BYOK (#15) — into one
documented, validated self-hosted configuration contract, the way #22
made "SQLite XOR PostgreSQL" explicit and startup-validated instead of an
implicit convention. See issue #48 for the full framing.

## Repository-reality check (read first)

The issue's execution prompt assumes each axis has its own settled
config surface to document, and asks to "add a startup-time validation
check... for any genuinely ambiguous/invalid combination... not yet
rejected." Direct inspection of every axis' actual merged code found:

1. **BYOK/AI (#15) has not landed at all.** `app/config.py` has zero
   AI-provider/BYOK settings fields — confirmed by reading the full
   `Settings` class. The issue's assumption that "#15 defines its own
   config requirements" describes a future state, not current
   repository reality. This document says so plainly rather than
   fabricating settings that don't exist.

2. **Every optional-feature axis already has a deliberate, tested,
   consistent "incomplete config degrades gracefully" pattern** — this
   is the single most important finding, because it means most of what
   the issue asks for ("which combinations are invalid, add validation
   for them") does not apply the way #22's DATABASE_URL/GRC_DATABASE_PATH
   check does:
   - Google OAuth (`app/google_oidc_config.py::resolve_google_oidc_config`):
     its own docstring states "a broken Google OAuth config must never
     crash a request or lock out local/break-glass login" — a partially
     usable admin-configured or env-var-configured row resolves to
     `usable=False`, never a startup crash.
   - Generic OIDC (`app/oidc_config.py::resolve_oidc_config`): same
     shape, same reasoning.
   - Evidence/S3 storage (`app/object_storage.py::build_s3_client`,
     `app/routers/evidence_artifacts.py`): an incomplete S3 config
     raises `ObjectStorageNotConfiguredError` → HTTP 503 *at the route
     that needs it*, not at startup — tested directly
     (`test_new_artifact_form_fails_cleanly_when_not_configured`).
   - Google Drive (`Settings.google_drive_configured`): same
     all-or-nothing bool pattern as the other three.

3. **The DB backend axis is genuinely different in kind, not degree.**
   `DATABASE_URL` and `GRC_DATABASE_PATH` are two *complete*, *each
   individually valid* ways to select a database — coexistence is a real
   "which one did you mean?" ambiguity with no safe default, which is
   exactly why #22 added a hard startup failure
   (`reject_ambiguous_database_config`). A *partially*-set feature-flag
   group (e.g. three of four required S3 variables) is not the same
   shape of problem — it has an unambiguous, already-correct resolution
   ("not configured yet"), and every one of the four optional-feature
   modules above already encodes exactly that on purpose.

4. **Conclusion:** retrofitting a new hard-startup-fail check onto the
   already-graceful-degradation axes would contradict an existing,
   deliberate, tested architectural pattern across four independent
   modules — not close a gap. No new runtime validation is added by this
   issue. The acceptance criterion "any genuinely ambiguous/invalid
   combination newly identified gets explicit startup-time validation"
   is satisfied by an empty set, stated explicitly rather than silently
   assumed.

This is a "challenge the issue when repository reality proves an
assumption wrong" situation per `.agent/LOOP.md` §3/§4 — surfaced here
rather than either silently implementing an unwanted behavior change or
silently doing nothing.

## What this issue actually delivers

`docs/deployment/configuration-contract.md` (new): one page describing
every axis (DB backend, evidence storage, auth mode(s), BYOK), which
combinations are valid, which are actually invalid (only the DB backend
XOR), and the shared "incomplete optional feature = gracefully disabled,
never a crash" pattern — named and cross-referenced explicitly for the
first time, since it existed in four separate docstrings but nowhere as
one stated contract. Cross-links `docs/deployment/kubernetes.md`,
`docs/deployment/authentication.md`, `docs/deployment/backup-restore.md`,
and issues #9/#15/#16-20/#22/#32 rather than re-deriving their content.

## Verification

`tests/test_deployment_configuration_contract.py` exercises the actual
`Settings` properties this document describes, for representative
combinations of each axis, as a living proof that the document matches
current code — not a new feature test, a documentation-accuracy guard
that would fail if any of these properties' semantics changed without
the doc being updated.

No headless/Desktop UAT: documentation-only change with one small
verification test, per `.agent/TESTING.md` §2's "Documentation-only
change: no headless or Desktop UAT required unless it changes the
operating/test contract itself" — it doesn't.
