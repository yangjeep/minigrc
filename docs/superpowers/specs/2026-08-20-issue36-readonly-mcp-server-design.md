# Issue #36: read-only MCP server for external agents

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature/architecture

## Goal

Expose a stable, read-only subset of miniGRC's compliance program state to
customer-owned agents over the Model Context Protocol, without granting
those agents any authority to mutate compliance state. See issue #36 for
the full product boundary and acceptance criteria.

## Primary-source verification done before designing

- Fetched `modelcontextprotocol.io/specification/2025-06-18/basic/authorization`
  directly: "Authorization is OPTIONAL for MCP implementations." Even where
  supported, HTTP-transport implementations only "SHOULD" (not "MUST")
  conform to the OAuth 2.1-based flow. This licenses a plain bearer-token
  scheme as spec-compliant, matching the issue's own explicit "API-key auth
  may ship first" permission — no stop condition here.
- Installed both `mcp==2.0.0` (latest PyPI, a major breaking rewrite that
  renames `FastMCP` to `MCPServer` under a new module with no compat alias,
  tracking a 2026-07-28 spec RC) and `mcp==1.29.0` into an isolated scratch
  venv and inspected both directly with `inspect.signature`/`dir()` rather
  than trusting WebFetch's doc-page summaries (which gave contradictory
  answers about whether `FastMCP` exists at all).
- **Decision: depend on `mcp>=1.29.0,<2.0.0`.** `1.29.0` is the last
  release of the mature, widely-deployed `FastMCP` line with a stable,
  documented `token_verifier` constructor parameter designed exactly for
  custom bearer-token verification against an app's own store. `2.0.0` was
  published only days before this session and has unproven real-world MCP
  client compatibility. Revisit once `2.x` has matured and real clients
  have adopted it.
