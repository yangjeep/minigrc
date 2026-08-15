# miniGRC MVP Product Requirements Document

Status: MVP scope freeze candidate

This document defines the product intent, architectural invariants, MVP boundaries, and acceptance criteria for miniGRC. GitHub issues define implementation slices; repository state defines what is actually implemented. When this PRD, an issue, and current code disagree, agents must surface the conflict rather than silently choosing one interpretation.

## 1. Product thesis

miniGRC is a lightweight, self-hostable compliance operating system for small and mid-sized technical organizations that need to build and run a defensible compliance program without buying a large GRC platform.

The primary MVP experience is SOC 2 Type II readiness. ISO 27001 remains a first-class compatible framework through the same control, evidence, policy, risk, testing, and audit foundations.

The product should answer one practical question better than anything else:

> How far are we from audit-ready compliance, what is blocking us, and what should happen next?

miniGRC is not primarily a checklist database. It is an operational system for defining scope, operating controls, collecting durable evidence, managing policies, testing controls, tracking findings, and continuously driving the program toward readiness.

## 2. Target users

Primary users:

- startup or SMB CISO / security lead;
- technical founder who owns security and compliance;
- engineering or IT leader acting as the compliance program owner;
- compliance lead at a technically capable organization that prefers self-hosted or portable systems.

Secondary users:

- control owners;
- policy reviewers and approvers;
- internal auditors/testers;
- external auditors consuming exported evidence packages.

The MVP assumes one organization per deployment. Multi-tenant SaaS operation is not an MVP requirement.

## 3. Core jobs to be done

A user should be able to:

1. define what service, systems, people, vendors, locations, data, and framework criteria are in compliance scope;
2. bootstrap a reasonable SOC 2 control program from that scope;
3. assign control and policy ownership;
4. operate recurring or event-driven controls through an audit period;
5. capture durable, provenance-bearing evidence from manual uploads or connectors;
6. manage policy drafts, reviews, approvals, effective versions, and superseded history;
7. test control operation and record deviations, findings, remediation, and retest;
8. see an explainable readiness view with prioritized blockers and next actions;
9. export a defensible audit/PBC package for a defined period;
10. optionally use a BYOK AI digital compliance TPM to summarize, prioritize, nag, and pre-fill repetitive compliance work without granting it authority to make compliance conclusions.

## 4. Product principles

### 4.1 SOC 2-first, framework-neutral core

SOC 2 Type II drives the primary UX and design pressure. The data model must not hard-code SOC 2 where a generic compliance concept exists.

A shared internal control may map to multiple SOC 2 criteria and ISO 27001 requirements. Operational facts must not be duplicated per framework.

### 4.2 Material compliance state is event-centric

Material compliance facts are recorded as immutable domain events. Current state is derived through deterministic projections/read models.

Target flow:

`command -> validate -> append event -> update canonical projection -> commit`

An audit log that merely describes a mutable row update is not sufficient when the historical fact itself matters.

Do not event-source incidental UI preferences or operational noise.

### 4.3 Historical approved/effective state is immutable

A policy/control/framework version that was approved or effective during an audit period must not be rewritten by a later edit.

Changes create new versions and new lifecycle events.

### 4.4 Evidence is captured, not linked by mutable reference alone

Google Drive, GitHub, AWS, IdPs, and other systems are evidence sources. Durable audit evidence must be captured/snapshotted into miniGRC-controlled storage with content hash and provenance.

A mutable external URL is never sufficient as the sole historical evidence representation.

### 4.5 Connectors provide facts, not compliance conclusions

Connectors may capture source facts, files, populations, configurations, and findings. They may not directly mark a control effective, satisfy a framework criterion, pass a test, or close a finding.

Material connector outputs enter miniGRC through core command/event boundaries.

### 4.6 AI assists; humans remain authoritative

The digital compliance TPM may observe, summarize, prioritize, recommend, nag, and draft.

It may not autonomously:

- approve a policy;
- attest that a control operated;
- pass a test;
- accept risk;
- close a finding/remediation;
- fabricate evidence;
- change framework applicability;
- mutate historical compliance facts outside authorized deterministic commands.

### 4.7 One relational backend per deployment

A deployment chooses exactly one relational database backend: SQLite or PostgreSQL.

No dual-write, shadow database, analytics secondary, cross-database projection, or mixed runtime mode is supported.

PostgreSQL may use materialized views for disposable/rebuildable read optimization. They are never authoritative state.

## 5. MVP functional scope

### 5.1 Compliance program and scoping

Scoping is a first-class domain capability because readiness has no meaningful denominator without scope.

A compliance program should represent, as applicable:

