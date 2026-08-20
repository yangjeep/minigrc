# Issue #26: Versioned connector registry and official connector repository structure

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Summary

Builds a versioned, auditable connector registry (`registry/registry.json`)
with discovery/filtering over #24-compatible manifests, and resolves
the "official connector repository" question using the issue's own
sanctioned fallback rather than creating new external infrastructure.
See `docs/superpowers/specs/2026-08-20-issue26-connector-registry-design.md`.

## A decision surfaced rather than made silently

The issue asks for a separate `yangjeep/minigrc-connectors` GitHub
repository, but explicitly offers an alternative: "document a
repository-grounded alternative if current project constraints make a
separate repo premature." Creating a new external repository is a
persistent, hard-to-reverse action on shared account state that this
agent's own operating rules require explicit user authorization for —
not something to decide mid-issue. Since the issue itself pre-
authorizes exactly this fallback, the resolved choice was to keep the
registry in this repository and document (`connectors/README.md`) the
deferred external-repo option rather than unilaterally creating one.

## What changed

- `registry/registry.json` (new) — the versioned, git-audited registry.
  Populated with the three connectors that genuinely already exist
  (AWS CloudTrail/IAM, Google Drive, Google Workspace Directory, all
  from #24's `app/connectors/adapters.py`) — no speculative/fake
  entries in the shipped file.
- `app/connector_registry.py` (new) — `RegistryEntry`, `load_registry`,
  `to_manifest`, `filter_by_capability`, `filter_compatible`,
  `install_from_registry` (the only path from a registry entry into an
  installed instance — a thin wrapper around #25's
  `install_connector_instance`).
- `connectors/README.md` (new) — documents the deferred external-repo
  decision, the intended future layout, and what already exists today
  (`app/connectors/`, `registry/registry.json`). No speculative empty
  subdirectories were created for connectors that don't have real code
  yet.

## Test strategy and results

- **Load/validation** (`tests/test_connector_registry.py`, 12 tests):
  a valid registry loads; a missing file, malformed JSON, a missing
  required field, an invalid `verification_status`, and a duplicate
  `(connector_id, version)` pair are all rejected with a clear
  `RegistryError`.
- **`to_manifest`**: round-trips every field into a real
  `ConnectorManifest` that itself passes #24's `validate_manifest`.
- **Filtering**: `filter_by_capability` correctly excludes a deprecated
  entry even when its capability matches; `filter_compatible` fails
  closed by default with no override, proven against a mix of
  compatible/incompatible entries.
- **No code execution during discovery**: a fake entry's
  `package_location` points at a function that raises if ever called
  — loading and filtering it never triggers that exception, proving
  neither `load_registry` nor the filter functions import/execute
  anything a `package_location` string names.
- **Install handoff**: publishing a fake reference-connector entry,
  discovering it, and installing it via `install_from_registry`
  produces the exact same `ConnectorInstance` shape #25's own tests
  already verify (secret never in plaintext, starts disabled) —
  through the real #25 lifecycle boundary, not a shortcut.
- **Regression guard on the shipped file**: a dedicated test loads the
  real, git-tracked `registry/registry.json` (path resolved relative
  to the test file, not hardcoded — matching this repo's own
  established convention from the earlier #12 hardcoded-path CI bug)
  and validates every entry, catching a future hand-edit mistake before
  it ships.
- **Full regression suite**: `pytest -q` — **984 passed, 6 skipped**
  (Postgres-gated), **21 deselected** (`uat` marker), **2 xfailed**
  (issue #65, pre-existing/unrelated), **0 failed**. Delta from the
  pre-#26 baseline (972) is exactly this issue's 12 new tests.
- **Lint/format**: clean (including the design doc's embedded code
  blocks).
- **No headless UAT / no HTTP route**: no user-visible surface in this
  issue, matching #24/#25's own accepted precedent.
- **No migration**: no new database table — the registry is a
  git-version-controlled file, which is itself the audit trail.

## Known deferred/untested paths

See design doc §9: `package_location` is schema-present but not yet
resolved/imported by anything (no real dynamic-import step exists
yet, since every current entry's manifest-building function is already
directly importable Python code in this same repository); no
signature/checksum enforcement (#27); no separate
`yangjeep/minigrc-connectors` repository (a user decision, not made
here); no HTTP route/admin UI to browse the registry interactively.
