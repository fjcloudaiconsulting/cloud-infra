# Runbooks

Step-by-step procedures for the recurring changes. Why things are built this way is in
[architecture.md](architecture.md); every setting made outside git, and what breaks when it is wrong, is in
[configuration-map.md](configuration-map.md). Every command below uses the owner's kubeconfig
(`~/.kube/hetzner`, over NetBird) and never prints a secret value.

Database restores are
[`clusters/hetzner/data/RESTORE.md`](../clusters/hetzner/data/RESTORE.md).

## Follow Flux and rollouts

One GitRepository and one Kustomization, both named `flux-system`, apply all of `clusters/hetzner` from `main`.
Flux polls git every minute and re-applies at least every 10 minutes. In Argo CD terms: the Kustomization is the
only Application, `reconcile` is Sync, `suspend` is turning auto-sync off. Flux has no UI here; the CLI is the view.

```bash
export KUBECONFIG=~/.kube/hetzner

# Which commit is live: both lines show main@sha1:<merge commit>, READY True.
flux get sources git
flux get kustomizations
# Apply a merge now instead of waiting for the poll (source first, then the apply).
flux reconcile kustomization flux-system --with-source
# What the last apply changed ("Deployment/tbd-prod/scheduler configured") and any failure, newest last.
flux events --for Kustomization/flux-system
flux logs --level=error --since=1h
# Every object Flux manages (pruning removes what leaves git).
flux tree kustomization flux-system

# Rollouts: Flux only applies the manifest; it does not wait for pods (no healthChecks), so READY True can hide a
# crashing pod. Check the workload itself. tbd-prod runs on the Hetzner cluster since INFRA-175: use
# KUBECONFIG=~/.kube/hetzner for the kubectl lines below (and flux's `--context` there).
kubectl -n tbd-prod rollout status deploy/backend --timeout=5m
kubectl -n tbd-prod get deploy,pods                       # READY 2/2 (scheduler 1/1), RESTARTS 0
kubectl -n tbd-prod get events --sort-by=.lastTimestamp | tail -20
kubectl -n tbd-prod logs deploy/backend -c migrate        # init container: migrations
kubectl -n tbd-prod logs deploy/backend -f --since=10m    # app log, follow
kubectl -n tbd-prod get deploy -o jsonpath='{..image}' | tr ' ' '\n' | sort -u   # images actually running

# Hold Flux during manual work (it would revert live edits), then hand back. Resume re-applies git at once.
flux suspend kustomization flux-system
flux resume kustomization flux-system
```

Swap the namespace for `tbd-staging`, `ziftbook-staging` or `data` (StatefulSets: `kubectl -n data rollout status sts/mysql`). A
release reaches the cluster only through a PR that changes an image tag under `clusters/`; there is no image
automation.

## Promote a TBD release (staging, then production)

A TBD release reaches `tbd-staging` by itself (INFRA-91): Renovate pushes `renovate/tbd-staging-tbd-staging-images`,
CI runs on it and Renovate fast-forwards it into `main`; Flux applies it. Production is the separate `tbd images` PR.

1. After the release run's `promote` job is green, wait for the staging bump on `main`
   (`git log origin/main -3 -- clusters/*/tbd-staging`). Tick "run again" on the Dependency Dashboard to force it.
2. Check staging runs the tag: `curl -s https://dev.thebetterdecision.com/health` reports the new version.
3. Merge the `tbd images` PR for `tbd-prod` once `Kubernetes checks` is green: its step `prod tags not ahead of staging` (INFRA-89) fails while the PR's tags are newer than `tbd-staging` on its tree. Update its branch if it is behind. The bound is the lowest `tbd-staging` tag, MCP image included (INFRA-147): if mcp did not move with the release, fix the tbd promote first.
4. A release broken on staging is fixed forward: do not merge the prod PR, release the fix; a newer tag replaces the PR.

## Post-deploy smoke

Every push to `main` that changes `clusters/*/tbd-prod/`, `clusters/*/tbd-staging/`, `clusters/*/ziftbook-staging/` or `clusters/*/ziftbook-prod/` runs the
**Post-deploy Smoke** workflow (INFRA-114) for the namespaces it touched. It runs on GitHub, outside the cluster, and
reaches the apps through Cloudflare like a user. Per namespace:

1. **Converge:** reads the backend tag from `clusters/<cluster>/<namespace>/backend.yaml` and polls the public version
   endpoint every 15 s until it reports that version, for up to 15 minutes. TBD uses `/health`. Ziftbook uses
   `dev.ziftbook.com/api/healthz` (staging) and `app.ziftbook.com/api/healthz` (prod, INFRA-90), which goes through the frontend to the backend. The Deployments use `Recreate`,
   which removes every old pod before a new one starts, so a match means no old backend pod is serving. If the first poll already matches (the push changed
   a policy, a Secret or the frontend only), the run waits 150 s for Flux to apply the push before the next checks.
