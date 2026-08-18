# miniGRC Testing, UAT, and Bug-Fix Contract

This file is binding for non-trivial product work. A feature is not complete because implementation tests are green; it must pass the applicable verification layers below.

## 1. Test pyramid and required layers

### 1.1 Unit tests

Purpose: prove isolated domain, validation, reducer/projector, calculation, parsing, and security behavior cheaply and deterministically.

Required for new or changed logic where an isolated boundary exists. Cover at minimum:

- success behavior;
- invalid/boundary inputs;
- authorization/policy helpers where isolated;
- event reducer/projector behavior;
- idempotency/replay semantics where applicable;
- historical immutability rules;
- date/cadence/readiness boundary calculations;
- malformed/untrusted external/provider input.

Do not test framework/library internals simply to increase test count.

### 1.2 Regression tests

Every bug fix and every changed previously-working behavior must have a regression test that fails before the fix and passes after it, unless reproducing the defect is genuinely impossible in automation and that exception is documented.

Regression tests must preserve the reported failure mode, not merely exercise a nearby happy path.

Existing meaningful tests must not be deleted or weakened to make CI green.

### 1.3 Integration tests

Purpose: prove boundaries that unit tests cannot: database transactions/migrations, event append + projection update, object storage, connector/provider adapters, auth/session/OIDC flows, exports, and cross-domain workflows.

Use real infrastructure where repository tooling supports it rather than mocks for the boundary under test.

Required examples by domain:

- SQLite and PostgreSQL for backend-neutral persistence/schema changes;
- event append + canonical projection atomicity and rebuild;
- S3-compatible object storage upload/download/hash/error atomicity;
- connector result validation -> authorized core ingestion boundary;
- OIDC callback/session/role mapping with a standards-compliant test provider or local test harness;
- audit/PBC export including artifact retrieval and hash verification.

Mocks/fakes are acceptable for remote vendor behavior when live calls are unsuitable, but the miniGRC side of the integration must still execute through its real boundary.

### 1.4 End-to-end / browser verification

For user-visible changes, exercise the real HTTP/browser flow including authentication, authorization, CSRF, forms, redirects, rendering, errors, and persisted state.

Do not infer that a template or route works solely from service-layer tests.

At minimum verify the representative workflow added/changed by the issue and relevant destructive/error paths.

### 1.5 Headless UAT (issue #38)

Headless UAT is the automated, scenario-based end-to-end acceptance layer any CLI agent or CI job can run without Claude Desktop or a human. It lives under `tests/uat/`, drives the real FastAPI app over a real socket (`uvicorn` in a background thread + a real `httpx.Client`, not an in-process ASGI transport and not a mock app), and exercises real login/session/CSRF/routes/forms/domain/persistence paths exactly like a deployed server would.

Run it with:

```bash
GRC_UAT_MODE=1 pytest -m uat tests/uat
python -m app.cli uat                      # thin wrapper around the above
python -m app.cli uat -k login_and_soc2    # select scenarios
GRC_UAT_MODE=1 TEST_DATABASE_URL=postgresql+psycopg://postgres@localhost:5432/minigrc_uat \
    pytest -m uat tests/uat                # PostgreSQL path
```

Headless UAT is excluded from the default `pytest` command (`addopts = "-m 'not uat'"` in `pyproject.toml`) so the ordinary fast suite is unaffected — it must be invoked explicitly, and requires `GRC_UAT_MODE=1` to build anything at all (fails closed otherwise). See
`docs/superpowers/specs/2026-08-18-issue38-headless-uat-design.md` for the full design, including why real-socket HTTP was chosen over a browser for this app, and the production-safety guarantees (no UAT code importable from `app/`, no UAT route ever exists, the harness never reads `GRC_DATA_DIR`/`DATABASE_URL` from the ambient environment).

Required for every user-visible feature/release-slice PR:

