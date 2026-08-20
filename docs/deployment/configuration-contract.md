# Self-hosted deployment/configuration contract

This page ties together every independent configuration axis a
self-hosted miniGRC deployment chooses across, so an operator doesn't
have to reverse-engineer the resolution rules from scattered docstrings.
It complements, and does not replace, [`kubernetes.md`](kubernetes.md)
(how to actually deploy), [`authentication.md`](authentication.md)
(auth-mode setup detail), and [`backup-restore.md`](backup-restore.md)
(backup/restore procedure) — this page is the map of *which
combinations of settings are valid*, not *how to operate* any one of
them.

See issue #48 for the gap this page closes, and issue #9 for the
managed-runtime/operational-readiness runbook this page is a companion
to, not a replacement for.

## The four axes

| Axis | Owning issue | Required to run at all? |
|---|---|---|
| Relational database backend | #22 | Yes — exactly one |
| Evidence object-storage backend (S3-compatible) | #32 | No — optional |
| Authentication mode(s) | #16–#20 | No beyond local login, which is always available |
| BYOK AI / Digital Compliance TPM | #15 | **Not yet implemented** |

## 1. Database backend — exactly one, hard-validated

Exactly one of `DATABASE_URL` (plain env var, no `GRC_` prefix — matches
the convention most Postgres-hosting platforms use) or
`GRC_DATABASE_PATH` (SQLite file path) may be set. Both being set at once
is rejected at startup with a clear error
(`app/config.py::reject_ambiguous_database_config`, called from
`app/main.py::create_app`) — not a silent "pick one," because there is no
safe default when an operator's intent is genuinely ambiguous between two
different, individually complete database selections.

Neither being set is valid: `Settings.resolved_database_path` falls back
to `<GRC_DATA_DIR>/grc.db`, a local SQLite file — the correct zero-config
default for evaluation/small deployments.

## 2. Optional features — a shared "incomplete = gracefully disabled" pattern

Every axis below this line is independently optional, and every one of
them uses the **same deliberate pattern**: a feature is either *fully*
configured (all of its required fields set) or treated as fully
*disabled* — there is no code path in this repository that hard-fails
startup over a partially-filled optional-feature config group. An
incomplete group degrades to "not configured" at the point of use
(a 404/503/"unconfigured" UI state), never a crash, and never a silent
guess at what the operator meant.

This is not an oversight; each module states it explicitly:

- **Google OAuth login** (`app/google_oidc_config.py`): "a broken Google
  OAuth config must never crash a request or lock out local/break-glass
  login."
- **Generic OIDC login** (`app/oidc_config.py`): the same resolution
  shape.
- **Evidence S3 storage** (`app/object_storage.py`,
  `app/routers/evidence_artifacts.py`): an incomplete S3 config raises
  `ObjectStorageNotConfiguredError` → HTTP 503 at the route that needs
  it, not at process startup.
- **Google Drive connector** (`Settings.google_drive_configured`): same
  all-or-nothing bool gate.

Because of this, **there is no new ambiguous/invalid combination across
these axes to add startup validation for** — every combination of
(evidence storage configured or not) × (which auth mode(s) enabled or
not) is valid; an operator simply gets fewer features until they finish
configuring one. See the design doc's "repository-reality check" for the
full investigation that reached this conclusion, including why
retrofitting a hard-fail check here would contradict, rather than
complete, the existing pattern.

### 2a. Evidence object-storage backend (issue #32)

Required together, or none: `GRC_S3_ENDPOINT_URL`, `GRC_S3_BUCKET`,
`GRC_S3_ACCESS_KEY_ID`, `GRC_S3_SECRET_ACCESS_KEY`
(`Settings.evidence_repository_configured`). Unconfigured: the evidence
artifact repository UI returns 503 rather than silently doing nothing.

### 2b. Authentication mode(s) — independently stackable, not mutually exclusive

Unlike the database axis, auth modes are **not** an XOR — any
combination may be enabled simultaneously:

- **Local email/password** — always available at `/login`; the
  break-glass path. Never disabled by any other mode's misconfiguration.
- **Google OAuth** — configurable via Admin > Authentication (DB-backed,
  preferred) or legacy `GRC_GOOGLE_OIDC_*` env vars. Resolution order
  when both exist: the DB-backed row wins when `enabled=true`
  (`resolve_google_oidc_config`); env vars are the legacy fallback for
  pre-Admin-UI deployments, not a second thing to disambiguate against —
  there is a stated precedence rule, not an unresolved conflict.
- **Generic OIDC** (Authentik/Keycloak/ZITADEL/Entra/Okta/...) — same
  DB-backed-preferred / env-var-legacy-fallback shape
  (`app/oidc_config.py`). See #19 (Authentik reference deployment, not
  yet done) and #20 (IdP identity metadata as evidence, not yet done)
  for remaining work on this axis.
- **Google Drive connector's OAuth** is a distinct grant from Google
  OAuth *login* (`GRC_GOOGLE_DRIVE_CLIENT_ID`/`_SECRET`, separate from
  `GRC_GOOGLE_OIDC_*`) — even if both point at the same Google Cloud
  project's client credentials, they are configured and resolved
  independently.

Local login is therefore the only mode a deployment can rely on being
present unconditionally; every other mode layers on top without
disabling it.

### 2c. BYOK AI / Digital Compliance TPM (issue #15) — not yet implemented

There is currently no AI-provider/BYOK configuration surface in this
repository at all — confirmed by reading the complete `app/config.py`
`Settings` class, which has zero AI/provider-credential fields. This
section will be filled in once #15 lands; until then, any documentation
describing BYOK config requirements would be describing a future,
unmerged state, not current behavior.

## 3. Cross-axis interactions

No cross-axis combination is currently invalid. In particular:

- Evidence storage, auth mode(s), and (once it exists) BYOK are each
  orthogonal to the database backend choice — nothing in any of their
  resolution logic reads `DATABASE_URL`/`GRC_DATABASE_PATH`.
- Auth modes are orthogonal to evidence storage — nothing in the OIDC/
  Google OAuth resolution logic depends on `evidence_repository_configured`,
  or vice versa.

The one deployment-topology constraint worth naming, which is **not** an
application-level config check (the app process has no way to observe
its own replica count) but matters operationally: a SQLite-file
deployment must run with exactly one web replica (see
[`kubernetes.md`](kubernetes.md)'s "Scaling considerations" — the
`PersistentVolumeClaim` is `ReadWriteOnce`). This is enforced by
deployment topology (Helm `values.yaml` vs. `values-production.yaml`),
not by `Settings`.
