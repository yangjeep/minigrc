# Official connector repository structure (issue #26)

Issue #26 asked for a separate official connector repository
(`yangjeep/minigrc-connectors`), with an explicit fallback: *"or
document a repository-grounded alternative if current project
constraints make a separate repo premature."*

## Decision

Creating a new external GitHub repository is a decision for the
project owner, not something an autonomous implementation loop should
do on its own — it is a persistent, hard-to-reverse action affecting
shared account/organization state. This directory documents the
intended structure instead, deferring the actual repository split
until a real need for independently-versioned, out-of-tree connectors
arises.

## What exists today

- **Manifests and adapters**: `app/connectors/` — `manifest.py`
  (the #24 contract), `result.py`, `ingestion.py`, `adapters.py` (real
  manifests for the three existing integrations: AWS CloudTrail/IAM,
  Google Drive, Google Workspace Directory).
- **Registry**: `registry/registry.json` — the versioned, git-audited
  index of connector entries (issue #26). Every change to it is an
  ordinary, reviewable commit.
- **Lifecycle**: `app/connector_lifecycle.py` — install/configure/
  enable/disable/test/execute (issue #25).

## Intended future layout

If/when a separate repository is created, or if in-repo connectors
grow enough to warrant their own top-level directory, the suggested
layout (matching the issue's own suggestion) is:

```text
connectors/
  google-drive/
  github/
  google-workspace/
  authentik/
  entra/
  aws/
registry/
  registry.json
```

No placeholder subdirectories are created here yet for connectors that
don't have real, working code behind them — each one should appear
only when a concrete connector actually lands for it, matching this
project's "no speculative infrastructure without a concrete use"
principle.
