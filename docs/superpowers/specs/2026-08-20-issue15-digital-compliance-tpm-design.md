# Issue #15 — BYOK AI provider abstraction + Digital Compliance TPM

Status: implemented (draft PR), Claude Desktop UAT PENDING

## 1. Repository-reality check before designing

Grepped before writing any code:

- No prompt-assembly, provider-client, or "AI"/"LLM" code exists anywhere in
  `app/` prior to this change, other than `app/ai_egress.py` (issue #49,
  merged), which is a data-egress boundary with **no HTTP client and no
  provider concept** — it only shapes/scrubs what a future call would send.
  Its own docstring says #15's prompt-assembly code must build every
  outbound request from one of its `project_*` functions.
- `app/google_oidc_config.py` / `app/oidc_config.py` are the established
  BYOK-shaped config-resolution pattern: a frozen `Resolved*Config`
  dataclass, a "most-recently-updated settings row, else legacy env vars,
  else unconfigured" resolution order, and total degradation to
  `usable=False` on any secret-resolution failure. Replicated here as
  `ResolvedAiProviderConfig` / `resolve_ai_provider_config`.
- `app/secrets.py` (`create_encrypted_secret`, `resolve_secret`) and the
  shared `Secret` model already provide encrypted-credential storage with
  no readback. Reused as-is — no new secret-storage mechanism.
- `app/jobs.py` / `app/worker.py` is a claim/run job **queue** (work is
  enqueued by request-time code), not a **scheduler** — there is no cron/
  periodic-trigger primitive in the repository. `app/cli.py`'s
  `aws-run-checks` and `generate-control-occurrences` commands are the
  established precedent for "one-shot CLI command, intended for an
  external cron, adds no scheduling infrastructure itself." The Nag
  capability follows that exact precedent (`ai-nag-scan` CLI command)
  rather than inventing a scheduler.
