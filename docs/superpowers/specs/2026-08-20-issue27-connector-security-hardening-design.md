# Issue #27: Connector security hardening, permission review, verification, and compatibility lifecycle

Parent epic: #23. Depends on #24 (merged), #25 (merged). Builds on #26
(merged, registry discovery/install handoff).

## 1. Repository-reality check

Before designing controls, what actually exists today matters more than
what the issue's threat list enumerates in the abstract:

- **Every connector that exists today is first-party, in-repo Python
  code** (`app/connectors/adapters.py`: `aws_cloudtrail_iam`,
  `google_drive`, `google_workspace`), committed through the same PR
  review + `protect-main` ruleset as any other code change. There is no
  third-party or dynamically-downloaded connector today.
- **`RegistryEntry.package_location` is schema-present but never
  resolved/imported by anything** (#26 §9, confirmed unchanged in
  `app/connector_registry.py`: neither `load_registry` nor
  `to_manifest`/`filter_*` touch it). There is no code path that turns a
  `package_location` string into executed bytes.
- **Consequence**: "malicious connector *package*," "package
  substitution," and "checksum/signature verification of downloaded
  artifact bytes" are not live attack surfaces yet — there is no
  artifact-fetch step to substitute or verify. Building a real
  signature/checksum-of-downloaded-bytes pipeline now, with nothing to
  fetch or verify, would be exactly the speculative infrastructure
  RULES.md and the issue's own execution prompt ("do not build a
  complex code-signing PKI if repository/package reality does not
  justify it") warn against.
