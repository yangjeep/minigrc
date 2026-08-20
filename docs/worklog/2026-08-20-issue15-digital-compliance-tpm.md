# Issue #15: BYOK AI provider abstraction + Digital Compliance TPM

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature (new bounded AI capability layer)

## Summary

Implements a provider-neutral, OpenAI-compatible BYOK provider
abstraction (`app/ai_provider.py`, `app/ai_provider_client.py`) and a
bounded Digital Compliance TPM task layer (`app/digital_tpm.py`)
covering exactly the four capabilities the issue names — Observe,
Prioritize, Nag, Pre-fill — built exclusively on top of #49's
`app/ai_egress.py` egress boundary and #14's `app/readiness.py`
deterministic readiness engine. See
`docs/superpowers/specs/2026-08-20-issue15-digital-compliance-tpm-design.md`
for the full design, repository-reality check, and honesty note on what
"missing facts are never invented" actually means and does not mean.

## Repository-reality check before starting

Confirmed before designing: #49/#86 (AI egress boundary) is merged to
main; #11/#13/#14/#30/#33's readiness/onboarding/control/policy/evidence/
finding domains exist and are exercised by `app/readiness.py`; no
prompt-assembly/provider-client code exists anywhere in `app/` prior to
this change; `app/jobs.py` is a request-triggered queue, not a scheduler
— there is no cron/periodic-trigger primitive in this repository, so Nag
follows the existing `aws-run-checks`/`generate-control-occurrences`
"one-shot CLI command, external cron adds the schedule" precedent rather
than inventing scheduling infrastructure.

## Scope decisions

1. **Prioritize is not a separate live AI call.** `compute_readiness_queue`'s
   existing deterministic sort already is the prioritization; a second
   AI call on every dashboard view would be redundant and add
   uncontrolled cost/egress-log noise. The `/ai/tpm` console renders the
   same deterministic queue under the "Prioritize" heading.
2. **Every task degrades to a deterministic path when AI is absent,
   disabled, misconfigured, or failing** — never a hard failure.
   Observe/Pre-fill fall back to a fact sheet built only from the same
   `AiPromptContext.fields` an AI call would have used; Nag falls back to
   a templated message built from the same `ReadinessItem` fields.