- organization/service/product being audited;
- framework and framework version;
- selected SOC 2 Trust Services Categories;
- target/audit operating period;
- systems/assets/environments;
- repositories and cloud environments;
- people/groups/functions;
- vendors/subservice organizations;
- locations;
- data categories where useful;
- exclusions and rationale.

Scope should drive applicability, starter control selection, relevant populations, connector suggestions, and readiness calculation.

### 5.2 SOC 2-first framework experience

SOC 2 is the default framework experience for a fresh program.

ISO 27001 remains selectable and operationally meaningful through the same internal controls and evidence.

Framework catalog/version metadata and organization-specific adoption/version pinning must be explicit.

Product-authored cross-framework mappings must be distinguishable from official framework text or equivalence.

### 5.3 Control operations

The system must distinguish:

- control design/version;
- owner;
- scope/applicability;
- cadence/trigger semantics;
- operating period;
- expected occurrence;
- actual occurrence/performance;
- evidence expectations and links;
- human review/attestation facts;
- testing/effectiveness conclusions.

Recurring, continuous, event-driven, and manually triggered controls must be representable without schema hacks.

Missing/overdue state should be computed from authoritative facts, not manually maintained as truth.

### 5.4 Policy and compliance-document lifecycle

The MVP needs a real business lifecycle, not only uploaded file versions.

Minimum lifecycle:

`draft -> in review -> approved -> effective -> superseded/withdrawn`

Required semantics:

- immutable approved/effective historical versions;
- author/reviewer/approver identity;
- approval and effective timestamps;
- change summary;
- supersedes relationship;
- content hash;
- control/framework linkage;
- event-backed lifecycle history.

Document bytes may live in object storage; compliance version state lives in the relational/event model.

### 5.5 miniGRC-owned evidence repository

The MVP must support an internal evidence-artifact repository independent of any specific connector.

Relational/event model owns:

- logical artifact identity;
- immutable captured versions;
- content hash;
- source/provider provenance;
- captured/collected timestamps;
- source revision/version metadata when available;
- actor/connector execution correlation;
- links to control occurrences, tests, findings, and periods.

S3-compatible storage owns file bytes.

Minimum evidence paths:

- manual upload;
- connector capture;
- version/history view;
- download/export;
- content hash verification;
- source provenance.

S3 object versioning, if enabled, is infrastructure protection and not the compliance-version model.

### 5.6 Assurance: testing, populations, findings, remediation

The MVP must support:

- evidence provenance;
- reproducible/frozen population snapshots where relevant;
- sample selection and rationale;
- test procedure/execution/result;
- deviations;
- findings;
- owner/severity/due date;
- remediation plan and updates;
- remediation evidence;
- retest;
- closure with preserved history.

No universal sample-size rule should be encoded as audit certainty.

### 5.7 Continuous readiness engine

Readiness is derived from authoritative scope and operational state.

The product must surface concrete conditions such as:

- incomplete scope;
- missing control owners;
- upcoming control operations;
- missed/overdue occurrences;
- completed occurrences missing evidence;
- stale evidence where a freshness expectation exists;
- evidence awaiting review;
- tests due/incomplete;
- test deviations;
- open findings;
- overdue remediation;
- retests due;
- policy review/approval gaps;
- connector/source gaps that block evidence collection.

Any overall score must be transparent, explainable, and secondary to actionable blockers. The product must never imply an auditor assurance opinion from an internal percentage.

### 5.8 Landing page: How far are you from compliance?

The primary authenticated landing experience should answer:

> How far are you from SOC 2 audit readiness?

For a new program, it should lead into scoping and onboarding.

For an active program, it should show:

- readiness stage/status;
- top blockers;
- overdue items;
- evidence gaps;
- open findings;
- policy gaps;
- prioritized next actions;
- direct links to resolve the underlying authoritative objects.

The landing page is an operating console, not a vanity dashboard.

### 5.9 Onboarding and program bootstrap

Expected flow:

1. define scope;
2. select SOC 2 categories/framework version;
3. identify/import people, systems, vendors, and environments;
4. generate/review an opinionated starter control program;
5. assign owners;
6. review required starter policies;
7. configure useful evidence connectors;
8. start an operating/audit period;
9. land on readiness blockers and next actions.

Starter content must be clearly product-authored/placeholder where licensing or official-text constraints apply.

### 5.10 Connector platform and marketplace

miniGRC core provides a provider-neutral connector contract with explicit capabilities, configuration schema, secret handling, runtime/result envelopes, provenance, health/history, and compatibility rules.

Initial capabilities may include:

- evidence/file capture;
- identity populations/groups;
- configuration snapshots;
- asset inventory;
- vulnerability findings;
- notifications.

A versioned, auditable registry is sufficient for marketplace v1. A full commercial marketplace backend is out of scope.

Google Drive is the first reference connector for durable evidence/file capture.

### 5.11 Identity and SSO

