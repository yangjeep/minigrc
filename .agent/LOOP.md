# miniGRC Agent Execution Loop

This file defines how an implementation agent should work through the MVP issue backlog safely and with minimal supervision.

## 1. Start-of-session bootstrap

For every new session:

1. Read root `CLAUDE.md`.
2. Read `.agent/README.md`, `.agent/RULES.md`, and `.agent/TESTING.md`.
3. Read `docs/product/minigrc-mvp-prd.md`.
4. Inspect the current branch, working tree, and latest merged commits.
5. Read the target GitHub issue and any parent/blocked-by/depends-on issues.
6. Read the issue-specific design/spec if one exists.
7. Inspect the actual code, migrations, tests, and current docs touched by the issue.
8. State which verification layers apply: unit, regression, integration, browser/E2E, headless UAT, Claude Desktop UAT.

Do not assume planned architecture has landed because an issue or design doc describes it.

## 2. Selecting work in a backlog loop

If no exact issue was supplied:

1. inspect open miniGRC issues;
2. identify P0/P1 MVP issues from the PRD families;
3. resolve dependencies against current merged repository state;
4. select the highest-priority issue whose prerequisites are actually present;
5. avoid starting work that overlaps an active branch/PR unless the issue is explicitly intended to rebase/build on it.

The intended near-term architecture order is:

- establish #21/#22 event-centric persistence foundation;
- revise/implement #11 control-operations design against that foundation;
- progress remaining SOC 2, evidence, policy, readiness, connector, identity, onboarding, audit-package, and digital-TPM work according to actual dependencies.

Issue numbers are not a substitute for dependency analysis.

## 3. Inspect before design

Before editing code, produce a short internal/current-task assessment covering:

- affected modules/tables/routes/templates;
- current domain source of truth;
- authorization path;
- current migration/data shape;
- relevant unit/regression/integration/E2E tests;
- UAT scenarios that will prove the user-visible outcome;
- issue assumptions that do not match repository reality;
- genuine blockers/decisions.

When independent investigations exist, parallelize them. Avoid parallel agents editing overlapping files/branches.

## 4. Design policy

For architecture-sensitive or multi-domain work, use the repository's established design/spec workflow before implementation when required by the issue.

A good design must state:

- authoritative facts/events;
- canonical projections/read models;
- lifecycle/state transitions;
- migration/backfill semantics;
- authorization and actor semantics;
- idempotency/retry/replay semantics;
- SQLite/PostgreSQL behavior;
- security/privacy concerns;
- downstream compatibility;
- test strategy by layer;
- representative headless UAT scenario(s) for user-visible work, plus a Claude Desktop UAT scenario where that layer is required for the release slice.

Challenge the issue if the simplest correct repository-grounded design differs from the wording.

## 5. Implementation policy

- Implement the smallest coherent slice that satisfies the issue.
- Prefer existing patterns where they do not violate the new PRD/architecture.
- Replace stale architecture patterns only in the scope necessary for the issue.
- Keep domain logic backend-neutral; isolate PostgreSQL-specific read optimization.
- For event-backed changes, append authoritative events and update canonical projections transactionally.
- Do not write connector/AI/provider code directly into compliance conclusions.
- Do not revive dead/placeholder surfaces merely because they are nearby.
- Add/update tests with the implementation; do not defer all testing until the end.

## 6. Layered verification loop

Follow `.agent/TESTING.md`. At minimum, after implementation:

1. run targeted **unit tests** for changed logic;
2. run/add **regression tests** for previously working behavior and every fixed defect;
3. run the full relevant automated regression suite;
4. run applicable **integration tests** through real boundaries (DB/event/projection/object storage/connector/OIDC/export as relevant);
5. run `ruff check .`;
6. run `ruff format --check .`;
7. run migration checks on **SQLite and PostgreSQL** where schema/backend-neutral persistence changed;
8. run available security/dependency/secret checks relevant to the change;
9. exercise representative **browser/E2E** flow through real auth/CSRF/routes/forms/rendering for user-visible work;
10. for event-backed domains, prove projection reset/rebuild reproduces equivalent current state;
11. verify no secret/token/user data leaked into fixtures/logs/events/exports;
12. run/extend **headless UAT** (`GRC_UAT_MODE=1 pytest -m uat tests/uat`, or `python -m app.cli uat`) for user-visible features/release slices — required, not skippable, even when Claude Desktop is unavailable;
13. prepare the **Claude Desktop UAT** runbook for user-visible features/release slices.

Do not treat a green unit test alone as proof of a stateful, migration, authorization, integration, or UX invariant. A green headless UAT run is required before Claude Desktop UAT is reported PASS, but headless UAT passing on its own is not a substitute for Claude Desktop UAT when that layer is available/required for the release slice.

## 7. Claude Desktop UAT gate

For user-visible work that is ready for acceptance:

1. start/deploy the exact build/commit under test;
2. execute the scenario-based UAT runbook from `.agent/TESTING.md` through Claude Desktop;
3. record PASS/FAIL for each scenario, including backend/environment/persona;
4. verify both visible outcome and relevant persisted/audit/event result where observable;
5. treat any FAIL as a blocking defect for that feature/release slice;
6. feed every defect into the bug-fix workflow below;
7. rerun failed and nearby high-risk UAT scenarios after the fix.