3. **Two new `app/ai_egress.py` projections** (`project_readiness_snapshot`,
   `project_reminder_context`) were added to the existing #49 module
   rather than duplicating scrubbing logic elsewhere — both are
   aggregate/system-generated-string-only projections (stage/category/
   counts, or one `ReadinessItem`'s own fields), so neither needs or
   accepts an `AiEgressOptIn`.
4. **No new event-sourced aggregate.** `AiProviderSettings`/
   `AiTaskExecution`/`AiReminderState` are plain rows, matching the
   `Job`/`AuditEvent`/`Secret` precedent — advisory/operational
   bookkeeping, not material compliance state (RULES.md §3's own
   carve-out for UI/operational noise applies here).
5. **Pre-fill's four representative workflows map 1:1 onto #49's four
   existing `project_*` functions** (evidence description, control-
   operation note, finding response, policy change summary). Two
   (evidence description, finding response) are wired into UI routes on
   existing detail pages this slice; all four are dispatchable from
   `draft_prefill` and covered by unit tests, per "representative set,"
   not "every workflow needs its own button yet."
6. **No "apply draft" route exists anywhere.** A human must manually
   transcribe a draft into the real form. This is what makes "AI cannot
   autonomously mutate" a structural guarantee rather than a UI
   convention — see the design doc §7 and
   `tests/test_digital_tpm_boundary.py`'s static AST scan.

## A real bug found and fixed during implementation

`app/ai_provider.py::resolve_ai_provider_config`'s first draft computed
`usable = bool(row.base_url and row.model and url_valid)` — it never
checked whether `row.secret_id` pointed at a secret that actually
resolved. A wrong/rotated `GRC_ENCRYPTION_KEY` or corrupt ciphertext
would silently produce `api_key=""` while `usable` stayed `True`,
indistinguishable from "this provider deliberately needs no API key"
(a legitimate, supported configuration for some self-hosted gateways).
Caught by `tests/test_ai_provider.py::test_wrong_encryption_key_degrades_to_unusable_not_raise`
(written before the fix, failed for the expected reason). Fixed by
having `_api_key` return `(value, resolution_failed)` instead of just a
string, so "no secret configured" and "secret configured but
unresolvable" are distinguishable, and folding `not secret_resolution_failed`
into `usable`. Regression test
`test_enabled_row_with_no_secret_at_all_is_still_usable` guards the
no-key-needed case doesn't regress from this fix.

## Note on a self-inflicted local mishap (not part of the shipped diff)

Mid-implementation, a `git checkout main && git reset --hard origin/main`
was run without first checking `git status`, discarding in-progress
uncommitted edits to several already-tracked files (the two `app/ai_egress.py`
projections, the three new model classes, the CLI command, router
registration/nav items, and the two evidence/finding route+template
pre-fill entry points). All new *untracked* files (every new module,
router, template, test, and the migration) were unaffected, since
`git reset --hard` never touches untracked files. All lost edits were
manually reconstructed from this session's own record and reverified
(tests, lint, migration cycle) before commit — no content was lost, but
the mistake itself is recorded here per this repository's practice of
being explicit about what actually happened during implementation,
matching the git-safety protocol's requirement to check `git status`
before any history-discarding command.

## Test report

- **Unit**: `tests/test_ai_provider.py` (21) — SSRF guard accept/reject
  matrix (cloud-metadata IPv4/IPv6 literals and hostname, embedded
  credentials, bad scheme, missing host), provider config resolution
  (unconfigured/disabled/usable/wrong-key/no-key-needed/missing-model).
  `tests/test_ai_provider_client.py` (18) — request construction (bearer
  auth present/absent, model/messages shape), malformed/empty/oversized/
  wrong-typed completion handling, timeout/connection-error safety,
  adversarial-content-is-inert-text, plus one real-socket round trip
  against a tiny local fake OpenAI-compatible FastAPI server. `tests/test_digital_tpm.py`
  (15) — Observe/Nag/Pre-fill deterministic-vs-AI-enabled branching,
  reminder cooldown/escalation-cap arithmetic, all four pre-fill task
  types, adversarial provider-output-never-mutates-a-domain-row.
  `tests/test_digital_tpm_boundary.py` (2) — static AST scan asserting no
  material-mutation domain command is ever referenced from the TPM
  module set, and that AI output is never assigned onto a domain
  model's own attribute. All PASS.
- **Regression**: full `pytest -q` — see below (all pre-existing tests
  unaffected; `tests/test_ai_egress.py`'s 43 pre-existing tests still
  pass unchanged, confirming the two new projections didn't disturb
  #49's existing scrubbing/opt-in guarantees).
- **Integration**: real-socket fake-provider round trip (above); AI-
  disabled path exercised through the full `draft_prefill`/
  `generate_due_reminders`/`generate_observe_narrative` call chains with
  no `AiProviderSettings` row at all (unit tests) and over real HTTP
  (headless UAT below).
- **Browser/E2E**: `tests/test_admin_ai_settings.py` (5) — admin-only
  settings page, valid save, SSRF-risky base URL rejected and never
  persisted, blank-key-resubmission keeps the existing secret, key never
  redisplayed. `tests/test_digital_tpm_routes.py` (9) — console loads for
  any role, write-only action forms absent from a reader's rendered
  page, admin/operator can trigger Observe/Nag, evidence/finding
  pre-fill buttons and routes, reader gets 403 attempting a pre-fill
  POST, unknown draft id is 404. All PASS.
- **Headless UAT** (`tests/uat/test_digital_tpm.py`, 2 scenarios,
  `GRC_UAT_MODE=1 pytest -m uat tests/uat/test_digital_tpm.py`): an
  operator creates a real readiness blocker (an unowned open finding
  plus an evidence artifact with no description), sees it surfaced on
  `/ai/tpm`, runs a nag scan (bounded — an immediate second run adds no
  new reminder), generates both representative pre-fill drafts and
  confirms missing facts render as explicit `[MISSING]` markers rather
  than being invented, and confirms the source `Finding`/
  `EvidenceArtifact` rows are byte-for-byte unchanged throughout; a
  reader can view the console but is rejected (403) attempting to
  trigger an action. **PASS.**
- **Security/integrity**: SSRF guard tested directly and through the
  admin save route; no credential/API key appears in any
  `AiTaskExecution` row, audit event, or rendered template (verified by
  reading every template and the `record_ai_egress`/`AiTaskExecution`
  write paths); adversarial provider-output test proves a
  hallucinated/malicious-looking completion only ever lands as inert
  `output_text`, never a domain mutation; static AST scan blocks any
  future accidental reference to a material-mutation command from the
  TPM module set.
- **Lint/format**: `ruff check .` / `ruff format --check .` — clean.
- **SQLite/PostgreSQL**: migration upgrade → downgrade → upgrade cycle
  verified against a live SQLite file; single Alembic head confirmed
  (`0bb731631a1b`, chained after #36's `544f2ba57e12` — both were
  generated against the same then-current head before #36 merged first,
  the same fork-avoidance rebase pattern as earlier PRs this session); no
  local PostgreSQL available in this environment — CI's `test-postgres`
  job is the parity check of record; the migration/models use only
  standard SQLAlchemy Core with no SQLite-specific syntax.
- **Claude Desktop UAT: PENDING** — requires a running deployment and
  either a real or self-hosted OpenAI-compatible endpoint; not runnable
  from this implementation session. Runbook: configure a BYOK provider
  at Admin > AI Provider (or leave it unconfigured to exercise the
  degrade-gracefully path), create an open finding with no owner, visit
  Digital TPM in the main nav, run a nag scan, generate a pre-fill draft
  from the finding's detail page, and confirm the finding's own fields
  never change.

## Known deferred/untested paths

- Auditor/PBC-response and risk/control-narrative pre-fill workflows are
  dispatchable from `draft_prefill` but not yet wired into a UI route —
  documented in the design doc §11, not silently dropped.
- No real external inference call was made anywhere in this session's
  verification (by design — a deterministic fake provider and
  `httpx.MockTransport` were used throughout, per the issue's own
  "do not make real external inference calls unless explicitly available
  and safe").
- Claude Desktop UAT itself: PENDING, so this PR is not release-complete
  until it runs.