- Inspected `FastMCP.streamable_http_app()`'s actual source (not docs) to
  confirm the auth wiring: passing `token_verifier` alone is not enough —
  `AuthenticationMiddleware`/`BearerAuthBackend` (which parses the
  `Authorization` header at all) is only installed when `auth=AuthSettings(...)`
  is also set. With `auth_server_provider` left `None`, no `/authorize`,
  `/token`, or `/register` routes are ever created — this repo implements
  only the MCP **resource-server** half (verify a bearer token against our
  own store), never a bespoke **authorization server**, directly satisfying
  the issue's "do not invent a custom auth protocol" requirement.
  `AuthSettings.resource_server_url` is left `None` too, so the
  `/.well-known/oauth-protected-resource` discovery route is not created —
  clients configure the API key as a plain `Authorization: Bearer <token>`
  header, which every MCP client that supports custom headers (Claude
  Desktop's `mcpServers` config, etc.) already allows.

## Authentication design

New `ApiToken` model (`app/models.py`), independent of `UserSession`:
plaintext tokens are never stored, only a SHA-256 hash (reusing
`app.security.hash_session_token` — the same hashing convention as browser
session tokens, not a new mechanism). Fields: `name`, `token_hash` (unique),
`scope` (fixed `"read"` for MVP — a single value, not a list column, since
no second scope exists yet to justify one; add a real scopes table only
when a second scope is a real requirement), `created_by_user_id`,
`created_at`, `last_used_at`, `expires_at` (nullable — no expiry by
default), `revoked_at` (nullable).

Token string shape: `mgrc_<32 random urlsafe bytes>` — the `mgrc_` prefix
is a well-known trend for API keys (Stripe, GitHub, etc.) that helps both
human recognition and automated secret scanners (this repo's own CI runs
GitGuardian) flag an accidentally-committed token.

`app/api_tokens.py`:
- `create_api_token(session, *, name, actor) -> (ApiToken, plaintext)` —
  the only place the plaintext ever exists; callers must display it
  immediately and never persist it themselves.
- `revoke_api_token(session, token, *, actor)` — idempotent, sets
  `revoked_at`, audit-logged.
- `verify_api_token(session, raw_token) -> ApiToken | None` — hashes the
  input, looks up by `token_hash`, rejects `revoked_at is not None` or an
  expired `expires_at`, and updates `last_used_at` on success. Never raises
  on a garbage/unknown token — returns `None`, matching
  `app.security.verify_password`'s fail-closed shape.

Admin UI (`app/routers/admin_api_tokens.py`, admin-only): list tokens
(name/created/last-used/revoked, never the hash or plaintext), a create
form, and a one-time reveal. The reveal is a direct `TemplateResponse` on
the POST itself — **never** a redirect-with-flash, because
`app/flash.py` carries its message in the redirect query string, which
would put the plaintext token in browser history, the `Referer` header of
whatever page loads next, and often a reverse-proxy access log. Revoking a
token is a POST with CSRF, same pattern as the rest of the admin UI.

## MCP server design

`app/mcp_server.py` builds a `mcp.server.fastmcp.FastMCP` instance:

```python
FastMCP(
    name="minigrc",
    token_verifier=ApiTokenVerifier(session_factory),
    auth=AuthSettings(issuer_url=..., resource_server_url=None, required_scopes=["read"]),
    stateless_http=True,
)
```

`stateless_http=True` because every tool call here is a stateless read —
no reason to hold a persistent MCP session's server-side state across
calls.

`ApiTokenVerifier.verify_token(token)` opens its own session from
`session_factory`, calls `verify_api_token`, commits (to persist
`last_used_at`), and returns an `mcp.server.auth.provider.AccessToken`
with `scopes=["read"]`, `subject=api_token.id` on success, `None`
otherwise — `RequireAuthMiddleware` then enforces `required_scopes=["read"]`
against exactly that, so an invalid/revoked/expired token is rejected by
the MCP transport itself (401/403) before any tool code runs. This is the
issue's "read-only must be enforced server-side, not by client convention"
requirement satisfied structurally: **there is no write tool registered at
all** — not a permission check inside a write path, because no such path
exists in this module. `app/mcp_server.py` never imports any
`append_and_project`/lifecycle-command function from any domain module.

### Deployment shape: a second, optional ASGI app/process

The MCP server is **not** mounted inside the main `app.main:app` FastAPI
instance. Reasons:
- `FastMCP.streamable_http_app()` returns its own `Starlette` app whose
  lifespan (`self.session_manager.run()`) must actually execute for the
  streamable-HTTP transport session manager to work; nesting that
  correctly under FastAPI's own lifespan requires extra plumbing
  (`contextlib.AsyncExitStack` combining both lifespans) for a feature most
  self-hosted operators will leave disabled.
  Running it as its own small ASGI app avoids that fragility entirely and
  keeps the main app's `create_app()` untouched.
- This still respects "one deployment = one organization" — it is one
  additional optional process serving the *same* database, not a second
  tenant or a second copy of the domain. An operator who does not want the
  MCP surface simply never starts it; the core product has zero dependency
  on it (RULES.md §8's "core must remain functional with connectors
  disabled" spirit, applied to this interoperability surface too).
- Matches this repo's existing precedent of CLI-launched auxiliary
  processes (`python -m app.cli uat`, the worker), rather than growing
  `app.main.create_app`'s responsibility.

`python -m app.cli mcp-server [--host 127.0.0.1] [--port 8100]` builds the
engine/session_factory exactly like every other CLI command
(`build_engine` → `init_db` → `make_session_factory`), builds the FastMCP
app via `app.mcp_server.build_fastmcp(session_factory, settings)`, and runs
it with `uvicorn.run(...)`.

## Tool surface (first slice)

Read-only, coarse, typed tools over domain/read-model concepts — never raw
tables, never arbitrary SQL:

- `get_program_summary` — scope-defined flag, readiness stage + reason
  (`app.readiness.compute_readiness_stage`), and the top N (capped 20)
  readiness blockers (`app.readiness.compute_readiness_queue`).
- `get_scope` — the singleton `ComplianceScope` fields, or an explicit
  "no scope defined yet" result.
- `list_controls` / `get_control` — `InternalControl` + owner + cadence +
  effective-version lifecycle status + occurrence counts (total/overdue).
- `list_policies` / `get_policy` — `Policy` + its `effective_version`
  metadata (version number, effective_at, sha256) — never raw file bytes.
- `list_findings` — `Finding` rows, filterable by `status`/`severity`.
- `list_evidence_artifacts` — `EvidenceArtifact` + `latest_version`
  metadata (title, version number, sha256, byte_size, media_type,
  source_type, captured_at) — no download URL, no object-storage key, no
  file bytes. Downloadable-reference access is explicitly deferred (see
  below): metadata access must not silently imply raw content access.
- `get_connector_status` — `ConnectorInstance` rows restricted to
  `connector_id`, `display_name`, `enabled`, `last_test_status`,
  `last_success_at`, `last_failure_at`, `last_error_summary` — `config_json`
  and `secret_ref_json` are never serialized by this tool, structurally
  (the serializer only reads the six named fields, never the row itself).

Every list tool is capped (default 50, max 200) and accepts a `limit`
argument; every response includes a `truncated: bool` so an agent can tell
whether it saw everything (RULES.md's "no silent caps" analog from the
workflow-authoring guidance applies just as well to an MCP response).

### Explicitly deferred from this slice

- Audit-period/PBC package summary and control-test/sample detail: the
  domains exist (`app/audit_package.py`, `app/control_tests.py`) but their
  serialization needs its own scoping decision (how much of a multi-KB
  package manifest is safe/useful to hand an external agent) that the
  issue does not force resolving today. Tracked as a follow-up tool, not
  silently dropped.
- Raw evidence/policy file content retrieval: the issue requires this be
  an "explicit authorization decision," not a default. No tool in this
  slice returns file bytes or a pre-signed download URL.
- OAuth **authorization-server** issuance (letting an end user log in via
  the existing OIDC identity and get a scoped delegated token minted for
  them): the resource-server half above already satisfies "OAuth support
  only to the degree justified"; standing up `/authorize`/`/token` routes
  would require real product decisions (consent screen, refresh-token
  storage, per-client registration) out of scope for "smallest safe
  slice." Documented as roadmap, not implemented, matching the issue's own
  "OAuth... implemented or explicitly staged."
- Request-rate limiting: this slice implements only per-tool result caps
  (`DEFAULT_LIMIT`/`MAX_LIMIT`), not a request-rate limiter of any kind.
  Acceptable for a single-org, self-hosted deployment with admin-issued
  tokens; noted here explicitly rather than silently absent, per the
  issue's "abuse protections appropriate to deployment model" language —
  a real limiter is a follow-up if this is ever exposed beyond a trusted
  operator's own agents.

## Security/privacy checklist against the issue's own list

- Read-only enforced structurally (no write tool exists) — not by
  client convention.
- No generic SQL/table-introspection tool.
- No secrets/tokens/credentials/session hashes ever serialized —
  `ApiToken.token_hash` itself is never returned by any tool either.
- Evidence/policy tools return metadata only, never bytes.
- Pagination/response-size bounds on every list tool (see above).
- Preserves one-deployment/one-org MVP boundary — no new tenancy concept.
- Respects existing admin-only visibility for token issuance itself
  (`require_admin`); MCP tool access is a new axis (has a valid `ApiToken`),
  not a bypass of any existing object-level rule, since every tool only
  reads objects with no per-row visibility restriction today.
- `record_audit_event` on token create/revoke; per-call MCP access is
  intentionally NOT audit-logged per call (that would multiply
  `AuditEvent` rows into a request log, which RULES.md §3's "do not
  event-source... transient... noise unless a concrete compliance reason
  exists" argues against) — `last_used_at` on `ApiToken` is the bounded,
  useful signal instead.

## Test strategy

- **Unit** (`tests/test_api_tokens.py`): create/verify/revoke, idempotent
  revoke, expired-token rejection, wrong-token rejection, `last_used_at`
  updates on success only.
- **Integration, real MCP client path** (`tests/test_mcp_server.py`): use
  `mcp.shared.memory.create_connected_server_and_client_session` to drive
  the actual `FastMCP` instance through a real in-memory `ClientSession` —
  `list_tools`, call each tool, verify no write tool is present in
  `list_tools()`'s result, verify a tool call never changes any table's row
  count (before/after `DomainEvent`/projection counts).
- **HTTP-transport auth tests**: mount `build_fastmcp(...).streamable_http_app()`
  directly in an `httpx.Client`/`ASGITransport` and prove: a request with no
  `Authorization` header is rejected (401), a revoked/expired token is
  rejected, and a valid token succeeds — proving the auth enforcement that
  the in-memory client-session path above structurally bypasses.
- **Regression**: full `pytest -q` suite.
- **SQLite/PostgreSQL**: the new `api_tokens` table and every tool query
  use plain SQLAlchemy Core/ORM `select`/`update` with no backend-specific
  SQL.
- **No headless UAT scenario** in this slice (no browser-facing route
  beyond the existing admin-UI pattern, which the admin token-management
  page below does get regular integration/E2E coverage for) — the
  MCP-client-facing surface itself is proven via the real-MCP-client
  integration test above, which exercises the identical protocol path a
  Claude Desktop UAT session would.
- **Claude Desktop UAT**: documented as PENDING in the PR (requires a
  running server + a real Claude Desktop MCP client config with the issued
  bearer token) — runbook included in the worklog.
