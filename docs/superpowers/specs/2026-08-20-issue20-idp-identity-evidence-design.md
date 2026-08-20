# Issue #20: IdP identity metadata as access-control evidence

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Repository-reality check (read first)

Before designing, inspected #17/#18's actual merged code
(`app/oidc.py`, `ExternalIdentity`, `OidcRoleMapping`), #32's evidence
model (`EvidenceSnapshot`), and #13's population/sampling model
(`ControlTestPopulation`/`ControlTestPopulationItem`). Two findings
reshape this issue's scope from what its own execution prompt assumes:

1. **Generic OIDC has no directory/admin API to call.** Standard OIDC
   discovery/token/userinfo endpoints only return the *currently
   authenticating* user's own claims — there is no standards-based "list
   every user/group in this IdP" endpoint. Building one would mean a
   provider-specific admin API integration per IdP (Authentik's own REST
   API ≠ Okta's Users API ≠ Entra's Graph API), which is explicitly what
   the issue's own non-goals rule out ("No Authentik-only implementation,"
   "No full SCIM directory sync"). **Resolution:** the identity/group
   population this issue can safely and honestly capture is *miniGRC's
   own already-collected record* of who has authenticated via the
   configured OIDC provider — `ExternalIdentity` rows plus each linked
   `User`'s currently-mapped role (`app/oidc_role_mapping.py`'s own
   output) — not a live call to an external directory. This exactly
   matches the issue's own first two "intended use cases" ("Snapshot
   current miniGRC users linked to an OIDC provider," "Snapshot ...
   miniGRC role mappings") and needs zero new provider-specific
   connector code.
2. **`ControlTestPopulationItem` is hardcoded to `control_occurrence_id`,
   not a generic/polymorphic item reference** — #13's population/sample
   model was built specifically for "sample N of these control
   occurrences," not for an arbitrary item type like "a person in an
   identity snapshot." Reusing it for an identity population would
   require making that table polymorphic — a real schema change to
   #13's already-shipped, tested model, out of proportion to "smallest
   coherent slice." **Resolution:** this issue implements the
   evidence-capture and access-review-linkage use cases fully; the
   fourth listed use case ("use a frozen snapshot as the population
   basis for #13 sampling") is explicitly deferred as a documented gap
   requiring its own future schema decision, not silently forced in.

## Design

### Reuse `EvidenceSnapshot` (#32) directly — no new table

`EvidenceSnapshot` (`app/models.py`) already models exactly what's
needed: `source_type`, `check_key`, `status`, `title`, `summary`,
`collected_at`, a bounded secret-free `normalized_payload_json`, and
`raw_payload_sha256` — the same shape `app/aws_connector.py::build_evidence_snapshot`
already uses for CloudTrail/IAM evidence. `app/oidc_identity_evidence.py`
(new) adds a second producer of this same table,
`source_type="oidc_identity_population"`, following that exact
construction pattern rather than inventing a parallel evidence model.

Captured per identity: `issuer`, `subject` (OIDC subject claim — a
stable, non-secret identifier), `user_id`, `email`, `role`,
`role_source`, `user_status`, `linked_at`. **Deliberately not captured:**
raw provider-side group/claim values — only the *derived* miniGRC role
persists durably anywhere in this codebase (`User.role`); storing raw
claims would mean adding new persistent storage for a potentially large,
provider-varying payload, which the issue's own "minimum necessary"
instruction argues against. No token, client secret, or authentication
payload of any kind is included — trivially true, since neither
`ExternalIdentity` nor `User` stores any such field.

### Provenance and immutability

`EvidenceSnapshot` rows already have no edit/delete route (`app/routers/evidence.py`)
— a correction is always a new snapshot. Once captured, later changes to
`ExternalIdentity`/`User.role` never touch an already-created snapshot's
`normalized_payload_json` — proven directly by a test that captures,
mutates role state, and re-reads the original row.

### Access-review linkage (never auto-completes anything)

Reuses `app/control_occurrences.py::link_evidence` (already used for AWS/
manual evidence) to attach the snapshot to a `ControlOccurrence` via the
existing `ControlOccurrenceEvidence` join table — the exact same
mechanism every other evidence type already uses. Capturing or linking a
snapshot never calls `perform_occurrence`; performing the occurrence
remains a fully separate, explicit human action.

### #14 readiness integration comes for free

`app/readiness.py::_compute_occurrence_missing_evidence_items` already
flags any performed-but-unlinked-evidence occurrence, for every control
domain, including one that uses an identity snapshot as its evidence —
**no new readiness code is needed**; this is exactly what "reuse the
generic evidence/provenance model" produces for free once the snapshot
is linked through the existing mechanism, satisfying the issue's fifth
intended use case ("surface stale/missing access-review evidence through
#14") without a bespoke new check.

### Minimal UI, admin-gated capture

`POST /admin/authentication/oidc/identity-snapshot` (added to the
already admin-gated `admin_authentication.router`) triggers a capture.
Admin-only for the *capture* action specifically — stricter than the
`require_write_access` bar most evidence-linking actions use elsewhere —
because identity/group population data is more sensitive than typical
operational evidence (it reveals who currently holds what access), and
least-privilege favors under- rather than over-granting here. *Viewing*
an already-captured snapshot reuses the existing, unmodified `/evidence`
routes (`require_login`, same as every other evidence type — no new,
inconsistent restriction added there).

## Test strategy

- **Unit** (`tests/test_oidc_identity_evidence.py`): snapshot payload
  correctness (issuer/subject/email/role/status/linked_at, and nothing
  else — explicit key-set assertion); zero-identities case; hash
  determinism; a payload key-set assertion proving no token/secret field
  is ever present.
- **Historical immutability**: capture → mutate `User.role`/`ExternalIdentity`
  state → re-read the original snapshot → unchanged.
- **Access-review integration**: link a captured snapshot to a
  `ControlOccurrence` via the existing `link_evidence`; assert the
  occurrence's `performed_at` is untouched by capture or linking alone.
- **Authorization**: capture route requires admin (403 for
  operator/reader); a reader/operator can still view an already-captured
  snapshot via the existing `/evidence` route (unchanged behavior).
- **#14 readiness**: an access-review-shaped occurrence performed without
  linked evidence still surfaces via the existing readiness check —
  proves the "comes for free" claim rather than assuming it.
- **Headless UAT** (`tests/uat/test_oidc_identity_evidence.py`): this
  does add a real new POST route and a real new button on an
  already-admin-gated page, and its resulting evidence is later selected
  through a real, different route's form field
  (`/occurrences/{id}/perform`'s `evidence_snapshot_id` dropdown) — a
  genuine new user-visible capability, not the "no route at all" shape
  of #42's backend-only precedent. Scenario: an admin captures the
  snapshot, an operator sees it offered in the evidence dropdown while
  performing a real access-review occurrence and selects it — proving
  the real end-to-end HTTP flow, not just the service function in
  isolation. A second scenario proves capturing a snapshot alone never
  touches any `ControlOccurrence` row; a third proves a non-admin is
  rejected (403) from the capture route.

## Known deferred/untested paths

- #13 population/sampling reuse: explicitly deferred, requires its own
  schema decision (making `ControlTestPopulationItem` polymorphic or
  adding a parallel identity-population-sample model) — not built here.
- No live external-IdP directory sync of any kind, by design.
- No automatic conclusion of least privilege, MFA compliance, or control
  effectiveness from a captured snapshot — the snapshot is evidence
  only; human review through the existing occurrence-performance/test
  workflow remains required.
