# Issue #37: RBAC (admin/operator/reader/auditor), OIDC role-mapping target, scoped service principals

Status: implemented, with two pieces of the issue's own scope explicitly
deferred (see §6/§7) because their real prerequisites are not merged.

## 1. Repository-reality check

- `User.role` is currently binary: `USER_ROLES = ("user", "admin")`
  (`app/models.py`), enforced by a `CHECK` constraint added in migration
  `8961da81a764`. 51 `require_admin` and 35 `require_login` call sites
  exist across `app/routers/*.py` and `app/registers/router.py`.
- **No generic OIDC exists.** `app/google_oidc.py`/`app/routers/google_oidc.py`
  is a bespoke Google-only login flow, not the provider-neutral #16-#20
  epic. It hardcodes new-account role assignment
  (`role = "admin" if (is_first_user and auto_provision_enabled) else "user"`,
  `app/routers/google_oidc.py:134`) — there is no claim/group mapping
  configuration to target at all. #16-#20 is explicitly deferred in the
  current roadmap ("do not start Phase-5 integrations merely because
  they are open"), so #37's own ask to "design together with generic
  OIDC" and "update generic OIDC group/claim mapping" cannot be
  implemented against code that does not exist yet.
- **No object/audit scoping exists.** #30 (compliance scoping/program
  bootstrap) has not landed. The issue's own auditor section explicitly
  allows for this: "If object/period scoping for auditors is too large
  for the first slice, define the coarse role now and stage finer-
  grained audit-scope filtering explicitly."
