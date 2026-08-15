# miniGRC Agent Execution Loop

This file defines how an implementation agent should work through the MVP issue backlog safely and with minimal supervision.

## 1. Start-of-session bootstrap

For every new session:

1. Read root `CLAUDE.md`.
2. Read `.agent/README.md` and `.agent/RULES.md`.
3. Read `docs/product/minigrc-mvp-prd.md`.
4. Inspect the current branch, working tree, and latest merged commits.
5. Read the target GitHub issue and any parent/blocked-by/depends-on issues.
6. Read the issue-specific design/spec if one exists.
7. Inspect the actual code, migrations, tests, and current docs touched by the issue.

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
- relevant tests;
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
- downstream compatibility.

Challenge the issue if the simplest correct repository-grounded design differs from the wording.

## 5. Implementation policy

- Implement the smallest coherent slice that satisfies the issue.
- Prefer existing patterns where they do not violate the new PRD/architecture.
- Replace stale architecture patterns only in the scope necessary for the issue.
- Keep domain logic backend-neutral; isolate PostgreSQL-specific read optimization.
- For event-backed changes, append authoritative events and update canonical projections transactionally.
- Do not write connector/AI/provider code directly into compliance conclusions.
- Do not revive dead/placeholder surfaces merely because they are nearby.

## 6. Verification loop

After implementation:

1. run targeted tests for changed behavior;
2. run full relevant test suite;
3. run `ruff check .`;
4. run `ruff format --check .`;
5. run migration checks on SQLite and PostgreSQL where schema/backend-neutral persistence changed;
6. run available security/dependency/secret checks relevant to the change;
7. exercise the representative user flow through the real route/service path where practical;
8. for event-backed domains, prove projection reset/rebuild reproduces equivalent current state;
9. verify no secret/token/user data leaked into fixtures/logs/events/exports.

Do not treat a green unit test alone as proof of a stateful/migration/security invariant.

## 7. Adversarial review

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

Use specialist/subagent review where available for independent lenses. Validate findings against code before fixing them.

## 8. Documentation and worklog

Update the smallest authoritative docs needed for the change.

At minimum follow current repository worklog conventions. Do not duplicate the same architecture truth across many files when one canonical file plus references is enough.

If a stale `CLAUDE.md`, architecture doc, or product-scope statement conflicts with a newly merged approved decision, update it in the same architecture change rather than leaving agents with contradictory instructions.

## 9. Git/PR completion

For normal implementation issues:

1. review final diff for unrelated edits and secrets;
2. commit logical units;
3. push remote task branch;
4. create/update a draft PR;
5. ensure PR description accurately states architecture, migrations, tests, security findings, and blockers;
6. inspect CI/check status when available;
7. update the GitHub issue with material decisions or blockers where useful;
8. stop before merge unless explicit authorization was given.

## 10. Continuing to the next issue

In an explicit multi-issue loop, continue only when:

- the completed issue's changes are committed/pushed and represented by a PR or are merged as required by the next dependency;
- starting the next issue will not depend on unreviewed semantics that could invalidate its design;
- there is no unresolved stop condition.

If the next issue truly depends on the current PR merging, stop at the dependency boundary rather than stacking unrelated implementation on uncertain foundations unless the task explicitly authorizes stacked branches.

## 11. Stop conditions and required report

When a stop condition from `.agent/RULES.md` is reached, report:

- the exact conflicting requirement/state;
- evidence from current code/schema/docs;
- why choosing silently creates material risk/rework;
- the smallest set of viable options;
- which issues/designs are affected.

Do not use stop conditions to avoid difficult but normal engineering work.

## 12. Final response contract

For each completed issue report:

- Summary
- Branch
- PR URL/status
- Commits
- Files changed
- Domain/event/projection changes
- Migration/backfill decisions
- Tests/lint/format/backend verification
- Security/integrity findings
- Decisions/assumptions
- Remaining blockers/dependencies
- Worklog

Keep the report factual; distinguish verified facts from deferred work.