A PR may be implementation-complete but must be reported `Desktop UAT: PENDING` until Claude Desktop UAT runs — headless UAT, by contrast, must already be reported PASS/FAIL (never PENDING) for any user-visible change, since `.agent/TESTING.md` §1.5's harness has no dependency on Claude Desktop's availability. It is not release-complete while required UAT (headless or Desktop) is pending or failing.

## 8. Bug-fix loop

Any defect discovered by tests, CI, adversarial review, headless UAT, Claude Desktop UAT, or a user report follows this exact loop:

1. **Capture** reproducible symptom, build/environment, actor/role, expected vs actual.
2. **Reproduce before fixing** on the affected branch/environment when practical.
3. **Classify/root-cause** the failing boundary rather than patching the visible symptom blindly.
4. **Add a regression test first** that fails for the expected reason. If automation is genuinely impossible, document why.
5. **Implement the smallest root-cause fix.** No unrelated refactor.
6. **Run targeted tests**, including the new regression test.
7. **Run full relevant regression suite** and applicable backend/integration/security checks.
8. **Rerun affected browser/E2E path.**
9. **Rerun the affected headless UAT scenario**, and the Claude Desktop UAT scenario when Desktop is available, when user-visible or UAT-discovered.
10. **Adversarially review the fix** for sibling bugs, auth bypass, event/history corruption, replay/idempotency errors, backend divergence, and migration/data issues.
11. **Document** root cause, regression coverage, fix, retest results, and residual risk in PR/issue/worklog.

Never “fix” a bug only by weakening/removing a meaningful test unless an explicit product requirement changed.

## 9. Adversarial review

Before finalizing, review the diff as an adversary. At minimum ask:

- Can an unauthorized or ordinary user perform an admin/material action?
- Can retries/replays duplicate an event, capture, notification, or provider action?
- Can current-state projection drift from event history?
- Can approved/effective history be rewritten?
- Can a migration fabricate or mis-associate historical facts?
- Can SQLite and PostgreSQL differ semantically?
- Can a connector/AI output bypass core validation?
- Can credentials, tokens, evidence content, or PII leak?
- Can a configurable URL create SSRF/internal-network exposure?
- Did the implementation expand scope or introduce speculative infrastructure?
- Is there an untested user-visible failure mode that the UAT scenario should cover?

Use specialist/subagent review where available for independent lenses. Validate findings against code before fixing them. Any validated defect returns to the bug-fix loop.

## 10. Documentation and worklog

Update the smallest authoritative docs needed for the change.

At minimum follow current repository worklog conventions. Record material verification/UAT defects and their regression coverage when they affected the implementation.

If a stale `CLAUDE.md`, architecture doc, or product-scope statement conflicts with a newly merged approved decision, update it in the same architecture change rather than leaving agents with contradictory instructions.

## 11. Git/PR completion

For normal implementation issues:

1. review final diff for unrelated edits and secrets;
2. commit logical units;
3. push remote task branch;
4. create/update a draft PR;
5. ensure PR description separately reports:
   - unit tests;
   - regression tests;
   - integration/backend tests;
   - browser/E2E scenarios;
   - headless UAT scenarios PASS/FAIL (required for user-visible changes, never reported PENDING);
   - Claude Desktop UAT PASS/FAIL/PENDING;
   - security/integrity checks;
   - bugs found, root causes, regression tests, and fix status;
6. inspect CI/check status when available;
7. update the GitHub issue with material decisions or blockers where useful;
8. stop before merge unless explicit authorization was given.

Do not summarize all verification as only “tests pass.”

## 12. Continuing to the next issue

In an explicit multi-issue loop, continue only when:

- the completed issue's changes are committed/pushed and represented by a PR or are merged as required by the next dependency;
- automated verification is green;
- required UAT is PASS, or the next work is engineering-independent and the current item is explicitly tracked as `UAT: PENDING` rather than falsely complete;
- starting the next issue will not depend on unreviewed semantics that could invalidate its design;
- there is no unresolved stop condition.

If the next issue truly depends on the current PR merging, stop at the dependency boundary rather than stacking unrelated implementation on uncertain foundations unless the task explicitly authorizes stacked branches.

## 13. Stop conditions and required report

When a stop condition from `.agent/RULES.md` is reached, report:

- the exact conflicting requirement/state;
- evidence from current code/schema/docs;
- why choosing silently creates material risk/rework;
- the smallest set of viable options;
- which issues/designs are affected.

Do not use stop conditions to avoid difficult but normal engineering work.

## 14. Final response contract

For each completed issue report:

- Summary
- Branch
- PR URL/status
- Commits
- Files changed
- Domain/event/projection changes
- Migration/backfill decisions
- Unit tests
- Regression tests
- Integration/backend tests
- Browser/E2E verification
- Headless UAT: PASS / FAIL with scenarios (required for user-visible work; not skippable)
- Claude Desktop UAT: PASS / FAIL / PENDING with scenarios
- Security/integrity findings
- Bugs found during verification, root cause, regression coverage, fix status
- Decisions/assumptions
- Known untested/deferred paths
- Remaining blockers/dependencies
- Worklog

Keep the report factual; distinguish verified facts from deferred work.
