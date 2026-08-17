# Issue #50: unhandled 500 deleting an InternalControl with dependent rows

**Date:** 2026-08-17
**Author:** Claude (agent)
**Type:** fix

## Summary

Fixes an unhandled `IntegrityError` (surfacing as a 500) when deleting an
`InternalControl` that has dependent rows without an ORM cascade —
concretely, a `ControlOccurrence` (#11, just merged to main). The fix is
in the generic register-grid infrastructure (`app/registers/router.py::
delete_row`), not control-specific code, since the same class of gap
exists for every deletable register.

## Root Cause

`app/registers/router.py::delete_row` called `db.delete(row)` with no
explicit flush and no `IntegrityError` handling. The actual DELETE only
executes when the request-scoped session commits at `get_db`'s teardown
(`app/deps.py`), by which point the route has already returned its 204
response object — `create_row`/`update_row` already guard their own
`db.flush()` calls against `IntegrityError` (returning a clean 422), but
`delete_row` never got the same treatment.

**Correction to the issue's own bug report:** the issue speculated
`ControlRequirementMapping` was a triggering example alongside a future
`ControlOccurrence`-style row. Reproducing directly against current main
showed this is only half right: `InternalControl.mappings` already has
`cascade="all, delete-orphan"` (an existing, deliberate design choice —
a requirement mapping is a structural join, not a historical compliance
fact, so cascading it away with the control is correct, not a bug).
`ControlOccurrence` is the row that actually triggers the failure,
because its relationship is deliberately *not* cascaded (occurrence
history must survive independently of the control's current metadata,
per the #11 design). Verified directly (`sqlite3.IntegrityError: FOREIGN
KEY constraint failed` reproduced against current main before the fix,
via both direct ORM `session.delete()` and the real HTTP `DELETE
/api/registers/controls/{id}` route with `TestClient(raise_server_exceptions=True)`).

## Files Changed

- `app/registers/router.py::delete_row` — wraps `db.delete(row)` in a
  `db.flush()`/`except IntegrityError` guard, mirroring `create_row`/
  `update_row`'s existing pattern exactly: rollback (discarding both the
  failed delete and its now-inapplicable `AuditEvent`) and return a clean
  422 with `{"__all__": [...]}`, which `register-grid.js`'s existing
  `errorMessage()` helper already renders correctly with no JS/template
  changes needed.
- `tests/test_register_api.py` — `test_delete_rejects_row_with_dependent_occurrence`
  (the real regression case) and `test_delete_still_cascades_mappings`
  (locks in that the pre-existing, intended mapping-cascade behavior is
  unaffected by this fix).

## Verification

- [x] Regression test added first and confirmed to fail for the expected
      reason before the fix (`git stash` the fix, rerun — raises the raw
      `sqlite3.IntegrityError`, uncaught).
- [x] `tests/test_register_api.py`: 21 passed (2 new, 19 existing
      unaffected — including every other register's delete path, since
      the fix is in shared infrastructure).
- [x] Full suite: run alongside the rest of this session's work; report
      actual result rather than assuming.
- [x] `ruff check .` / `ruff format --check .` clean.
- [x] No dependent rows silently cascade-deleted by this fix (occurrence
      case: control AND occurrence both survive after the rejected
      delete; mapping case: unaffected, still cascades as before).

## Decisions & Alternatives Rejected

- **Fixed in the shared `delete_row`, not `CONTROLS_REGISTER_CONFIG`
  specifically** — every deletable register (risks, vendor systems,
  evidence, people, etc.) has the identical latent gap; a control-only
  fix would leave the same bug reachable elsewhere. This mirrors exactly
  where `create_row`/`update_row`'s own `IntegrityError` handling already
  lives.
- **Did not disable `deletable` for controls** (the pattern used by
  `admin_users`/`frameworks`/`connections`/`admin_jobs`, all of which
  simply omit delete entirely) — controls have always been deletable
  through this app, and disabling it now would be a larger behavior
  change than this bug warrants; a clean 422 with a clear message is the
  smaller, more consistent fix per the issue's own stated acceptable
  approaches.
- **Did not treat `ControlRequirementMapping`'s cascade as something to
  "fix"** — verified it's an existing, deliberate relationship
  (`cascade="all, delete-orphan"`), and a mapping is not itself a
  historical compliance fact the way a `ControlOccurrence` claim is;
  changing that behavior would be a distinct, debatable product decision
  outside this bug's scope, not something to silently bundle in here.

## Known Gaps / Follow-ups

None — this closes the acceptance criteria as stated: no unhandled 500,
regression test in place, no dependent rows silently cascade-deleted.
