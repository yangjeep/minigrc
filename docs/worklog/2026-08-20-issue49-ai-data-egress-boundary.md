# Issue #49: AI data-egress/confidentiality boundary for BYOK providers

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature (security/data-governance boundary)

## Summary

Defines and enforces, in code, the default boundary on what compliance
data may leave a miniGRC deployment in a prompt sent to a BYOK AI
provider — ahead of #15 (BYOK provider abstraction/Digital Compliance
TPM), which does not exist yet in this repository. The issue is explicit
that this must *inform* #15's implementation, not follow it; this PR
builds a standalone, mandatory `app/ai_egress.py` boundary module that
#15 will be required to build every outbound provider request from,
rather than documenting a convention with nothing enforcing it. See
`docs/superpowers/specs/2026-08-20-issue49-ai-data-egress-boundary-design.md`
for the full design and repository-reality check.

## Dependency-graph note (per #35's execution loop)

Confirmed before starting: no open PRs exist in this repository, and no
`ai_provider`/`digital_tpm`/`byok`/`ai_egress`/`egress`-named code exists
anywhere in `app/` (grepped case-insensitively). #49's own text
authorizes exactly this ordering ("Should inform #15's implementation,
not follow it"), so #49 proceeding before #15 is not a broken
dependency — it is the dependency direction the issue itself specifies.

## Scope decisions

1. **Three-tier boundary, not a binary allow/deny.** Tier 1 (always
   included): structured/enumerable metadata — titles, dates, status/
   severity/origin enums, hashes, and derived booleans. Tier 2 (opt-in
   only, requires an explicit, non-empty-reason `AiEgressOptIn`):
   free-text narrative fields a human authored (descriptions, notes)
   that may incidentally contain sensitive detail. Tier 3 (never, no
   opt-in path exists in this module at all): identifying PII of a
   specific person (`Person.email`/`display_name`/`external_id`,
   `Policy.owner` as a bare name string) and raw file bytes/full
   document content.
2. **PII is a hard exclusion, more conservative than the issue's literal
   minimum.** The issue frames PII alongside raw-bytes/full-content as a
   single "opt-in eventually possible" bucket; this design makes PII
   hard-excluded instead, since no concrete feature today needs to send
   a person's name/email to a BYOK provider and building a granular
   per-field PII-opt-in mechanism now would be speculative
   infrastructure ahead of a real consumer (`.agent/RULES.md`). The
   issue's own Non-goals explicitly permit a conservative default
   without per-field configurable redaction.
3. **Reused the existing `AuditEvent`/`record_audit_event` audit trail**
   (same pattern `app/secrets.py` already uses for "Created encrypted
   secret" entries) rather than a new table/migration. AI egress isn't
   itself a material compliance event — it doesn't change any control/
   policy/finding state — so it belongs in the existing generic,
   already-rendered-generically (`templates/admin/audit_log.html`)
   operational audit trail, not a new DomainEvent aggregate.
4. **No BYOK provider client, prompt template system, or route/UI in
   this issue** — all explicitly #15's scope per the issue's own
   Non-goals. This module is read/log-only: it returns data and writes
   an audit trail; it never calls a network endpoint and has no
   mutation surface, so it cannot be used to bypass #15's (not yet
   built) hard AI-authority boundary.
5. **No headless UAT** — no user-visible surface (no route/template) is
   introduced, matching #42's own precedent for a backend-only
   service-layer module.

## What changed

- `app/ai_egress.py` (new): `AiEgressOptIn` (frozen dataclass, raises
  `ValueError` on an empty/blank `reason` in `__post_init__`),
  `AiPromptContext` (frozen dataclass; `fields` is a read-only
  `MappingProxyType` so a future caller cannot widen what gets sent by
  mutating an already-built context), `project_evidence_artifact`/
  `project_control_occurrence`/`project_finding`/`project_policy` (each
  a fixed Tier 1/Tier 2 allowlist reading directly from the ORM row —
  none of them accept a `Person` row or call `app/object_storage.py`/
  `app/storage.py`), and `record_ai_egress` (writes one `AuditEvent`
  with `entity_type=f"ai_egress:{entity_type}"`, `action="prompt_sent"`,
  and a JSON `detail` reproducing exactly `fields_sent` and
  `included_free_text` — no credential parameter exists on this
  function's signature at all; `_scrub_credential_like` additionally
  scrubs credential-shaped substrings from the serialized detail as
  defense-in-depth).
- `tests/test_ai_egress.py` (new, 18 tests).
- `docs/superpowers/specs/2026-08-20-issue49-ai-data-egress-boundary-design.md`
  (new design doc).

No schema/migration change; no existing file touched.

## Test strategy and results

- **Default projection per entity type** includes exactly the
  documented Tier 1 fields and excludes every Tier 2/3 field, even when
  the underlying row's free-text fields are populated with clearly
  sensitive-looking content (a planted email + employee id in a
  description/evidence note).
- **Opt-in projection** adds exactly the documented Tier 2 fields (and
  only those), still excludes Tier 3, including when a `ControlOccurrence`
  has a real linked `responsible_person`/`performed_by` `Person` with an
  email and display name — asserted absent from the projected fields
  under both default and opt-in calls.
- **Opt-in reason enforcement**: blank/whitespace-only `reason` raises
  `ValueError`; missing `actor` raises `TypeError` (no silent default).
- **`record_ai_egress` correctness**: the persisted `AuditEvent.detail`,
  parsed back, exactly reproduces `AiPromptContext.fields` and
  `included_free_text` — proving the audit trail is genuine evidence of
  what left the deployment, not a paraphrase. A default call and an
  opt-in call against the same artifact produce audit records
  distinguishable from each other after the fact via `included_free_text`.
- **Credential-scrub defense-in-depth**: a credential-shaped string
  planted inside an opted-in free-text field is redacted in the
  persisted detail; `record_ai_egress`'s signature is asserted to have
  no `key`/`secret`/`token`/`credential`-named parameter at all (the
  primary guarantee — the scrub is secondary).
- **Immutability**: attempting to add a key to an already-built
  `AiPromptContext.fields` raises `TypeError` (`MappingProxyType`).
- **No `project_person` function exists** — asserted directly via
  `hasattr`, so Tier 3's "no code path" claim isn't just a happy-path
  test gap.
- **Full regression suite**: `pytest -q` — see final verification
  section below for the exact count; no existing test was modified.
- **Lint/format**: `ruff check .` and `ruff format --check .` both
  clean on the full repository.
- **SQLite/PostgreSQL**: no schema change; the only write path is
  `record_audit_event` inserting into the pre-existing `audit_events`
  table (`entity_type`/`entity_id`/`action`/`detail`/`actor`, all plain
  `String`/`Text` columns), already exercised on both backends by
  dozens of existing call sites. No backend-specific code introduced by
  this module. Live execution of this issue's own new test file against
  a real PostgreSQL instance is `PENDING — unavailable in cloud
  execution environment` (no local Postgres in this session), consistent
  with this repository's existing CI-only Postgres split for
  non-schema-changing service-layer modules; not required for the
  reasons above.
- **Headless UAT**: not applicable — no user-visible route/template
  introduced (matches #42's precedent).
- **Claude Desktop UAT**: not applicable, same reason.

## Security/integrity review

Adversarial review performed against `.agent/LOOP.md` §9's checklist,
focused on: PII leakage bypass (direct and indirect, including via
`json.dumps(..., default=str)`), raw-content leakage, credential
leakage (including whether a careless future caller could smuggle a
credential through `provider_name`/`model`), opt-in bypass, mutability/
aliasing of the "immutable" `fields` mapping, audit-log fidelity,
`AuditEvent.entity_type` collision with any existing consumer, and test
vacuity. See the following section for findings and disposition.

## Known deferred/untested paths

- No BYOK provider HTTP client, prompt-assembly/template system, SSRF
  boundary review for a configurable provider endpoint, or route/UI —
  all explicitly #15's scope, not built here (issue's own Non-goals).
- No granular per-field configurable redaction policy — the three-tier
  boundary is fixed, not organization-configurable, for MVP (issue's
  own Non-goals explicitly permit this).
- A future deliberate "attach the actual file" feature is not provided
  by this module and would need its own, separately designed and
  authorized mechanism — not a hidden opt-in path here.
- Live-PostgreSQL execution of this issue's own tests is PENDING in
  this cloud environment (no local Postgres); not required given no
  schema/backend-specific code path exists.