The MVP supports generic OIDC authentication while preserving local break-glass administration.

Required properties:

- provider-neutral OIDC login;
- deterministic external identity linking by issuer + subject;
- no silent email-only account merge;
- explicit claim/group-to-role mapping;
- least privilege for unknown claims;
- local break-glass admin path;
- Authentik as a verified self-hosted reference provider, not a hard dependency.

### 5.12 BYOK AI abstraction and Digital Compliance TPM

AI configuration is optional and organization/deployment scoped.

Provider layer should support an OpenAI-compatible boundary where practical, including configurable endpoint/model/credential with secure secret handling.

The digital compliance TPM consumes deterministic miniGRC readiness/domain state and provides four classes of value:

#### Observe

Summarize program state, evidence, policies, control operations, tests, findings, and remediation.

#### Plan/prioritize

Explain the highest-value next actions based on deterministic readiness conditions.

#### Nag

Generate useful reminders/escalations for due/overdue/stale work using a bounded reminder policy. Deterministic code decides that the condition exists and who is eligible; the model may phrase/contextualize the message.

#### Pre-fill

Draft repetitive compliance text such as:

- evidence descriptions;
- control-operation notes;
- policy change summaries;
- auditor/PBC responses;
- finding management responses;
- remediation updates;
- risk/control narratives.

Drafts must expose missing facts rather than inventing them and remain human-reviewed until normal authorized submission/approval.

### 5.13 Audit/PBC package

For a defined audit period/scope, miniGRC must be able to produce a portable evidence package containing or referencing:

- scope/program metadata;
- framework version and criteria in scope;
- controls and historical effective versions;
- expected/actual occurrences;
- evidence artifacts and hashes;
- test results and samples where included;
- deviations/findings/remediation;
- relevant policy versions/approvals;
- manifest with artifact metadata and hashes.

An external auditor portal is not required for MVP. A defensible export is sufficient.

## 6. Persistence architecture

### 6.1 Selected relational database

Exactly one database backend is active per deployment:

- SQLite for low-ops/simple deployments; or
- PostgreSQL for production/larger deployments.

All domain events, canonical projections, relational configuration, auth data, and relational read models live in the selected backend.

### 6.2 Domain event store

Material compliance events require stable:

- event ID;
- aggregate type/id;
- aggregate sequence/version;
- event type and schema version;
- occurred-at and recorded-at semantics;
- actor identity where applicable;
- correlation/causation metadata where useful;
- immutable payload;
- idempotency/uniqueness constraints.

### 6.3 Canonical projections

Canonical projections are deterministic and rebuildable from authoritative events plus explicitly versioned static/catalog inputs.

Event append and synchronous projection update should occur atomically in one selected relational DB transaction where immediate consistency is desired.

### 6.4 Read optimization

SQLite may use ordinary views/indexes/application-maintained projection tables.

PostgreSQL may additionally use materialized views and PostgreSQL-specific indexing for expensive cross-domain readiness/auditor queries.

These optimizations are disposable and rebuildable.

### 6.5 Object storage

File bytes use an S3-compatible backend abstraction. Self-hosted deployments should be able to point this at an appropriate compatible implementation.

The DB/event store remains authoritative for artifact identity, provenance, compliance versioning, lifecycle, and relationships.

## 7. Security and authorization requirements

The MVP must preserve or introduce:

- CSRF protection for state-changing browser actions;
- secure server-side sessions;
- admin/user authorization boundaries, evolving only through explicit issues;
- safe OIDC state/nonce/token validation via mature libraries;
- encrypted connector/AI provider credentials;
- no secret readback to normal clients;
- no secrets/tokens in domain events, logs, exports, fixtures, or generated audit packages;
- least-privilege connector scopes;
- explicit connector permission/capability review;
- safe handling of untrusted evidence/file metadata/content;
- auditability of material authorization and compliance changes;
- no AI or connector bypass of deterministic authorization/domain validation;
- immutable historical approved/effective compliance facts through normal application paths.

## 8. Explainable readiness stages

The MVP may use readiness stages such as:

- scope incomplete;
- foundation incomplete;
- operating controls;
- evidence gaps;
- testing/remediation incomplete;
- audit-package ready.

Exact labels may change through UX work. The important invariant is that every stage/blocker is traceable to concrete authoritative state.

## 9. Explicit MVP non-goals

Unless separately approved, the MVP does not require:

- multi-tenant SaaS organizations/org switching;
- Kubernetes as a functional dependency;
- microservices or distributed event infrastructure;
- Kafka/CDC/event buses;
- simultaneous SQLite + PostgreSQL operation;
- SCIM provisioning;
- SAML;
- autonomous AI approval/attestation/testing/finding closure;
- an external auditor login portal;
- a broad CSPM/vulnerability scanner;
- a general workflow/rules engine;
- a commercial connector marketplace backend;
- every major cloud/IdP connector;
- advanced BI/warehouse infrastructure;
- official certification claims;
- copying/licensing protected framework content without a valid source/right.

