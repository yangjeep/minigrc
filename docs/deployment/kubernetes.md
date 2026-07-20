# Kubernetes deployment

miniGRC ships a Helm chart at `charts/minigrc/` for running the web
process and background worker on Kubernetes. It targets a single
external PostgreSQL instance you manage yourself — the chart never
bundles or manages a database.

## Chart layout

- `web` Deployment — runs `uvicorn app.main:app`, exposed via a
  ClusterIP `Service` and optional `Ingress`.
- `worker` Deployment — runs `python -m app.worker`, no exposed port.
- `migrate` Job — Helm `pre-install,pre-upgrade` hook that runs
  `python -m app.cli migrate` once per release before web/worker pods
  roll out. Deleted automatically after it succeeds
  (`helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded`).
- `ConfigMap` — non-secret environment (`GRC_LOG_LEVEL`,
  `GRC_APP_ENV`, `GRC_DATA_DIR`, etc.), merged from `values.yaml`'s
  `config` map.
- Optional `PersistentVolumeClaim` — for SQLite-file and
  import-watch-directory deployments. Disabled for the external
  Postgres / stateless production path.
- `ServiceAccount` with default pod/container security contexts:
  non-root (`runAsUser: 10001`, matching the Dockerfile's `grc` user),
  no privilege escalation, all capabilities dropped.

## Required external Secret

The chart deliberately never creates a Kubernetes `Secret` — it only
references one by name (`externalSecret.name`, default
`minigrc-secrets`) via `envFrom.secretRef` on both Deployments and the
migration Job. Create it yourself, e.g.:

```bash
kubectl create secret generic minigrc-secrets \
  --from-literal=GRC_ENCRYPTION_KEY="$(openssl rand -base64 32)" \
  --from-literal=DATABASE_URL="postgresql+psycopg://user:pass@host:5432/minigrc" \
  --from-literal=GRC_SESSION_SECRET="$(openssl rand -base64 32)"
```

`GRC_ENCRYPTION_KEY` is required once the [Secret foundation](../worklog/2026-07-20-secret-foundation.md)
is in use for encrypted external-connection credentials — losing it
makes existing encrypted secrets unrecoverable, so back it up outside
the cluster. `DATABASE_URL` is required for any deployment using
external PostgreSQL (i.e. every production deployment; see
`values-production.yaml`). Add Google OAuth client secrets to the
same Secret if that login path is enabled.

## Development vs. production values

- `values.yaml` — single replica, SQLite on a PVC, no Ingress. Good
  for evaluating the chart against a local cluster (kind/minikube).
- `values-production.yaml` — 2 web replicas, Ingress with TLS enabled,
  `persistence.enabled: false` (stateless pods against external
  Postgres).

```bash
helm install minigrc ./charts/minigrc \
  -f charts/minigrc/values-production.yaml \
  --set image.tag=<your-built-tag>
```

## Scaling considerations

- **Worker** scales horizontally without coordination: job claiming
  uses an atomic `UPDATE ... WHERE status = 'pending'` guard (see
  `app/jobs.py`), so multiple worker replicas can run against the
  same job table safely.
- **Web** scales horizontally once `persistence.enabled: false` — the
  PVC is `ReadWriteOnce`, so keep `web.replicaCount: 1` for any
  deployment that mounts it (SQLite-file or watched-import-directory
  use). Move to external PostgreSQL to unlock multi-replica web.

## No bundled PostgreSQL

This chart does not include a PostgreSQL `StatefulSet` and will not.
Operating a production-grade Postgres (backups, failover, upgrades) is
out of scope for this project — bring your own managed instance or
in-cluster Postgres operator and point `DATABASE_URL` at it.

## Validation performed

Validated locally with `helm lint` and `helm template` (both default
and `values-production.yaml` overrides) — confirmed all 7 resource
kinds render, non-root security contexts and `/health` probe paths are
present on web/worker/migration workloads, and YAML output parses
cleanly. **Not** deployed to a live cluster in this environment (none
available) — that verification is a known gap, left for the next
person with cluster access.