- **Headless UAT is required** whenever the change is user-visible — it is not optional merely because Claude Desktop is unavailable during implementation.
- **Claude Desktop UAT remains the final interactive/client acceptance layer** for release slices where useful/required, and may be reported `PENDING` when Claude Desktop isn't available during implementation — but headless UAT must still PASS; the absence of Desktop is never permission to skip end-to-end acceptance entirely.
- A defect found via Claude Desktop UAT should normally produce or extend a headless regression scenario where automation is practical, per the bug-fix loop (§4).
- Failure artifacts (last recorded HTTP exchanges + traceback) land under `artifacts/uat/<run>/` (gitignored) via `tests/uat/conftest.py`'s failure hook; a machine-readable summary is available via `pytest`'s own `--junitxml`.

### 1.6 Claude Desktop UAT

Feature/release UAT is a separate human-facing acceptance layer performed from Claude Desktop against a running miniGRC environment after automated tests pass.

The implementation agent must prepare a concise UAT runbook for every user-visible feature or coherent release slice. The runbook must include:

- build/commit/PR under test;
- environment/backend under test;
- prerequisites/seed/setup;
- user persona/role;
- exact workflow/scenario;
- expected visible result;
- important negative/authorization case;
- expected persisted/audit/event result where observable;
- cleanup/reset instructions when needed.

UAT should be scenario-based, not a checklist of routes. Prefer real CISO/control-owner workflows such as:

`define scope -> assign owner -> perform control -> attach evidence -> observe readiness change`

or

`draft policy -> review -> approve/effective -> supersede -> verify old version remains historically available`.

Claude Desktop UAT output must record PASS/FAIL per scenario and concrete defects with reproduction steps. A UAT failure blocks completion of the affected feature/release slice.

## 2. Test selection by change type

Every issue must explicitly state which layers apply.

- Pure isolated logic: unit + regression/full suite.
- DB/schema/event/projection: unit + regression + SQLite integration + PostgreSQL integration + rebuild/migration verification.
- User-visible route/UI: unit/service where useful + regression + integration + browser/E2E + headless UAT (required) + Claude Desktop UAT (PASS or explicitly PENDING).
- Connector/external integration: unit + malformed/error cases + integration boundary + user-visible configuration/run flow + headless UAT with deterministic fakes + Desktop UAT where operator-facing.
- Evidence/object storage: unit + storage integration + failure atomicity + download/export flow + headless UAT + Desktop UAT.
- Auth/OIDC/authorization: unit + integration + browser negative cases + security review + headless UAT + Desktop UAT.
- AI/Digital TPM: deterministic task eligibility tests + provider adapter integration/fake server + authorization/output-boundary tests + prompt-injection/adversarial tests + headless UAT with AI enabled/disabled/failing + Desktop UAT.
- Documentation-only change: no headless or Desktop UAT required unless it changes the operating/test contract itself.

## 3. Database/backend matrix

miniGRC supports exactly one relational backend per deployment, but backend-neutral product behavior must be tested against both SQLite and PostgreSQL where persistence semantics are affected.

A SQLite pass is not evidence of PostgreSQL correctness and vice versa.

For schema/persistence changes verify, as applicable:

- migration from current head/representative prior schema;
- clean-database migration;
- constraints/indexes/FKs;
- transaction rollback behavior;
- aggregate sequence/idempotency concurrency semantics;
- projection rebuild;
- materially equivalent domain results across both backends.

PostgreSQL-only materialized/read optimizations need tests proving canonical semantics remain unchanged and a non-Postgres fallback still works.

## 4. Bug-fix workflow

Every defect found by automated testing, adversarial review, CI, headless UAT, Claude Desktop UAT, or later user report follows this loop:

