# Worklog

Audit trail for significant changes to this codebase: what changed, why,
and what was verified. Adapted from the LeaseLab agentic-development
worklog convention.

## Entry format

Each entry is a markdown file in this directory named `YYYY-MM-DD-<slug>.md`.

### Template

```markdown
# <Title>

**Date:** YYYY-MM-DD
**Author:** <name or agent>
**Type:** feat | fix | refactor | docs | test | chore

## Summary

1-3 sentences: what changed and why.

## Files Changed

- `path/to/file.py` — what changed and why

## Verification

- [ ] Tests pass (`pytest`)
- [ ] Lint/format clean (`ruff check .`, `ruff format --check .`)
- [ ] Manually verified (describe how, if applicable)

## Decisions & Alternatives Rejected

Anything non-obvious about *why* this approach, and what else was
considered.

## Known Gaps / Follow-ups

What's deliberately left undone, and what would trigger picking it up.
```

## Rules

1. Create an entry for every non-trivial change (new features, schema
   changes, architectural decisions, notable bug fixes). Trivial changes
   (typos, formatting) don't need one.
2. Entries are append-only — don't edit past entries; add a new one if a
   past decision is reversed, and note the reversal.
3. Use ISO 8601 dates.