- **What *is* live today**: the git-tracked `registry/registry.json`
  file itself (any entry in it is trusted the moment it merges to
  `main`), the `ConnectorInstance`/`ConnectorExecution` lifecycle (#25),
  and the `ConnectorManifest`/`ConnectorResult` contract (#24). Real,
  addressable gaps in *these* existing surfaces are this issue's actual
  scope.

This mirrors #26 §1's own resolved pattern: implement the concrete
protections repository reality actually supports, and document the
residual/deferred ones honestly rather than building unused ceremony.

## 2. Threat model

| Threat (from issue #27) | Live today? | Treatment |
|---|---|---|
| Malicious connector code | No (no third-party/dynamic loading exists) | Documented residual risk — see §7 |
| Supply-chain/package substitution | No (no artifact fetch step exists) | Documented residual risk — see §7 |
| Tampered manifests (registry entries) | **Yes** — `registry.json` is data, not code, and a bad edit is a real risk | `load_registry`'s existing fail-closed validation, extended (§3) |
| Connector/publisher identity confusion | **Yes** — nothing today stops two entries claiming the same `connector_id` under different publishers | New check in `load_registry` (§3) |
| Capability escalation via result | **Yes**, and already enforced | `validate_result`'s existing capability-overclaim check (#24) — reverified with a fresh adversarial test (§6), not re-implemented |
| Capability escalation via upgrade | **Yes** — no upgrade path exists at all today, so an admin who reviewed v1.0.0's capabilities has no mechanism to see v1.1.0's before it takes effect | New `plan_connector_instance_upgrade`/`apply_connector_instance_upgrade` (§4) |
| Downgrade attack | **Yes** — same gap; nothing prevents silently moving an instance to an older, possibly-vulnerable manifest version, because no upgrade/version-change path exists yet to guard | Same new upgrade path, fails closed on downgrade unless explicitly acknowledged (§4) |
| Secret exfiltration via result payload | **Yes**, and already enforced | `validate_result`'s secret-shaped-key rejection (#24) — unchanged |
| SSRF/internal network access | Connector-specific (AWS/Drive/Workspace call fixed, documented provider endpoints, not operator-configurable URLs) | No new configurable-URL surface introduced by this issue; out of scope |
| Excessive provider permissions, invisible to the operator | **Yes** — `required_permissions` exists on every manifest/entry but nothing surfaces it as a reviewable, risk-flagged summary before install | New `build_permission_review` (§5) |
| Deprecated connector kept silently running | **Yes** — `filter_by_capability` hides deprecated entries from *discovery*, but an already-installed `ConnectorInstance` has no relationship back to the registry's `deprecated` flag at all | New `list_deprecated_installed_instances` (§5) |

## 3. Registry integrity hardening (`app/connector_registry.py`)

Two additions to `load_registry`'s existing fail-closed validation,
consistent with its established pattern (raise `RegistryError`, no
silent skip):

1. **Identity confusion**: if the same `connector_id` appears across
   entries (any version) with more than one distinct `publisher`,
   raise `RegistryError`. A legitimate publisher never needs to change
   identity under the same `connector_id`; two different publishers
   claiming the same id is exactly what package/identity squatting
   looks like.
2. **Malformed checksum**: if `checksum_sha256` is present, it must
   match `^[0-9a-f]{64}$` (case-insensitive input, normalized to
   lowercase) or the entry is rejected. This does not yet *verify*
   anything against real bytes (§1 — there is nothing to verify against
   today), but it fails closed on an obviously corrupt/truncated value
   now, so the field is trustworthy the day a real artifact-fetch step
   is added rather than silently accepting garbage until then.

## 4. Connector instance upgrade/rollback lifecycle (`app/connector_lifecycle.py`)

No upgrade path exists today — `ConnectorInstance.manifest_version` is
set once at install and never changed by any function. This is the
issue's one genuine new piece of runtime behavior.

```python
@dataclass(frozen=True)
class ConnectorUpgradePlan:
    connector_id: str
    from_version: str
    to_version: str
    direction: str  # "upgrade" | "downgrade" | "same"
    new_capabilities: tuple[str, ...]  # in to_manifest, not in from_manifest
    new_required_permissions: tuple[str, ...]  # in to_manifest, not in from_manifest
    # Full (not just new) capabilities/permissions of the reviewed
    # to_manifest — apply_connector_instance_upgrade verifies whatever
    # to_manifest it receives still matches these exactly (§6.1).
    to_capabilities: tuple[str, ...]
    to_required_permissions: tuple[str, ...]


def plan_connector_instance_upgrade(
    instance: ConnectorInstance, from_manifest: ConnectorManifest, to_manifest: ConnectorManifest
) -> ConnectorUpgradePlan:
    """Pure/no I/O. Raises ConnectorLifecycleError for identity confusion
    (to_manifest.connector_id != instance.connector_id) and
    ConnectorContractError for an incompatible core-contract version
    (via check_compatibility) — both always invalid, no override.
    Never raises for a downgrade: that is a valid *plan* outcome the
    caller must explicitly acknowledge via apply_connector_instance_upgrade.
    """


def apply_connector_instance_upgrade(
    session, instance, plan, to_manifest, *, actor, allow_downgrade=False
) -> ConnectorInstance:
    """Mutates instance.manifest_version = to_manifest.version.
    Raises ConnectorLifecycleError if plan.direction == "downgrade" and
    not allow_downgrade — a downgrade is never silent/automatic.
    Also raises if instance.manifest_version no longer equals
    plan.from_version (stale-plan/concurrency guard, §6.1), or if
    to_manifest's version/capabilities/permissions don't exactly match
    what the plan reviewed (no manifest substitution at apply time,
    §6.1). Records an audit event whose action is "rollback" when
    allow_downgrade was actually used for a downgrade, "upgrade"
    otherwise — so the audit trail distinguishes an intentional,
    explicit rollback from routine forward movement. The event detail
    includes from_version/to_version and any new_capabilities/
    new_required_permissions from the plan, so the exact thing an
    operator was shown before approving is preserved historically.
    """
```

Design decisions:

- **Plan/apply split** mirrors the existing `test_connector_instance`
  pattern (a safe, no-mutation preview step separate from the mutating
  one) and directly satisfies "operators can see connector
  capabilities... before activation" for upgrades, not only fresh
  installs.
- **No automatic/background upgrade.** Nothing in this issue or the
  registry polls for new versions and calls `apply_connector_instance_upgrade`
  on its own — every version change is an explicit, actor-attributed
  call, matching RULES.md's "no AI/connector output bypasses
  authorization" applied to registry-originated version changes too.
- **Rollback is a real, sanctioned operator action, not something a
  compromised registry entry can trigger.** `allow_downgrade=False` by
  default blocks a downgrade from `plan_connector_instance_upgrade`'s
  automatic direction detection; an operator who genuinely needs to
  roll back a bad upgrade passes `allow_downgrade=True` explicitly, and
  that fact is recorded distinctly in the audit trail.
- **Version comparison** reuses `manifest._SEMVER_RE`'s already-enforced
  `X.Y.Z` shape (both `ConnectorManifest.version` and
  `ConnectorInstance.manifest_version` are validated semver strings by
  the time either exists) via a small tuple-comparison helper — no new
  version-parsing dependency.

## 5. Permission review and deprecation visibility (`app/connector_registry.py`)

```python
@dataclass(frozen=True)
class PermissionReview:
    connector_id: str
    display_name: str
    verification_status: str
    deprecated: bool
    capabilities: tuple[tuple[str, str], ...]  # (capability_id, human description)
    required_permissions: tuple[str, ...]
    broad_permission_warnings: tuple[str, ...]  # subset of required_permissions flagged as high-risk
    supported_auth_methods: tuple[str, ...]


def build_permission_review(entry: RegistryEntry) -> PermissionReview: ...


def list_deprecated_installed_instances(
    session: Session, entries: list[RegistryEntry]
) -> list[ConnectorInstance]:
    """Installed instances whose (connector_id, manifest_version) matches
    a registry entry with deprecated=True. Reporting only — does not
    disable/remove anything. An operator decides whether/when to act,
    the same way #25's health fields (last_failure_at, etc.) are
    surfaced without an automatic disable."""
```

- **Broad-permission heuristic** (`_BROAD_PERMISSION_MARKERS`): a
  required-permission string is flagged if it case-insensitively
  contains `"admin"`, equals `"*"`, or contains `"root"`/`"owner"`/
  `"full_access"`/`"generatecredentialreport"`. This is an honest,
  narrow heuristic, not a claim of exhaustive risk classification —
  documented as such in the docstring, matching the registry's own
  "verification status is honestly limited" precedent from #26 §3.
- **No auto-disable on deprecation.** The issue's "deprecated connector
  warning/disable semantics" is satisfied by *warning* (a reporting
  function an operator-facing nag/readiness surface can call), not a
  silent auto-disable — auto-disabling a connector an operator depends
  on without their action would itself be a new, unreviewed behavior
  change and contradicts "core must remain functional with connectors
  disabled/unavailable" being an operator choice, not an automatic one.
- **No HTTP route/admin UI in this slice** — same accepted precedent
  #24/#25/#26 already established (backend service layer only). Both
  `build_permission_review` and `list_deprecated_installed_instances`
  return data shaped for a future review/nag UI to render; wiring that
  UI is deferred to whichever issue defines the connector platform's
  operator-facing surface (not yet filed as a separate issue — #28's
  own execution prompt only asks to "demonstrate... install/configure/
  test/capture through #24/#25," not to build that UI either).

## 6. Adversarial verification plan

- **Fake malicious connector, full lifecycle path**: a `connector_callable`
  that returns a `ConnectorResult` for a capability its manifest never
  declared, run through the real `run_connector_instance` (not just the
  isolated `validate_result` unit #24 already covers) — proving the
  protection holds at the actual execution boundary, not only in
  isolation.
- **Registry identity confusion**: two entries, same `connector_id`,
  different `publisher` → `RegistryError`.
- **Malformed checksum**: a non-hex / wrong-length `checksum_sha256` →
  `RegistryError`; a well-formed one loads normally.
- **Upgrade/downgrade**: same-connector upgrade with new capabilities
  succeeds and the plan surfaces them; downgrade rejected without
  `allow_downgrade`; downgrade with `allow_downgrade=True` succeeds and
  audits as `"rollback"`; cross-connector-id "upgrade" rejected as
  identity confusion; incompatible-core-version "upgrade" rejected.
- **Permission review**: a manifest with an `"admin.directory.user.readonly"`-
  style broad permission is flagged; a narrow one (`"drive.readonly"`)
  is not.
- **Deprecation visibility**: an installed instance whose backing entry
  is later marked `deprecated=True` in the registry is returned by
  `list_deprecated_installed_instances`; a non-deprecated one is not.

### 6.1 Findings from self-review, fixed before shipping

Adversarially reviewing the plan/apply split itself (not just the
individually-listed scenarios above) surfaced two real gaps, both fixed
and both now regression-tested:

1. **Stale-plan/concurrency gap.** `apply_connector_instance_upgrade`
   originally never checked that `instance.manifest_version` still
   equalled `plan.from_version` before mutating. A plan computed
   against an old starting version, applied after someone else already
   moved the instance to a newer version, would silently move it
   *backward* from that newer version — while `plan.direction` still
   read `"upgrade"`, because it was computed against the stale starting
   point, not the instance's current state. Fixed with the same
   optimistic-concurrency shape `app.registers`'s PATCH API already
   uses via `expected_updated_at`: raise if the instance has moved on
   since the plan was built. Regression:
   `test_apply_rejects_stale_plan_after_concurrent_version_change`.
2. **Manifest-substitution gap.** Nothing verified that the
   `to_manifest` passed to `apply_connector_instance_upgrade` was the
   *same* manifest `plan_connector_instance_upgrade` had actually
   checked compatibility/capabilities/permissions against — a caller
   could apply with a manifest carrying extra capabilities the plan
   never surfaced. Checking `to_manifest.version == plan.to_version`
   alone would not have closed this: two manifest objects can share a
   version string while differing in capabilities. Fixed by capturing
   the full reviewed `to_capabilities`/`to_required_permissions` on the
   plan and verifying `to_manifest` matches all three fields exactly
   before applying. Regression:
   `test_apply_rejects_substituted_manifest_not_matching_reviewed_plan`.

## 7. Residual risk (documented, not hidden)

- **No cryptographic verification of connector code exists.** Every
  connector today is first-party source in this repository; its
  integrity rests on git history, PR review, and the `protect-main`
  ruleset (1 approval required) — not on an independent signature
  chain. This is the same trust boundary every other module in this
  codebase already has, made explicit here rather than implied.
- **No sandboxing.** A connector callable runs in-process with the same
  privileges as the rest of the app. `verification_status` (`official`/
  `community`/`unofficial`) records what the registry *asserts*, never
  a sandboxing or code-safety guarantee (#26 §3, unchanged).
- **No third-party/dynamic package loading exists yet**, so package
  substitution/tampered-artifact-bytes verification has no real target
  to check against. The `checksum_sha256` field's shape is now
  enforced (§3), and the compatibility/identity checks in this issue
  are written to keep working unchanged the day a real dynamic-import
  step is added — but that step itself remains future work, not
  something this issue simulates against nothing.
- **The broad-permission heuristic is not exhaustive.** It flags a
  narrow, documented set of known-risky patterns; a genuinely dangerous
  but differently-named permission string would not be flagged. This is
  disclosed in the function's own docstring, not silently assumed
  complete.

## 8. Definition of done

- Registry entries with identity confusion or a malformed checksum
  fail closed at load time.
- An installed connector instance can be safely upgraded, with new
  capabilities/permissions surfaced before the change takes effect, and
  protected against silent/automatic downgrade.
- An explicit, audited rollback path exists for a genuine bad upgrade.
- Operators (or a future UI built on this data) can see a connector's
  capabilities, required permissions, and broad-permission warnings
  before installing it.
- Already-installed instances whose registry entry has since been
  deprecated are discoverable, without any silent auto-disable.
- Capability escalation via a connector's returned result is
  reverified at the real execution boundary, not only in isolation.
- Residual trust in connector code is documented, not hidden.
