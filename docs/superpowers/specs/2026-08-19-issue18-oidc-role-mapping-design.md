# Issue #18: OIDC claim/group-to-role mapping and authorization hardening

Parent epic: #16. Depends on #17 (merged, PR #74) — this issue only
extends the generic OIDC path (`app/oidc.py`, `app/routers/oidc.py`,
`app/oidc_config.py`); it does not touch Google's separate login flow.

## 1. Repository-reality check

- `USER_ROLES = ("admin", "operator", "reader", "auditor")`
  (`app/models.py`). `app/deps.py::require_write_access` gates
  `admin`/`operator`; `require_admin` gates `admin` only. `reader` and
  `auditor` are currently **functionally identical** — both are simply
  outside `WRITE_ROLES` — no other code distinguishes them today.
- Admins already have a real role-edit path:
  `app/routers/admin_users.py::update_user`
  (`POST /admin/users/{id}/edit`) — the only existing place a role is
  ever changed by a human today. It already enforces "at least one
  active admin must remain."
- #17's `_resolve_user` (`app/routers/oidc.py`) assigns a new SSO user's
  role via a fixed policy (first user + auto-provision -> `admin`, else
  `operator`) and never touches an existing user's role on return login.
  #18 extends this — it does not replace #17's identity-linking/no-merge
  logic.

## 2. Scope decisions

1. **One configurable source claim, not several.** The issue says "one
   or more source claims" but gives no concrete need for more than one
   in this deployment shape. `OidcProviderSettings.role_claim_name`
   (default `"groups"`) names the single claim to read. Smallest
   coherent slice; an operator whose IdP emits a differently-named claim
   just points this at it. (env fallback: `GRC_OIDC_ROLE_CLAIM_NAME`.)
