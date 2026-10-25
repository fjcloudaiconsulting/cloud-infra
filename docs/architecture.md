# Architecture

How the FJ Consulting apps (TBD, Ziftbook) run, end to end. This page is the map; each section
links to the README or manifest that holds the detail. Change the diagram in the same PR as the
infrastructure it describes.

```mermaid
flowchart LR
  user([Users]) --> cf

  subgraph cloudflare["Cloudflare (terraform/cloudflare)"]
    cf["DNS and proxy<br/>thebetterdecision.com: Full strict"]
  end
  worker["Workers ziftbook-landing, tbd-landing<br/>ziftbook.com apex and www, thebetterdecision.com apex<br/>(code in the app repos)"]
  cf --> worker

  subgraph hetzner["Hetzner nbg1 (terraform/hetzner, ansible/)"]
    subgraph hk3s["3 x CAX21 ARM64, k3s HA (clusters/hetzner)"]
      cfd["cloudflared: Cloudflare Tunnel, the only ingress"]
      flux["Flux (clusters/hetzner/flux-system)"]
      netbird["NetBird peer (clusters/hetzner/netbird)"]
      subgraph ns_tbdp["tbd-prod"]
        tbdprod["TBD production (INFRA-48, here since INFRA-175)"]
      end
      subgraph ns_tbds["tbd-staging"]
        tbdstg["TBD staging + MCP server (INFRA-67, here since INFRA-173; MCP INFRA-147)"]
      end
      subgraph ns_zif["ziftbook-staging"]
        zifapp["Ziftbook staging (INFRA-7, here since INFRA-173)"]
      end
      subgraph ns_zifp["ziftbook-prod"]
        zifprod["Ziftbook production (INFRA-82, here since INFRA-181)"]
      end
      subgraph hdata["data (clusters/hetzner/data)"]
        hmysql[("MySQL 8.4")]
        hpostgres[("Postgres 18")]
        hbackup["CronJob db-backup 02:00 UTC"]
      end
    end
  end
  cf -->|"tunnel: app and dev (TBD), dev and app (ziftbook.com)"| cfd --> tbdprod & tbdstg & zifapp & zifprod
  tbdprod & tbdstg --> hmysql
  zifapp & zifprod --> hpostgres
  hbackup --> hmysql & hpostgres

  subgraph aws_backups["AWS (terraform/tbd-backups)"]
    bucket[("S3 Object Lock bucket + KMS")]
  end
  hbackup -->|"put-only key"| bucket
  probe -->|"list-only role"| bucket

  subgraph control["Control plane"]
    gh["GitHub fjcloudaiconsulting/cloud-infra"]
    tfc["HCP Terraform (org FlamaCorp)<br/>AWS via OIDC roles, Cloudflare via API token"]
    ghcr["GHCR images"]
    probe["Freshness probe (GitHub Actions)"]
  end
  gh --> tfc --> aws_backups & cloudflare
  gh -->|"main"| flux
  ghcr -->|"ghcr-pull secret"| hk3s
  owner([Owner Mac]) -->|"NetBird, TCP 6443 only"| netbird
```

## Ingress

