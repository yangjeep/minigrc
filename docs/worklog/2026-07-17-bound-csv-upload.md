# Bound framework CSV uploads

**Date:** 2026-07-17
**Author:** Claude (agent)
**Type:** fix

## Summary

PR #1 review (Codex) flagged that `POST /frameworks/{id}/import` read the
entire uploaded CSV into memory with `file.file.read()` with no size limit,
so an authenticated client could exhaust the process's memory with a large
file. Added a bounded, chunked reader that enforces the existing
`GRC_MAX_UPLOAD_MB` setting (already used for policy uploads) before the
CSV parser or the database ever sees the content. Also fixed a related,
lower-severity Copilot finding: `Jinja2Templates`/`StaticFiles` used
CWD-relative paths (`"app/templates"`, `"app/static"`), which break if the
process is started from a different working directory.

## Files Changed

- `app/csv_import.py` — added `read_csv_upload()` (chunked read, raises
  `CsvTooLargeError` once the configured byte limit is exceeded) and
  `CsvTooLargeError`.
- `app/routers/frameworks.py` — `import_requirements_csv` route now reads
  via `read_csv_upload(file, max_bytes=settings.max_upload_bytes)` inside a
  try/except, flashing an error and touching nothing in the database when
  the file is too large.
- `app/main.py` — resolve `app/templates` and `app/static` relative to
  `Path(__file__).resolve().parent` instead of the process CWD.
- `tests/test_requirements.py` — `test_oversize_csv_rejected_without_touching_database`
  (sets `max_upload_mb = 0`, confirms the requirement set is unchanged) and
  `test_csv_within_size_limit_still_imports` (confirms the normal path
  still works after the change).

## Verification

- [x] Tests pass (`pytest`) — 66 passed
- [x] Lint/format clean (`ruff check .`, `ruff format --check .`)
- [x] Manually verified — reused the same chunked-read-with-running-total
  pattern already proven in `app/storage.py::save_policy_version_upload`
  for policy uploads.

## Decisions & Alternatives Rejected

- Reused the existing `GRC_MAX_UPLOAD_MB` / `max_upload_bytes` setting
  rather than adding a new CSV-specific env var — one upload-size knob is
  simpler and the CSV import route doesn't need a different limit than
  policy uploads.
- Kept the CSV content fully in memory (as `bytes`) after the bounded read,
  rather than switching `import_requirements_csv` to a streaming parser —
  the size is already capped by the same limit as policy files, so an
  additional streaming CSV parser would be a generic abstraction with only
  one caller.

## Known Gaps / Follow-ups

- No CSV row-count cap yet (only a byte-size cap). The spec for a future
  vendor-roster CSV importer calls for both; this framework-requirements
  importer stays byte-capped only since importing thousands of orderly
  clauses per file is not a realistic size concern relative to memory.
- The `duplicate requirement mapping` and `risk score range` findings from
  the same PR review were verified already fixed in later commits
  (`UniqueConstraint` + `IntegrityError` handling in
  `app/routers/controls.py`; range validation in `app/routers/risks.py`)
  — no action needed.