2. **Claim value extraction never guesses a delimiter.** A list claim
   (`["a", "b"]`, the normal OIDC `groups` shape) is used item-by-item.
   A string claim is treated as **one** value, never split on comma/
   space — the issue explicitly warns against guessing provider-specific
   group formats. Values are capped at 1000 entries (defensive limit
   against an oversized/malicious claim array — explicitly called out
   in the issue's testing checklist).
3. **Mapping is a deployment-wide table of `claim_value -> role` rows**
   (`OidcRoleMapping`, unique on `claim_value`), not per-provider or
   per-claim-name-scoped. One admin-managed list, matching
   `GoogleOidcSettings`/`OidcProviderSettings`'s existing "single active
   config" simplicity.
4. **Role precedence when a user's claims match more than one mapped
   role**: highest-precedence role wins, using `USER_ROLES`'s existing
   order (`admin > operator > reader > auditor`) as the precedence list
   — no new hierarchy concept introduced.
5. **Unknown/unmatched claim values -> the least-privileged role
   (`reader`)**, not "leave whatever role they already had." This is a
   deliberate, active enforcement of "unknown groups/claims grant no
   additional privilege by default" — a passive "do nothing" would let a
   role granted by a mapping that later gets removed/renamed silently
   persist as if still authorized, which is exactly the escalation-via-
   staleness the issue warns against. `reader` is chosen over `auditor`
   only because they are currently equivalent and `reader` is the more
   generic name; this can change if the two roles ever diverge.
6. **Mapping is fully inert until an admin configures at least one
   `OidcRoleMapping` row.** With zero mapping rows, every #17 behavior
   is byte-for-byte unchanged (new users still get the plain default-
   provisioning role; returning users' roles are never touched). This
   is the smallest way to satisfy "avoid irreversible destructive
   synchronization unless explicitly chosen" — the feature has no effect
   at all until an admin opts in by adding a mapping.
7. **A new `User.role_source` column (`"local"` | `"oidc_mapped"`)
   is the local/externally-derived boundary the issue asks for.**
   - `"local"`: an admin's explicit decision (via `/admin/users/{id}/edit`,
     which now always stamps `role_source="local"` on every save,
     regardless of whether the value changed) or the bootstrap first-
     admin grant. **Never** touched by OIDC login.
   - `"oidc_mapped"`: owned by the SSO login flow; recomputed from
     current claims on every login once at least one mapping row
     exists. Chosen as the default for every non-bootstrap SSO-created
     user (even before any mapping row exists) so that a mapping added
     *after* a user's first login still takes effect on their *next*
     login, without a separate backfill/migration step — the recompute
     function itself is a no-op whenever the mapping table is empty
     (§6), so this default has zero observable effect until an admin
     opts in.
   - The bootstrap first-admin (first user ever, auto-provision on) is
     always `role_source="local"` **regardless of mapping
     configuration** — the issue's explicit "preserve an explicit local
     admin/break-glass path independent of OIDC group claims" is treated
     as an absolute invariant for the one account a fresh deployment
     cannot function without, not merely a fallback for the unconfigured
     case.
   - Existing rows (pre-#18 users, including every account #17 already
     created) backfill to `"local"` — nothing pre-existing becomes
     retroactively OIDC-managed by this migration.
8. **Every actual role change from mapping is audited**
   (`action="oidc_role_mapping_applied"`, detail names the old/new role
   and the matched claim values) — satisfies "retain enough audit
   history to explain why a user received or lost a mapped role." A
   recompute that lands on the *same* role as before writes nothing (no
   audit noise on every ordinary login).
9. **No claim-derived role can ever come from client-supplied request
   data.** Mapping is computed exclusively from the verified ID token's
   claims (already signature-checked in #17's `verify_identity`) — the
   callback route takes no role/claim input from the query string or
   form body at all, so there is no surface for a client to inject one.

## 3. Model

```python
class OidcRoleMapping(Base):
    """Admin-configured claim-value -> miniGRC role mapping (issue #18)
    for the generic OIDC login path. One row per distinct claim value —
    unique so there is never an ambiguous "which mapping wins" question
    for a single value."""

    __tablename__ = "oidc_role_mappings"
    __table_args__ = (
        UniqueConstraint("claim_value", name="uq_oidc_role_mapping_claim_value"),
        CheckConstraint(f"role IN {USER_ROLES}", name="ck_oidc_role_mapping_role"),
    )
    id: str
    claim_value: str
    role: str
    updated_by: str
    created_at, updated_at
```

`OidcProviderSettings` gains `role_claim_name: str = "groups"`.
`User` gains `role_source: str = "local"` with
`CheckConstraint("role_source IN ('local', 'oidc_mapped')")`.

## 4. Logic (`app/oidc_role_mapping.py`, new)

```python
LEAST_PRIVILEGED_ROLE = "reader"
ROLE_PRECEDENCE = USER_ROLES  # ("admin", "operator", "reader", "auditor")
_MAX_CLAIM_VALUES = 1000


def extract_claim_values(claims: dict, claim_name: str) -> list[str]: ...
def compute_mapped_role(claim_values: list[str], mapping_by_value: dict[str, str]) -> str: ...
def load_role_mappings(session) -> dict[str, str]: ...
def apply_role_mapping(session, user, claims, *, role_claim_name, actor) -> None:
    """No-op if no mapping rows exist, or if user.role_source == 'local'.
    Otherwise recomputes and, only on an actual change, updates
    user.role, stamps role_source='oidc_mapped', and writes an audit
    event."""
```

`app/oidc.py::OidcIdentity` gains a `claims: dict` field (the full
verified claim set) so the router can read the configured role claim
without a second JWT decode — provider-neutral module stays agnostic of
what any particular claim name means; only the router/role-mapping
module cares.

`app/routers/oidc.py::_resolve_user`'s creation branch: bootstrap first
user stays `("admin", "local")` unconditionally; every other new user
starts `("operator", "oidc_mapped")` — then `apply_role_mapping` runs
immediately after resolving *any* user (new or returning), correcting
the initial value when mapping is configured and no-op'ing otherwise.

## 5. Admin UI

- `/admin/authentication/oidc` form gains one field: **Role/group claim
  name** (default `groups`).
- `/admin/authentication/oidc/roles` (new) — list existing mappings +
  add form (claim value, role select) + per-row delete. Duplicate
  `claim_value` rejected with a clear flash (unique constraint).
- `/admin/users/{id}/edit` — shows the user's current `role_source` for
  context (read-only text) before an admin overrides it. Any save always
  stamps `role_source="local"`.
- `/admin/users` list — `role_source` added as a read-only register-grid
  column, matching the issue's "explicit, reviewable" requirement.

## 6. Security review (adversarial, at design time)

- **Escalation via unmapped claim**: impossible — unmapped values never
  match, `compute_mapped_role` floors to `reader`.
- **Escalation via stale mapping removal**: covered — removing a
  mapping row demotes on the *next* login of any affected `oidc_mapped`
  user (recomputed every time, not cached).
- **Self-lockout / zero-admin state**: unaffected — `admin_users.py`'s
  existing "at least one active admin must remain" check is untouched
  and still the only path that can demote the last admin; OIDC role
  sync only ever touches `oidc_mapped` users, and the bootstrap admin is
  always `local`.
- **Client-supplied role injection**: impossible — role is computed only
  from the already-signature-verified ID token claims (§2.9).
- **Cross-user mapping bleed**: impossible — `apply_role_mapping` only
  ever mutates the single `user` object the login just resolved.
- **Audit-data leakage**: the audit detail includes matched claim
  *values* (group names), which are not secrets — no token/credential
  material is ever included.

## 7. Test strategy

- **Unit** (`tests/test_oidc_role_mapping.py`): claim extraction (list/
  string/missing/oversized-capped/non-string items); role-precedence
  resolution across multiple matches; least-privilege floor on no match;
  `apply_role_mapping` no-ops when unconfigured or `role_source=="local"`,
  recomputes and audits only on an actual change, is idempotent (same
  claims twice -> one audit event).
- **Route tests** (extending `tests/test_oidc_routes.py` /
  `tests/test_oidc_db_config.py`): bootstrap first user stays
  admin/local regardless of mapping config; a second new user gets the
  mapped role at creation when mapping is configured; a second new user
  gets the plain #17 default (operator/local) when mapping is NOT
  configured — an explicit regression proof that #17's behavior is
  unchanged when #18 is unused; a returning user's role is promoted and
  demoted across two logins with different group claims; an admin-pinned
  (`role_source="local"`) user is never touched by a subsequent OIDC
  login even when mapping would compute something different; unmapped
  group -> `reader`, not escalation; role cannot be injected via
  query/body params on the callback.
- **Admin route tests**: mapping CRUD (`require_admin`, duplicate
  claim-value rejection, role-claim-name save); `admin_users.py`'s
  update route stamps `role_source="local"`.
- **Migration**: existing rows backfill `role_source="local"`; clean
  `alembic upgrade head` + `alembic check` (no drift).
- **Headless UAT (required)**: admin configures a mapping and the role
  claim name -> a new SSO employee whose asserted group matches gets the
  mapped role -> the same employee's next login with a different group
  is re-mapped (promotion and demotion) -> an admin manually overrides
  the role via the real Users edit form -> a subsequent SSO login with
  yet another group no longer overrides the admin's pinned role.

## 8. Known deferred/untested paths

- No way to "un-pin" a `role_source="local"` user back to OIDC-managed
  short of an admin re-editing their role through the same form (which
  re-pins it) — no explicit "resume automatic mapping" action. Small,
  additive if a future issue asks for it.
- `reader`/`auditor` remain functionally identical outside this feature
  (not something #18 is responsible for changing).
- No claim-name auto-discovery from provider metadata — `role_claim_name`
  is always admin-configured.
