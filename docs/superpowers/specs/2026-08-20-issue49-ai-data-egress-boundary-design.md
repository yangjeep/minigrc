# Issue #49: AI data-egress/confidentiality boundary for BYOK providers

Parent epic: #10 (SOC 2 Type II-first). Depends on: #15 (BYOK provider
abstraction) per the issue's dependency line, but the issue itself is
explicit that it must **inform #15's implementation, not follow it** —
this design and its enforcement module exist before #15 does, so #15's
provider abstraction/prompt-assembly code has no path to a provider that
skips this boundary.

## 1. Repository-reality check

- **#15 does not exist yet.** Grepped `app/` for `ai_provider`,
  `digital_tpm`, `byok`, `AIProvider`, `ai_egress`, `egress` (case
  insensitive) before writing any code: zero hits except this issue's
  own future module and the two docs that reference #15/#49 by number.
  There is no BYOK provider client, no prompt-assembly code, no AI
  routes, no AI-related DB table. `git log`/open PRs confirm no active
  or completed work toward #15.
- **This means the issue's own conditional instruction — "if #15's
  prompt-assembly code already exists, add an explicit
  boundary-enforcement layer... into it" — does not apply.** There is
  nothing existing to retrofit. The corresponding non-conditional
  requirement instead governs this issue: define the boundary and build
  it as a **standalone, mandatory module** that #15 must depend on and
  call through exclusively once it exists. This issue does not implement
  #15's BYOK HTTP client, prompt templates, or any AI feature UI — those
  remain #15's scope (see Non-goals in the issue and §6 below).
- **What a "summarize evidence" feature would actually touch**, from
  `app/models.py`: `EvidenceArtifact`/`EvidenceArtifactVersion` (title,
  free-text `description`, and version metadata incl. `object_key`,
  `original_filename`, `sha256`, `media_type`, `byte_size`,
  `source_object_id`/`source_revision_id`); `ControlOccurrence`
  (`scope_note`, `evidence_note` free text, plus `responsible_person_id`/
  `performed_by_person_id` FKs to `Person`); `Finding` (free-text
  `description`, `closure_note`, `owner_person_id` FK to `Person`);
  `Policy` (free-text `description`, a plain string `owner` field — not
  FK-linked, just a name). `Person.email`/`display_name`/`external_id`
  are the concrete PII this repository actually has. File bytes
  themselves are never loaded through `app/models.py` — they live in
  `GRC_DATA_DIR`/S3-compatible storage and are read only through
  `app/object_storage.py`/`app/storage.py`.
- **Existing secret-handling precedent** (`app/secrets.py`,
  `app/crypto.py`): plaintext secrets are never returned through an API
  response after creation; `resolve_secret` is a server-side-only call.
  This issue's logging function follows the same shape — it must not be
  *able* to receive a credential, not merely be trusted not to log one.