1. **Capture** — record concrete symptom, environment/build, actor/role, inputs, expected vs actual, and reproducible steps.
2. **Reproduce** — reproduce on the relevant branch/environment before changing code. If reproduction fails, investigate rather than guessing.
3. **Classify** — determine root boundary: domain logic, projection/event history, migration, authorization, UI, integration, provider, storage, concurrency, or configuration.
4. **Regression test first** — add the narrowest automated test that demonstrates the bug and verify it fails for the expected reason.
5. **Minimal fix** — fix the root cause without unrelated refactoring.
6. **Targeted verification** — run the new regression test and directly related tests.
7. **Full regression** — run the repository-standard suite and all applicable backend/integration/security checks.
8. **Re-run affected integration/E2E scenarios** — especially the path that originally exposed the bug.
9. **Re-run the affected headless UAT scenario, and the Claude Desktop UAT scenario when Desktop is available**, when the defect was user-visible or found during either UAT layer.
10. **Adversarial review** — check whether the fix creates a sibling bug, authorization bypass, historical mutation, replay/idempotency issue, backend divergence, or data migration problem.
11. **Document** — update issue/PR/worklog with root cause, test added, fix, verification, and any residual limitation.

Never fix a bug only by changing expected output/tests unless the product requirement itself was explicitly changed.

## 5. UAT defect handling

When headless or Claude Desktop UAT finds a defect:

- create/update a GitHub bug issue when the defect is non-trivial or needs separate tracking;
- link the bug to the originating feature/PR;
- do not mark the feature UAT complete until affected scenarios are rerun successfully in the layer(s) that caught it;
- use the bug-fix workflow above; a defect found in Claude Desktop should normally gain a headless regression scenario where automation is practical, so it cannot regress silently between Desktop sessions;
- retain the original UAT reproduction steps in the issue/worklog;
- rerun nearby high-risk scenarios, not only the exact failing click path.

Critical/high integrity, authorization, secret-handling, event-history, or data-loss defects block merge/release until resolved.

## 6. Release/MVP regression gate

Before an MVP release candidate is accepted:

- all unit/regression tests green;
- full SQLite suite green;
- full PostgreSQL suite/integration path green;
- migration checks green on both supported backends where applicable;
- connector/object-storage/OIDC integration suites green for included MVP capabilities;
- projection rebuild/integrity verification green;
- security/dependency/secret checks green;
- representative browser/E2E workflows green;
- headless UAT (`GRC_UAT_MODE=1 pytest -m uat tests/uat`) green on SQLite and, for persistence-sensitive scenarios, PostgreSQL;
- Claude Desktop UAT runbook executed for the release candidate with no unresolved blocking defects;
- audit/PBC export integrity scenario passes when that feature is in the release;
- AI-off mode passes; AI-enabled representative UAT passes when Digital TPM is included.

## 7. Definition of done for an issue

An issue is complete only when:

- applicable automated tests were added/updated;
- bug fixes include regression coverage;
- targeted and full relevant suites pass;
- integration/backend tests pass where applicable;
- representative browser/E2E flow passes for user-visible changes;
- headless UAT scenarios pass for user-visible changes — this is required and may not be skipped merely because Claude Desktop is unavailable;
- a Claude Desktop UAT runbook exists and has been executed when the feature is user-visible and ready for acceptance, or the PR explicitly states Desktop UAT is pending and therefore not release-complete;
- UAT defects (headless or Desktop) have been fixed/retested or explicitly remain blocking;
- PR/worklog report exact commands/scenarios/results rather than saying only “tests pass”.

## 8. Required final test report

Every implementation checkpoint/PR description must report:

- Unit tests: added/changed + result
- Regression tests: added/changed + result
- Integration tests: boundaries/backends exercised + result
- Browser/E2E: scenarios exercised + result
- Headless UAT: scenarios exercised + PASS/FAIL (required for user-visible changes; not skippable)
- Claude Desktop UAT: runbook/scenarios + PASS/FAIL/PENDING
- Security/integrity checks: result
- Bugs found during verification: root cause + regression test + fix status
- Known untested/deferred paths and why

Do not collapse these into a single generic “tests green” line.