2. **Frontend:** `GET /` must return 200 three times in a row, 10 s apart. It gives up after 5 minutes without that. `tbd-staging` sits behind [Cloudflare Access](#cloudflare-access-on-tbd-staging) and checks `GET /robots.txt` (the frontend serves it, Access bypasses it) without following redirects, since a followed login redirect is a 200 page.
3. **App smoke:** TBD runs its own `scripts/smoke-test.sh`, fetched from the tbd repo at the deployed tag, once: three
   health reads, one login as the [smoke account](#tbd-smoke-account) and one authenticated read. No user data changes
   (the login adds a session and an audit row). It is never retried, so the login rate limit (10 a minute) is never
   reached. Ziftbook runs its own `scripts/smoke-test.sh` the same way, for `ziftbook-staging` and `ziftbook-prod`: two
   read-only GETs, `/api/healthz` and `/api/health/dependencies` (503 when Postgres is unreachable), no login.
   `tbd-staging` has no smoke account, so it stops after step 2: the version wait and the frontend check, no app smoke.

Any failure opens the issue `[post-deploy-smoke] <namespace>`, or comments on it if it is already open. The next full
pass closes it. The failure says which step failed:

| Verdict | Meaning | Look at |
|---|---|---|
| `never converged (live X, expected Y)` | The new backend never served: Flux did not apply, the image did not pull, the migration or the pod crashed. `live none` means the app was down | [Follow Flux and rollouts](#follow-flux-and-rollouts): `flux get kustomizations`, `kubectl -n <namespace> get pods,events` |
| `frontend GET / returned N` (`/robots.txt` for `tbd-staging`) | The backend serves, the frontend does not | `kubectl -n <namespace> get pods -l app=frontend`, its logs |
| `app smoke failed` | The app's own checks failed. The run log shows which one. Also shown when the `tbd-prod` environment secrets are missing (the script exits 2 before logging in) | The run log, then `/health/dependencies` (Ziftbook: `/api/health/dependencies`) and the backend logs |
| `could not fetch ...` | The GitHub API did not return the app's smoke script at that tag | The tag exists in the app repo, then re-run |
| `bad backend tag` | The manifest has no plain `vX.Y.Z` backend tag | The manifest |

Run it again, for example after a fix outside git: Actions > Post-deploy Smoke > Run workflow > branch `main`, choose the
namespace. `gh workflow run post-deploy-smoke.yml -f namespace=tbd-prod` does the same. Only `main` may use the
`tbd-prod` environment, so a run from another branch fails before it starts.

What it does not cover: the TBD scheduler and the Ziftbook worker have no public endpoint. They run the backend image
and tag, so a tag that does not pull still shows as `never converged`. The TBD staging MCP server (INFRA-147) runs its
own image and is not checked: look at `kubectl -n tbd-staging get pods -l app=mcp` after a bump. A GitHub Actions
outage means no smoke; the [synthetic uptime](#synthetic-uptime) check and the daily release drift probe still run.

The smoke account's credentials are the `tbd-prod` environment secrets `SMOKE_USERNAME` and `SMOKE_PASSWORD` in this
repo. Deployments from `main` only can read them. Rotating them is part of [TBD smoke account](#tbd-smoke-account).

## Write or rotate a Kubernetes Secret

Secrets are `clusters/**/<name>.secret.yaml`, SOPS-encrypted to the cluster's age key (`.sops.yaml`). Flux decrypts
them in the cluster. Two rules decide how to work with one:

- **Encrypting a new file needs only the public key**, which is in `.sops.yaml`. This works on any laptop.
- **Editing an existing file needs the private key**, which is kept offline. Without it, `sops` fails with
  `Failed to get the data key required to decrypt the SOPS file`.

So put each value that is written or rotated on its own (an API key, a certificate) in **its own Secret file**, and
regenerate the whole file to change it. Example, a key read from a silent prompt (it stays out of shell history and
the screen):

```sh
F=clusters/hetzner/<dir>/<name>.secret.yaml   # the path must match .sops.yaml
read -rs K; printf %s "$K" | kubectl create secret generic <name> -n <namespace> \
  --from-file=<key>=/dev/stdin --dry-run=client -o yaml \
  | sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F"; unset K
grep -c '<key>: ENC' "$F"   # must print 1; 0 means not encrypted: do not commit
```

Commit, push, merge. Flux applies it within a few minutes. A pod reads env at start, so after a key change run
`kubectl -n <namespace> rollout restart deploy/<name>`. To edit a multi-key Secret instead, load the offline key
first (`export SOPS_AGE_KEY_FILE=<path to the key file>`), then `sops edit <file>`.

## Ziftbook production secrets

Three files, none typed by hand. Production has its own Postgres login roles (`ziftbook_prod_app`,
`ziftbook_prod_migrate`, see `data/postgres-bootstrap-prod.yaml`), so `data/postgres-ziftbook-prod` (their passwords, read
by the bootstrap Job) and `ziftbook-prod/ziftbook` (the two connection URLs) are generated together, from fresh random
passwords, with the public key only. Run from the repo root in bash; nothing is printed except the two counts (3 each:
two values and the MAC):

```sh
set -euo pipefail
PM=$(openssl rand -hex 32); PA=$(openssl rand -hex 32); H=postgres.data.svc.cluster.local
enc() { sops encrypt --filename-override "$1" --input-type yaml --output-type yaml /dev/stdin > "$1.tmp" && mv "$1.tmp" "$1"; }
printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: postgres-ziftbook-prod\n  namespace: data\ntype: Opaque\nstringData:\n  migrate-password: %s\n  app-password: %s\n' "$PM" "$PA" \
  | enc clusters/hetzner/data/postgres-ziftbook-prod.secret.yaml
printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: ziftbook\n  namespace: ziftbook-prod\ntype: Opaque\nstringData:\n  database-url: postgresql+psycopg://ziftbook_prod_app:%s@%s:5432/ziftbook_prod\n  migrate-database-url: postgresql+psycopg://ziftbook_prod_migrate:%s@%s:5432/ziftbook_prod\n' "$PA" "$H" "$PM" "$H" \
  | enc clusters/hetzner/ziftbook-prod/ziftbook.secret.yaml
unset PM PA
grep -c 'ENC\[' clusters/hetzner/data/postgres-ziftbook-prod.secret.yaml clusters/hetzner/ziftbook-prod/ziftbook.secret.yaml
```

To rotate, run it again and commit both files; Flux does not rerun the bootstrap Job for a Secret change: delete it
(`kubectl -n data delete job ziftbook-prod-bootstrap`, Flux recreates it), then restart `deploy/backend` and
`deploy/worker` in `ziftbook-prod`.

The namespace's `ghcr-pull` is staging's credential, which only the offline key can read, so it is derived from the
staging file (owner, needs `SOPS_AGE_KEY_FILE`); it prints `1`:

```sh
set -euo pipefail
export SOPS_AGE_KEY_FILE=<path to the key file>
G=clusters/hetzner/namespaces/ziftbook-prod-ghcr-pull.secret.yaml
sops -d clusters/hetzner/namespaces/ziftbook-staging-ghcr-pull.secret.yaml \
  | sed -E 's#^( *namespace:) ziftbook-staging$#\1 ziftbook-prod#' \
  | sops encrypt --filename-override "$G" --input-type yaml --output-type yaml /dev/stdin > "$G.tmp" && mv "$G.tmp" "$G"
sops -d "$G" | grep -c 'namespace: ziftbook-prod$'
```

Until the pull secret is merged the production pods wait (`ImagePullBackOff`) and start by themselves once Flux applies it.

## Ziftbook admin TOTP key

Since v0.25.0 the backend encrypts each platform admin's TOTP secret with `ZIF_TOTP_KEY` (ZIF-71): base64 of 32 random
bytes, one per environment, in Secret `ziftbook-totp` (key `key`). Without it the API starts, logs the ERROR `totp key
unset: admin second factor unavailable`, and admin TOTP answers 503 (recovery codes still work). Both environments
have one (INFRA-195). Generated, never typed, with the public key only, in bash from the repo root; nothing is printed
except `1`. It refuses to overwrite an existing file:

```sh
env=staging   # or prod
F=clusters/hetzner/ziftbook-$env/ziftbook-totp.secret.yaml
K=$(openssl rand -base64 32) && [ ${#K} -eq 44 ] && test ! -e "$F" \
  && kubectl create secret generic ziftbook-totp -n ziftbook-$env \
       --from-file=key=<(printf %s "$K") --dry-run=client -o yaml \
  | sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F.tmp" \
  && mv "$F.tmp" "$F" && grep -cE '^ +key: ENC' "$F"
rm -f "$F.tmp"; unset K
```

Rotate only if the key leaks: a new key makes every enrolled second factor unreadable, so each admin enrols again
(recovery codes still sign in). The SOPS file in git is the backup, readable with the age key like the database
credentials; the live Secret in the namespace is a second copy. Only the backend reads it: the worker and the admin
CLI (`python -m app.admin enrol`) do not.

## Ziftbook first-run setup (production)

A Ziftbook database with no user redirects every page to `/setup` and refuses sign-up (409 `setup_required`), and the
backend logs the ERROR `setup token unset: first-run setup refused` while it has no token (ZIF-70, since v0.25.0).
`/setup` creates the first platform admin once, against `ZIF_SETUP_TOKEN` (backend only, Secret `ziftbook-setup`, key
`token`, at least 32 characters; shorter counts as unset). It works only while there is no user, so the token is
single-use; staging is already set up.

1. Write the Secret on a branch, generated, never typed, with the public key only, in bash from the repo root. Nothing
   is printed except `1`; it refuses to overwrite an existing file. Open the PR and merge it. The backend reads the
   token at start: if the Secret arrives without a manifest change, `kubectl -n ziftbook-prod rollout restart deploy/backend`:

   ```sh
   F=clusters/hetzner/ziftbook-prod/ziftbook-setup.secret.yaml
   T=$(openssl rand -base64 32) && [ ${#T} -eq 44 ] && test ! -e "$F" \
     && kubectl create secret generic ziftbook-setup -n ziftbook-prod \
          --from-file=token=<(printf %s "$T") --dry-run=client -o yaml \
     | sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F.tmp" \
     && mv "$F.tmp" "$F" && grep -cE '^ +token: ENC' "$F"
   rm -f "$F.tmp"; unset T
   ```

2. After the rollout, check `kubectl -n ziftbook-prod logs -l app=backend -c backend --tail=-1 | grep -c 'setup token unset'`
   prints `0` on the new pods. Then put the token on the clipboard without showing it, open https://app.ziftbook.com/setup,
   paste it and create the admin account (email, name, password; TOTP enrolment follows, it uses `ziftbook-totp`):

   ```sh
   kubectl -n ziftbook-prod get secret ziftbook-setup -o jsonpath='{.data.token}' | base64 -d | pbcopy
   ```

3. Delete `clusters/hetzner/ziftbook-prod/ziftbook-setup.secret.yaml` in a PR. Flux prunes the Secret; the backend
   keeps the old value in its environment until the next restart, which is harmless once a user exists.

## MySQL database per app environment

Each app environment gets its own database and user in the shared MySQL (`data/mysql-0`), with grants on that
database only. The image creates `tbd` and `tbd_app` on an empty volume only, so any later database is created by
hand as root, which can log in only over the pod's socket. The SQL is additive and idempotent: a rerun creates
nothing twice and resets the password and connection cap. TBD staging (INFRA-67) is the example; for another
environment change the names, the cap and the Secret.

The password is the one in the app's `database-url` (hex, so it needs no escaping). Read it from the SOPS file with
the offline key loaded, or after the merge from the cluster
(`kubectl -n tbd-staging get secret tbd -o jsonpath='{.data.database-url}' | base64 -d` in place of the `sops` call).
Run from the repo root in bash or zsh (no comments inside the commands: an interactive zsh does not treat `#` as
one). The first block prints `exit 0` and nothing else; `exit 1` alone means no password was extracted and nothing ran.

```sh
PW=$(sops -d --extract '["stringData"]["database-url"]' clusters/hetzner/tbd-staging/tbd.secret.yaml \
  | sed -E 's#^mysql\+aiomysql://tbd_staging:([0-9a-f]+)@.*#\1#')
[[ $PW =~ ^[0-9a-f]{64}$ ]] && sed "s/@PW@/$PW/g" <<'SQL' | kubectl -n data exec -i mysql-0 -- sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot'
CREATE DATABASE IF NOT EXISTS tbd_staging;
CREATE USER IF NOT EXISTS 'tbd_staging'@'%' IDENTIFIED BY '@PW@';
ALTER USER 'tbd_staging'@'%' IDENTIFIED BY '@PW@' WITH MAX_USER_CONNECTIONS 20;
GRANT ALL PRIVILEGES ON tbd_staging.* TO 'tbd_staging'@'%';
SQL
echo "exit $?"; unset PW
kubectl -n data exec mysql-0 -- sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -r -e "SHOW GRANTS FOR tbd_staging"'
```

The check prints exactly:

```text
GRANT USAGE ON *.* TO `tbd_staging`@`%`
GRANT ALL PRIVILEGES ON `tbd_staging`.* TO `tbd_staging`@`%`
```

In a database grant `_` is still a (deprecated) wildcard, so `tbd_staging` also matches `tbd` + any one character +
`staging`: no other database has such a name. It is left unescaped on purpose: the nightly grants dump would double an
escaping backslash, and the replayed grant would name a different database. The password reaches
`mysql` on stdin, so it is never on a command line in the pod (locally it is briefly in `sed`'s). The cap (20) keeps staging from
using up `max_connections` (120) that production needs. The backend (15 + 7) and mcp (9, `tbd-staging/mcp.yaml`)
add up to 31 at full pools: under load staging hits its own cap, never production's. Nothing here touches `tbd` or its users, and the MySQL pod
does not restart. The nightly grants dump includes the new user, so a restore brings it back.

**First account.** TBD makes the first user of an empty database a superadmin with a verified email, and skips the
captcha for it. Register it yourself before the hostname is reachable (before approving the `cloudflare` apply that
adds the record), through a port-forward. Never empty the database while the host is live: remove the IngressRoute
in git first, register again, then restore it.
The three `read` lines take the username, the email and the password (silent); the `curl` prints `201`.

```sh
kubectl -n tbd-staging port-forward svc/backend 18000:8000 &
read -r U
read -r E
read -rs P
P="$P" jq -n --arg u "$U" --arg e "$E" '{username:$u,email:$e,password:env.P}' \
  | curl -s -o /dev/null -w '%{http_code}\n' -H 'content-type: application/json' --data @- http://127.0.0.1:18000/api/v1/auth/register
unset P; kill %1
```

## Add a public hostname for an app

Traffic path: Cloudflare (proxied), then the Cloudflare Tunnel `hetzner-k3s` (`cloudflared` in `clusters/hetzner/cloudflared`), then the app Service.

1. **DNS:** add a proxied CNAME to `<tunnel id>.cfargotunnel.com` in [`terraform/cloudflare/main.tf`](../terraform/cloudflare/main.tf) (the TBD `app` and `dev` records are entries of one map there; Ziftbook's are separate resources, copy the one for your zone).
2. **Route:** the hostname -> Service rule lives in the tunnel config in [`terraform/cloudflare/tunnel.tf`](../terraform/cloudflare/tunnel.tf); `cloudflared` only runs it.
3. **Also:** a staging `dev.*` host needs a Cloudflare Access app ([`terraform/cloudflare/access.tf`](../terraform/cloudflare/access.tf), INFRA-117) or it is public; a new zone must join `local.app_zones` in `main.tf` (zone settings and rate limit) and get a policy in [`terraform/cloudflare-tokens/main.tf`](../terraform/cloudflare-tokens/main.tf) first, or the `cloudflare` apply fails; the app namespace's NetworkPolicy must admit the `cloudflared` namespace (model: `clusters/hetzner/tbd-prod/networkpolicy.yaml`).
4. **Check** after the `cloudflare` workspace apply: `curl -sI https://<host> | head -1`.

## Mail (Mailgun)

Every app sends through the Mailgun HTTP API with the official SDK (`mailgun-python`), in the EU region. No SMTP,
and no in-cluster mail server.

| Environment | Sending domain | DNS |
|---|---|---|
| Every dev/staging environment of every app | `m.fjconsulting.dev`, shared | `fjdev_mail` in `terraform/cloudflare` |
| Production | `m.<appdomain>`, one per app (TBD: `m.thebetterdecision.com`; Ziftbook: `m.ziftbook.com`, created 2026-10-06, wired in `ziftbook-prod/worker.yaml`, INFRA-139) | the app zone's records in `terraform/cloudflare` |

Each environment has its **own sending key**: send-only, scoped to the domain, and revoked on its own if it leaks.
A domain's record set is MX `mxa`/`mxb.eu.mailgun.org`, SPF `include:mailgun.org`, DKIM TXT, DMARC with Mailgun
reporting, and the tracking CNAME `email.m` to `eu.mailgun.org`.

**Add a dev environment** (for example TBD staging). The domain already exists, so only two steps are needed:

1. Mailgun > Sending > Domains > `m.fjconsulting.dev` > Sending API keys > Add sending key, with the environment
   name as the description.
2. Write the key to a new Secret `<app>-mailgun` (key `api-key`) in the environment's namespace, as in the Secret
   section. Then wire the app's env to it, with the app's own setting names: Ziftbook uses `ZIF_MAILGUN_DOMAIN`,
   `ZIF_MAILGUN_REGION` (eu) and `ZIF_MAILGUN_API_KEY` from `secretKeyRef ziftbook-mailgun/api-key` (see
   [`clusters/hetzner/ziftbook-staging/worker.yaml`](../clusters/hetzner/ziftbook-staging/worker.yaml)).

**Add a production domain:**

1. Add the domain in Mailgun (EU).
2. Copy its records into `terraform/cloudflare` (the TBD block is the model).
3. Merge and apply, then verify the domain in Mailgun.

If Mailgun's "automatic setup" for Cloudflare was used instead, the records already exist outside Terraform. Adopt
them with `import` blocks (id `<zone_id>/<record_id>`, from the Cloudflare API) rather than deleting them, as
INFRA-47 did. Once the apply has adopted them, remove the blocks in a follow-up PR (its plan must show no changes).

The domain `m.fjconsulting.dev` is shared, so Mailgun webhooks on it deliver every dev app's events to every
registered URL. Each app tags its messages and ignores events for messages it did not send.

**Check:**

```sh
# Use DoH: local dig can be intercepted (NetBird DNS) and answer empty even when the records exist.
curl -s -H 'accept: application/dns-json' 'https://cloudflare-dns.com/dns-query?name=m.fjconsulting.dev&type=MX'   # mxa/mxb.eu.mailgun.org
kubectl -n <namespace> exec deploy/<name> -- sh -c 'test -n "$<APP>_MAILGUN_API_KEY" && echo set'
```

Then check that the Mailgun dashboard shows the domain as verified. A lookup made before the records existed is
cached as "no record" for up to 30 minutes (the zone's SOA minimum), so Mailgun can show MX unverified for that
long after the apply. Press Verify again later; nothing needs changing.

## Ziftbook worker heartbeat (Healthchecks.io)

The Ziftbook worker GETs a Healthchecks.io ping URL after every good loop, about every 30 s plus the time it spends on
due jobs (ZIF-21). A loop that fails (Postgres down, a failing prune or sweep) sends no ping, so the check alerts on
a worker that runs but cannot work, not only on a dead one. A failed ping logs one WARNING `healthcheck ping failed`
and the worker carries on. One check per environment, in the owner's Healthchecks.io account:

1. Healthchecks.io > Add Check, name `ziftbook-staging-worker` or `ziftbook-prod-worker`, Simple schedule, period
   1 minute, grace 5 minutes. A rollout or a long batch of jobs leaves a gap of a minute or two; the grace absorbs it.
   Alerts go to the account's integrations (email by default).
2. Copy the check's full ping URL (with `https://`; the worker cannot ping without the scheme) into
   Secret `ziftbook-healthcheck`, key `url`, as in
   [Write or rotate a Kubernetes Secret](#write-or-rotate-a-kubernetes-secret), with
   `F=clusters/hetzner/ziftbook-<env>/ziftbook-healthcheck.secret.yaml`. The URL is the credential: whoever has it
   can keep the check green, so it never goes in plain text.
3. After Flux applies it, `kubectl -n ziftbook-<env> rollout restart deploy/worker` (env is read at start). The
   check turns up within a minute.

The variable is optional in `worker.yaml`: without the Secret the worker runs and sends nothing, and nothing says so,
so a check that stays "new" means the Secret is missing. Rotate: a check's ping URL cannot be changed, so create a
new check with the same settings, redo steps 2 and 3, then delete the old check.

Alert drill (staging). Suspend Flux first: its reconcile (every 10 minutes, and on every merge) restores
`replicas: 1` and can land before the alert does.

```sh
export KUBECONFIG=~/.kube/hetzner
flux suspend kustomization flux-system
kubectl -n ziftbook-staging scale deploy/worker --replicas=0
# wait for the alert: period plus grace, about 6 minutes
flux resume kustomization flux-system   # re-applies git at once, replicas back to 1
```

The check recovers at the worker's first ping, a minute or so after the pod starts.

## R2 for Ziftbook photos

Service photos (ZIF-133) live in Cloudflare R2, EU jurisdiction, two buckets per environment, all in
`terraform/cloudflare/r2.tf`:

| Bucket | Access | Rules |
|---|---|---|
| `ziftbook-uploads-<env>` | private; the browser PUTs with a presigned URL | CORS `PUT` from `https://dev.ziftbook.com` (staging) or `https://app.ziftbook.com` (prod), header `content-type`; every object expires after 1 day |
| `ziftbook-media-<env>` | public read on `img-dev.ziftbook.com` (staging) or `img.ziftbook.com` (prod) only | none; the app sets `Cache-Control` on upload |

The S3 endpoint of an EU bucket is `https://<account id>.eu.r2.cloudflarestorage.com`, set as
`ZIF_S3_ENDPOINT` on backend and worker (the `.eu` matters: the default endpoint does not see these buckets).
Bucket names and the jurisdiction are fixed at creation.

One-time owner steps, in this order:

1. Cloudflare dashboard > R2 Object Storage: enable R2 on the account (it asks for a payment method even on the
   free tier). The `cloudflare` workspace token needs "Workers R2 Storage Write" (`terraform/cloudflare-tokens`)
   applied too; without either, the plan passes and the `cloudflare` apply fails on every R2 resource. Then approve
   the `cloudflare` apply that creates the buckets.
2. R2 > Manage API tokens > Create Account API token, once per environment: name `ziftbook-<env>-storage`,
   permission **Object Read & Write**, "Apply to specific buckets only" with that environment's two buckets, TTL
   forever (or a rotation date you will keep). The secret is shown once.
3. Write both keys straight into Secret `ziftbook-storage` (keys `access-key-id`, `secret-access-key`), from silent
   prompts, in bash from the repo root. It prints `2`; anything else, or nothing, means the file was not written:

   ```sh
   env=staging   # then prod
   F=clusters/hetzner/ziftbook-$env/ziftbook-storage.secret.yaml
   printf 'Access Key ID: '; read -rs A; echo; printf 'Secret Access Key: '; read -rs S; echo
   kubectl create secret generic ziftbook-storage -n ziftbook-$env \
     --from-file=access-key-id=<(printf %s "$A") --from-file=secret-access-key=<(printf %s "$S") \
     --dry-run=client -o yaml \
     | sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F.tmp" \
     && mv "$F.tmp" "$F" && grep -cE '^ +(access-key-id|secret-access-key): ENC' "$F"
   rm -f "$F.tmp"; unset A S
   ```

Rotate: create a new token, redo step 3, merge, `kubectl -n ziftbook-<env> rollout restart deploy/backend
deploy/worker`, then delete the old token. Until the Secret exists, uploads answer 503 and nothing else
breaks.

## Ziftbook Turnstile (public bookings)

The Ziftbook API refuses every public booking (403) while it has no Turnstile secret, and logs ERROR `turnstile
secret unset: public bookings refused` at start (ZIF-116, fail closed). One widget per environment, outside Terraform like
TBD's:

1. Cloudflare dashboard > Turnstile > Add widget: name `ziftbook-staging`, hostname `dev.ziftbook.com` (prod:
   `ziftbook-prod`, `app.ziftbook.com`), mode Managed, no pre-clearance. Note its site key and secret key.
2. Write both into Secret `ziftbook-turnstile` (keys `site-key`, `secret`) from silent prompts, in bash from the
   repo root. It prints `2`; anything else, or nothing, means the file was not written:

   ```sh
   env=staging   # then prod
   F=clusters/hetzner/ziftbook-$env/ziftbook-turnstile.secret.yaml
   printf 'Site key: '; read -rs K; echo; printf 'Secret key: '; read -rs S; echo
   kubectl create secret generic ziftbook-turnstile -n ziftbook-$env \
     --from-file=site-key=<(printf %s "$K") --from-file=secret=<(printf %s "$S") \
     --dry-run=client -o yaml \
     | sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F.tmp" \
     && mv "$F.tmp" "$F" && grep -cE '^ +(site-key|secret): ENC' "$F"
   rm -f "$F.tmp"; unset K S
   ```

3. After the merge, `kubectl -n ziftbook-<env> rollout restart deploy/backend deploy/frontend` if the pods started
   before Flux applied the Secret (env is read at start). Check: the backend's `api started` line says
   `"turnstile": "on"` and the ERROR line is gone; then book once on the public page.

Rotate: Turnstile > the widget > Rotate secret key, then steps 2 and 3; the restart is required, since the pods
keep the old secret until they restart.

## TBD smoke account

TBD's post-deploy smoke test (`scripts/smoke-test.sh` in the tbd repo) logs in as a dedicated production user: active,
email verified, **no MFA** by design (TBD-371, the script cannot answer a TOTP challenge). Its username is also the only
entry of `FOUNDER_COUNT_EXCLUDE_USERNAMES`, so it is not counted as a founder.

The credentials live in these places, kept equal:

- `tbd-prod/tbd-smoke` (`clusters/hetzner/tbd-prod/tbd-smoke.secret.yaml`, SOPS), keys `username` and `password`. Anyone
  with cluster access reads them for a manual run (`KUBECONFIG=~/.kube/hetzner`):

  ```bash
  SU=$(kubectl -n tbd-prod get secret tbd-smoke -o jsonpath='{.data.username}' | base64 -d)
  SP=$(kubectl -n tbd-prod get secret tbd-smoke -o jsonpath='{.data.password}' | base64 -d)
  SMOKE_USERNAME=$SU SMOKE_PASSWORD=$SP SMOKE_BASE_URL=https://app.thebetterdecision.com ~/src/tbd/scripts/smoke-test.sh; unset SU SP
  ```

- cloud-infra environment `tbd-prod` secrets `SMOKE_USERNAME` / `SMOKE_PASSWORD` (write-only), used by the
  [post-deploy smoke](#post-deploy-smoke).

**Rotate** (never display the password; the hash is TBD's own `bcrypt`): generate it in memory, set the bcrypt hash on
the user row in the production database (`UPDATE users SET password_hash=..., password_changed_at=UTC_TIMESTAMP() WHERE
username=...`, expect 1 row), run the smoke test above with the new value, then `printf %s "$P" | gh secret set
SMOKE_PASSWORD --env tbd-prod -R
fjcloudaiconsulting/cloud-infra` and rewrite `tbd-smoke.secret.yaml` with
`jq -n ... | sops encrypt --input-type json --output-type yaml --filename-override <file> /dev/stdin > <file>`. Last
rotated 2026-10-04 (INFRA-48).

## Cloudflare Access on TBD staging

`dev.thebetterdecision.com` needs a login (INFRA-117): Cloudflare Access, email one-time PIN, allow-list = the owner
(`local.access_allow_emails` in `terraform/cloudflare/access.tf`), 24 h session. Only `GET /health` and `GET /robots.txt`
are open, for the [post-deploy smoke](#post-deploy-smoke), plus what an MCP client calls without a browser
(INFRA-147): `/mcp`, the two `/.well-known/oauth-*` documents (subpaths included) and `/api/v1/oauth/*`. The consent
page `/oauth/authorize` stays behind Access: the browser gets the PIN prompt, then the app login and step-up. Staging
is therefore not indexable. Every other path, `/api`
included, needs the session cookie; the frontend and API share the host, so the browser carries it. Google SSO is off
on staging, so there is no external callback to break. Staging mail links open only in a browser that has logged in.

**Prerequisites (owner, once):** Zero Trust on the account (Free plan, team name) and the One-time PIN login method
on. Terraform cannot do these: until then `cloudflare` plans fail with `access.api.error.not_enabled`.

**Apply order:** merge, apply `cloudflare-tokens` (adds the account-scoped "Access: Apps and Policies Write" to the
`cloudflare` workspace token), then apply `cloudflare`. The smoke's `/robots.txt` check answers 200 before and after,
so the order never breaks it.

**Add an allowed address:** append it to `access_allow_emails`, PR, apply. **Check:** `curl -sI https://dev.thebetterdecision.com/`
answers 302 to `*.cloudflareaccess.com`; `/health` and `/robots.txt` answer 200; `/api/` answers 302.
MCP paths: `curl -s https://dev.thebetterdecision.com/.well-known/oauth-protected-resource/mcp` returns the backend's
JSON, `curl -s -o /dev/null -w '%{http_code}\n' -X POST https://dev.thebetterdecision.com/mcp` answers 401 from the MCP
server (not 302), `curl -s -X POST https://dev.thebetterdecision.com/api/v1/oauth/token` answers 400 `invalid_request`
(not 302), and `/api/v1/auth/me` still answers 302.
**Lockout:** applications edited in the dashboard are drift that the next apply reverts; to open staging again, remove
`cloudflare_zero_trust_access_application.tbd_staging` in a PR.

## Cloudflare API tokens

Every account-owned Cloudflare token is in [`terraform/cloudflare-tokens`](../terraform/cloudflare-tokens/main.tf)
(INFRA-133), applied by workspace `cloudflare-tokens` after approval like every other stack. The workspace runs with
the bootstrap token (Account API Tokens: Edit), the only token made by hand and the only one it does not manage.

- **Change a scope:** edit the token's policies in a PR. The plan shows the policy diff; the apply updates the token in
  place and its value does not change, so no consumer needs touching. Keep one key per level in every `resources`
  object (one policy per zone): the provider compares the API's bytes, and a two-key object can come back reordered
  and fail the apply. Permission group ids: `GET /accounts/<id>/tokens/permission_groups` with the bootstrap token.
- **New token for a consumer Terraform can write** (an HCP Terraform variable today): add a
  `cloudflare_account_token` with `create_before_destroy` and the consumer resource reading its `.value`, as
  `cloudflare_workspace` does. Its value then never passes through a person.
- **Token made by hand** (or by another project): add it to `imported-tokens.json`.
  [`set-bootstrap-token.sh`](../terraform/cloudflare-tokens/set-bootstrap-token.sh) writes every such token in that
  shape to `~/Downloads/INFRA-133-imported-tokens.json` (`resources` as the exact compact string the API returns).
  The PR's plan must show the import and no change. Imported tokens have `prevent_destroy`, so dropping an entry makes
  the plan fail instead of deleting the token at Cloudflare. To stop managing one (Terraform rejects `removed` on a
  single `for_each` instance), use two PRs, each applied:
  1. Drop the entry, add a standalone `resource "cloudflare_account_token" "leaving"` with exactly the entry's values
     (same policy and permission group order, `resources` strings copied verbatim), and
     `moved { from = cloudflare_account_token.imported["<key>"] to = cloudflare_account_token.leaving }` (one argument
     per line). Plan: the move, 0 to change.
  2. Delete that resource and the `moved` block, add `removed { from = cloudflare_account_token.leaving }` with
     `lifecycle { destroy = false }` inside it. Plan: "will no longer be managed", 0 to destroy.

  An imported token with an expiry drops out of state once it expires and the next plan tries to create it with a past
  date: renew it (dashboard, then update `expires_on` in the file) or stop managing it as above before it expires.
- **Rotate a token Terraform made:** HCP Terraform > `cloudflare-tokens` > New run > Plan and apply, with "Replace
  resources" set to that token. The new token is created, the consumer rewritten, then the old one deleted. Then start
  a plan on the consumer's workspace to prove it.
- **Rotate an imported token:** its consumer is not in Terraform, so a replace would mint a value nothing receives.
  Wire the consumer first (a `tfe_variable`, or a GitHub secret resource with a credential to write it), or rotate
  it by hand in the dashboard (Roll) and set the new value at the consumer.
- **Rotate the team token** (`TFE_TOKEN`): HCP Terraform > Settings > Teams > `cloudflare-tokens` > Team API tokens >
  create a new one and delete the old, then store the new one as the sensitive env var `TFE_TOKEN` of workspace
  `cloudflare-tokens` (never through chat). It can only read runs and write the variables of workspace `cloudflare`.
- **Rotate the bootstrap token:** dashboard > Manage Account > Account API Tokens > the bootstrap > Roll, then
  `pbpaste | bash terraform/cloudflare-tokens/set-bootstrap-token.sh; pbcopy </dev/null` from the repo root (the
  clipboard is cleared even when the script fails).
- **Bootstrap token leaked:** its blast radius is the whole account. It can mint a token with any permission, widen
  any existing token, and roll any token (which hands out the new value), so treat every account token as exposed.
  1. Delete it in the dashboard (Manage Account > Account API Tokens), make a new one, store it with the script.
  2. Delete every account token not in `terraform/cloudflare-tokens` (tokens it minted survive its delete).
  3. Plan and apply `cloudflare-tokens`: policy drift on managed tokens shows up and is reverted.
  4. Replace-run every token Terraform made; Roll every imported one and set its value at its consumer.
  5. Read the account audit log for token creations, edits and rolls, and the zones' audit for what they did.

## Metrics to Grafana Cloud (Alloy)

One Grafana Alloy DaemonSet in `observability` (INFRA-85, design in [architecture.md](architecture.md), Telemetry)
sends metrics to the Grafana Cloud stack's OTLP gateway every 60 s:

- **Node:** CPU, memory, load, pressure, disk I/O, OOM kills (`job="node"`), from the host's `/proc` and `/sys` (recursive
  read-only mounts; the host's `/` is not mounted).
- **Pods and k3s:** per-container CPU, memory, throttling, OOM events and pod network from cAdvisor, plus the whole node
  (`id="/"`) and the k3s service (`id="/system.slice/k3s.service"`) (`job="cadvisor"`). Disk space is
  `container_fs_usage_bytes` / `container_fs_limit_bytes{id="/",device="/dev/root"}`; PVCs are local-path directories
  on that disk. Usage is against the full size, including ext4's reserved blocks (about 5%), so writes fail near 95%:
  alert at 85 to 90%. The kubelet's own `/metrics` is not scraped (about 58,000 control-plane series on k3s, which tripled
  Alloy's memory).
- **Apps:** whatever an app sends as OTLP/HTTP metrics or traces to `http://alloy.observability.svc:4318` (set
  `OTEL_EXPORTER_OTLP_ENDPOINT` to that; adoption is INFRA-105 for TBD and INFRA-106 for Ziftbook). Only the app namespaces
  may reach the port (`tbd-staging`, `tbd-prod`, `ziftbook-staging`, `ziftbook-prod`). Alloy deletes `url.*`, `http.request.header.*`, `client.address`,
  `exception.message`, `exception.stacktrace` and the old `http.url`, `http.target`, `http.client_ip`, `net.sock.peer.addr`
  from data point, span, span event, scope and resource attributes (by key only: span names and messages are the app's to keep clean).

Traces (Ziftbook staging, INFRA-106; TBD staging, INFRA-105) are forwarded too. Logs: only Ziftbook's (staging and prod, ZIF-89), read through the kube API (RBAC: pods and pods/log in those two namespaces only) and kept only when a line is a JSON object with `ts`, `level`, `logger` and `msg`; see [Find a request's logs](#find-a-requests-logs). Config: [`clusters/hetzner/observability/alloy/config.alloy`](../clusters/hetzner/observability/alloy/config.alloy);
a change there rolls the pod (the ConfigMap name carries a hash).

**Credentials** are Secret `observability/grafana-cloud` (keys `otlp-endpoint`, `instance-id`, `token`), file
`clusters/hetzner/observability/grafana-cloud.secret.yaml`. The token belongs to a Grafana Cloud access policy with the
`metrics:write`, `traces:write` and `logs:write` scopes (`traces:write` since INFRA-106, `logs:write` since ZIF-89; editing a policy's scopes applies to its existing tokens, so the Secret does not change). To write or rotate it, create a new token on that policy (Grafana Cloud > Administration >
Cloud access policies > the policy > Add token), regenerate the whole file (only the public key is needed), merge,
restart, then delete the old token. The plaintext only passes through a pipe, never a file:

```sh
F=clusters/hetzner/observability/grafana-cloud.secret.yaml
read -rs T; printf %s "$T" | kubectl create secret generic grafana-cloud -n observability \
  --from-literal=otlp-endpoint='<OTLP endpoint>' --from-literal=instance-id='<Instance ID>' \
  --from-file=token=/dev/stdin --dry-run=client -o yaml \
  | sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F"; unset T
grep -cE '(otlp-endpoint|instance-id|token): ENC\[' "$F"   # 3
# After the merge (env is read at start):
kubectl -n observability rollout restart ds/alloy
```

**Check** it is working:

```sh
kubectl -n observability get pods                 # alloy-xxxxx 1/1 Running; CreateContainerConfigError = Secret missing
kubectl -n observability logs ds/alloy --since=10m | grep -E 'level=(error|warn)'
# Nothing, or only "Failed to open directory, disabling udev device properties" once at start (harmless).
# A pod that never starts with a recursiveReadOnly error: the runtime lacks recursive read-only mounts.
# "401" or "Unauthorized" from otelcol.exporter.otlphttp: wrong token, instance ID or a revoked token.
kubectl -n observability top pod                  # memory: limit 256Mi
```

In Grafana Cloud, Explore with the Prometheus data source: `up` shows one series per job (`node`, `cadvisor`),
each 1. `sum by (namespace) (container_memory_working_set_bytes{container!=""})` shows memory per namespace.
For the Alloy UI (pipeline graph, component health): `kubectl -n observability port-forward ds/alloy 12345`, then
http://localhost:12345.

**Memory:** measured locally (Alloy v1.20.1, this config, the node's real cAdvisor payload, no app traffic yet) at
52 to 100 MiB working set: about 55 MiB anonymous memory plus up to about 45 MiB of the Alloy binary's page cache.
Request 128Mi (headroom for app metrics), limit 256Mi, `GOMEMLIMIT` 200MiB. Scraping the kubelet's `/metrics` as well took it to 160 MiB. If `kubectl top` shows it near the limit, look
for a new high-cardinality series in the app metrics before raising it.

## Drain or lose an app node

Since INFRA-177, losing one app node (node-1 or node-2) keeps both prod apps serving. Their backend and frontend run two
replicas, one per app node (required anti-affinity), with a PodDisruptionBudget `minAvailable: 1`; cloudflared does the
same, and CoreDNS holds two replicas with a PDB (`clusters/hetzner/kube-system/coredns.yaml`). Prod pods leave a lost node after
30 s. The TBD scheduler, the Ziftbook worker, Flux and metrics-server are single pods and move to the other node.
Staging keeps one replica and is down until its pods move.

**Access.** NetBird's state volume is local-path storage on one app node, so kubectl over NetBird stops while that node
is drained or down. That is accepted (INFRA-177): break-glass is SSH to any other server
(`ssh root@<public IPv4> k3s kubectl ...`, port 22 open to `ssh_allowed_cidrs` in `terraform/hetzner`), the same path
as [Hetzner cluster bootstrap](#hetzner-cluster-bootstrap). Run drains over SSH for that reason.

**Planned drain**, one app node at a time, from a different server. Kernel reboots use `ansible/reboot.yml`, which does
this for every node that needs it, db-1 last, and waits between nodes (ansible/README.md "Rolling reboot"; monthly, or
when a node shows `/var/run/reboot-required`). By hand:

```bash
ssh root@$N1 k3s kubectl drain node-2 --ignore-daemonsets --delete-emptydir-data --timeout=5m
# ... maintenance ...
ssh root@$N1 k3s kubectl uncordon node-2
```

While the node is cordoned, expect `Pending`: the evicted replica of each prod backend and frontend, the second CoreDNS
(its spread counts db-1 as an empty node) and NetBird when it lived there. They schedule onto the node when it is
uncordoned. Uncordon, and wait until `kubectl get deploy -A` shows every prod backend and frontend, cloudflared and
CoreDNS at 2/2, before draining the other app node. Draining it earlier still evicts every pod without a PDB (Flux, the
scheduler, the worker, staging) and then times out on the PDB-covered ones, leaving it cordoned: uncordon it.
Meanwhile `rollout status` times out and READY shows 1/2 for those Deployments; that is the cordon, not the release.

**Lost node** (NotReady, not planned):

1. For about 50 s (the node monitor grace period) requests to its pods still fail; then their endpoints drop and the
   other replicas serve. Nothing to do.
2. Check it in the Hetzner console. If it is up but cut off from the private network, power it off there: its
   cloudflared keeps taking tunnel requests it cannot serve.
3. Once it is off, `ssh root@<other node> k3s kubectl taint node <node> node.kubernetes.io/out-of-service=nodeshutdown:NoExecute`
   (node-1 or node-2 only: on db-1 it would keep MySQL and Postgres off their pinned volumes).
   This force-deletes its pods, which otherwise stay `Terminating`. Hold every prod image merge until then: a
   `Recreate` rollout waits for those pods and takes the app down meanwhile. Never set the taint on a node that is
   still running.
4. If a TBD backend crash-loops in `migrate` after about 10 minutes, a session from the lost node still holds the
   migration lock: find it in `SHOW PROCESSLIST` on `mysql-0`, check its `Host` is in the lost node's pod range
   (`k3s kubectl get node <node> -o jsonpath='{.spec.podCIDR}'`), never the new pod's running migration, and `KILL` it (Ziftbook:
   `pg_terminate_backend` on the holder in `pg_locks` where `locktype = 'advisory'`, matched the same way on
   `client_addr` in `pg_stat_activity`).
5. When the node is back, remove the taint (`... taint node <node> node.kubernetes.io/out-of-service-`); the Pending
   replicas schedule onto it.

## Node memory and the upsize

Memory is read from the kubelet, and Grafana Cloud alerts on it (INFRA-108, [Memory alerts](#memory-alerts) below). Run the check
below before every new workload and when an alert fires.

### Check

```bash
export KUBECONFIG=~/.kube/hetzner
N=$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
# available = node memory minus working set: what kubelet evictions use. rss leaves out page cache.
kubectl get --raw "/api/v1/nodes/$N/proxy/stats/summary" \
  | jq '.node.memory | {availableMi: (.availableBytes/1048576|floor), workingSetMi: (.workingSetBytes/1048576|floor), rssMi: (.rssBytes/1048576|floor)}'
kubectl describe node "$N" | sed -n '/Allocated resources/,/Events/p' | grep -E 'memory|Resource'   # requests, limits
kubectl get node "$N" -o jsonpath='MemoryPressure={.status.conditions[?(@.type=="MemoryPressure")].status}{"\n"}'   # False
# Containers killed for memory, and evicted pods: expect no output.
kubectl get pods -A -o json | jq -r '.items[] | select(any(.status.containerStatuses[]?; .lastState.terminated.reason == "OOMKilled" or .state.terminated.reason == "OOMKilled")) | "\(.metadata.namespace)/\(.metadata.name) OOMKilled"'
kubectl get pods -A --field-selector status.phase=Failed -o jsonpath='{range .items[?(@.status.reason=="Evicted")]}{.metadata.namespace}/{.metadata.name} Evicted{"\n"}{end}'
```

Baseline, 2026-10-04 12:11Z, TBD prod and Ziftbook staging live, little traffic: available 1335Mi (35%), working set
2497Mi, RSS 1806Mi, of which the k3s process (API server, datastore, kubelet, containerd) 846Mi and all pods 872Mi
(`mysql-0` 222Mi the largest). Requests 1916Mi (49%), limits 5290Mi (138%).

2026-10-05 10:48Z, the same plus Alloy (INFRA-85), k3s on `GOGC=50` and Flux capped at 256Mi (INFRA-80): available 1817Mi (47%),
working set 2014Mi, RSS 1594Mi, k3s process 625Mi, all pods 955Mi (`mysql-0` 235Mi the largest, Alloy 54Mi). Requests 2044Mi
(53%), limits 4010Mi (104%). Measured on the old Lightsail node (INFRA-80).

### Trigger

Upsize (a larger server type in `terraform/hetzner`) when any of these holds:

1. `availableMi` under **768** (20% of allocatable) for 30 minutes: alert `NodeMemoryLow` emails it (it uses 20% of
   the node's memory, 766Mi). Three checks on different days under 768 count too.
2. Any container `OOMKilled`, any `Evicted` pod, or `MemoryPressure=True`: alerts `NodeOOMKill` and
   `NodeMemoryPressure` (and `NodeMemoryCritical` on the way there). Evictions themselves have no metric: a dip
   under 100Mi shorter than the 60 s scrape can evict unseen, so run the check after any unexplained restart.
3. Before a new workload goes on the node: `availableMi` minus its expected use (the RSS of a comparable workload
   already running, else its memory requests) would land under 768. Memory requests above 85% of allocatable
   (3257Mi) also block it, since the scheduler stops placing pods.

### Request log retention

The privacy policy says request logs are kept "up to 7 days" (INFRA-130). They exist only as container stdout under
`/var/log/pods` on the node (Traefik access logging is off), and Ziftbook's JSON lines in Grafana Cloud Loki (ZIF-89). kubelet rotates
by size only (10Mi x 5 files, defaults), so a quiet container would keep lines for weeks. DaemonSet `observability/log-retention`
(`clusters/hetzner/observability/log-retention/`) runs hourly on every node: for every file under `/var/log/pods` it reads the first
line's timestamp, the oldest, and empties a live `*.log` or deletes a kubelet-rotated `*.log.<ts>[.gz]` once that line is
older than 6 days (worst case 6 days 1 hour). It covers every pod on the node (Ziftbook and the databases too). It never
unlinks a live log: `kubectl logs -f` on a truncated container goes quiet until reconnected, and a container keeps only
its last 0 to 6 days of history. After a clean pass it posts the gauge `log_retention_last_success_timestamp_seconds`
to Alloy; Grafana rule `LogRetentionStale` (group `log-retention`, applied like the memory rules below, `api PUT
/api/v1/provisioning/folder/platform/rule-groups/log-retention grafana/log-retention.rules.json`) mails the owner when it is
over 3 hours old or absent (it fires until the first run after the first apply). Per node since INFRA-171 (the DaemonSet
labels its heartbeat `instance=<node>`).

Check, read-only: `kubectl -n observability get ds log-retention`, `kubectl -n observability logs
ds/log-retention` (one line per file emptied or deleted), and for any pod `kubectl logs --timestamps <pod> -c <c> | head -1`
must be under 7 days old. Not covered: Cloudflare's own edge logs, and Mailgun's. The Ziftbook lines in Grafana Cloud
are kept 14 days, the Grafana Cloud Free plan's fixed retention ([configuration-map.md](configuration-map.md)).

### Memory alerts

Grafana Cloud evaluates rule group `node-memory` (folder Platform) every minute and emails the owner through contact
point `owner-email` (Grafana's own mail, no credential). Definitions: [`grafana/`](../grafana/). "Available" is the
kubelet's number, `machine_memory_bytes - container_memory_working_set_bytes{id="/"}` from cAdvisor, the same value as
`availableMi` in the check above (both 1779Mi on 2026-10-05). It read about 165Mi above node_exporter's `MemAvailable`
on 2026-10-05, because the working set leaves out inactive page cache.

| Alert | Fires when | Severity, repeat |
|---|---|---|
| `NodeMemoryLow` | available under 20% of the node (766Mi today) for 30 min | warning, daily |
| `NodeMemoryCritical` | available under 10% for 5 min | critical, every 4 h |
| `NodeMemoryPressure` | available under 200Mi at one evaluation (the kubelet sets MemoryPressure and evicts at 100Mi) | critical, every 4 h |
| `NodeOOMKill` | the kernel OOM-killed any process in the last 10 min (`node_vmstat_oom_kill`, containers and k3s alike) | critical, every 4 h |
| `NodeMetricsAbsent` | a node's `node` or `cadvisor` scrape is not up for 10 min (Alloy down, node down), per node seen in the last day: the alerts above are blind for it | warning, daily |

The thresholds are ratios, so they follow an upsize. When the cAdvisor series stop, the memory rules keep their last
state (no "resolved" mail while memory is still low) and `NodeMetricsAbsent` reports the gap. Trend: dashboard
**Node memory** (`/d/node-memory`), 7 days by default; the rules link to its panel.

The rules and the contact point are API-provisioned, so the Grafana UI cannot edit them (silences still work:
Alerting > Silences). The dashboard is not locked: UI edits to it are lost on the next apply. To change any of them,
edit the file in `grafana/`, merge, then apply it with a short-lived token: Grafana > Administration > Users and
access > Service accounts > Add service account (role Editor) > Add service account token (expiry 1 day). From the
repo root, in bash or zsh, with `<stack>` from the stack's URL (Cloud Portal > the stack > Launch); the token only
passes through `read -rs` and curl's stdin, never its arguments:

```bash
read -rs T; G=https://<stack>.grafana.net
api() { printf 'Authorization: Bearer %s\n' "$T" | curl -sS --fail-with-body -H @- -H 'Content-Type: application/json' -X "$1" "$G$2" --data-binary "@$3" && echo; }
api PUT /api/v1/provisioning/folder/platform/rule-groups/node-memory grafana/node-memory.rules.json
api PUT /api/v1/provisioning/contact-points/owner-email grafana/contact-point.json
api POST /api/dashboards/db grafana/node-memory.dashboard.json
unset T
```

Each call prints the JSON Grafana returns; an error prints its reason. Then delete the service account. A fresh
stack first needs the folder (`POST /api/folders` with `{"uid":"platform","title":"Platform"}`) and the contact point
once (`POST /api/v1/provisioning/contact-points`).

### Synthetic uptime

Grafana Cloud Synthetic Monitoring (INFRA-172) probes `https://app.thebetterdecision.com/health/dependencies` and
`https://app.ziftbook.com/api/healthz` (the backend's readiness path) from the outside, expecting 200, and replaces the
Route 53 health check. Rule `ProdEndpointDown` (group `synthetic-uptime`, folder Platform) emails `owner-email` when
`probe_success` averages under 0.5 over 6 minutes for 2 minutes (both probes failing). No data fires only when both checks report nothing; one check disappearing resolves its own series silently, then every 4 h.
Payloads: [`grafana/synthetic-*.json`](../grafana/).

Free tier: 100,000 test executions a month; one HTTP check run from one probe is one execution. Every 2 minutes is
21,600 runs in a 30-day month (22,320 in 31 days) per check per probe; 2 checks x 2 probes = 89,280, inside the limit
with 10% to spare. Every minute would be 178,560 and bill. Adding a third probe or check needs a slower frequency.
Verify the limit in the stack's Synthetic Monitoring usage page before applying.

Apply (once, then on every payload change). Token: Grafana > Testing & synthetics > Synthetics > Config > Access
tokens > Generate (the SM access token, not a Grafana service account); the API URL is on the same Config page
(`https://synthetic-monitoring-api-<region>.grafana.net`). From the repo root, in bash or zsh, with `jq`:

```bash
read -rs T; S=https://synthetic-monitoring-api-<region>.grafana.net
sm() { curl -sS --fail-with-body -H @<(printf 'Authorization: Bearer %s\n' "$T") -H 'Content-Type: application/json' "$@"; }
sm "$S/api/v1/probe/list" | jq -r '.[] | select(.public) | [.id, .name, .region, .public, .online] | @tsv'
```

Pick two EU probes in different countries from that list (for example Frankfurt and Amsterdam), then:

```bash
P='[<id1>,<id2>]'
for f in grafana/synthetic-*.check.json; do
  jq --argjson p "$P" '.probes = $p' "$f" | sm -X POST "$S/api/v1/check/add" --data-binary @- && echo
done
```

`check/add` is not idempotent: a second run fails on the duplicate job and target. To change a check, list it
(`GET /api/v1/check/list`), then `POST /api/v1/check/update` with the same body plus its `id` and `tenantId`. Then
apply the rule with the Grafana API as under [Memory alerts](#memory-alerts):
`api PUT /api/v1/provisioning/folder/platform/rule-groups/synthetic-uptime grafana/synthetic-uptime.rules.json`.
Check: the checks show Up in Synthetics, and `probe_success{job="tbd-prod-health"}` returns 1 in Explore. To test the
alert, edit one check's target in the UI to a 404 path for about 10 minutes (both probes fail), expect the mail, then
restore it: re-run `check/update` with the original payload (the UI edit is not in git). Finally revoke the token and
clear it: `sm -X DELETE "$S/api/v1/token/delete"; unset T`.

### Ziftbook app alerts

Rule group `ziftbook-app` (folder Platform, ZIF-89) watches Ziftbook production through the metrics the app already
exports over OTLP. Grafana Cloud promotes `service.name` and `deployment.environment.name` to labels, so the queries
select `deployment_environment_name="production"`; staging raises nothing. A pod's counters start a new series
(`service.instance.id` is per process), so the two counter rules take "now minus the last value seen up to a day before the
window, or 0 if the series is newer" instead of `increase()`, which never counts a series' first jump; looking back a
day, not one sample, keeps an export gap from counting a pod's lifetime total as new. A dead or stuck worker is first
the [Healthchecks.io check](#ziftbook-worker-heartbeat-healthchecksio) (ZIF-21, about 6 min); `ZiftbookJobQueueStuck`
also fires about 2 min later, because its no-data case is what catches a worker that runs but exports no metrics.

| Alert | Fires when | Severity, repeat |
|---|---|---|
| `ZiftbookApiErrors` | 5 or more API responses with a 5xx in 10 min (`http_server_request_duration_seconds_count`, FastAPI's native metric; `/api/healthz` is not counted). An absolute count, sized for pre-launch traffic: switch to a ratio once production serves about one request a second | critical, every 4 h |
| `ZiftbookJobsGaveUp` | any job given up after its 5th attempt in the last hour, per kind (`ziftbook_job_runs_total{job_outcome="gave_up"}`). Never retried by the worker | warning, daily |
| `ZiftbookJobQueueStuck` | the oldest due job of a kind waited over 10 min for 5 min (`ziftbook_job_queue_oldest_age_seconds`; the worker polls every 30 s), or no queue metric at all: the worker reports every kind every minute, 0 when empty | critical, every 4 h |

When one fires:

```sh
export KUBECONFIG=~/.kube/hetzner
# -l reads every replica (deploy/backend reads one pod) and needs --tail=-1; fromjson? skips a line that is not JSON, such as a crash traceback.
kubectl -n ziftbook-prod logs -l app=backend -c backend --tail=-1 --since=30m | jq -cR 'fromjson? | select(.level == "ERROR" or .status >= 500)'
kubectl -n ziftbook-prod logs -l app=worker -c worker --tail=-1 --since=2h | jq -cR 'fromjson? | select(.msg == "job gave up" or .level == "ERROR")'
```

Each line carries `request_id` (also in the `X-Request-ID` response header) and `trace_id`; a job line carries
`job_id` and `job_kind`. Errors are logged as the class and frames only. A given-up job keeps its `last_error` in the
`jobs` table; resending it is an app decision, not an infra one. Older lines, or a pod that is gone: [Find a request's logs](#find-a-requests-logs).

Apply as under [Memory alerts](#memory-alerts):
`api PUT /api/v1/provisioning/folder/platform/rule-groups/ziftbook-app grafana/ziftbook-app.rules.json`. Check in
Explore that `count by (__name__, service_name) ({__name__=~"http_server_request_duration_seconds_count|ziftbook_job_runs_total|ziftbook_job_queue_oldest_age_seconds", deployment_environment_name="production"})`
returns the names with `service_name` `ziftbook-api` or `ziftbook-worker` (tbd-prod has the same HTTP metric): a
misnamed metric or label leaves its rule silent, except `ZiftbookJobQueueStuck`, which fires on no data.
`ziftbook_job_runs_total` exists only after a job has run since the worker started, and the HTTP metric only after real
API traffic.

### Find a request's logs

Ziftbook's log lines (staging and prod) are in Grafana Cloud Loki for 14 days (ZIF-89). Grafana > Explore > the stack's
Loki data source, with the `request_id` from the `X-Request-ID` response header, or a `trace_id`:

```logql
{service_namespace="ziftbook", k8s_namespace_name="ziftbook-prod"} | json | request_id="<X-Request-ID>"
{service_namespace="ziftbook", k8s_namespace_name="ziftbook-prod"} | json | trace_id="<trace id>"
{service_name="ziftbook-worker", k8s_namespace_name="ziftbook-prod"} | json | job_id="<job id>"
{service_name="ziftbook-api", k8s_namespace_name="ziftbook-prod"} | json | level="ERROR"
```

Stream labels: `service_name` (`ziftbook-api`, `ziftbook-worker`, `ziftbook-migrations`, `ziftbook-frontend`),
`service_namespace="ziftbook"`, `k8s_namespace_name` and `k8s_container_name`. The pod is structured metadata, not a
label: `| k8s_pod_name="backend-..."` tells the two prod backend replicas apart. From a trace in Tempo, "Logs for this
span" matches on `service_name` and `service_namespace`.

Only a line that is a JSON object with `ts`, `level`, `logger` and `msg` is sent (the privacy notices cover the app's
own records only). A crash traceback, raw alembic or Next.js output, or anything else stays on the node: read it with
`kubectl logs` (as in [Ziftbook app alerts](#ziftbook-app-alerts)) while the pod lives. A new Alloy pod, on every
change to `config.alloy`, re-reads each container's current log file and sends its last hour again; the copies are
identical lines.

Nothing arrives? In order:

```sh
export KUBECONFIG=~/.kube/hetzner
kubectl -n observability logs -l app=alloy --prefix --tail=-1 --since=1h | grep -iE 'forbidden|/v1/logs|error' | tail   # 401/403 on /v1/logs: the token lacks logs:write
kubectl auth can-i get pods/log -n ziftbook-prod --as=system:serviceaccount:observability:alloy   # yes; -n data must say no
```

Then the drop counters (`kubectl -n observability port-forward ds/alloy 12345`, then
`curl -s localhost:12345/metrics | grep loki_process_dropped_lines_total`): reason `not_app_json` is a JSON object
missing one of the four keys, `replay` a line over an hour old. A line that is not a JSON object is dropped without a
count.

### Node disk

Rule `NodeDiskHigh` (group `node-disk`, folder Platform, ZIF-89) mails the owner when a node disk is over 80% full for
30 min, daily, from cAdvisor's `container_fs_usage_bytes / container_fs_limit_bytes` for the node (`id="/"`). It
covers every node's root disk and, on db-1, `/dev/sdb`, the Hetzner volume that holds the MySQL and Postgres data
(41.9 GB, 0.3 GB used on 2026-10-17). A silent scrape is `NodeMetricsAbsent`'s job. Apply:
`api PUT /api/v1/provisioning/folder/platform/rule-groups/node-disk grafana/node-disk.rules.json`. When it fires
for `/dev/sdb`: grow the volume (below); for a root disk, look for old images (`k3s crictl rmi --prune`) and logs first.

**Grow the DB volume** (one-way: a Hetzner volume never shrinks, and it is billed by size). The databases keep running.

1. PR raising `size` (GB) of `hcloud_volume.db` in `terraform/hetzner/main.tf`. Before merging, check the PR's plan
   shows `hcloud_volume.db` updated in place, never replaced; then merge and approve the `hetzner` run in TFC.
2. On db-1 over SSH, grow the ext4 filesystem online:
   `resize2fs "$(findmnt -no SOURCE /var/lib/rancher/k3s/storage)"`.
3. Check: `df -h /var/lib/rancher/k3s/storage` shows the new size.

## Upgrade k3s

k3s is upgraded by Rancher's system-upgrade-controller (`clusters/hetzner/system-upgrade/`, INFRA-179), not by hand. The k3s
pin lives twice: `version` in both Plans (`plans.yaml`, the source of truth for the running cluster) and `k3s_version` in
`ansible/group_vars/all.yml` (what a rebuilt node installs). Renovate bumps both in one PR, a patch only after the release is
7 days old. Merging that PR is the upgrade: Flux applies the Plans and the controller rolls the servers one at a time, node-1
and node-2 (Plan `k3s-app`: cordon, drain, upgrade, uncordon), then db-1 (Plan `k3s-db`: cordon only, no drain, because its
local-path volumes would take the databases down). db-1's prepare waits until every app node carries the new version's label,
so it is always last. Each prepare also waits for every node Ready and fails (the job retries) while another node is cordoned.
The controller's job backoff limit is 99 and finished jobs live 900 s (`SYSTEM_UPGRADE_JOB_BACKOFF_LIMIT`,
`SYSTEM_UPGRADE_JOB_TTL_SECONDS_AFTER_FINISH`), so a blocked prepare retries, at growing intervals up to 6 min, for hours.

**Before merging**
1. Every node Ready and nothing cordoned: `kubectl get nodes -L role`. No `ansible/reboot.yml` run in progress.
2. etcd snapshot on node-1: `ssh root@<node-1 public IPv4> k3s etcd-snapshot save --name pre-<version>` (no rollback by revert,
   see below; the snapshot is the way back).
3. Tell the tbd and ziftbook sessions: prod keeps one of two replicas through each app node (INFRA-177), staging has a short outage.

**Watch** (about 15 to 20 minutes in total: per app node a 60 s guard, the drain, the upgrade and a 2 min delay; db-1 after both):

    kubectl -n system-upgrade get plans,jobs,pods
    kubectl get nodes -L role -w
    kubectl -n system-upgrade logs -f job/<job>          # the prepare container, then upgrade

At the running version (a rehearsal) each upgrade job logs "Binary already been replaced" and k3s is not restarted, which still
proves the order: cordon, drain on app nodes only, uncordon, db-1 last. Check `/readyz/etcd` on each
server and the prod hosts after the roll, then run the [post-deploy smoke](#post-deploy-smoke).

**Pause.** One node: `kubectl label node <node> plan.upgrade.cattle.io/<plan>=disabled` (remove it with a trailing `-`). Pausing
an app node also holds db-1, whose gate waits for every app node. Everything: `flux suspend kustomization flux-system` and
`kubectl -n system-upgrade scale deploy/system-upgrade-controller --replicas=0`; undo both to resume.

**A node left cordoned** (job failed or was paused mid-way): the next prepare refuses to run while any other node is cordoned.
Find why the job failed (`kubectl -n system-upgrade logs job/<job>`), fix it, and `kubectl uncordon <node>` once it is Ready.

**Plan edits re-run the roll.** Any change to a Plan spec (prepare script, drain flags) changes its hash, so the whole roll runs
again at the same version: both app nodes drained, db-1 cordoned. Merge such edits like a k3s bump.

**No rollback by revert.** k3s-upgrade refuses a downgrade ("Current ... is higher"): fix forward with a newer release, or
restore the etcd snapshot.

**NetBird.** Its PVC is on node-2, so if node-2's upgrade fails kubectl over NetBird stays down. Break-glass:
`ssh root@<public IPv4> k3s kubectl ...` ([Hetzner nodes](#hetzner-nodes-ansible-rebuild-break-glass)).

**Minors** (Renovate waits for a tick on the Dependency Dashboard): read the release notes, one minor at a time, and change in the
same PR the k3s-ansible pin (`ansible/requirements.yml`) and the kubectl image in `system-upgrade/kustomization.yaml` (it must
stay within one minor of the server; no Renovate manager sees it). A rebuilt node installs `k3s_version` and is upgraded on join.

## Hetzner nodes: Ansible, rebuild, break-glass

Host config and k3s live in `ansible/`, run by hand from that directory; the procedures are in
[`ansible/README.md`](../ansible/README.md): "Steady-state run" (any Ansible change), "Rolling reboot" (kernel
updates), "Rebuilding one node" (app node, node-1 and db-1 each have their own steps) and "Volume-missing drill
(db-1)", which also shows what the mount guard does. For a tunnel hostname, see [Add a public hostname for an
app](#add-a-public-hostname-for-an-app).

**Break-glass**, when kubectl over NetBird is gone: `ssh root@<public IPv4> k3s kubectl ...` on any server, from an
address in the `hetzner` workspace variable `ssh_allowed_cidrs` (a new home address means updating that variable and
applying first). With SSH gone too, use the Hetzner Cloud Console, project `fj-platform` > Servers > the server: Power
(power cycle) and Console (VNC). The servers have no root password (SSH key only), so the console login first needs
Rescue > Reset root password (only on a running OS with the qemu guest agent), or Rescue > Enable rescue & power cycle
to mount the root disk from the rescue system. Afterwards lock root's password again: `passwd -l root`.

## Hetzner cluster bootstrap

One time, for the new cluster (INFRA-171, epic INFRA-164): `clusters/hetzner/` is its Flux path (final, never moved),
reconciled from `main`. Three k3s servers: node-1 and node-2 (`role=app`), db-1 (`role=db`,
taint `role=db:NoSchedule`, local-path storage on the Hetzner volume). No Traefik: ingress is the Cloudflare Tunnel
`hetzner-k3s` (`terraform/cloudflare/tunnel.tf`). App environments move later, one ticket each (INFRA-173, INFRA-181,
INFRA-175). Steps marked **owner** need the owner's accounts or the offline age key; key material only ever passes
through `read -rs`, a pipe or `ssh` stdin, never the screen, argv or a plaintext file. Run everything from the repo
root on `main` at the merged SHA, in bash or zsh.

What is in the tree and why:

- **Copied byte for byte** (SOPS files are bound to the age key, not to a path, so a copy decrypts): `sops-smoke`,
  `observability/grafana-cloud` (write-only token; per-node series via `instance`), `data/mysql`, `data/postgres`,
  `data/postgres-ziftbook-prod` (the same passwords keep the app Secrets valid when each environment moves with its
  dump), `data/backup-s3` (the put-only uploader key). **Not copied**: the NetBird setup key (single-use, spent on
  the old Lightsail node), the `ghcr-pull` and app Secrets (they move with their environment), Valkey (removed by INFRA-122
  before `tbd-prod` moves), Traefik and its certificates (no Traefik here).
- `data/db-backup` dumps every set (`DUMP_SETS` lists all four).
- `observability/log-retention` is a DaemonSet on every node (db-1 included), and its heartbeat carries the node name.

**1. k3s (lead).** `ansible/README.md`, First bootstrap. Then `N1=<node-1 public IPv4>` in this shell and
`ssh -i ~/.ssh/fjc-hetzner-deploy root@$N1 k3s kubectl get nodes -L role` lists node-1, node-2, db-1 Ready with their
roles. Every `ssh` below uses that key (`ssh-add ~/.ssh/fjc-hetzner-deploy` once, or add `-i` to each).

**2. `sops-age` (owner).** The same private key as the old cluster (`.sops.yaml` has one recipient):

```bash
ssh root@$N1 k3s kubectl create namespace flux-system
# Wait for the prompt, then paste the AGE-SECRET-KEY line (74 chars); anything else changes nothing.
read -rsp 'Paste the key line, then Enter: ' K; K=${K%$'\r'}; echo; if [ ${#K} -eq 74 ]; then printf '%s\n' "$K" | ssh root@$N1 k3s kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=/dev/stdin; else echo "got ${#K} chars, expected 74: nothing changed"; fi; unset K
```

**3. Tunnel token (owner, after the `cloudflare` workspace applied `tunnel.tf`).** The output is sensitive and the
state is remote, so `terraform output` cannot read it locally: read it from the HCP Terraform API with the token
`terraform login` stored, straight into a SOPS file:

```bash
F=clusters/hetzner/cloudflared/cloudflared-token.secret.yaml
T=$(jq -r '.credentials["app.terraform.io"].token' ~/.terraform.d/credentials.tfrc.json)
tfc() { printf 'Authorization: Bearer %s\n' "$T" | curl -sSf -H @- "https://app.terraform.io/api/v2$1"; }
WS=$(tfc /organizations/FlamaCorp/workspaces/cloudflare | jq -r .data.id)
OUT=$(tfc "/workspaces/$WS/current-state-version-outputs" | jq -r '.data[] | select(.attributes.name == "hetzner_tunnel_token") | .id')
[ -n "$OUT" ] && tfc "/state-version-outputs/$OUT" | jq -j '.data.attributes.value // empty' \
  | kubectl create secret generic cloudflared-token -n cloudflared --from-file=TUNNEL_TOKEN=/dev/stdin --dry-run=client -o yaml \
  | sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F"
unset T OUT
grep -c 'TUNNEL_TOKEN: ENC\[AES256' "$F"   # 1. 0 or "No such file": rm -f "$F", check the apply ran, run again
```

Commit the file in a PR and merge (no kustomization lists it: Flux picks up every file under `clusters/hetzner`).

**4. NetBird (owner).** At https://app.netbird.io (its `Default` policy is already off):

1. Setup Keys -> Create Setup Key: name `hetzner-k3s`, Reusable off, Ephemeral off, auto-assigned group `k3s-hetzner`,
   expiry 7 days. Encrypt it from the clipboard (nothing is shown):

   ```bash
   F=clusters/hetzner/netbird/setup-key.secret.yaml
   read -rs NB_KEY   # paste the setup key, press Enter
   [ -n "$NB_KEY" ] && printf 'NB_SETUP_KEY=%s\n' "$NB_KEY" \
     | kubectl create secret generic netbird-setup-key -n netbird --from-env-file=/dev/stdin --dry-run=client -o yaml \
     | sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F"
   unset NB_KEY
   grep -c 'NB_SETUP_KEY: ENC\[AES256' "$F"   # 1, else git checkout or rm the file and run again
   ```

   Commit in a PR and merge. The key is spent at enrolment (step 5); the peer identity then lives on the
   `netbird-state` volume.
2. The access policy is Terraform ([`terraform/netbird`](../terraform/netbird/README.md); the owner creates workspace
   `netbird` with env `NB_PAT` from NetBird service user `terraform` first): `owner-to-hetzner-k3s`, source
   `owner-devices`, destination group `k3s-hetzner` (the peer's own group), TCP 6443, one way. Group membership is in
   the same stack, by peer name: before re-enrolling, delete the old peer, so the new one gets the same name and
   the next apply picks it up. The peer runs with
   hostNetwork, so its NetBird IP lands on the node's loopback, where the API server listens. No NetBird Network or
   route: traffic for the routing peer's own private IP is not routed (only the other nodes answered), and the peer
   is pinned to one node by its state volume anyway, so routes to the other nodes add no availability.
   No login-expiration change: it applies only to peers added by SSO login, not by setup key.

**5. Flux (lead).** v2.9.6, source and kustomize controllers,
applied once by hand, then Flux manages itself (the memory patch in `flux-system/kustomization.yaml` lands on its first
apply). Not `apply -k`: the encrypted Secrets in the folder fail kubectl validation.

```bash
ssh root@$N1 k3s kubectl apply -f - < clusters/hetzner/flux-system/gotk-components.yaml
ssh root@$N1 k3s kubectl -n flux-system wait --for=condition=Available deploy --all --timeout=5m
ssh root@$N1 k3s kubectl apply -f - < clusters/hetzner/flux-system/gotk-sync.yaml
```

**6. kubeconfig over NetBird (owner).** The CA comes from the admin kubeconfig Ansible fetched; the token is
`netbird/owner-admin` (90 days; revoke it as below). `ssh` writes it straight into a 0600
file that the kubeconfig names as `tokenFile` (kubectl reads it on every call), so it never sits in a variable or
argv. Renewing is that one `ssh` line again. The server is the peer's NetBird name; its certificate is the API
server's, which names `kubernetes`, hence `--tls-server-name`.

```bash
export KUBECONFIG=~/.kube/hetzner
kubectl config view --raw --kubeconfig ~/.kube/fjc-hetzner.yaml -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' \
  | base64 -d > ~/.kube/hetzner-ca.crt && grep -c 'BEGIN CERTIFICATE' ~/.kube/hetzner-ca.crt   # 1
kubectl config set-cluster hetzner --server=https://hetzner-k3s.netbird.cloud:6443 --tls-server-name=kubernetes \
  --certificate-authority="$HOME/.kube/hetzner-ca.crt" --embed-certs
(umask 077; ssh root@$N1 k3s kubectl -n netbird create token owner-admin --duration=2160h > ~/.kube/hetzner-token)
wc -c < ~/.kube/hetzner-token   # several hundred bytes; 0 means the ssh failed: run it again
kubectl config set-credentials owner-admin && kubectl config set users.owner-admin.tokenFile "$HOME/.kube/hetzner-token"
kubectl config set-context hetzner --cluster=hetzner --user=owner-admin && kubectl config use-context hetzner
chmod 600 ~/.kube/hetzner
# macOS nc -G does not time out on dropped packets; GNU timeout does.
for p in 6443 10250 22; do timeout 4 nc -z hetzner-k3s.netbird.cloud $p && echo "$p open" || echo "$p closed"; done
# expect 6443 open, 10250 and 22 closed
```

Renew before the 90 days run out: the same `ssh` token line. Revoke every token at once with
`kubectl -n netbird delete sa owner-admin`; your own access ends too. Flux recreates the account within its interval
(up to 10 minutes; `sudo k3s kubectl get sa -n netbird owner-admin` over `ssh` on node-1 shows when), then mint a new
token over `ssh` (node-1, as above) and rewrite `~/.kube/hetzner-token`. Anyone who can create pods
or tokens in namespace `netbird` holds cluster-admin: never grant namespace-scoped rights there.

**7. Checks (lead).** With `KUBECONFIG=~/.kube/hetzner`:

```bash
flux get sources git; flux get kustomizations   # main@sha1:<merge commit>, READY True
kubectl get nodes -L role                       # 3 Ready
kubectl -n cloudflared get pods -o wide         # 2/2 Running, one on node-1, one on node-2
kubectl -n cloudflared logs -l app=cloudflared --prefix | grep 'Registered tunnel connection' | cut -d' ' -f1 | sort | uniq -c   # 4 per pod
kubectl -n observability get pods -o wide       # alloy and log-retention: one each per node, db-1 included
kubectl -n data get pods,jobs -o wide           # mysql-0 and postgres-0 Running on db-1; both bootstrap Jobs Complete
kubectl -n data get cronjob db-backup           # SUSPEND True
kubectl get pv -o custom-columns=NAME:.metadata.name,CLAIM:.spec.claimRef.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0],PATH:.spec.hostPath.path
# data-mysql-0 and data-postgres-0 on db-1 under /var/lib/rancher/k3s/storage; netbird-state on an app node
ssh root@<db-1 public IPv4> findmnt -no SOURCE /var/lib/rancher/k3s/storage   # /dev/sdb, the volume
```

Cloudflare: Zero Trust -> Networks -> Tunnels -> `hetzner-k3s` is HEALTHY with 2 connectors. Grafana Cloud, Explore:
`sum by (instance) (up{job=~"node|cadvisor"} == 1)` is 2 for each of node-1, node-2, db-1;
`container_fs_limit_bytes{instance="db-1", id="/"}` has a `/dev/sdb` series (the volume) next to the root disk;
`log_retention_last_success_timestamp_seconds` has one series per node.

**8. Alert rules (owner or lead, once, after this PR merges).** `NodeMetricsAbsent` (rule group `node-memory`) and
`LogRetentionStale` (`log-retention`) are per node since INFRA-171; apply both files as in [Memory alerts](#memory-alerts)
(`api PUT .../rule-groups/node-memory grafana/node-memory.rules.json` and `.../rule-groups/log-retention
grafana/log-retention.rules.json`). The memory rules were already `by (instance)`. A node retired on purpose keeps `NodeMetricsAbsent` firing for a day and `LogRetentionStale` for up to 7
days: silence them then.