## 10. MVP acceptance criteria

The MVP is ready to call feature-complete when all of the following are demonstrably true:

- [ ] A new deployment can define a SOC 2 program scope and start an operating period.
- [ ] SOC 2 is the primary framework experience and ISO 27001 remains usable through shared controls.
- [ ] Material compliance facts for actively developed domains are event-backed and projections rebuild deterministically.
- [ ] SQLite and PostgreSQL provide materially equivalent domain semantics, with exactly one active per deployment.
- [ ] Policies support draft/review/approval/effective/superseded history with immutable approved versions.
- [ ] Controls support expected/actual occurrences, ownership, cadence, evidence linkage, and historical version pinning.
- [ ] Evidence can be uploaded/captured into miniGRC-owned S3-compatible storage with hash and provenance.
- [ ] Google Drive proves the generic connector abstraction end to end.
- [ ] Testing, findings, remediation, and retest preserve historical facts.
- [ ] Landing/readiness view explains concrete blockers and links to next actions.
- [ ] Onboarding moves a fresh deployment from scope to an actionable starter program.
- [ ] Generic OIDC and explicit role mapping work with local break-glass administration preserved.
- [ ] BYOK AI can nag and pre-fill from grounded facts while failing safely when absent/unavailable.
- [ ] AI cannot autonomously approve, attest, pass tests, close findings, or fabricate evidence.
- [ ] A scoped audit/PBC package can be exported with artifact hashes and historical versions.
- [ ] Full test/lint/migration/security checks are green for the supported deployment paths.

## 11. Issue families and dependency intent

Current issue families include:

- #10–#15: SOC 2 Type II-first operating model/readiness/AI;
- #16–#20: generic OIDC/SSO and identity evidence;
- #21–#22: event-centric persistence and SQLite/PostgreSQL contract;
- #23–#28: connector platform/registry/security and Google Drive reference connector.

Additional issues should cover the remaining MVP gaps identified by this PRD, especially:

- compliance scoping/program bootstrap;
- policy/compliance-version lifecycle;
- S3-compatible evidence repository;
- readiness landing/onboarding integration;
- audit/PBC package;
- expansion of #15 into the Digital Compliance TPM/provider abstraction where its existing body is too narrow.

Dependencies must follow repository reality, not issue numbering. Planned infrastructure described in an issue is not considered available until merged code proves it exists.

## 12. Agent execution contract

For autonomous/semi-autonomous implementation loops:

1. Read root `CLAUDE.md`, then `.agent/README.md`, `.agent/RULES.md`, `.agent/LOOP.md`, and this PRD.
2. Inspect current repository state and open GitHub issues before selecting work.
3. Build the dependency graph from actual merged code plus issue relationships.
4. Select the highest-priority unblocked issue.
5. Inspect relevant code, migrations, tests, docs, and recent merged work before designing.
6. Challenge issue assumptions when repository reality disagrees.
7. For architecture-sensitive work, produce/update the repository design/spec before implementation when the issue requires it.
8. Implement the smallest coherent slice.
9. Run targeted tests, full relevant tests, lint/format, migration checks, and security/dependency checks available in the repo.
10. Perform an adversarial review for authorization, history integrity, idempotency/replay, migration safety, secret leakage, and scope creep.
11. Fix validated findings.
12. Update worklog/docs/issue state, commit logically, push, and create/update a draft PR.
13. Do not merge unless the task contract explicitly authorizes merge.
14. Move to the next issue only when dependency and branch/PR state make that safe.

### Stop and surface a decision when

Stop instead of silently deciding when:

- this PRD and current repository architecture materially conflict;
- an architectural invariant would be violated;
- an issue assumes a dependency exists but it has not landed;
- a destructive migration/backfill cannot be proven safe;
- historical compliance truth would need to be invented or rewritten;
- an authorization/security boundary is materially ambiguous;
- implementation requires a new external service/infrastructure not already approved;
- scope must expand materially beyond the issue/PRD to proceed correctly.

Ordinary implementation choices that can be resolved from repository patterns should not trigger unnecessary stops.

## 13. Source-of-truth precedence

When sources disagree, use this precedence and report the conflict:

1. safety/security/integrity invariants explicitly stated in current approved architecture/PRD;
2. current merged repository behavior/schema for what exists now;
3. accepted GitHub issue task contract for the slice being implemented;
4. current design/spec for that issue;
5. historical docs/worklogs.

Do not pretend an old hard constraint still applies after an explicit approved architecture decision replaces it. Update stale docs as part of the relevant architecture change.