- `app/readiness.py::compute_readiness_queue` /
  `compute_readiness_stage` (issue #14) are already the sole authoritative,
  deterministic source for "what is blocking readiness" — Observe/
  Prioritize wrap these and add zero independent readiness logic.
- No existing SSRF guard validates an arbitrary *admin-configured* base
  URL (the one precedent, `app/google_drive.py::parse_drive_file_id`,
  validates a Drive file ID/URL shape, not a configurable HTTP endpoint).
  A new, narrowly-scoped guard was written for the provider `base_url`.

## 2. Scope

Implements exactly the four bounded capabilities the issue names —
Observe, Prioritize, Nag, Pre-fill — over a provider-neutral,
OpenAI-compatible BYOK client. Deliberately **not** an agent runtime: there
is no tool-calling loop, no multi-step planning, no model-initiated HTTP
request beyond one bounded chat-completion call per task invocation, and
no code path anywhere by which model output reaches a material-mutation
domain command (see §7).

## 3. Data model (all plain rows — not event-sourced)

Per `.agent/RULES.md` §3's own carve-out ("Do not event-source ... other
noise unless a concrete compliance reason exists") and the precedent of
`Job`/`AuditEvent`/`Secret`: AI provider configuration and task-execution
history are operational/advisory bookkeeping, not material compliance
state. The compliance facts a human eventually acts on (a submitted
evidence description, an approved policy, a closed finding) already go
through their own event-backed domain commands unchanged by this feature.

- **`AiProviderSettings`** — single most-recently-updated row is the
  active config (same shape as `GoogleOidcSettings`): `enabled`,
  `display_name`, `base_url`, `model`, `secret_id` (FK `Secret`, the API
  key/token), `timeout_seconds`, `updated_by`/timestamps.
- **`AiTaskExecution`** — one row per Observe/Nag/Pre-fill invocation:
  `task_type`, `entity_type`/`entity_id` (nullable for program-wide
  Observe), `provider_name`, `model`, `prompt_template_version`, `status`
  (`ok`/`error`/`disabled`), `included_free_text`, `output_text`,
  `error_message`, `created_by`/`created_at`. This is the "useful
  non-secret execution metadata" the issue explicitly allows — no hidden
  chain-of-thought field exists anywhere in this table or in the
  provider-client return type.
- **`AiReminderState`** — one row per deterministic reminder key
  (`f"{category}:{link}"` from a `ReadinessItem`): `last_sent_at`,
  `send_count`, `escalation_level` (capped), `suppressed_until`. This is
  the entire "avoid uncontrolled spam" bookkeeping; it holds no free text
  and no AI output.

## 4. BYOK provider abstraction (`app/ai_provider.py`, `app/ai_provider_client.py`)

- `resolve_ai_provider_config(db, settings) -> ResolvedAiProviderConfig`
  mirrors `resolve_google_oidc_config` exactly: reads the newest
  `AiProviderSettings` row, resolves its `Secret` via `app.secrets`,
  degrades to `usable=False` (never raises) on missing config or any
  `SecretNotResolvableError`/`EncryptionNotConfiguredError`/
  `DecryptionError`. No API key is ever placed on the dataclass's `repr`
  path beyond the field itself, and no route ever serializes this
  dataclass back to a client.
- `validate_provider_base_url(base_url)` — SSRF guard run both at admin
  save time and defensively again at call time. Rejects: non-`http(s)`
  schemes, empty/missing host, credentials embedded in the URL
  (`user:pass@host`), and the well-known cloud-metadata targets
  (`169.254.169.254`, any `169.254.0.0/16` literal, `metadata.google.internal`)
  — the one class of endpoint that has no legitimate reason to ever be a
  BYOK chat-completions target and is the textbook SSRF-to-cloud-credential
  pivot. Deliberately does **not** block other private/loopback IPs:
  self-hosted BYOK (a LocalAI/Ollama/vLLM gateway on the deployment's own
  network) is the documented target deployment model, and blocking RFC1918
  ranges would break the primary self-hosted use case for no safety gain
  (the admin who configures this endpoint already has the authority to
  configure it — the risk this guard closes is a configured endpoint
  silently retargeting a cloud identity endpoint, not "admin points it at
  their own network").
- `call_chat_completion(config, *, system_prompt, user_prompt) -> ProviderCallResult`
  — one POST to `{base_url}/chat/completions` (OpenAI-compatible shape),
  bearer-auth if a key is present. `ProviderCallResult` is `ok`/`content`/
  `error`/`usage`, never raises: timeouts, connection errors, non-2xx,
  and malformed/missing `choices[0].message.content` all resolve to
  `ok=False` with a human-readable `error`, never an exception that could
  crash a caller or an admin-triggered route.

## 5. Digital TPM task layer (`app/digital_tpm.py`)

- **Observe** — `observe_program_summary` wraps
  `compute_readiness_stage` + `compute_readiness_queue` into counts by
  category; an explicit `POST /ai/tpm/observe` action (not a background
  call, not fired on every page view) additionally asks the provider for
  one contextual paragraph, built from a new `ai_egress.project_readiness_snapshot`
  projection (stage, stage reason, category counts, and the queue's own
  deterministic `reason` strings — never raw ORM rows). Persisted as one
  `AiTaskExecution(task_type="observe")`.
- **Prioritize** — deliberately **not** a separate live AI call.
  `compute_readiness_queue`'s existing deterministic sort (soonest-due
  first) already *is* the prioritization; the `/ai/tpm` console renders it
  directly under this capability's heading, matching "deterministic
  readiness conditions ... are the source of truth" without adding a
  second, redundant, cost-incurring provider call on every dashboard
  load.
- **Nag** — `generate_due_reminders(session, settings, now=...)`: for each
  current `ReadinessItem`, computes/loads its `AiReminderState`; skips if
  `suppressed_until` is in the future or `last_sent_at` is inside a fixed
  7-day cooldown (`REMINDER_COOLDOWN_DAYS`); otherwise builds a
  deterministic fallback message from the item's own fields, optionally
  asks the provider to phrase it (via `ai_egress.project_reminder_context`),
  bumps `send_count`/`escalation_level` (capped at
  `MAX_ESCALATION_LEVEL = 3`), and persists both the `AiReminderState`
  update and an `AiTaskExecution(task_type="nag")` row. Exposed only via
  an explicit, write-role-gated action (`POST /ai/tpm/nag-scan` and the
  `ai-nag-scan` CLI command) — no automatic background trigger exists in
  this slice, matching the `aws-run-checks`/cron precedent in §1.
- **Pre-fill** — `draft_prefill(session, settings, *, task_type, entity, actor)`
  dispatches on `task_type` to one of the four representative workflows
  already scaffolded by #49's `project_*` functions: evidence description
  (`evidence_description`), control-operation attestation note
  (`control_operation_note`), finding remediation response
  (`finding_response`), policy change summary (`policy_change_summary`).
  Always produces *some* draft: if the provider is usable it asks for a
  grounded draft; if not (disabled/misconfigured/failing), it falls back
  to a deterministic fact-sheet built only from the same `AiPromptContext.fields`
  (e.g. `"Evidence artifact 'X' — latest version 3, captured 2026-08-01,
  sha256 <hash>. Description: [MISSING — none recorded]"`) — so pre-fill
  is fully functional with AI absent, per the issue's hard requirement.
  Every field absent from context is rendered as an explicit
  `[MISSING — ...]` marker, never silently omitted, in both the AI and
  fallback paths — the system prompt instructs the model to do the same
  for facts it cannot find in the supplied context, but see the honesty
  note in §9 about what this can and cannot guarantee for the AI path.
  Every invocation persists one `AiTaskExecution(task_type="prefill")` row
  and is available to any authenticated write-role user directly from the
  entity's page (Evidence detail, Finding detail).

## 6. Routes and UI

- `GET/POST /admin/ai/settings` (admin-only) — same masked-secret,
  blank-keeps-existing pattern as `admin_authentication.py`'s Google/OIDC
  pages. Runs `validate_provider_base_url` server-side before saving;
  rejects with a flash error rather than persisting an invalid endpoint.
- `GET /ai/tpm` (any logged-in user, read-only) — Observe summary,
  Prioritize queue, reminder history, recent drafts. No AI call on this
  GET.
- `POST /ai/tpm/observe`, `POST /ai/tpm/nag-scan` (`require_write_access`)
  — explicit, deliberate actions only.
- `GET /ai/tpm/drafts/{id}` — view one `AiTaskExecution`'s draft text plus
  a persistent banner: "AI-generated draft. Not submitted anywhere —
  review and copy what's useful into the real form yourself." There is
  no "apply" button anywhere; a human must manually transcribe content
  into the real authorized form, which is what makes §7's guarantee
  structural rather than a UI convention.
- `POST /evidence/{id}/ai-draft-description`,
  `POST /findings/{id}/ai-draft-response` (`require_write_access`) — the
  two representative pre-fill entry points wired into existing detail
  pages, redirecting to the draft view above.

## 7. Hard authority boundary — how it is actually enforced

Not a policy statement alone — structural:

1. `app/digital_tpm.py` and `app/ai_provider_client.py` contain **no
   import and no call** of any material-mutation domain command
   (`approve_policy`/effective-version transitions, `close_finding`,
   control-effectiveness/attestation writes, evidence upload commands,
   framework-adoption changes). This is enforced by a static AST-based
   regression test (`tests/test_digital_tpm_boundary.py`, mirroring
   `tests/test_backup.py`'s `ast`-scan precedent) that fails if any of
   those symbol names is ever referenced from these modules.
2. The only two things a completed AI task can ever write are an
   `AiTaskExecution.output_text` and an `AiReminderState` bookkeeping row
   — both inert data, read by nothing except the TPM's own display
   routes. Neither model has a foreign key or trigger that feeds any
   other domain table.
3. `ProviderCallResult.content` is never passed to `session.add`/`.update`
   on any domain (non-AI) model anywhere in the codebase — verified by
   the same static scan plus an integration test that feeds the fake
   provider a deliberately adversarial payload (`"ACTION: approve_policy
   policy_id=... "` embedded in the completion text) and asserts no
   `Policy`/`Finding`/`InternalControl` row changes as a result, only a
   new inert `AiTaskExecution` row.

## 8. Security / prompt-injection / SSRF testing summary

- Untrusted free text (an evidence `description`, a finding
  `closure_note`) only ever reaches a provider through `app.ai_egress`'s
  existing opt-in + scrub path (unchanged from #49) — this feature adds
  no second, unscrubbed path to any provider.
- The system prompt for every task type explicitly frames the supplied
  JSON context as **data, not instructions**, and instructs the model to
  never claim an action was taken — but per §9, the credible guarantee is
  structural (§7), not "the model always obeys its system prompt."
- `validate_provider_base_url` is exercised by unit tests for every reject
  reason (bad scheme, missing host, embedded credentials, metadata IP
  literal, metadata hostname) and by an admin-route integration test that
  a save attempt against `http://169.254.169.254/...` is rejected and
  never persisted.
- No credential ever appears in a log line, `AiTaskExecution` row,
  `AuditEvent` detail, or exported fixture — `ProviderCallResult`,
  `AiTaskExecution`, and every route template were reviewed for this; the
  provider API key exists only inside `call_chat_completion`'s local
  scope and the `Secret`'s ciphertext.

## 9. Honesty note on what "missing facts are not invented" actually means

This system cannot force a third-party model to be truthful. What it
*can* and does guarantee, and what is actually tested:

- The prompt sent to the provider is built exclusively from
  `AiPromptContext.fields` — never a raw ORM row, never file bytes, never
  a field outside the relevant `project_*` function's reviewed allowlist.
- A fact absent from the source data is passed to the model as an
  explicit `None`/"missing" value in the context, not omitted — so the
  *input* never lets the model confuse "absent" with "not mentioned."
- The non-AI deterministic fallback path (used whenever the provider is
  disabled, misconfigured, or failing) can make this guarantee
  completely, because it only ever echoes `AiPromptContext.fields`
  verbatim with an explicit `[MISSING — ...]` marker for each `None`.
- When AI *is* enabled, a model can still hallucinate despite instruction
  — this is a known, documented limitation, not a false claim of
  detection. It is not testable by this codebase, and no test claims
  otherwise.

## 10. Test strategy by layer

- **Unit**: provider config resolution (usable/unusable/secret-decrypt-
  failure), `validate_provider_base_url` reject/accept matrix,
  `ai_egress.project_readiness_snapshot`/`project_reminder_context`
  scrubbing, reminder cooldown/escalation-cap arithmetic, deterministic
  pre-fill fallback text for every one of the four task types with a
  missing field, malformed-provider-response handling
  (`call_chat_completion` given non-JSON / missing `choices` / HTTP 500 /
  timeout via `httpx.MockTransport`).
- **Regression**: the static AST boundary scan (§7.1); a
  provider-adversarial-payload test (§7.3).
- **Integration**: one real-socket fake OpenAI-compatible server
  (small FastAPI app + uvicorn in a background thread, mirroring
  `tests/uat/harness.py`'s pattern at a much smaller scale) round-tripping
  a real `httpx` POST through `call_chat_completion`; AI-disabled path
  through the full `draft_prefill`/`generate_due_reminders` call chain
  with no `AiProviderSettings` row at all.
- **Browser/E2E**: admin settings save/reject, evidence/finding "draft
  with AI" buttons.
- **Headless UAT** (`tests/uat/test_digital_tpm.py`): an operator
  configures a fake BYOK provider, sees a readiness blocker surfaced on
  `/ai/tpm`, runs a nag scan and observes one bounded reminder recorded,
  drafts both representative pre-fill workflows and confirms the
  originating `EvidenceArtifact`/`Finding` rows are byte-for-byte
  unchanged, and a reader can view `/ai/tpm` but cannot trigger
  nag-scan/observe/draft actions (403).
- **Claude Desktop UAT**: PENDING — runbook recorded in the worklog;
  requires a running deployment + a real or self-hosted OpenAI-compatible
  endpoint, not runnable from this implementation session.

## 11. Explicitly deferred (documented, not silently dropped)

- Auditor/PBC-response and risk/control-narrative pre-fill workflows: the
  scaffolding (`draft_prefill`'s dispatch table) supports adding them, but
  only two representative workflows are wired into UI routes this slice,
  per "smallest coherent slice" and the issue's "representative set."
- Any notion of AI-initiated multi-step tool use: out of scope by design
  (§2), not a gap.
- Real scheduling/cron integration for `ai-nag-scan`: intentionally left
  to the operator's own cron, matching `aws-run-checks` precedent.