- **Existing audit precedent** (`app/audit.py::record_audit_event`,
  backing the `audit_events` table used throughout — e.g.
  `app/secrets.py`'s "Created encrypted secret" entries): a plain,
  generic, already-rendered-generically (`templates/admin/audit_log.html`
  loops `entity_type`/`action`/`detail`/`actor` with no per-type
  branching) audit trail already exists for exactly this kind of
  "record what happened, for auditor-facing history" fact. AI egress is
  not itself a material compliance event (it doesn't change any
  control/policy/finding state) — it's operational/security audit
  trail, the same category `record_audit_event` already serves for
  secret creation. No new table, no migration, no DomainEvent aggregate.

## 2. What may leave the deployment, by default

A **conservative, three-tier boundary** (issue's Non-goals explicitly
allow a documented default rather than per-field configurable redaction
rules):

### Tier 1 — always included (the default, no opt-in needed)

Structured/enumerable metadata: object titles/names of compliance
*things* (never people), status/severity/origin enum values, dates,
counts, content hashes, and derived booleans. Concretely, per entity
type the default projection includes only:

| Entity | Default fields |
|---|---|
| `EvidenceArtifact` | `title`, `version_count`, `latest_version_number`, `media_type`, `byte_size`, `sha256`, `captured_at`, `source_type` |
| `ControlOccurrence` | `control_name`, `origin`, `due_at`, `performed_at`, `has_responsible_owner` (bool) |
| `Finding` | `title`, `severity`, `status`, `due_date`, `has_owner` (bool) |
| `Policy` | `title`, `status`, `effective_date`, `next_review_date`, `has_named_owner` (bool) |

This matches the issue's own examples verbatim ("structured metadata,
control names, dates").

### Tier 2 — opt-in only, and logged as such

Free-text narrative fields a human actually typed, which may
incidentally contain sensitive detail even though the field itself
isn't classified PII: `EvidenceArtifact.description`,
`ControlOccurrence.scope_note`/`evidence_note`,
`Finding.description`/`closure_note`, `Policy.description`.

Including any of these requires the caller to pass an
`AiEgressOptIn(actor, reason)` — a single, explicit, non-empty-reason
argument distinct from the default call shape (empty/omitted reason
raises `ValueError`, so it can't be satisfied by an accidental default).
`AiPromptContext.included_free_text` records whether this happened, and
`record_ai_egress` persists that flag into the audit trail, so an
opt-in call is distinguishable from a default one after the fact, not
just at call time.

### Tier 3 — never included by this module, no opt-in path exists

- **Identifying PII of a specific person**: `Person.email`,
  `Person.display_name`, `Person.external_id`, and `Policy.owner` (a
  free-text name string, not FK-linked, but still identifies an
  individual). No function in this module accepts a `Person` row or
  returns any of these values — not even under `AiEgressOptIn`. Where a
  human's involvement is relevant, the default projection instead
  exposes a boolean (`has_responsible_owner`, `has_owner`,
  `has_named_owner`) — enough for a deterministic readiness/prioritize
  feature to reason about "is someone assigned," never who.
- **Raw evidence file bytes / full extracted document content**:
  structurally impossible through this module, because every function
  takes an already-loaded ORM row and never calls
  `app/object_storage.py`/`app/storage.py` to read bytes. A future
  feature that deliberately wants to send a file's actual content to a
  provider is not "the opt-in path" for this module — it would need its
  own, separately designed and authorized mechanism, which this issue
  does not build (there is no concrete consumer of it yet — #15 owns
  deciding whether/how such a feature is ever justified).

This is deliberately **more conservative than the issue's literal
wording strictly requires** for PII (the issue frames PII alongside
raw-bytes/full-content as a single "opt-in eventually possible" bucket;
this design makes PII a hard exclusion instead). Justification: no
concrete feature today needs to send a person's name/email to a BYOK
provider, so building a granular per-field PII opt-in mechanism now,
with nothing to validate it against, is exactly the "speculative
abstraction ahead of a concrete use" `.agent/RULES.md` warns against.
The issue's own Non-goals section explicitly permits this: "a
documented, conservative default boundary... is sufficient." If #15
later needs a real PII-inclusion workflow (e.g., "draft a reminder
addressed to Jane"), that is a new, explicit, separately-reviewed
decision — not a parameter added quietly to this module.

## 3. Enforcement mechanism (`app/ai_egress.py`, new)

```python
@dataclass(frozen=True)
class AiEgressOptIn:
    actor: str
    reason: str  # must be non-empty — enforced, not just documented


@dataclass(frozen=True)
class AiPromptContext:
    entity_type: str
    entity_id: str
    fields: MappingProxyType  # exactly what would leave the deployment, already scrubbed, read-only
    included_free_text: bool  # True only when an AiEgressOptIn was honored
    opt_in_actor: str | None = None  # who granted the opt-in (independent of record_ai_egress's actor)
    opt_in_reason: str | None = None  # why — persisted, not discarded after validation (see §6 finding 5)


def project_evidence_artifact(artifact, *, opt_in=None) -> AiPromptContext: ...
def project_control_occurrence(occurrence, *, opt_in=None) -> AiPromptContext: ...
def project_finding(finding, *, opt_in=None) -> AiPromptContext: ...
def project_policy(policy, *, opt_in=None) -> AiPromptContext: ...


def record_ai_egress(
    session,
    context: AiPromptContext,
    *,
    task_type: str,
    provider_name: str = "",
    model: str = "",
    actor: str = "system",
) -> AuditEvent: ...
```

Every string value a `project_*` function puts into `fields` is passed
through `_scrub_string` (email-shaped and credential-shaped patterns)
**before** it is wrapped into the `AiPromptContext` — i.e. before it
could reach a real provider request, not only before it reaches the
audit log. This was corrected during adversarial review (§6 below):
the first version of this module only scrubbed the log copy, which
would have left the *actual* egress unprotected. A matched value is
replaced in full, never just the matched substring, so a credential
split across a line break can't leave a partially-redacted tail exposed
while the marker falsely signals a full redaction.

`record_ai_egress` is the **only** function that writes a log record,
and its signature has no credential parameter at all — it cannot leak
what it never receives. It additionally applies `_scrub_string` to its
own caller-supplied `task_type`/`provider_name`/`model` strings, since
those aren't part of `fields` and so aren't covered by the projection-time
pass above. The scrub throughout is belt-and-suspenders, not the primary
guarantee — the primary guarantee is tier 1/2's fixed allowlists never
including a `Secret`/credential field in the first place (verified: no
entity type in this module has a secret-shaped column), and the
credential patterns are deliberately restricted to distinctive prefixes
(OpenAI `sk-`, GitHub `ghp_`/`gho_`/`github_pat_`, Google `AIzaSy`,
Slack `xox[baprs]-`, AWS `AKIA...`, labeled `api key:`/`Bearer`, and
`scheme://user:pass@` connection strings) rather than a generic
high-entropy heuristic — a bare-hex/base64 heuristic would false-positive
on Tier 1's own legitimate `sha256` field, corrupting real audit data to
close a gap (Azure OpenAI's prefix-less key format, AWS secret access
keys) that isn't reliably distinguishable from a content hash by regex
in the first place. That residual gap is accepted and documented rather
than "fixed" by a change that would do more harm than good.

`record_ai_egress` writes one `AuditEvent` with
`entity_type=f"ai_egress:{context.entity_type}"`,
`entity_id=context.entity_id`, `action="prompt_sent"`, and `detail` a
JSON object containing `task_type`, `provider_name`, `model`,
`included_free_text`, and `fields_sent` — the literal
`AiPromptContext.fields` that were sent, not a paraphrase, so the log is
genuinely "inspectable" per the issue's requirement. This reuses the
existing generic `templates/admin/audit_log.html` rendering with no
template change.

This module is intentionally **read/log-only** — it returns data and
writes an audit trail; it never calls out to a network endpoint, never
constructs a full prompt string/template (that composition is #15's
job, built exclusively from `AiPromptContext.fields`), and never
authorizes or performs any mutation. It cannot be used to bypass #15's
existing hard authority boundary (AI may never autonomously approve/
attest/pass/close/accept-risk) because it has no mutation surface at
all — that boundary is unaffected by this issue and remains #15's to
enforce when its command-handling code exists.

## 4. Non-goals (explicitly deferred to #15)

- No BYOK provider HTTP client, no OpenAI-compatible request/response
  code, no configurable endpoint/model/credential handling (SSRF
  boundary review for a configurable endpoint is #15's concern, not
  this module's — this module never makes a network call).
- No prompt template/assembly system. `AiPromptContext.fields` is the
  contract #15 must build from; how it turns that into an actual
  provider request string is out of scope here.
- No HTTP route/admin UI. Same precedent as #42 (`InternalControlVersion`)
  and the connector-registry issues: a backend service-layer module with
  no user-visible surface yet needs no headless UAT — there is nothing
  for a browser/HTTP flow to exercise.
- No granular per-field configurable redaction policy (issue's own
  Non-goals). The three-tier boundary above is fixed, not
  organization-configurable, for MVP.

## 5. Test strategy

- Default projection per entity type includes exactly the documented
  Tier 1 fields and excludes every Tier 2/3 field, including when the
  underlying row's free-text fields are populated with clearly
  sensitive-looking content.
- Opt-in projection adds exactly the documented Tier 2 fields, still
  excludes Tier 3, and raises `ValueError` for an empty/whitespace-only
  reason (can't be "opted in" by accident or by a blank string).
- No function accepts a `Person` row, and no returned `fields` dict for
  any entity, opt-in or not, ever contains an `@` (email-shaped) value
  or a `Person.display_name` value planted on a linked responsible/owner
  person — adversarial check that the "never" tier really has no code
  path, not just that the happy-path tests don't exercise it.
- `record_ai_egress` persists an `AuditEvent` whose `detail` JSON,
  parsed back, exactly reproduces `context.fields` and
  `included_free_text` — proving the log is genuinely inspectable
  evidence of what was sent, not a lossy summary.
- A credential-shaped string planted inside an opted-in free-text field
  (simulating a worst-case accidental paste) is redacted by
  `_scrub_credential_like` in the persisted `detail`, demonstrating the
  defense-in-depth scrub actually fires — while the primary test suite
  confirms the fixed allowlists never include a secret-shaped field to
  begin with.
- A representative end-to-end scenario: build an `EvidenceArtifact` +
  version, project it with and without opt-in, record both, and assert
  the default call's audit record contains only Tier 1 fields while the
  opt-in call's contains Tier 1 + `description` and nothing from Tier 3
  — this is the issue's requested "representative AI-assisted
  draft/summary call... showing exactly what data left the deployment."

No headless UAT / no HTTP route: matching #42's own precedent, this is
a backend-only module with no user-visible surface in this issue.

## 6. Adversarial review findings and fixes

An independent adversarial-review pass (per `.agent/LOOP.md` §9) against
the first version of this module found five real, validated gaps, all
fixed before merge:

1. **HIGH — Tier 1 "always included" `title`/`control_name` fields are
   unconstrained free text and could carry PII with no opt-in gate at
   all** (e.g. a `Finding.title` containing a pasted email). Fixed by
   moving the scrub to projection time (§3 above) and applying it to
   every string field, Tier 1 included — closing the concrete leak
   while keeping the issue's own explicit "control names are default-safe"
   example intact for the overwhelming normal case where a title is just
   a label.
2. **MEDIUM — the original credential scrub only substituted the matched
   span, so a credential split across a line break left its tail exposed
   while the marker falsely signaled full redaction.** Fixed by
   redacting the entire field value whenever any part of it matches
   (`test_split_credential_leaves_no_tail_exposed`).
3. **MEDIUM — the credential regex missed common non-OpenAI BYOK/provider
   credential shapes** (GitHub, Google, Slack, AWS access-key-id,
   labeled `API Key: value` with a space, connection-string passwords).
   Fixed by adding distinctive-prefix patterns for each
   (`test_additional_credential_shapes_are_scrubbed`); a generic
   prefix-less high-entropy heuristic was deliberately rejected (see §3)
   because it would corrupt the legitimate `sha256` field
   (`test_sha256_hash_is_never_mistaken_for_a_credential` proves it
   doesn't).
4. **MEDIUM — `AiEgressOptIn`'s non-empty-reason check used `str.strip()`,
   which does not treat zero-width space (U+200B) or the BOM (U+FEFF) as
   whitespace, so an invisible-only "reason" defeated the check; a
   non-string `reason` raised an unrelated `AttributeError` instead of a
   documented error.** Fixed with a Unicode-category-based check
   (stripping category C/Z characters before testing for emptiness) and
   an explicit `isinstance` guard raising `TypeError` for a non-string
   `actor`/`reason` (`test_blank_or_invisible_reason_rejected`,
   `test_non_string_reason_raises_type_error_not_attribute_error`).
5. **MEDIUM — the opt-in's `actor`/`reason` were validated and then
   discarded; `record_ai_egress` never persisted *why* Tier 2 content
   was included, undermining the log's value as evidence of a deliberate
   decision.** Fixed by carrying `opt_in_actor`/`opt_in_reason` on
   `AiPromptContext` and persisting both in `record_ai_egress`'s detail,
   independent of that function's own `actor` parameter (which identifies
   who/what triggered the call and may legitimately differ from who
   granted the opt-in — e.g. a scheduled job acting on an earlier human
   decision) (`test_opt_in_reason_is_persisted_independently_of_record_ai_egress_actor`).

No findings for: PII leakage via relationship traversal/`__repr__`/the
`json.dumps(..., default=str)` fallback (never triggered — every field
is a pre-stringified scalar); raw file bytes/`object_key`/source ids
(structurally unreachable); `MappingProxyType` mutability/aliasing
(solid — the raw dict never escapes a `project_*` function before
wrapping); `AuditEvent.entity_type` collision with an existing consumer
(none — the admin audit-log template renders every `entity_type`
generically and nothing else in the codebase branches on it).

## 7. Definition of done

- A documented, default (metadata-only) data-egress boundary exists,
  covering the concrete entity types a "summarize evidence"/pre-fill
  feature would touch today.
- Raw evidence bytes/full document content are structurally
  unreachable through this module; PII is a hard exclusion with no
  opt-in code path (a deliberately more conservative stance than the
  issue's minimum, justified in §2).
- The boundary is enforced in code (`app/ai_egress.py`), not documented
  as a convention only — #15, when implemented, has no way to build a
  provider request except by starting from an `AiPromptContext`
  produced by this module's projection functions.
- `record_ai_egress` cannot leak a provider credential: it has no
  credential parameter, and its output is defensively scrubbed of
  credential-shaped substrings regardless.
