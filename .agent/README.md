# miniGRC Agent Guide

This directory contains the execution contract for coding agents working on miniGRC.

Read in this order for non-trivial work:

1. `CLAUDE.md`
2. `.agent/README.md` (this file)
3. `.agent/RULES.md`
4. `.agent/LOOP.md`
5. `docs/product/minigrc-mvp-prd.md`
6. the GitHub issue for the task
7. the issue-specific design/spec, if one exists
8. relevant current architecture/domain docs and code

## Why this directory exists

The root `CLAUDE.md` should stay short enough to load usefully on every Claude Code session. Anthropic's project-memory guidance favors concise, specific shared instructions and supports importing additional project files with `@path` references. Long product and execution detail therefore lives here and in the PRD instead of turning `CLAUDE.md` into an unreviewable wall of text.

## What is authoritative

- The PRD defines product intent, MVP boundaries, and cross-feature invariants.
- GitHub issues define implementation slices and acceptance criteria.
- Merged repository state defines what actually exists today.
- Design/spec docs explain the intended implementation for a particular issue.
- Worklogs are historical context, not current architecture authority.

If these disagree materially, do not silently reconcile them. Surface the conflict according to `.agent/LOOP.md`.

## Agent posture

- Inspect before editing.
- Prefer repository-grounded decisions over remembered patterns.
- Make the smallest coherent change that completes the issue.
- Challenge stale issue assumptions when code proves they are wrong.
- Preserve historical compliance truth.
- Treat security, authorization, event history, provenance, and migration correctness as product behavior, not cleanup.
- Use parallel investigation/review when tasks are independent, but do not create conflicting parallel writes.
- Do not implement speculative abstractions without the issue/PRD requiring the seam and at least one concrete use validating it.

## Normal task lifecycle

`inspect -> design/validate -> implement -> test -> adversarial review -> fix -> document -> commit -> push -> draft PR -> verify`

Do not merge unless the explicit task contract authorizes merge.
