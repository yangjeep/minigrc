# Features 8-9: native import subsystem + watched import directory

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Eighth and ninth phases of the platform pivot (umbrella issue #5, PR #6),
"checkpoint 3" group. A shared, checksum-idempotent, all-or-nothing
importer registry (`app/imports.py`) used by web uploads, the CLI, and a
new watched import directory (`app/import_directory.py`), all routed
through the Feature 7 job system.

## Files Changed

- `app/models.py` — `ImportJob` model.
- `app/imports.py` — importer registry, `run_import`, CSV-injection
  neutralization, `enqueue_and_run_import` (job-routed wrapper).
- `migrations/versions/d62fe88db84e_add_import_jobs_table.py`.
- `app/routers/frameworks.py` — existing CSV import now goes through
  `run_import`/`ImportJob` (unchanged behavior/UX).
- `app/routers/risks.py`, `app/templates/risks/list.html` — new risk
  register CSV import.
- `app/cli.py` — `import-csv` and `import-directory` subcommands.
- `app/import_directory.py` — inbox/processing/archive lifecycle.
- `app/config.py`, `app/worker.py` — optional `GRC_IMPORT_WATCH_DIR`/
  `GRC_IMPORT_WATCH_IMPORTER` worker polling.
- `tests/test_imports.py` (11), `tests/test_imports_router.py` (3),
  `tests/test_import_directory.py` (10).

## Verification

- [x] Tests pass (`pytest` — 291 passed, 1 skipped)
- [x] Lint/format clean
- [x] Ran the real `python -m app.cli import-directory` binary against a
      scratch directory with a real CSV file — confirmed it moved through
      inbox → archive/completed with a manifest.json, and the row was
      actually written to the database.
- [x] Light browser check of the new risk-import form (renders correctly,
      admin nav present). The actual CSV-injection-via-upload path wasn't
      separately re-verified in-browser — the file-upload browser tool is
      restricted to the project workspace root and can't reach the
      scratchpad; the identical multipart-upload code path
      (FastAPI `UploadFile`) is already exercised by
      `test_risks_import_route_creates_rows_and_tracks_import_job`, and
      the neutralization itself is unit-tested directly.

## Decisions & Alternatives Rejected

- **Reused `app/csv_import.py::import_requirements_csv` unchanged** for
  the framework-requirements importer rather than rewriting it — it
  already validates all rows before writing any (the exact all-or-nothing
  guarantee this feature needs).
- **CSV formula injection neutralized on every free-text import field**
  (title/description/category/owner/treatment_plan) — this app's own
  register grids could later export/display imported data in a
  spreadsheet-adjacent context, so treating imported CSV content as
  untrusted the same way uploaded files are treated is consistent with
  the existing security posture, not new caution invented for this
  feature.
- **Markdown-policy and generic-evidence importers deferred, not built.**
  Policy storage requires content-validated PDF/DOCX bytes
  (`app/storage.py`), not markdown text — building a markdown-policy
  importer would mean either bypassing that validated pipeline or
  duplicating significant Policy-specific logic. Asset register CSV
  deferred too — no `Asset` model exists in this codebase to import into
  (that entity was never built in this pivot). Documented honestly here
  rather than shipping something that doesn't fit the actual domain
  model.
- **Watched-directory claiming uses a plain `os.rename`**, not a database
  lock — atomic on the same filesystem, so it's inherently safe across
  multiple worker processes without any DB coordination for the
  file-level claim (the DB-level idempotency check inside `run_import`
  is a second, independent layer of duplicate protection).
- **No compose.yaml changes for the watched directory** — it lives under
  `GRC_DATA_DIR`, already a shared volume between `app` and `worker`.

## Known Gaps / Follow-ups

- Markdown-policy and generic-evidence-file importers not built (see
  above) — would need their own design work against the existing
  Policy/Evidence models, not a natural extension of the CSV pattern.
- No importer auto-detection in the watched directory — one importer
  per watched directory instance (`--importer` is required), matching a
  typical single-purpose watched-folder deployment. Multi-importer
  detection (by filename convention or content sniffing) is a reasonable
  future extension if a second concrete need for it appears.
- `import-directory` CLI runs one pass; continuous watching needs an
  external loop/cron or the worker's `GRC_IMPORT_WATCH_DIR` polling.
