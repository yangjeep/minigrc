# Issue #36: read-only MCP server for external agents

**Date:** 2026-08-20
**Author:** Claude (agent)
**Type:** feature

## Summary

Adds a stable, read-only [MCP](https://modelcontextprotocol.io) surface
over a small, representative slice of miniGRC's compliance domains —
readiness, scope, controls, policies, findings, evidence metadata, and
connector health — authenticated by an admin-issued API token, with no
write/mutation tool of any kind. See
`docs/superpowers/specs/2026-08-20-issue36-readonly-mcp-server-design.md`
for the full design, including the primary-source MCP-spec/SDK research
that shaped the auth approach.

## What changed

- `pyproject.toml`: added `mcp>=1.29.0,<2.0.0` (pinned below the brand-new
  `2.0.0` rewrite — see design doc for why).
- `app/models.py`: new `ApiToken` model + `API_TOKEN_SCOPES = ("read",)`.
- `migrations/versions/544f2ba57e12_...py`: additive `api_tokens` table.
  **Found and fixed a real migration bug before shipping it**: the
  model's first draft used `CheckConstraint(f"scope IN {API_TOKEN_SCOPES}")`
  — for a single-element tuple this renders as `scope IN ('read',)`, a
  trailing comma that is invalid `IN (...)` SQL on both SQLite and
  PostgreSQL. Found by actually running the generated migration against a
  real SQLite file (this session's standing migration-testing habit),
  not by inspection alone. Fixed with an explicit join that is correct
  for any tuple length.
- `app/api_tokens.py` (new): `create_api_token`/`revoke_api_token`/
  `verify_api_token`, reusing `app.security.hash_session_token`'s hashing
  convention rather than inventing a second one.
- `app/mcp_server.py` (new): `build_fastmcp(session_factory, settings)` —
  a `FastMCP` instance with `token_verifier`/`AuthSettings` wired for
  resource-server-only bearer-token verification (no authorization-server
  routes are ever registered), and nine read-only tools:
  `get_program_summary`, `get_scope`, `list_controls`/`get_control`,
  `list_policies`/`get_policy`, `list_findings`,
  `list_evidence_artifacts`, `get_connector_status`. Every list tool is
  capped (default 50, max 200) and reports `truncated`.
- `app/routers/admin_api_tokens.py` + `app/templates/admin/api_tokens/*`
  (new): admin-only token issuance/revocation UI. The one-time plaintext
  reveal is rendered directly on the creation POST's response — never via
  `redirect_with_flash`, which would put the token in the redirect query
  string (browser history/`Referer`/proxy access logs).
- `app/cli.py`: new `python -m app.cli mcp-server [--host] [--port]`
  subcommand — the MCP server runs as its own small ASGI process sharing
  the same database, not mounted inside `app.main.create_app` (see design
  doc for why).
- `app/main.py`: registered the admin router and nav item.

## Primary-source verification before implementing

- `modelcontextprotocol.io/specification/2025-06-18/basic/authorization`:
  "Authorization is OPTIONAL for MCP implementations" — licenses the
  plain bearer-token approach as spec-compliant, matching the issue's own
  "API-key auth may ship first" permission.
- Installed both `mcp==2.0.0` and `mcp==1.29.0` into an isolated scratch
  venv and inspected both directly (`inspect.signature`/`dir()`) rather
  than trusting WebFetch's doc-page summaries, which gave contradictory
  answers about whether `FastMCP` still exists. Chose `1.29.0`'s mature,
  documented `FastMCP`/`TokenVerifier` API over `2.0.0`'s week-old
  `MCPServer` rewrite.
- Inspected `FastMCP.streamable_http_app()`'s actual source to confirm:
  with `auth_server_provider` left unset, no `/authorize`/`/token`/
  `/register` route is ever created — this implementation is the MCP
  **resource-server** half only, never a bespoke **authorization
  server**, directly satisfying the issue's "do not invent a custom auth
  protocol."

## Test strategy and results

- **Unit** (`tests/test_api_tokens.py`, 8 tests): create/verify/revoke,
  idempotent revoke, expired/revoked/unknown-token rejection, plaintext
  never appears in the audit-event detail.
- **Integration, real MCP client** (`tests/test_mcp_server.py`, 10
  tests): drives the actual `FastMCP` instance through a real
  `mcp.shared.memory.create_connected_server_and_client_session` client —
  `list_tools` contains no write-shaped tool name, each domain tool
  round-trips real seeded data, connector secrets/config are structurally
  absent from `get_connector_status`'s output, and a full call sweep
  across every tool changes `DomainEvent`'s row count by exactly zero.
- **HTTP-transport auth** (`tests/test_mcp_server_http_auth.py`, 5
  tests): mounts the real `streamable_http_app()` and proves, over actual
  HTTP, that missing/garbage/revoked/expired tokens all get 401 before
  any MCP protocol code runs, and a valid token passes the gate — this is
  the path the in-memory client test above structurally bypasses.
- **Browser/E2E** (`tests/test_admin_api_tokens.py`, 7 tests): admin-only
  access, full create → one-time-reveal → list-never-shows-it-again →
  revoke flow, blank-name rejection, wrong-CSRF rejection, 404 on
  revoking an unknown token.
- **Headless UAT** (`tests/uat/test_api_tokens.py`, 2 scenarios, PASS):
  `admin issues → views plaintext once → confirms it never reappears →
  revokes`, and `a non-admin cannot reach the admin token-management
  surface at all`. Both run against the real app over a real socket
  (`GRC_UAT_MODE=1 pytest -m uat tests/uat/test_api_tokens.py`).
- **Regression**: full `pytest -q` — see PR for the final count; ran
  clean at 8+10+5+7 = 30 new tests added on top of the existing suite
  with zero new failures.
- **Lint/format**: `ruff check .` / `ruff format --check .` clean.
- **SQLite/PostgreSQL**: the migration was manually applied and
  round-tripped (upgrade → downgrade → upgrade, twice) against a real
  SQLite file, with the `CHECK` constraint verified to actually reject a
  non-`"read"` scope value. No local PostgreSQL was available in this
  environment (same limitation as every prior issue this session) — CI's
  `test-postgres` job is the parity check of record; the model/migration
  use only standard SQLAlchemy Core constructs with no SQLite-specific
  syntax.
- **Claude Desktop UAT: PENDING.** Requires a running deployment plus a
  real Claude Desktop MCP client configuration pointed at
  `python -m app.cli mcp-server`'s endpoint with the issued bearer token
  as an `Authorization` header — not runnable from this implementation
  session. Runbook below.

## Claude Desktop UAT runbook (PENDING — not yet executed)

1. Build/commit: this PR's branch/head commit.
2. Environment: SQLite (default), a running `uvicorn app.main:app` plus
   `python -m app.cli mcp-server --port 8100` against the same
   `GRC_DATA_DIR`/`DATABASE_URL`.
3. Prerequisites: an admin user; at least one control, one policy, and
   one finding seeded (the existing seed data or onboarding flow is
   sufficient) so the read tools have something to return.
4. Persona: an admin issues the token (`/admin/api-tokens/new`); a
   separate operator configures Claude Desktop's MCP client config with
   `"url": "http://<host>:8100/mcp"` and
   `"headers": {"Authorization": "Bearer <issued token>"}`.
5. Scenario: ask Claude "what's blocking SOC 2 readiness right now?" and
   confirm it calls `get_program_summary`/`list_findings` and grounds its
   answer in the actual returned blockers, not invented ones.
6. Negative case: ask Claude to "close finding X" or "approve policy Y" —
   confirm no tool exists to do so and Claude reports it cannot perform
   the action.
7. Persisted-result check: after the session, confirm no new
   `DomainEvent`/`AuditEvent` row was created by any of the read
   requests (only `ApiToken.last_used_at` should have moved).
8. Revoke the token from `/admin/api-tokens` and confirm Claude's next
   tool call fails with an auth error immediately.

## Known deferred/untested paths

- Audit-period/PBC package summary and control-test/sample detail tools:
  the domains exist but their own scoping/redaction decision is deferred
  to a follow-up, not silently dropped (see design doc).
- Raw evidence/policy file byte retrieval: no tool returns file bytes or
  a download URL in this slice, per the issue's explicit "explicit
  authorization decision" requirement.
- OAuth **authorization-server** issuance (an end user minting a scoped
  delegated token via the existing OIDC login) is documented as roadmap,
  not implemented — the resource-server half already satisfies "OAuth
  support only to the degree justified."
- Claude Desktop UAT itself is PENDING, per above — this PR is not
  release-complete until it runs.
