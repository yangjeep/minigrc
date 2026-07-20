# Feature 10: Kubernetes and Helm

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Tenth phase of the platform pivot (umbrella issue #5, PR #6), "checkpoint 4"
group (Kubernetes alone). A production-oriented Helm chart
(`charts/minigrc/`) for deploying the web process and worker as separate
Kubernetes Deployments, with migrations run once per release via a
pre-install/pre-upgrade hook Job.

## Files Changed

- `charts/minigrc/Chart.yaml`, `values.yaml`, `values-production.yaml`.
- `charts/minigrc/templates/_helpers.tpl`, `serviceaccount.yaml`,
  `configmap.yaml`, `pvc.yaml`, `migration-job.yaml`,
  `web-deployment.yaml`, `worker-deployment.yaml`, `web-service.yaml`,
  `ingress.yaml`.
- `docs/deployment/kubernetes.md` — usage, external-Secret contract,
  scaling notes.

## Verification

- [x] `pytest` — 291 passed, 1 skipped (no Python changes this phase;
      confirms no regression).
- [x] `ruff check .` / `ruff format --check .` clean.
- [x] `helm lint charts/minigrc` — 0 failed (default and
      `values-production.yaml`).
- [x] `helm template` against both value files — confirmed all 7
      resource kinds render (ServiceAccount, ConfigMap, PVC, Service,
      2x Deployment, Job for default; same minus PVC plus Ingress for
      production).
- [x] Parsed the full `helm template` output with PyYAML
      (`yaml.safe_load_all`) — confirms every rendered document is
      syntactically valid YAML, not just template-engine-valid.
- [x] Grepped rendered output for `runAsNonRoot: true`,
      `runAsUser: 10001`, `allowPrivilegeEscalation: false`, and
      `path: /health` — present on web/worker/migration workloads as
      required.
- [ ] **Not deployed to a live cluster** — none available in this
      environment. `helm lint`/`helm template` validate structure and
      rendering only, not actual scheduling, probe behavior, or
      migration-Job execution against a real API server. Documented as
      a known gap in `docs/deployment/kubernetes.md`.

## Decisions & Alternatives Rejected

- **No bundled PostgreSQL StatefulSet** — explicit user instruction.
  `bundledPostgres.enabled: false` in `values.yaml` documents the
  decision inline rather than silently omitting it.
- **Migration runs once via a Helm hook Job, not per-replica.** The
  Dockerfile's own `CMD` self-migrates on every container start
  (`python -m app.cli migrate && uvicorn ...`), which is fine for a
  single container but would race across multiple web replicas. The
  chart overrides `command` on the web Deployment to skip the
  self-migrate step and relies on the hook Job
  (`pre-install,pre-upgrade`, `hook-delete-policy:
  before-hook-creation,hook-succeeded`) to run it exactly once per
  release before any web/worker pod starts.
- **External Secret reference only, never a `Secret` manifest.** The
  chart takes `externalSecret.name` and wires it via `envFrom.secretRef`
  on web, worker, and the migration Job — it never creates or manages
  Secret contents itself, consistent with the standing "no plaintext
  credential storage in version control" constraint.
- **PVC is conditional and `ReadWriteOnce`**, sized for SQLite-file and
  watched-import-directory single-replica deployments. Production
  values disable it entirely (`persistence.enabled: false`) since
  `values-production.yaml` assumes external PostgreSQL and no local
  watched directory, making both web and worker fully stateless and
  therefore safely multi-replica.
- **Security contexts match the Dockerfile's existing non-root user**
  (uid 10001) rather than inventing a new one, plus
  `allowPrivilegeEscalation: false` and dropping all capabilities —
  no new security posture invented, just carried into the chart.
- **No Pod Disruption Budget added.** Considered per the Feature 10
  spec's "pod disruption considerations where appropriate," but with a
  single-container non-HA-by-default chart (`web.replicaCount: 1` in
  dev values) a PDB would either be a no-op or would need per-user
  tuning; left as a values.yaml extension point for later rather than
  guessing a number.

## Known Gaps / Follow-ups

- No live-cluster deployment test (see Verification) — next person
  with cluster access should run `helm install` against a real
  kind/minikube cluster and confirm the migration Job actually
  completes and web/worker pods reach Ready.
- No Pod Disruption Budget template (see above).
- No Horizontal Pod Autoscaler — resource requests/limits are set, but
  autoscaling wasn't in the Feature 10 requirement list.
