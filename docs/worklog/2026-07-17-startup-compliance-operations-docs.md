# Startup compliance operations — docs pass and final verification

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** docs

## Summary

Final commit on `feat/startup-compliance-operations`: documentation pass
covering everything the branch shipped (admin authorization; People and
Vendor/System register; vendor roster snapshots; Google OIDC login,
Drive connector, Approvals, and optional Workspace Directory sync; AWS
CloudTrail/IAM evidence), plus final quality-gate verification.

## Files Changed

- `README.md` — new positioning statement ("a lightweight compliance
  index and evidence ledger for startups"), a new "Optional integrations"
  section (Google OIDC, Google Drive, AWS, vendor roster CSV), an
  expanded feature-status table, and security notes covering the new
  encryption-at-rest and admin-gating surfaces.
- `docs/architecture.md` — new sections: Admin authorization, People and
  Vendor/System register, Google Drive Approvals and Workspace Directory,
  Evidence and the AWS connector; Authentication/Policy storage sections
  extended for OIDC/Drive; Testing section lists the 8 new test files.
- `docs/product-scope.md` — feature-status table updated for every area
  this branch touched; "Next PR candidates" rewritten to reflect what
  shipped.
- `docs/domain/domain-model.md`, `docs/decisions/architectural-decisions.md`
  — kept current incrementally through commits 1–8 (ADRs #12–#22); this
  commit is the final consistency pass, not a rewrite.

## Verification

- [x] `pytest` — 190 passed
- [x] `ruff check .` — clean
- [x] `ruff format --check .` — clean
- [x] `docker compose config` — valid
- [x] `docker build` — **could not be run in this sandbox**: the Docker
  Desktop VM's registry pulls hang indefinitely, identical to the
  limitation already documented in the foundation PR (#1). Verified this
  is the same sandbox network restriction, not a new Dockerfile/compose
  problem — `docker compose config` itself validates cleanly.
  Equivalent verification performed instead: the full 190-test suite
  exercises real HTTP routes via `TestClient` (including every new
  router), plus an explicit restart-persistence check — building the app
  twice against the same `data_dir` and confirming a `Person` row
  created in the first "boot" is present in the second. A maintainer
  with unrestricted Docker network access should still do a real
  `docker compose up` pass before merging either stacked PR.
- [x] Alembic upgrade verified at every commit: fresh empty database
  (all 6 new migrations apply in order) and upgrade-from-the-
  `feat/initial-grc-foundation` schema head (same migration chain,
  starting from the pre-existing `647102981d1c` revision). Two migrations
  needed hand-editing after autogenerate: `ad57f3b48bfb` (server_default
  backfill for `users.role`, batch mode for the `users.person_id` FK add
  in `872531f7b0cc`) and `a1d7da5c4a94` (server_default backfill for
  `source_type` columns, explicit `UPDATE` backfill for
  `policy_versions.captured_at` — verified against a manually seeded
  pre-migration row, not just an empty database).
- [x] Full diff against `feat/initial-grc-foundation` reviewed: no
  `.db`/`.env` files, no debug `print()` statements outside CLI output,
  no secrets (the one `AKIA`-prefixed string in the diff is an
  obviously-fake test fixture, `AKIAFAKEEXAMPLE12345`, used with
  botocore Stubber — never a real credential).

## Known Gaps / Follow-ups (branch-wide)

- GitHub/Azure/Asana connectors remain unbuilt (out of scope for this
  branch — see `docs/product-scope.md` "Next PR candidates").
- No route for a Google-OIDC-created user to set a local password.
- Real `docker compose up` should still be verified by someone with
  unrestricted Docker network access.
- All 8 commits are individually reviewable; see each commit's own
  worklog entry (`docs/worklog/2026-07-17-*.md`) for scoped decisions,
  alternatives rejected, and known gaps.
