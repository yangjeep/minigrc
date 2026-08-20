# Issue #26: Versioned connector registry and official connector repository structure

Parent epic: #23. Depends on #24 (merged). Builds on #25 (merged) for
the install handoff.

## 1. Repository-reality check and a stop-condition-adjacent decision

The issue's own "Official connector repository" section explicitly
offers a sanctioned fallback: *"Establish a separate official
repository, preferably `yangjeep/minigrc-connectors`, **or document a
repository-grounded alternative if current project constraints make a
separate repo premature**."*

Creating a new external GitHub repository is exactly the kind of
hard-to-reverse action affecting shared account/organization state that
this agent's own operating rules require explicit user authorization
for — it is not something to decide autonomously mid-issue. Since the
issue itself pre-authorizes the alternative, the resolved choice is:
**keep the registry inside this repository** (`registry/registry.json`,
version-controlled and auditable by construction — literally the
structure the issue itself suggests) and **document, rather than
create,** the future external-repository option. This is not a
silent scope reduction: it is the issue's own explicitly offered
fallback, applied because the actual repo-creation decision belongs to
the user, not to an autonomous implementation loop.

`app/connectors/adapters.py` (#24) already has real reference manifests
(`aws_manifest()`, `google_drive_manifest()`,
`workspace_directory_manifest()`) and `app/connector_lifecycle.py`
(#25) already has `install_connector_instance` — this issue's job is
the discovery/browse layer in front of them, not new provider code.

## 2. Scope decisions

1. **Registry entries are plain, JSON-serializable data — never a
   Python import path resolved during browsing.** `ConnectorManifest`
   (#24) is a Python dataclass built by calling a function today;
   importing a module just to *read* its manifest during discovery
   would already execute arbitrary top-level code, violating "must not
   execute untrusted connector code" during browse. A `RegistryEntry`
   instead embeds the manifest's own declarative fields directly as
   JSON (capabilities, config schema, compatibility bounds, etc.) plus
   registry-specific metadata (verification status, package location,
   checksum, deprecation). `to_manifest()` builds a real
   `ConnectorManifest` from an entry with no import/execution of
   anything — the referenced `package_location` (a module:function
   string, e.g. `"app.connectors.adapters:aws_manifest"`) is only ever
   resolved at actual install time, through #25's already-authorized
   lifecycle, when an admin has explicitly chosen to trust it.
2. **The registry file itself is the audit trail.** No new database
   table: `registry/registry.json` is a plain, git-version-controlled
   file — every change to it is a normal, reviewable commit, which is
   a stronger and simpler audit trail than a DB table would be for
   data that changes by code review, not by runtime user action.
3. **Verification status is honestly limited.** `VERIFICATION_STATUSES
   = ("unofficial", "community", "official")` records what this
   registry *asserts*, not a security guarantee beyond what is
   actually checked (matching the issue's own explicit warning:
   "without implying a security guarantee beyond what #26 actually
   verifies"). No signature verification is implemented in this slice
   (deferred to #27) — a `checksum_sha256` field exists in the schema
   for forward compatibility but is not yet enforced against anything.
4. **Duplicate `(connector_id, version)` entries are rejected at load
   time**, not silently overwritten — a malformed/tampered registry
   fails closed.
5. **Fail-safe compatibility filtering.** `filter_compatible` uses
   #24's own `check_compatibility` — an entry outside the running core
   contract version is excluded from discovery results by default, not
   installable "unless explicitly overridden" (no override mechanism
   is added in this slice, since none is required yet and adding one
   speculatively would be exactly the premature complexity the epic
   warns against).
6. **Registry load failure never breaks the running app or already-
   installed connectors.** `load_registry` raises a clear
   `RegistryError` for the caller to handle; nothing in #25's
   lifecycle (`ConnectorInstance`/`ConnectorExecution`) depends on the
   registry file existing or being parseable at runtime — an
   already-installed instance keeps working with a broken/missing
   registry file, since installation only ever *consulted* the
   registry at install time, and never stores a live reference to it.

## 3. Schema (`registry/registry.json`)

```json
{
  "registry_version": 1,
  "entries": [
    {
      "connector_id": "aws_cloudtrail_iam",
      "display_name": "AWS CloudTrail + IAM posture",
      "publisher": "minigrc",
      "version": "1.0.0",
      "capabilities": ["configuration.snapshot"],
      "required_permissions": ["cloudtrail:DescribeTrails", "iam:GenerateCredentialReport"],
      "supported_auth_methods": ["ambient_credentials", "assume_role"],
      "config_schema": [
        {"name": "account_label", "type": "str", "required": true, "secret": false},
        {"name": "external_id", "type": "str", "required": false, "secret": true}
      ],
      "min_core_contract_version": 1,
      "max_core_contract_version": null,
      "verification_status": "official",
      "package_location": "app.connectors.adapters:aws_manifest",
      "checksum_sha256": null,
      "deprecated": false
    }
  ]
}
```

## 4. Module (`app/connector_registry.py`, new)

```python
VERIFICATION_STATUSES = ("unofficial", "community", "official")


class RegistryError(ValueError):
    """A registry file or entry is malformed/tampered/duplicate."""


@dataclass(frozen=True)
class RegistryEntry:
    connector_id: str
    display_name: str
    publisher: str
    version: str
    capabilities: tuple[str, ...]
    required_permissions: tuple[str, ...]
    supported_auth_methods: tuple[str, ...]
    config_schema: tuple[ConfigFieldSpec, ...]
    min_core_contract_version: int
    max_core_contract_version: int | None
    verification_status: str
    package_location: str
    checksum_sha256: str | None
    deprecated: bool


def load_registry(path: str) -> list[RegistryEntry]: ...
def to_manifest(entry: RegistryEntry) -> ConnectorManifest: ...
def filter_by_capability(entries, capability: str) -> list[RegistryEntry]: ...
def filter_compatible(entries, core_contract_version: int = CORE_CONTRACT_VERSION) -> list[RegistryEntry]: ...
def install_from_registry(
    session, entry, *, display_name, config, secret_values, actor, encryption_key
) -> ConnectorInstance:
    """Converts entry -> ConnectorManifest, then calls #25's
    install_connector_instance — the only path from a registry entry
    into an installed instance, reusing #25's lifecycle/security
    boundaries rather than bypassing them."""
```

## 5. Official connector repository (documented alternative, §1)

No new repository is created in this issue. `connectors/README.md`
(new, in *this* repo) documents:

- the deferred decision to spin off `yangjeep/minigrc-connectors` once
  a real third-party/external connector actually needs independent
  versioning — a decision for the user, not this loop;
- the intended future layout (`connectors/<name>/` per connector) for
  whichever repository eventually hosts it;
- that today, connector manifests/adapters live in `app/connectors/`
  and the registry lives in `registry/registry.json`, both in this
  repository — no speculative empty directories are created for
  connectors that don't exist yet (RULES.md's "no speculative
  infrastructure without a concrete use validating it").

## 6. Security review (adversarial, at design time)

- **No code execution during discovery/browse**: `load_registry` and
  every filter function only ever parse/compare JSON-derived dataclass
  fields — `package_location` is a plain string, never imported by
  this module.
- **Malformed/tampered entries**: missing required schema fields,
  an invalid `verification_status`, or a duplicate `(connector_id,
  version)` pair all raise `RegistryError` at load time, before any
  entry is usable.
- **Incompatible versions fail closed**: `filter_compatible` excludes
  anything `check_compatibility` rejects; no override path exists to
  bypass this in this slice.
- **Install handoff never bypasses #25**: `install_from_registry` is a
  thin wrapper that calls `install_connector_instance` — no parallel
  "install" path exists that skips manifest validation, config-schema
  validation, or secret handling.
- **Registry unavailable degrades safely**: a missing/broken registry
  file raises a clear, catchable error from `load_registry`; nothing
  else in the app depends on it existing.

## 7. Test strategy

- **Load/validation**: a valid registry loads correctly; a missing
  required field, an invalid `verification_status`, a malformed
  version string, and a duplicate `(connector_id, version)` pair are
  all rejected with a clear `RegistryError`; a nonexistent file path
  raises cleanly rather than crashing.
- **`to_manifest`**: round-trips every field into a real
  `ConnectorManifest` that itself passes #24's `validate_manifest`.
- **Filtering**: `filter_by_capability` and `filter_compatible` both
  proven against a registry containing a mix of matching/non-matching/
  incompatible entries — including the "fails closed by default, no
  override" compatibility case.
- **Install handoff (fake reference connector)**: publish a fake entry
  in a test registry, discover it via `filter_by_capability`, and
  install it via `install_from_registry` — proving it produces the
  exact same `ConnectorInstance` shape #25's own tests already verify,
  through the real lifecycle boundary, not a shortcut.
- **No code execution during discovery**: a registry entry whose
  `package_location` points at a module that would raise/have side
  effects on import is loaded and filtered successfully without ever
  importing it — only `install_from_registry` (never `load_registry`/
  the filter functions) would need to resolve it, and this slice's
  `to_manifest`/install handoff never resolves `package_location` at
  all yet (deferred to whichever future issue actually needs to
  dynamically import third-party connector code — see §8).

## 8. Definition of done

- A versioned/auditable connector registry exists
  (`registry/registry.json`, git-version-controlled).
- miniGRC can discover compatible connectors by capability/version
  through `app/connector_registry.py`.
- Registry browsing never executes connector code.
- Install handoff uses #25's lifecycle controls exclusively.
- The official-connector-repository question is answered with the
  issue's own sanctioned documented alternative, not silently ignored
  or unilaterally decided.
- No unnecessary marketplace backend complexity (no ratings, payments,
  user-submitted publishing workflow, or override-policy UI).

## 9. Known deferred/untested paths

- `package_location` is schema-present but **not yet resolved/imported
  by anything** — this issue proves the registry can describe where a
  connector's manifest-building code *would* live, without needing to
  actually dynamically import it yet. Wiring a real "activate this
  registry entry's actual Python code" step is deferred until a real
  non-adapter-module connector needs it.
- No signature/checksum enforcement (#27) — `checksum_sha256` is
  schema-present, unchecked.
- No separate `yangjeep/minigrc-connectors` repository (§1/§5) — a
  user decision, not made here.
- No HTTP route/admin UI to browse the registry interactively — same
  backend-only precedent #24/#25 already established; deferred to
  whichever issue defines the operator-facing surface.