Every app host goes through the Hetzner cluster's Cloudflare Tunnel: `dev.thebetterdecision.com` (staging, since INFRA-173) and
`app.thebetterdecision.com` (TBD production, since INFRA-175, proxied CNAMEs to the tunnel), `dev.ziftbook.com`
(staging, since INFRA-173) and `app.ziftbook.com` (production, since INFRA-181). The uptime checks target TBD's `app.../health` (liveness only: `/ready` and `/health/dependencies` are not routed publicly, INF-09) and Ziftbook's `app.../api/healthz`, through the tunnel since INFRA-175. The ziftbook.com apex and www are a Cloudflare Worker (`ziftbook-landing`), and since INFRA-61 the
thebetterdecision.com apex is the Worker `tbd-landing`; their code and deploys live in the app repos, and their custom
domains are attached by hand ([configuration-map.md](configuration-map.md#worker-and-snippet-access-infra-98)).
`www.thebetterdecision.com` is a proxied record that a redirect rule in `terraform/cloudflare` sends to the apex (301).

The tunnel is the only way in to the TBD workloads (INF-01). In `tbd-prod` and `tbd-staging` only `cloudflared` reaches the backend
and MCP pods, and their uvicorn reads `X-Forwarded-For` and `X-Forwarded-Proto` from the pod network only. The TBD
processes take the visitor's address from `CF-Connecting-IP` (`CLIENT_IP_HEADER`). `tests/test_tbd_manifests.py` and
`tests/test_tbd_edge.py` hold these in place. `cloudflared` itself reaches only cluster DNS, the pods and ports the
tunnel routes to, and Cloudflare's edge; `tests/test_network_policies.py` keeps its egress in step with `tunnel.tf`.

Both app zones rate-limit the apps' sign-in and token endpoints at the edge (INFRA-123), and ziftbook.com also its
unauthenticated database probe `/api/health/dependencies` (INFRA-140): more than 20 requests in 10 s
from one IP (per Cloudflare data center) to those paths gets a 429 from Cloudflare for 10 s, which caps the volume one
address can send to them. The zones are on the Free plan, which allows one such rule per zone and matches on the path only,
so the rule covers every proxied host of the zone. Each app keeps its own per-account and per-address limits behind it.

A zone custom rule caps request bodies on the TBD app hosts (`app.` and `dev.`, BE-13): a request that declares a
`Content-Length` over 16 MiB gets Cloudflare's 403 block page. The largest upload TBD accepts is 5 MiB.

- Terraform: [`terraform/cloudflare/main.tf`](../terraform/cloudflare/main.tf), rate limit in
  [`terraform/cloudflare/rate_limit.tf`](../terraform/cloudflare/rate_limit.tf), body cap in
  [`terraform/cloudflare/body_limit.tf`](../terraform/cloudflare/body_limit.tf)

## Node and k3s

Three Hetzner CAX21 ARM64 servers in nbg1 run k3s HA (`terraform/hetzner`, host config in `ansible/`). There is no EKS, RDS, ALB or NAT. The Lightsail node was decommissioned on 2026-10-14 (INFRA-176).

## Namespaces and quotas

| Namespace | Holds | Memory quota (requests / limits) | Notes |
|---|---|---|---|
| `tbd-prod` | TBD production (`app.thebetterdecision.com`, INFRA-48); on the Hetzner cluster since INFRA-175 | 1856Mi / 3200Mi | Pod Security restricted; no priority class; backend and frontend 2 replicas, one per app node (INFRA-177); scheduler: exactly one pod |
| `tbd-staging` | TBD staging (`dev.thebetterdecision.com`) and the MCP server (`/mcp`, INFRA-147); on the Hetzner cluster since INFRA-173 | 384Mi / 960Mi | PriorityClass `staging`, enforced by a quota; no scheduler pod (INFRA-67); behind Cloudflare Access, owner login only (INFRA-117), except the smoke paths and the MCP client's OAuth paths (INFRA-147) |
| `ziftbook-staging` | Ziftbook staging (`dev.ziftbook.com`); on the Hetzner cluster since INFRA-173 | 512Mi / 1Gi | PriorityClass `staging` (-100, never preempts), enforced by a quota |
| `ziftbook-prod` | Ziftbook production (`app.ziftbook.com`, INFRA-82); on the Hetzner cluster since INFRA-181 | 1280Mi / 2560Mi | Pod Security restricted; no priority class; image bumps are a PR, never automerged; backend and frontend 2 replicas, one per app node (INFRA-177) |
| `data` | MySQL and Postgres with the backups  | 1Gi / 1536Mi | The Namespace object is excluded from Flux pruning (its contents are not) |
| `netbird` | NetBird peer, `owner-admin` | none | Pod Security privileged (hostNetwork) |
| `observability` | Grafana Alloy (metrics to Grafana Cloud, INFRA-85) | none (request 128Mi, limit 256Mi) | Pod Security privileged (read-only hostPath for node metrics) |
| `system-upgrade` | system-upgrade-controller and the two k3s upgrade Plans (INFRA-179) | none | Pod Security privileged (upgrade jobs run privileged with the host mounted) |

The app namespaces enforce Pod Security restricted, `data` baseline. Every app namespace has a LimitRange with default memory limits, and its `default`
ServiceAccount pulls from GHCR through `ghcr-pull`. Manifests:
[`clusters/hetzner/namespaces/`](../clusters/hetzner/namespaces/).
Their egress (`egress-no-imds` in each `networkpolicy.yaml`) reaches every pod and Service and the internet, except
instance metadata and the node network: no app pod uses the Kubernetes API or a node address. A workload that
needs one must get its own egress rule first.

## Data stores

All run in `data` on the Hetzner cluster as StatefulSets on local-path volumes, images pinned by digest.

| Store | For | Key settings |
|---|---|---|
| MySQL 8.4 | TBD production (`tbd`, users `tbd_app`, `tbd_backup`); TBD staging (`tbd_staging`, user `tbd_staging`, grants on its own database only, 20 connections at most) | buffer pool 128M, performance_schema off, binlog off |
| Postgres 18 | Ziftbook staging (`ziftbook`) and production (`ziftbook_prod`); the two roles are shared and created by Ziftbook's pinned `bootstrap.sql`, run as a Job per database | shared_buffers 64MB, no parallel workers |

A NetworkPolicy denies ingress to `data` by default, for the namespaces it hosts: MySQL accepts the pods of `tbd-prod`
and `tbd-staging` that hold a database URL (backend and scheduler, backend and MCP), Postgres (Hetzner only since
INFRA-181) the backend and worker of `ziftbook-staging` and `ziftbook-prod`, and the backup and bootstrap pods reach
their databases inside `data`. Frontends are not admitted. A new workload that connects needs its `app` label in that
policy; `tests/test_network_policies.py` derives the list from the manifests.
Egress from `data` is deny-by-default too (K8S-06). The servers get no egress rule: they only answer, and look up no
client names. The backup reaches cluster DNS, both servers and HTTPS on public addresses (S3); the Ziftbook bootstrap
Jobs cluster DNS, Postgres and HTTPS on public addresses (the pinned `bootstrap.sql`). Nothing there reaches the node
network, the Kubernetes API or instance metadata. A new pod in `data` has no network until it gets its own egress rule;
`tests/test_data_egress.py` checks every pod template.
Database auth and the NetBird ACL complete the controls.
Manifests and comments: [`clusters/hetzner/data/`](../clusters/hetzner/data/).

## Jobs, locks, sessions and rate limits

One standard for every app and environment (decided in INFRA-102): this state lives in the app's own
database, MySQL or Postgres. No app gets Valkey or Redis for it. Ziftbook is the reference
implementation.

| Concern | Standard | Postgres | MySQL 8.4 |
|---|---|---|---|
| Background jobs | A `jobs` table, indexed on `next_attempt_at`. Enqueue in the caller's transaction with a dedupe key; claim a batch with `FOR UPDATE SKIP LOCKED`; the bumped `next_attempt_at` is both the lease and the backoff; attempts are capped. Handlers must be safe to repeat | One `UPDATE ... RETURNING` (Ziftbook `backend/app/jobs.py`) | `SELECT ... FOR UPDATE SKIP LOCKED`, then `UPDATE`, in one READ COMMITTED transaction (REPEATABLE READ gap locks would block enqueues). No `RETURNING`; no subquery on the table being updated. Dedupe with `INSERT ... ON DUPLICATE KEY UPDATE id = id` |
| Periodic work | A loop in the worker only when each tick claims its rows (`SKIP LOCKED`) or holds a lease, since every replica runs it; otherwise one scheduler Deployment with `replicas: 1` and `Recreate` | | |
| Migration lock | A session-level lock held by the migrating connection | `pg_advisory_lock` | `GET_LOCK('<app>_migrate.<db>')` |
| Short locks (inside one transaction) | Lock the business row `FOR UPDATE` | `pg_advisory_xact_lock` is fine too | No transaction-scoped `GET_LOCK`: lock a row |
| Leases (held across requests, an external call or a long tick) | A lease row, inserted once by a migration. Acquire and renew with `UPDATE ... SET holder = ?, expires_at = <db now> + ttl WHERE name = ? AND (expires_at < <db now> OR holder = ?)`: 1 matched row means held. Renew before each side effect and stop when a renew fails. Never a session-level lock on a pooled connection: the pool keeps it or loses it silently | `now()` | `NOW(6)` on a `DATETIME(6)` column (whole seconds make a same-second renew match but change nothing, which some drivers report as 0 rows) |
| Sessions | An opaque random token in the cookie, only its hash in a `sessions` table; idle and absolute expiry checked on every read; revoking deletes the row | Ziftbook `backend/app/auth.py` | |
| Rate limits | A fixed window per key in a `rate_limits` table, one upsert per attempt in its own transaction, committed before the request runs (a failed login must not roll back its count), keys locked in sorted order. A database error fails the request: never fail open | Ziftbook `backend/app/limits.py` | `ON DUPLICATE KEY UPDATE` assigns left to right: set `hits` before `window_start` |
| Single-use tokens, nonces, webhook dedupe | A row with a unique key and `expires_at`; a duplicate key means a replay. Purged by the sweeper | | A plain `INSERT`, catching error 1062 (not `ON DUPLICATE KEY UPDATE`, which hides the replay) |
| Caches | None. An in-process TTL cache when a measured need appears; the database stays the source of truth | | |

Why the database and not Valkey:

- **One failure domain.** Valkey is a second one, with its own outages (on 2026-05-13 its timeouts
  turned every TBD auth endpoint into a 500). A limiter in the database fails together with the user
  lookup, so it never has to choose between failing open and failing closed (INFRA-121).
- **Backups.** The nightly dump covers the database. Valkey is not dumped: its state lives only in
  its AOF.
- **Memory and pods.** No extra pod per app and environment. A shared Valkey is not safe (below), so
  every environment would need its own, as tbd-staging will (INFRA-67).
- **Both engines.** Every row in the table works on MySQL 8.4 and Postgres 18; the column on the right
  lists the dialect differences.

Costs, accepted:

- One committed write per auth attempt on the shared database. A Cloudflare rate-limit rule in front
  of every app's auth endpoints absorbs bursts first (INFRA-123).
- Each replica's connection pool now carries sessions and limits too. Cap every app's pool so that
  replicas x pool stays under the server's `max_connections` before HPA (INFRA-100).
- After a real restore, delete every session row and every single-use link (Ziftbook `email_tokens` and
  invite links): sessions signed out and links used or revoked after the dump would otherwise come back
  ([RESTORE.md](../clusters/hetzner/data/RESTORE.md#6-real-restore-into-data)).

**A shared Valkey is not an option.** Valkey 8 ACL key patterns (`~tbd:*`) can confine
reads and writes, but `KEYS`, `SCAN`, `FLUSHDB` and `FLUSHALL` take no key argument and must be denied one
by one. Database rules (`db=<id>`) exist only from Valkey 9.1.0
([ACL SETUSER history](https://valkey.io/commands/acl-setuser/)). Neither helps with what stays
instance-wide: `maxmemory` with `noeviction`, the single command thread, AOF rewrites. One app filling
the store fails the other app's writes, and TBD's sessions fail closed, so its logins go down.

**The exception: TBD (ended).** TBD kept Valkey until tbd 0.296.0 (sessions with Lua rotation and reuse detection, MFA nonces,
rate limits and throttles, locks, dedupe keys, one cache) and moved:

1. INFRA-121: rate limits move to MySQL first, so they no longer fail open.
2. INFRA-122: sessions and the remaining keys moved to MySQL (0.296.0). The Valkey StatefulSet was
   removed with the old data stores (INFRA-175).

A new app that wants Valkey needs a measured reason in its PR (for example invalidation that must
reach every replica), and then gets one per environment, never shared.

## Backups and restore

- Nightly at 02:00 UTC, CronJob `db-backup` dumps MySQL then Postgres and uploads each set (dump,
  grants, manifest last) to the Object Lock bucket under `tbd-mysql/`, `tbd-staging-mysql/`, `ziftbook-postgres/` (Ziftbook staging) and `ziftbook-prod-postgres/`, all from the Hetzner cluster since INFRA-175.
  It uses a put-only IAM user (`k3s-backup-uploader`), SSE-KMS and a SHA256 checksum. Objects are
  locked for 7 days (GOVERNANCE).
- A GitHub Actions probe at 04:17 UTC checks each backup prefix, each with its own size floor, and
  opens a `[backup-stale]` issue when one is stale.
- Restore: [`clusters/hetzner/data/RESTORE.md`](../clusters/hetzner/data/RESTORE.md), restore drill (steps 1-5) passed 2026-10-02 (INFRA-31).

Bucket, KMS, IAM boundaries and owner steps: [`terraform/tbd-backups/README.md`](../terraform/tbd-backups/README.md).
Job: [`clusters/hetzner/data/db-backup.yaml`](../clusters/hetzner/data/db-backup.yaml).

## Access

kubectl and the flux CLI reach the cluster over NetBird only (Flux in the cluster pulls from public
GitHub over HTTPS). A node is the NetBird peer, and the NetBird
policy allows owner devices to reach TCP 6443 and nothing else. Authentication uses tokens of the
ServiceAccount `netbird/owner-admin`. Manifest: [`clusters/hetzner/netbird/`](../clusters/hetzner/netbird/). SSO through
NetBird's API server proxy is being evaluated (INFRA-71).

## Mail

Mailgun HTTP API (SDK `mailgun-python`), EU region, for every app and environment; no SMTP and no mail server in the
cluster. Dev and staging environments of every app share the sending domain `m.fjconsulting.dev`, each with its own
send-only sending key in its own Secret; production gets `m.<appdomain>` per app. DNS for both is in
`terraform/cloudflare`. Procedures: [runbooks.md](runbooks.md#mail-mailgun).

## Telemetry

One standard for every app and environment (decided in INFRA-84; adoption tickets INFRA-105 for TBD and INFRA-106 for
Ziftbook). Each app owns its OpenTelemetry SDK setup and every span; FastAPI's native telemetry supplies only the
HTTP metrics.

| Signal | How | Library |
|---|---|---|
| Traces | The app's own `tracing.py` (the Ziftbook pattern): the HTTP SERVER span in a middleware, SQL spans from SQLAlchemy engine events, job, worker and migration spans | `opentelemetry-sdk` and the OTLP HTTP exporter, no contrib instrumentation packages |
| Metrics | FastAPI native: `http.server.request.duration` and `http.server.active_requests` | the app registers a global `MeterProvider` before `FastAPI()` is built |
| Logs | JSON lines on stdout with `request_id`, `trace_id`, `span_id`; no OTLP log export from the app. Alloy reads Ziftbook's lines through the kube API and sends the JSON ones to Grafana Cloud Loki (ZIF-89, 14-day retention). | stdlib `logging` (the Ziftbook `logs.py` pattern) |

Every app builds FastAPI with:

```python
FastAPI(telemetry={"tracing": False, "metrics": True, "logs": False, "operation_spans": False,
                   "auto_configure": False, "exclude": lambda scope: scope["path"] in HEALTH_PATHS})
```

Rules every app keeps (traces and logs alike):

- **Attributes are an allowlist.** The SERVER span carries `http.request.method`, `http.route`,
  `http.response.status_code` and `error.type` only. Never `url.path` or `url.query`: they carry invite tokens, OAuth
  codes and customer emails. SQL spans carry the statement, never its parameters, and values are always bound, never
  formatted into SQL text.
- **No exception messages.** `record_exception=False` on every span; an error is its class (plus SQLSTATE and
  constraint for a database error) and its frames, never `str(error)`, which can quote an email. The same holds for log
  records, including the `sys` and `threading` excepthooks.
- **Access logs carry no path, query or client IP:** method, route template, status, duration and the correlation
  ids. uvicorn's own access line (raw path with query, client IP) stays disabled.
- **`traceparent` only.** The app extracts and injects with a hard-coded `TraceContextTextMapPropagator`, reading only
  the `traceparent` header; never baggage or `tracestate`, which are client-controlled text that would ride into the
  jobs table. `OTEL_PROPAGATORS=tracecontext` is a backstop for the global propagator, not the guarantee.
- **Native tracing, logs and operation spans stay off.** Native tracing always exports the request path and query
  (only five cloud-signature parameters redacted), and native logs export exception messages. `auto_configure` stays
  off because it adds a second exporter to a provider the app already configured.

Environment for every process (`<role>` is `api`, `worker`, `scheduler` or `migrations`):

| Variable | Value |
|---|---|
| `OTEL_SERVICE_NAME` | `<app>-<role>` |
| `OTEL_RESOURCE_ATTRIBUTES` | `service.namespace=<app>,deployment.environment.name=<prod or staging>,service.version=<version>` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | the in-cluster collector (INFRA-85); `http://127.0.0.1:4318` for the local LGTM stack |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` |
| `OTEL_PROPAGATORS` | `tracecontext` |
| `OTEL_METRIC_EXPORT_INTERVAL` | `60000` |
| `OTEL_LOGS_EXPORTER` | `none` |
| `OTEL_METRICS_EXPORTER` | `otlp` |
| `OTEL_TRACES_EXPORTER` | `otlp` in staging; production default: see the namespace `otel` ConfigMap |

Traces are a per-environment switch, not an app setting: the app ships the capability and exports only when
`OTEL_TRACES_EXPORTER=otlp`, and the namespace's `otel` ConfigMap holds the value. To investigate a production issue,
set it to `otlp` in `clusters/hetzner/<app>-prod/otel.yaml` (optionally `OTEL_TRACES_SAMPLER=parentbased_traceidratio`
with `OTEL_TRACES_SAMPLER_ARG`), merge, restart the deployments that read it (Ziftbook: backend and worker; TBD: backend and scheduler); set it back to `none` afterwards.

Export path (built by INFRA-85): apps send OTLP over HTTP to one Grafana Alloy DaemonSet in the cluster, which
forwards metrics and traces to the Grafana Cloud OTLP gateway, and scrapes the
node and cAdvisor (`/metrics/cadvisor` only: k3s kubelet `/metrics` carries the whole control plane, about 58k series).
Tailing pod stdout into Grafana Cloud Logs comes later, under the rules below. One collector, so the Grafana Cloud
credentials (a write-only access policy token for an EU stack) live in one SOPS Secret. A DaemonSet never runs two
copies during a rollout. Not the `k8s-monitoring` Helm chart, which deploys several Alloys plus node-exporter and
kube-state-metrics. Measured by INFRA-85 against the real cAdvisor payload: 52 to 100 MiB working set (request 128Mi,
limit 256Mi).

The collector is the second line of defense, not the first:

- It tails logs only from an allowlist of app namespaces, and from each app only after that app has adopted this
  standard (TBD after INFRA-105, which disables its raw access line). Never `data`: Postgres logs row values on
  constraint errors.
- It deletes `url.path`, `url.query`, `url.full`, `client.address`, `http.request.header.*` and `exception.message`
  from every span before export.
- Grafana Labs becomes a sub-processor for whatever reaches it; the apps' privacy documents must name it.

Known cost: the SERVER span lives in an `@app.middleware("http")` middleware, which ends it before FastAPI records the
duration metric, so histogram exemplars do not link to traces (a pure ASGI middleware would fix it). Revisit native
tracing when FastAPI can leave out the path and query.

## Secrets

Kubernetes Secrets live in git as `*.secret.yaml`, SOPS-encrypted to the cluster's age key, and
Flux decrypts them in the cluster. CI fails on any unencrypted Secret. Terraform secrets are
sensitive HCP Terraform variables. Procedures never display secret values: see
[README.md, Kubernetes secrets](../README.md#kubernetes-secrets) and
[runbooks.md](runbooks.md#write-or-rotate-a-kubernetes-secret) (a new file needs only the public key).

## Infrastructure as code

- **Terraform:** every `terraform/<stack>/` is an HCP Terraform workspace with VCS flow: plan on
  the PR, apply on merge after approval in the TFC UI. The stacks are `hetzner` (servers), `cloudflare` (DNS, zone settings), `tbd-backups` (backup
  chain) and `netbird`, among others. `tbd-backups` assumes OIDC roles whose policies root mints once from [`aws/bootstrap/`](../aws/bootstrap/), so that
  workspace never manages its own roles. `cloudflare` uses an API token.
- **Kubernetes:** Flux (source and kustomize controllers) applies `clusters/hetzner` from `main`.
- **CI** ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)): `terraform fmt` and validate,
  tflint, the tbd-backups fences check, kubeconform, the SOPS check and the Python tests.

## Monitoring and alerts

| Signal | Source | Goes to |
|---|---|---|
| Stale backup | freshness probe | GitHub issue `[backup-stale]` |

Node memory, node disk, log-retention, synthetic uptime and Ziftbook app alerts are Grafana Cloud rules (`grafana/`, [runbooks.md](runbooks.md)); INFRA-85 added the Alloy collector from [Telemetry](#telemetry).

## Cost

AWS holds the backup chain (`terraform/tbd-backups`). The servers are billed by Hetzner.