- **No service-principal consumer exists.** #36 (read-only MCP) and the
  connector platform (#23-#28) — the concrete use cases for scoped
  non-human credentials — are both explicitly deferred in the current
  roadmap. Building a full token-issuance/verification system with zero
  callers would be exactly the "speculative infrastructure... without...
  at least one concrete use validating it" `.agent/README.md` warns
  against.
- **Object ownership is already separate from roles.** `InternalControl.owner_person_id`
  (issue #11) is a `Person` FK, entirely independent of `User.role`. No
  change needed there — already satisfies "object assignments are not
  encoded as global roles."
- **The register-grid JSON API (`app/registers/router.py`) has no
  write/read distinction below "admin."** `_check_permission` only ever
  raises for actions listed in a register's `require_admin_for`
  (`admin_users`, `admin_jobs`, `connections`, `trust_center` sections).
  Two registers — `controls` (`app/routers/controls.py`) and
  `framework-requirements` (`app/routers/frameworks.py`) — have no
  `require_admin_for` at all, meaning `create`/`edit`/`delete` on them
  today only require `require_login`, i.e. any active user regardless of
  role.
- **Every plain-route mutation not already `require_admin` is currently
  only `require_login`-gated.** Inventoried directly (not from the issue
  text): `controls.py` (`set_control_owner`, `generate_control_occurrences`,
  `create_control_occurrence`, `add_mapping`), `occurrences.py` (`perform`),
  `evidence.py` (`map_requirement`, `map_control`), `people.py`
  (`create_person`, `update_person`), `policies.py` (`create_policy`,
  `update_policy`, `upload_policy_version`, `retire_policy`), `risks.py`
  (`create_risk`, `import_risks`), `frameworks.py` (`create_framework`,
  `update_framework`, `create_requirement`, `import_requirements`,
  `update_assessment`, `add_note`), `vendor_systems.py` (create vendor,
  edit vendor, `create_roster_snapshot`). This is the actual, concrete
  work #37's "reader/auditor cannot mutate material state" acceptance
  criterion requires — not object-level scoping, which needs #30 first.

## 2. Role model

```
USER_ROLES = ("admin", "operator", "reader", "auditor")
```

- **admin** — everything `operator` can do, plus every currently
  `require_admin`-gated route (program/connector/auth/user/config
  administration). Unchanged from today's `admin`.
- **operator** — every currently `require_login`-gated mutation route
  that is not already `require_admin`. This is today's plain `user`
  role, renamed and migrated 1:1 — no behavior change for existing
  accounts beyond the name.
- **reader** — every currently `require_login`-gated *read* route,
  minus every mutation. Cannot perform any state-changing action.
  Dashboard's existing audit-activity admin-only filtering
  (`app/routers/dashboard.py`, from #12's UAT finding) already excludes
  non-admins from that one admin-flavored widget; nothing else in the
  read surface exposes secrets/config today, confirmed by grep (no
  connector credentials, Google OAuth client secrets, or encryption
  keys are rendered in any `require_login`-only template).
- **auditor** — identical server-side read access to **reader** in this
  slice (see §1 — no scoping infrastructure exists yet to differentiate
  "audit-relevant" from general internal visibility), also cannot
  mutate. The distinction the issue draws (audit-scoped historical
  versions/evidence/testing vs. general org visibility) is a real
  product requirement but has no object/period-scoping mechanism to
  express it against yet (#30). Defining the coarse role now, per the
  issue's own explicit allowance, and staging the scoping filter as
  future work once #30 exists is the correct sequencing — building
  fake/partial scoping now would be worse than an honest coarse role.

## 3. Authorization mechanism

`app/deps.py` gains one new dependency, `require_write_access`, sitting
between `require_login` and `require_admin` in strictness:

```python
WRITE_ROLES = frozenset({"admin", "operator"})


def require_write_access(user: User = Depends(require_login)) -> User:
    if user.role not in WRITE_ROLES:
        raise HTTPException(status_code=403, detail="...")
    return user
```

Every plain-route mutation inventoried in §1 that is not already
`require_admin` switches its dependency from `require_login` to
`require_write_access`. `require_admin`-gated routes are untouched —
admin already implies write access, and RULES.md's "smallest coherent
change" argues against re-deriving those from a lower-privilege
primitive.

`app/registers/router.py::_check_permission` gains a second,
unconditional check: `create`/`edit`/`delete` (the write actions;
`bulk_update` already reuses the `"edit"` action string) require
`user.role in WRITE_ROLES` *in addition to* the existing
`require_admin_for` override, which stays a strictly stronger
requirement for the four registers that already declare it. This closes
the `controls`/`framework-requirements` gap identified in §1 and is a
general safeguard for any future register that doesn't declare
`require_admin_for` explicitly.

`require_login` itself is unchanged — "any active, authenticated user"
already correctly includes all four roles for read access; that is
exactly what reader/auditor need.

## 4. Migration

New Alembic migration:

1. `batch_alter_table("users")`: drop `ck_user_role` (SQLite requires
   batch mode to touch a CHECK constraint — same pattern as
   `8961da81a764`).
2. `op.execute("UPDATE users SET role = 'operator' WHERE role = 'user'")`
   — deterministic, unconditional, additive-only rename; existing
   `admin` rows are untouched. Run between drop and re-create because
   the *old* constraint (`role IN ('user','admin')`) would reject
   `'operator'` if the update ran before the drop.
3. `batch_alter_table("users")`: re-create `ck_user_role` as
   `role IN ('admin','operator','reader','auditor')`.

No existing account gains privilege it didn't already have (`user` →
`operator` is the same tier, `admin` → `admin` unchanged); no existing
account loses access it needs (operator retains every write path `user`
already had). Downgrade path defined symmetrically (see the migration
file) for completeness, though downgrading would need to decide what to
do with any `reader`/`auditor` rows created after upgrade — documented
as a data-loss-risk warning in the migration's own downgrade docstring,
matching this repo's existing convention of never silently inventing
history.

## 5. Google OIDC provisioning (the one real OIDC entrypoint today)

`app/routers/google_oidc.py:134`'s hardcoded
`role = "admin" if (is_first_user and auto_provision_enabled) else "user"`
becomes `... else "operator"` — the direct, mechanical rename keeping
identical behavior (first auto-provisioned user is still `admin`;
every subsequent Google sign-in still gets full write access, now named
`operator`). This is the full extent of "OIDC mapping" work this slice
can honestly claim, given §1's finding that no configurable claim/group
mapping exists to update.

## 6. Deferred: configurable OIDC claim/group → role mapping

Out of scope until #16-#20 (generic OIDC) actually lands — there is no
claims-mapping configuration surface to wire the new roles into. When
that epic starts, it should target `USER_ROLES` from #37 directly
(least-privilege fallback for unmapped claims, no silent escalation from
arbitrary IdP group names, issuer+subject as external identity
authority) — the role vocabulary this issue establishes is exactly the
contract that work needs; nothing here blocks it.

## 7. Deferred: scoped service/API principal model

Out of scope for the same reason: #36 (read-only MCP) and the connector
platform (#23-#28) are the concrete consumers this concept needs, and
both are explicitly deferred in the current roadmap. Building a
`ServicePrincipal`/scope table with zero callers now would be exactly
the kind of speculative infrastructure `.agent/README.md` and
`.agent/RULES.md` warn against.

What this issue *does* commit to now, architecturally: `USER_ROLES`
stays a closed set of the four human roles above — no `"service"` or
`"api"` value is ever added to it. When #36 or a connector needs
non-human access, it must get a separate principal/credential concept
with its own explicit scopes (`grc:read`, `audit:read`,
`evidence:metadata:read`, or whatever that issue's own grounding in #36
settles on), never a `User` row and never inheritance from whichever
admin happened to create it. This is a one-line invariant to hold, not
a system to build today.

## 8. Test strategy

- **Unit — permission matrix**: for each of the four roles, assert
  `require_write_access` allows admin/operator and rejects
  reader/auditor with 403; `require_admin` behavior unchanged.
- **Regression — every route inventoried in §1**: a reader (and
  separately an auditor) attempting each previously-`require_login`-only
  mutation now gets 403 and causes no state change; an operator
  performing the same action still succeeds exactly as `user` did
  before. This is the actual regression class #37 exists to close.
- **Register-grid regression**: reader/auditor `POST`/`PATCH`/`DELETE`
  on the `controls` and `framework-requirements` registers now 403;
  operator still succeeds.
- **Migration test**: seed rows with the old `role='user'`/`role='admin'`
  values pre-migration (SQLite + PostgreSQL), run the migration, assert
  `user` rows become `operator`, `admin` rows are untouched, and the new
  CHECK constraint rejects `'user'` again post-migration.
- **OIDC provisioning test**: first auto-provisioned Google sign-in ->
  `admin`; a subsequent one -> `operator` (was `user`).
- **Headless UAT**: a representative journey — operator performs a
  material action (e.g. records an occurrence) successfully; a reader
  attempting the same action is rejected cleanly, matching
  `.agent/LOOP.md` §9's "can an unauthorized user perform an admin/
  material action?" adversarial question, now generalized to
  "material," not just "admin."
