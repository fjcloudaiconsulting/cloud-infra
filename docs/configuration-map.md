# Configuration map

Settings that live **outside git**: GitHub org and repo settings, GitHub Apps, Mend Renovate, HCP
Terraform, AWS, Cloudflare, Grafana Cloud, the cluster's out-of-band secrets, Jira. They were set by hand in UIs and
consoles, so git cannot show drift and a wrong value fails silently. This page says what is set, where,
what depends on it, and how it shows when it breaks. Names only: secret values never go in this repo
(it is public).

Verified against the live settings on 2026-10-03. When you change any setting below, change this page
in the same PR or right after.

## When something breaks

| Symptom | Likely cause | Look at | Fix |
|---|---|---|---|
| `cloudflare` plan fails with `access.api.error.not_enabled`, or `dev.thebetterdecision.com` shows a Cloudflare Access error | Zero Trust not enabled on the account, or the One-time PIN login method is off | Zero Trust > Settings > Authentication | Owner: enable Zero Trust (Free), turn on One-time PIN, re-run the plan ([runbook](runbooks.md#cloudflare-access-on-tbd-staging)) |
| Release job fails with `Resource not accessible by integration` on create-a-release | The release App token lacks `workflows`. GitHub treats a new tag as creating workflow files when a later `main` commit changed one | [Release chain](#release-and-deploy-chain) step 4, [GitHub Apps](#github-apps) | Org owner: App permissions > Workflows: Read and write, then accept on the org installation. Re-run the failed job |
| Release job fails creating the release or tag with `Repository rule violations found` (creations restricted) | The release App is missing from the repo's `tag protection` ruleset bypass list, or release-please ran with another token (`GITHUB_TOKEN` or a non-admin PAT) | [Tag protection](#tag-protection) | Ruleset `tag protection` > Bypass list > `fjcloudaiconsulting-release` (app) > Always. Re-run the failed job |
| Release job is green but `promote` and `smoke` are skipped, no tag | `main` moved on after the release PR merged ("main is at X; skipping"). Expected | The newer main run | Nothing, the newest commit's run releases it. If the newest run also skipped or failed, re-run it |
| No release PR after a merge | Only `feat`, `fix`, `perf`, `revert` and breaking commits release (Renovate's `fix(deps):` does); `chore`, `ci`, `docs`, `build`, `test`, `refactor`, `style` do not. Or the release job failed | Run of the merge commit, job `Release / release` (`publish` in `.github`) | Fix the job; a `chore` merge alone never opens one |
| Staging image bump sits on a `renovate/ziftbook-staging-*` or `renovate/tbd-staging-*` branch and never merges, or shows up as a PR | Renovate lost its branch-automerge permission (owner private ops notes), or CI did not run on the branch | [Rulesets](#branch-protection), `ci.yml` push filter | Restore the permission per the private notes. CI must report `Terraform checks` and `Kubernetes checks` on the branch |
| No app image bumps, Dependency Dashboard lists `ghcr.io` lookup failures (other dependencies still get PRs) | `GHCR_READ_TOKEN` (Mend org secret) expired or revoked | [Mend Renovate](#mend-renovate) | New classic PAT with only `read:packages`, owned by the owner's GitHub user (the username is fixed in `renovate.json` hostRules); replace the Mend secret, tick "run again" on the dashboard |
| `ImagePullBackOff` in `ziftbook-prod`, `ziftbook-staging`, `tbd-staging` or `tbd-prod` | The `ghcr-pull` credential expired or was revoked | `clusters/*/namespaces/*-ghcr-pull.secret.yaml` | Re-encrypt a new GHCR read credential into every file (see [Cluster](#cluster-out-of-band-material)) |
| Hetzner: `cloudflared` pods in `CreateContainerConfigError`, tunnel `hetzner-k3s` DOWN | Secret `cloudflared/cloudflared-token` missing, or the tunnel was replaced (new token) | `kubectl -n cloudflared describe pod`, Zero Trust > Networks > Tunnels | [Bootstrap runbook](runbooks.md#hetzner-cluster-bootstrap) step 3, merge, restart the Deployment |
| db-1: `mysql-0`/`postgres-0` Pending or db-1 NotReady; on db-1 `systemctl status k3s` names `var-lib-rancher-k3s-storage.mount`, or db-1 boots into emergency mode | The Hetzner volume `fjc-db` is detached or missing. The mount guard keeps k3s down so the databases never start on empty data dirs on the root disk | `findmnt /var/lib/rancher/k3s/storage` on db-1, Hetzner console > Volumes | Re-attach `fjc-db` (automount off). From emergency mode, power cycle db-1: fstab mounts it and k3s starts. On a running db-1, `mount -a` and `systemctl start k3s` ([`ansible/README.md`](../ansible/README.md) "Volume-missing drill (db-1)" step 6) |
| kubectl times out, `k3s kubectl` over SSH on the surviving server too; Flux stops reconciling | etcd lost quorum: two of the three servers (node-1, node-2, db-1) are down, and one alone cannot serve the API. Pods already running usually keep serving | Hetzner console (which servers are off), `systemctl status k3s` on the survivor | Power a second server back on with its disk intact: quorum returns by itself. A rebuild cannot join without quorum. Only if two servers are lost for good (one-way, k3s docs "cluster-reset"): on the survivor `systemctl stop k3s`, `k3s server --cluster-reset`, then `systemctl start k3s`; then for each lost server `k3s kubectl delete node <name>` on the survivor and rebuild it per [`ansible/README.md`](../ansible/README.md) "Rebuilding one node" from step 4, with `-e k3s_join_peer=<survivor private IP>` unless the survivor is node-1 |
| `kubectl`/`flux` cannot connect | NetBird client not connected, or the `owner-admin` token expired | [`clusters/hetzner/netbird/`](../clusters/hetzner/netbird/) | Connect NetBird; renew the token per the [bootstrap runbook](runbooks.md#hetzner-cluster-bootstrap) |
| A pod or Job in `ziftbook-staging` or `tbd-staging` is never created (`FailedCreate`, "exceeded quota: staging-class-only") | Every pod there must set `priorityClassName: staging`; the quota rejects any other, Jobs and CronJobs included | `kubectl -n <ns> get events` | Add `priorityClassName: staging` and explicit memory requests that fit the `memory` quota |
| Flux shows the Kustomization Ready but a workload is down | `flux-system` has no `healthChecks`: pod, quota or PSA failures do not turn it red | `kubectl -n <ns> get pods,events` | Fix the manifest. Namespace limits: [`namespaces/ziftbook-staging.yaml`](../clusters/hetzner/namespaces/ziftbook-staging.yaml) |
| A k3s upgrade Plan sits idle: `k3s-db` prepare logs "waiting for Plan k3s-app", or a prepare fails with "another node is cordoned", a node stays `SchedulingDisabled` | The gate holds db-1 until every app node carries the new version label; the quorum guard refuses to start while another node is cordoned or NotReady. A node is left cordoned after a failed or paused job | `kubectl -n system-upgrade get plans,jobs,pods`, `kubectl -n system-upgrade logs job/<job> -c prepare`, `kubectl get nodes -L role` | Fix the failing job or node, `kubectl uncordon <node>`; a deliberate pause: [runbook](runbooks.md#upgrade-k3s) |
| k3s upgrade job fails with "Current ... is higher" (or similar downgrade refusal) | A Plan `version` older than the running k3s, e.g. a revert of an upgrade PR. k3s-upgrade never downgrades | `kubectl -n system-upgrade logs job/<job> -c upgrade`, `kubectl get nodes` (VERSION) | Fix forward: pin the Plans and `k3s_version` to the running version or newer. A real rollback is an etcd snapshot restore ([runbook](runbooks.md#upgrade-k3s)) |
| Flux cannot apply a `*.secret.yaml` | `flux-system/sops-age` missing or holds the wrong key | README, Kubernetes secrets | Recreate the secret from the offline key |
| Ansible k3s play fails in a sops lookup ("failed to get the data key") | `SOPS_AGE_KEY_FILE` not set to the Ansible age key, or that key is lost | [Ansible age key](#cluster-out-of-band-material) | `export SOPS_AGE_KEY_FILE=~/.config/sops/age/ansible-hetzner.txt`; a lost key is replaced as in that row |
| TBD staging backend stuck in `Init` (migrate restarts with "Access denied" or "Unknown database") | The `tbd_staging` database or user does not exist, or its password differs from `tbd-staging/tbd` `database-url` | `kubectl -n tbd-staging logs deploy/backend -c migrate` | Rerun [MySQL database per app environment](runbooks.md#mysql-database-per-app-environment); it is idempotent and resets the password |
| TBD staging `mcp` pod in `ImagePullBackOff` ("not found"), or `/mcp` on `dev.thebetterdecision.com` answers 404 or 502 | The tbd release did not publish `ghcr.io/fjcloudaiconsulting/tbd/mcp` at the tag `tbd-staging/mcp.yaml` pins; or the pod crashed: `PLATFORM_AI_* must not be set` (a key added to its env) or an `API_TOKEN_HMAC_KEY` / `JWT_SECRET_KEY` validator error | `kubectl -n tbd-staging describe pod -l app=mcp`, `kubectl -n tbd-staging logs deploy/mcp` | Publish the image from tbd (its `test.yml` image matrix and `release.yml` promote list), or fix the env. Every agent token failing with 401 means its `API_TOKEN_HMAC_KEY` differs from the backend's: both read `tbd` key `api-token-hmac-key` |
| TBD staging pods in `CreateContainerConfigError` | Secret `tbd-captcha` (keys `site-key`, `secret`) missing | `kubectl -n tbd-staging describe pod` | Cloudflare dashboard > Turnstile > widget `tbd-staging` (hostname `dev.thebetterdecision.com`): write its site key and secret key as keys `site-key` and `secret` of a new `tbd-staging/tbd-captcha.secret.yaml`, as in [runbooks.md](runbooks.md#write-or-rotate-a-kubernetes-secret) |
| Ziftbook staging sends no mail (once ZIF-151 ships) | Secret `ziftbook-mailgun` (key `api-key`) missing (the worker starts without it), or the Mailgun domain is not verified | `kubectl -n ziftbook-staging logs deploy/worker`, Mailgun domain status | Write `ziftbook-mailgun.secret.yaml` (INFRA-47 guide, part D), then `kubectl -n ziftbook-staging rollout restart deploy/worker` (env is read at pod start), or fix the DNS records until Mailgun shows the domain verified |
| Ziftbook admin sign-in: TOTP answers 503, the backend logs `totp key unset` | Secret `ziftbook-totp` (key `key`) missing; optional in `backend.yaml` (ZIF-71, since v0.25.0) | `kubectl -n <ns> get secret ziftbook-totp` | [runbooks.md](runbooks.md#ziftbook-admin-totp-key), then `kubectl -n <ns> rollout restart deploy/backend`. Never replace an existing key |
| Every app.ziftbook.com page redirects to `/setup`; the backend logs `setup token unset: first-run setup refused` | No user exists yet and Secret `ziftbook-setup` (key `token`, 32+ characters) is missing; optional in prod `backend.yaml` (ZIF-70, since v0.25.0) | `kubectl -n ziftbook-prod get secret ziftbook-setup` | [runbooks.md](runbooks.md#ziftbook-first-run-setup-production); after setup, delete the Secret file |
| Healthchecks.io alerts that a Ziftbook worker is down, or a check stays "new" | Worker pod not running; or its loop fails (log `worker iteration failed`, usually Postgres); or the ping cannot leave (`healthcheck ping failed`); "new": Secret `ziftbook-healthcheck` (key `url`) missing, the worker starts without it | `kubectl -n <ns> get pods -l app=worker`, `kubectl -n <ns> logs deploy/worker` | Fix the cause; for a missing Secret, [runbooks.md](runbooks.md#ziftbook-worker-heartbeat-healthchecksio) steps 2 and 3 (ZIF-21) |
| Ziftbook photo upload answers 503 | Secret `ziftbook-storage` missing (ZIF-133) | `kubectl -n <ns> get secret ziftbook-storage`, `kubectl -n <ns> logs deploy/backend` | [runbooks.md](runbooks.md#r2-for-ziftbook-photos) step 3, then restart `deploy/backend deploy/worker` |
| Ziftbook photo upload fails in the browser with a CORS error, or with 403 or 404 at R2 | CORS origin in `terraform/cloudflare/r2.tf` differs from the page origin; 403: the env's R2 token was revoked or is not scoped to that bucket; 404 (no such bucket): the endpoint lacks `.eu` (EU buckets answer only on `<account id>.eu.r2.cloudflarestorage.com`) | Browser devtools (the preflight `OPTIONS`), R2 > Manage API tokens | Fix the origin in `r2.tf`; or a new token per the runbook |
| Ziftbook public booking answers 403, backend logs `turnstile secret unset: public bookings refused` | Secret `ziftbook-turnstile` (keys `site-key`, `secret`) missing in that namespace; the pods start without it (ZIF-116, fail closed) | `kubectl -n <ns> logs deploy/backend \| grep -i turnstile` | [runbooks.md](runbooks.md#ziftbook-turnstile-public-bookings) |
| Ziftbook payments off (no Stripe checkout), or Stripe shows webhook deliveries failing | Secret `ziftbook-stripe` (keys `secret-key`, `webhook-secret`, `publishable-key`) missing in that namespace, the pods started before it, or the endpoint's signing secret changed in Stripe | `kubectl -n <ns> get secret ziftbook-stripe`; Stripe > Developers > Webhooks > the endpoint > recent deliveries | [runbooks.md](runbooks.md#ziftbook-stripe-payments) |
| `cloudflare` plan fails with Cloudflare `Authentication error` | Token `tf: cloudflare workspace` was deleted, rolled in the dashboard, or the variable was overwritten by hand | Workspace `cloudflare-tokens` | A deleted token: plan and apply there, it is recreated and the variable rewritten. Rolled, or the variable overwritten: neither shows in a plan, so start a run with Replace resources = `cloudflare_account_token.cloudflare_workspace`. Change token scopes only in `terraform/cloudflare-tokens` |
| Cloudflare 429 page (error 1015) on sign-in, sign-up, password reset, MFA or an invite link, or a 429 to an MCP client's OAuth token or registration call (TBD, INFRA-147), or the Ziftbook post-deploy smoke failing with 429 on `/api/health/dependencies` (shared CI egress address) | The edge rate limit (INFRA-123): more than 20 requests in 10 s from one IP to the zone's auth paths, which on ziftbook.com include `GET /api/session` and `/api/health/dependencies` (INFRA-140) and on thebetterdecision.com `/api/v1/oauth/token` and `/api/v1/oauth/register` (INFRA-147). Blocks last 10 s. Many users behind one NAT share the counter | `terraform/cloudflare/rate_limit.tf`, Cloudflare Security > Analytics of the zone | Wait 10 s. If real users trip it, raise `requests_per_period` or drop a chatty path in a PR |
| A TBD upload or import on `app.` or `dev.thebetterdecision.com` gets a Cloudflare 403 block page | The body cap (BE-13): the request declared a `Content-Length` over 16 MiB | `terraform/cloudflare/body_limit.tf`, Cloudflare Security > Events of the zone | A real file over the cap is over TBD's own 5 MiB limit too. If a legitimate request trips it, raise the cap in a PR (keep the digit count and the value in step), or set `enabled = false` |
| `thebetterdecision.com` does not resolve, or `www.thebetterdecision.com` 301s to an apex that does not answer | The apex custom domain was detached from `tbd-landing` (its DNS record goes with it), or the Worker was deleted | Workers & Pages > `tbd-landing` > Settings > Domains & Routes; [Worker and Snippet access](#worker-and-snippet-access-infra-98) | Re-attach `thebetterdecision.com` to `tbd-landing` (API or dashboard, same section). A deleted Worker: re-run tbd's `Deploy Apex Landing` workflow first |
| A PR has no `Terraform Cloud/FlamaCorp/<ws>` check | Workspace missing, or its trigger path was not touched. Re-running GitHub checks does not trigger a plan | HCP Terraform workspace | An absent check is not a pass. Push a change under the stack's directory |
| `aws` says the token has expired, aws-mcp tools missing | AWS login session expired | n/a | Owner logs in to AWS again (private ops notes) |
| Ziftbook Renovate PR fails `pnpm install` with `ERR_PNPM_MINIMUM_RELEASE_AGE_VIOLATION` | A package version is younger than pnpm's 1 day policy | INFRA-77 | Re-run CI a day later |
| No new metrics in Grafana Cloud | Alloy pod in `CreateContainerConfigError` (Secret `observability/grafana-cloud` missing), or the exporter logs `401` (token revoked, or wrong Instance ID) | `kubectl -n observability get pods`, `kubectl -n observability logs ds/alloy` | Write the Secret per the [runbook](runbooks.md#metrics-to-grafana-cloud-alloy), then `kubectl -n observability rollout restart ds/alloy` |
| `LogRetentionStale` mail, or request logs older than 7 days on the node | CronJob `observability/log-retention` failing (image pull, hostPath, script error) or Alloy down (heartbeat refused) | `kubectl -n observability get cronjob,jobs`, `kubectl -n observability logs job/<latest>` | [Runbook](runbooks.md#request-log-retention); `kubectl -n observability create job --from=cronjob/log-retention lr-manual` to rerun |
| A Grafana alert is Firing (Alerting > Alert rules) but no email arrives | Contact point `owner-email` failing (its row in Alerting > Contact points shows the last error), or the mail is in spam (sender `noreply@grafana.net`) | [Grafana Cloud](#grafana-cloud) | Fix `grafana/contact-point.json` and re-apply it ([runbook](runbooks.md#memory-alerts)) |
| `Kubernetes checks` red at step `prod tags not ahead of staging`: `<app>-prod` image is newer than `<app>-staging` | Prod bumped (or staging not yet bumped) ahead of staging; one app's red also blocks the other app's staging automerge | Step 10 of [Release and deploy chain](#release-and-deploy-chain) | Merge or make the staging bump on `main` first, then update the prod branch. Never merge the prod bump first |

## Release and deploy chain

The flow explained, with diagrams: [release-flow.md](release-flow.md). This section lists the settings.

Every arrow depends on a setting listed in this page. The chain is proven end to end for Ziftbook
staging (v0.20.2, 2026-10-03). TBD uses the same chain (INFRA-91): `tbd-staging` (INFRA-67) bumps by branch,
`tbd-prod` (INFRA-48, on Hetzner since INFRA-175) bumps arrive as PRs and are never automerged (proven by Renovate dry
run only, check both on the first TBD release after INFRA-91). Ziftbook production (`ziftbook-prod`, INFRA-82/INFRA-90) works the same: the renovate.json rule for `clusters/**/ziftbook-prod/**` gives one grouped PR on branch prefix `ziftbook-prod-` with `automerge: false`; steps 6 to 9 differ: Renovate pushes `renovate/ziftbook-prod-ziftbook-prod-images` (no `ci.yml` push filter; CI runs on the PR) and step 8 is a PR the owner merges once step 10 is green and `dev.ziftbook.com/api/healthz` shows the version. The post-deploy smoke then checks `app.ziftbook.com`. For TBD read `tbd-staging` and
`renovate/tbd-staging-tbd-staging-images` (the second `tbd-staging` is the group name) in steps 6 to 9;
the release path to `dev.thebetterdecision.com` is these nine steps.

The rules are [release contract section 8](https://github.com/fjcloudaiconsulting/.github/blob/main/RELEASE_CONTRACT.md#8-deploy-handoff)
(decision INFRA-87). A release is not a deploy. Staging follows `vX.Y.Z` and is fast-forwarded into
`main` without a PR (steps 6 to 9). Production is bumped by a PR the owner merges only after the same tag
runs in that app's staging. The CI check for the tag rule is step 10 (INFRA-89); TBD got staging in
INFRA-67 and this flow in INFRA-91. A release that fails on staging is fixed forward; marking its GitHub
release a prerelease keeps it out of the drift watch below.

| # | Step | Needs |
|---|---|---|
| 1 | PR merged to the app repo's `main` | Branch protection, required checks |
| 2 | CI on `main` builds `sha-<7>` images | Shared workflow `build-image@v1` in `fjcloudaiconsulting/.github` |
| 3 | release-please opens or updates the release PR | Release App token, environment `release`, secrets `RELEASE_APP_ID` and `RELEASE_APP_PRIVATE_KEY` |
| 4 | Owner merges the release PR; the release job creates the tag and GitHub release at the release commit | App permission **Workflows: write** |
| 5 | `promote` retags the `sha-` image as `vX.Y.Z` (same digest, never rebuilt); `smoke` runs | `sha-` image still on GHCR, `promote-release@v1` |
| 6 | Renovate in cloud-infra sees the new tag and pushes `renovate/ziftbook-staging-ziftbook-images` | `GHCR_READ_TOKEN`, regex versioning in `renovate.json` |
| 7 | CI runs on that branch push | `ci.yml` `push` filter `renovate/ziftbook-staging-**` |
| 8 | Renovate fast-forwards the branch into `main` without a PR | Renovate's branch-automerge permission (owner private ops notes); the pushed commit must already have both checks green |
| 9 | Flux applies the commit; Deployments use `Recreate` | `sops-age`, `ghcr-pull` |
| 10 | CI step `prod tags not ahead of staging` (`check-prod-tags.py`, in `Kubernetes checks`) fails a PR whose `<app>-prod` image tags are newer than that app's `<app>-staging` tags on the tree (the lowest of them, so a staging-only image such as `tbd/mcp` left on an older tag blocks the prod bump: tbd must promote mcp on every release) | Required check `Kubernetes checks` in the `main protection` ruleset (no ruleset change); covers any `<app>-prod` dir with no code change |

Step 10 is a required check: since INFRA-107 the no-bypass ruleset blocks a red merge for everyone, and Renovate's fast-forward lands only a commit whose checks are already green. The same step also runs on every push to `main`. It compares tags in git, not what staging actually runs (the post-deploy smoke and the staging `/health` do that), and a PR that bumps staging and prod together passes it (review). A red check means "wait for the staging bump on `main`, then update the branch".

Drift watch: workflow `release-drift-probe.yml` (daily) opens or updates one `[release-drift]` issue when an app's latest GitHub release has been absent from `clusters/` for 2+ days (production bump PR unmerged, or Renovate never opened it), and closes it when clear. No credentials; it reads the app repos' releases because GHCR is private. It depends on `tbd` and `ziftbook` staying public repos (GITHUB_TOKEN reads); the run goes red if either goes private. Only repos in `WATCH_REPOS` (default `ziftbook tbd`, tbd since the INFRA-48 cutover) are checked.

## GitHub

### GHCR retention (INFRA-92)

Workflow `ghcr-retention.yml` (weekly, Monday 05:41 UTC, plus manual dispatch) prunes the 7 private container packages `tbd/{backend,migrations,frontend,mcp}` and `ziftbook/{backend,frontend,migrations}`: untagged versions older than 7 days and sha- versions older than 30 days go, unless a kept index references them or `clusters/**` pins their digest; any version with a v*, major.minor, `latest` or other non-sha- tag is never deleted. The token is the run's `GITHUB_TOKEN` (`packages: write`), no PAT.

| Setting | Where | Notes |
|---|---|---|
| Admin role for repo cloud-infra on each package | Package > Package settings > Manage Actions access (owner, UI, per package) | Deleting a version needs Admin; Read (INFRA-107) is not enough. Granting cloud-infra Admin on a package is an owner decision; the risk assessment is in the private ops notes. Grant per app when enabling it (ziftbook first) |
| `DELETE_APPS` | `env:` in `.github/workflows/ghcr-retention.yml` (git) | Empty = every run is a dry run. `"ziftbook"` or `"ziftbook,tbd"` enables deletes for those apps, only on `main` and only on the schedule or a dispatch with mode=delete |
| Delete cap | `max_deletes` dispatch input; `MAX_DELETES` env | Per package, counting only packages that will delete (app in `DELETE_APPS`, delete mode); dry-run packages never count. Default 150, about 2x the measured weekly churn per package (input default and scheduled fallback). Over the cap in any deleting package aborts the run with zero deletes |
| Restore window | GitHub | A deleted version can be restored for 30 days (runbook "GHCR retention") |

The step summary and logs are public.

Symptoms: run red with `DELETE ... HTTP 403` = the Admin grant is missing for that package, or (403/429 after many deletes) the GITHUB_TOKEN rate limit: rerun later, the outermost-first order keeps a partial run safe; red with `version_count ... does not match` = versions changed while listing, rerun; red with `pinned digest ... is not in the listing` = `clusters/` pins an image GHCR no longer has (a real outage risk, fix the pin first).

### Branch protection

Branch and tag protection per repo: see the owner's private ops notes.

cloud-infra `main` also has a ruleset with **no bypass** that requires `Terraform checks` and `Kubernetes checks`
(strict), so not even an admin merge goes past a red check (INFRA-107, created 2026-10-23). A staging bump that Renovate
automerges as a branch still lands, because the pushed commit already carries both green checks.

### Tag protection

Git release tags `v*` are immutable by policy. Each repo below has a ruleset `tag protection` on
`refs/tags/v*`: restrict creations, updates and deletions, block force pushes. Only the bypass actors
can create a `v*` tag, so the release App must stay on the list of every app repo or the next release
fails at the tag (see the symptom table). The App bypass is only as strong as the `release`
environment that holds its key (`main` only, see [Per-repo settings](#per-repo-settings)) and the
`main` branch protection. These rulesets cover git refs only, not the GHCR `vX.Y.Z` image tags.

Per-repo rulesets and bypass lists: see the owner's private ops notes. Each app repo needs the ruleset, the App installed and the `release` environment ([Per-repo settings](#per-repo-settings)).

### GitHub Apps

Org settings > GitHub Apps (installations). Permission changes ask the org owner to accept on the
installation, and the grant applies to **every repository in the installation**.

| App | Repos | Notes |
|---|---|---|
| `fjcloudaiconsulting-release` | selected: each app repo on the shared release flow (Ziftbook, app-template, tbd from INFRA-42), and `.github` (2026-10-10, INFRA-161) | Permissions: contents, issues, pull requests write; metadata read; **workflows write** (added 2026-10-03, INFRA-78). Private key held by the owner and stored only as the `release` environment secret. In `.github` the key moves `v1` by design: a release there happens only when the owner merges the Release PR (`main` protected, 1 approving review). Keep cloud-infra out of the selection |
| `renovate` (Mend) | selected: `.github`, cloud-infra, ziftbook, tbd (added 2026-10-04, INFRA-109) | Repos are added on GitHub, not in Mend |
| `terraform-cloud`, `gitguardian`, `atlassian`, `claude`, `tbd-branch-protection-probe` | various | Not part of the release chain. `atlassian` links PRs to Jira |

### Per-repo settings

- **Environment `release`** (Ziftbook, tbd from INFRA-42, `.github` from INFRA-161; create the same in every new app repo, app-template does not ship
  it): deployment branches limited to `main`, **no required reviewers** (the job runs on every `main`
  push, a reviewer would block each one), secrets `RELEASE_APP_ID` and `RELEASE_APP_PRIVATE_KEY`.
- **Environments `tbd-prod`, `tbd-staging`, `ziftbook-staging` and `ziftbook-prod`** (cloud-infra, INFRA-114, INFRA-90): the post-deploy smoke jobs run in them.
  `tbd-prod`: deployment branches "Selected branches and tags", `main` only, no tag rule, no required reviewers;
  secrets `SMOKE_USERNAME` and `SMOKE_PASSWORD` (the [TBD smoke account](#cluster-out-of-band-material)). A missing
  secret shows as `app smoke failed` on the `[post-deploy-smoke] tbd-prod` issue. `tbd-staging` (INFRA-135), `ziftbook-staging` and `ziftbook-prod` hold nothing (a job
  that names a missing environment creates it, without restrictions).
- **Environment `landing`** (tbd and ziftbook, INFRA-60/INFRA-98): deployment branches `main` only; secret
  `CLOUDFLARE_API_TOKEN`, a per-Worker token for that app's landing Worker. Scopes and rules:
  [Worker and Snippet access](#worker-and-snippet-access-infra-98).
- **GHCR package read access for CI** (INFRA-107): every `tbd` and `ziftbook` package grants the `cloud-infra` repo Read under Package settings > Manage Actions access. The `image provenance` step of `Kubernetes checks` needs it to verify attestations; a missing grant shows as a red `Kubernetes checks` with a registry authorization error. A new image needs the grant after its first publish.
- Dependabot and per-repo GitHub settings: see the owner's private ops notes.
- Actions default workflow permission is read-only everywhere checked. "Allow GitHub Actions to create and approve pull requests" stays off (release-please uses the release App token, nothing approves with `GITHUB_TOKEN`). Per-repo state: see the owner's private ops notes.
- `.github` hosts the contract, the reusable workflows, the Renovate preset and the weekly conformance
  probe. Apps consume them by the moving major tag `@v1`. A merged `feat`/`fix` opens a release-please Release PR there;
  the owner merging it is the approval, and the `publish` workflow tags `vX.Y.Z` and moves `v1` (INFRA-161).

### Actions secrets and variables

**Rule (owner, 2026-10-04, INFRA-99).** GitHub keeps only the secrets and variables a workflow needs to
run: CI, release and deploy credentials and deploy targets. Every value an app reads at runtime, secret or
not, lives under `clusters/` once Flux deploys the app: secrets in a SOPS-encrypted Secret, everything else
as plain `env` in the app's manifests. A runtime value found in GitHub moves there, or is deleted from GitHub
when the cluster already holds it.
Every GitHub secret or variable has a workflow on `main` that reads it; one without a reader is deleted.
The app-template README carries the same rule for new apps.

Inventory, 2026-10-04, every repo in the org (names from the API, consumers from `main`). Org level: no Actions secrets,
Actions variables or Dependabot secrets. cloud-infra (until INFRA-114's `tbd-prod` environment, row below) and app-template: none at any level; `.github`: only env `release` (row below, INFRA-161)
(app-template's `ci.yml` reads the two `release` secrets, which each new repo sets per its checklist).
tbd and ziftbook have no repo-level Dependabot secrets; tbd's environment `copilot` and fjconsulting-website's environment `dev` hold nothing.

| Repo | Level | Name | Read by | Class | Fate |
|---|---|---|---|---|---|
| tbd | env `release` | secrets `RELEASE_APP_ID`, `RELEASE_APP_PRIVATE_KEY` | `ci.yml` job `Release` (shared `release.yml@v1`, INFRA-159) | pipeline | stays |
| .github | env `release` (main only) | secrets `RELEASE_APP_ID`, `RELEASE_APP_PRIVATE_KEY` | `publish.yml` (INFRA-161) | pipeline | stays |
| tbd | repo | secret `GIST_TOKEN` | `ci.yml` (coverage badges, `main` only) | pipeline | stays |
| tbd | repo | secret `PROTECTION_PROBE_APP_KEY`, variable `PROTECTION_PROBE_APP_ID` | `branch-protection-probe.yml` | pipeline | stays |
| cloud-infra | env `tbd-prod` (main only) | secrets `SMOKE_USERNAME`, `SMOKE_PASSWORD` | `post-deploy-smoke.yml` (INFRA-114) | pipeline: the post-deploy smoke logs in as the smoke account; copy of `tbd-prod/tbd-smoke` | stays |
| ziftbook | env `release` | secrets `RELEASE_APP_ID`, `RELEASE_APP_PRIVATE_KEY` | `ci.yml` job `Release` (shared `release.yml@v1`, INFRA-159) | pipeline | stays |
| tbd | env `landing` (main only) | secret `CLOUDFLARE_API_TOKEN` | `apex-deploy.yml` `deploy-worker` | pipeline; token `tbd-landing deploy`, adopted by workspace `cloudflare-tokens` (INFRA-133 import), value set by hand when the token was made | stays |
| ziftbook | env `landing` (main only) | secret `CLOUDFLARE_API_TOKEN` | `landing.yml` | pipeline; token `ziftbook-landing deploy`, adopted by workspace `cloudflare-tokens` (INFRA-133 import), value set by hand when the token was made | stays |
| fjconsulting-website | repo | secrets `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | `deploy.yml`, `deploy-dev.yml` | pipeline; token `fjconsulting-website-github-actions` (its HCP workspace uses `fjconsulting-website-terraform-cloud`), both adopted by `cloudflare-tokens` (INFRA-133 import), scopes changed only there | stays |

`GHCR_READ_TOKEN` is a Mend org secret, not a GitHub one (see [Mend Renovate](#mend-renovate)). No app
runtime value is left in GitHub. A new repo in the org gets its rows here.

## Mend Renovate

Hosted Mend app, Free plan, repos selected in
the Mend installation settings of the org.

- **`GHCR_READ_TOKEN`**: Mend org secret, a classic PAT with only `read:packages`, 90-day expiry, created
  2026-10-02 (expires around 2026-12-31). cloud-infra's `renovate.json` reads it as
  `{{ secrets.GHCR_READ_TOKEN }}`; Mend rejects encrypted secrets in repo config. Rotation: new token,
  replace the Mend secret.
- cloud-infra rules in `renovate.json`: database majors never proposed (data dirs upgrade by hand after a
  fresh dump); `ghcr.io/fjcloudaiconsulting/**` tracks `vX.Y.Z` only, no digest pin, production bumps stay
  PRs; manifests under `clusters/**/ziftbook-staging/**` bump in one grouped branch and automerge by
  branch. `clusters/**/tbd-staging/**` the same way (INFRA-91, group `tbd staging images`, branch prefix
  `tbd-staging-`; the MCP image moves in the same group, INFRA-147); `tbd-prod` is one grouped PR (backend, migrations, frontend, scheduler if present), no automerge.
- Each repo has a Dependency Dashboard issue; ticking "run again" there forces a run.
- App repos (tbd, ziftbook) add repo-level rules in their own `renovate.json` (tbd#833, ziftbook#180), not in the shared preset: there they would also bundle cloud-infra's platform image bumps into one docker group and put staging app-image majors behind dashboard approval, breaking the ziftbook-staging automerge. Rules: non-major updates grouped per ecosystem, FastAPI and uvicorn standalone, OpenTelemetry in its own group, npm updates wait 1 day (pnpm 12's default release age), majors and runtime upgrades only on Dependency Dashboard approval, datastore image majors off. tbd needs no `GHCR_READ_TOKEN`: it references no private `ghcr.io` image.
- Local dry run (check a config without waiting for Mend): Node 24, then `renovate --platform=local --dry-run=full` with `GITHUB_COM_TOKEN`, `RENOVATE_TOKEN` and `RENOVATE_SECRETS='{"GHCR_READ_TOKEN":"..."}'` in the environment. The local platform reads only committed files, and `local>` presets do not resolve there. `LOG_LEVEL=debug LOG_FORMAT=json` prints a `packageFiles with updates` record with each update's branch; it does not render the dashboard, so approval gates show only on the first Mend run.

## HCP Terraform

Org `FlamaCorp`. Workspaces are created in the UI or through the API with the owner's token (VCS flow, plan on PR,
apply on merge after approval in the UI). The workspace name must equal the `cloud` block name, and the `tbd-backups`
name is pinned in an AWS trust policy: never rename it.

| Workspace | Stack | Auth |
|---|---|---|
| `cloudflare` | `terraform/cloudflare` | Sensitive env var `CLOUDFLARE_API_TOKEN`, written by `cloudflare-tokens` (token `tf: cloudflare workspace`; its scopes are `cloudflare_account_token.cloudflare_workspace` there, never edit them in the dashboard); variable `account_id` |
| `cloudflare-tokens` (INFRA-133) | `terraform/cloudflare-tokens` | Env `CLOUDFLARE_API_TOKEN` (**sensitive**): the bootstrap token, the one hand-made Cloudflare token (account-owned, Account API Tokens: Edit only, stored by the owner with a script that checks it first). Only this workspace holds it, and the workspace does not manage it. Env `TFE_TOKEN` (**sensitive**): token of team `cloudflare-tokens`, custom access on workspace `cloudflare` only (variables write, runs read). Token scopes change only in `terraform/cloudflare-tokens`; exposure notes: see the owner's private ops notes |
| `tbd-backups` | `terraform/tbd-backups` | OIDC, two roles (plan, provisioner); variable `aws_account_id` |
| `hetzner` (INFRA-169) | `terraform/hetzner` | Env `HCLOUD_TOKEN` (**sensitive**): a project token, held only in the workspace. Variables `ssh_allowed_cidrs` (HCL list of IPv4 CIDRs allowed on port 22) and `deploy_ssh_public_key` (`ssh-ed25519 ...`, not sensitive). Both have no default, so a plan without them fails. The Hetzner project and its tokens are hand-made |

The org also holds workspaces `tbd` (repo tbd, `infra/terraform`) and `fjconsulting-website`, which belong to those repos. CI pins Terraform 1.16.6; check the workspaces' Terraform version matches after a bump.
Every stack requires the same `~> 1.16.0` (a test holds it to the CI pin), so a workspace left on another minor fails
its plan. Lock files carry `h1:` hashes for both platforms in use: refresh them with
`terraform providers lock -platform=linux_amd64 -platform=darwin_arm64`.

A speculative plan runs with the workspace's own credentials. `tests/test_terraform_fences.py` keeps every stack to
the providers and provider arguments listed in that file and refuses `external` and `http` data sources, remote
state, provisioners, remote modules, override files and file reads outside the repository. A new provider or
provider argument is a change to those lists in the PR that needs it.

Never set `TFC_AWS_RUN_ROLE_ARN` on `tbd-backups`: it would give unapproved PR plans the apply role. The IAM documents are
in [`aws/bootstrap/`](../aws/bootstrap/); root creates the roles once and a workspace never manages its
own role.

## AWS

- The backup chain (Object Lock bucket, KMS key, uploader users, OIDC providers, role `github-actions-backup-probe`) is managed by the `tbd-backups` stack; the OIDC provider `app.terraform.io` and the `tfc-*` roles were created by hand once (root). Details: `terraform/tbd-backups/README.md`.
- Access, audit and cost alerts: see the owner's private ops notes.

## Cloudflare

- `thebetterdecision.com`: managed by `terraform/cloudflare`, SSL mode Full (strict), HSTS one year. Proxied to the
  node: none since INFRA-176 (`app` is TBD production, through the tunnel). The apex is the Worker `tbd-landing`
  (custom domain attached by hand, INFRA-61, [below](#worker-and-snippet-access-infra-98)); `www` is proxied only so the
  Terraform redirect rule `tbd_redirects` can 301 it to the apex. The rest is DNS-only.
- `ziftbook.com`: the zone itself is read, not created, by Terraform, but its settings (HSTS one year,
  minimum TLS 1.2, SSL Full strict) and the proxied records `dev.ziftbook.com` (staging) and `app.ziftbook.com` (production, INFRA-82; both CNAMEs to the Hetzner tunnel, INFRA-173 and INFRA-181) are managed there. Apex and www are the Worker `ziftbook-landing`, deployed by Ziftbook CI; the Terraform rule `ziftbook_redirects` 301s www to the apex before the Worker (INFRA-131).
- Hostnames (owner ruling 2026-10-03): staging `dev.<domain>`, production `app.<domain>`, other services
  by the same pattern (`docs.`, `blog.`). Ziftbook staging is `dev.ziftbook.com` (INFRA-47).
- Mail: Mailgun EU for every app and environment (owner ruling 2026-10-03). All dev/staging environments share one
  sending domain, `m.fjconsulting.dev` (zone `fjconsulting.dev`, read but not managed by `terraform/cloudflare`, which
  owns only the `m.` records: MX, SPF, DKIM `mta._domainkey.m`, DMARC with Mailgun reporting, tracking, same set as TBD). Each dev environment has its own
  send-only sending key scoped to that domain. Production gets per-app `m.<appdomain>` domains (TBD:
  `m.thebetterdecision.com`; Ziftbook: `m.ziftbook.com`, records `ziftbook_mail` in `terraform/cloudflare`, DKIM selector `email`, INFRA-139). The domain and the keys are made by hand (below). Env names `ZIF_MAILGUN_DOMAIN`,
  `ZIF_MAILGUN_REGION`, `ZIF_MAILGUN_API_KEY`; the Ziftbook code that reads them is ZIF-151.
- Shared-domain webhooks: Mailgun delivers every dev app's events to every webhook URL registered on `m.fjconsulting.dev`,
  so an app must ignore events for messages it did not send (apps tag their messages; handled in the ZIF tickets).
- Rate limit (INFRA-123, `terraform/cloudflare/rate_limit.tf`): a new sign-in or token endpoint, or an unauthenticated one that queries
  the database, in either app needs its path added there. The zone allows one such rule (Free), managed only in Terraform. The path match relies on the
  zone's URL normalization staying on (type Cloudflare, scope incoming; a dashboard setting, not in Terraform; read
  2026-10-04 on both app zones).
- Body cap (BE-13, `terraform/cloudflare/body_limit.tf`): a custom rule on thebetterdecision.com blocks a request to `app.` or `dev.` whose declared `Content-Length` is over 16 MiB. Custom rules on this zone are managed only in Terraform: one added in the dashboard is drift the next apply removes. Token scope: Zone WAF Write, as for the rate limit.
- Cloudflare Access on `dev.thebetterdecision.com` (INFRA-117, `terraform/cloudflare/access.tf`): email one-time PIN, allow-list the owner, bypass only `/health` and `/robots.txt` for the post-deploy smoke, and the MCP OAuth paths `/mcp`, `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource` and `/api/v1/oauth/*` (INFRA-147: an MCP client cannot pass the PIN). The consent page `/oauth/authorize` stays behind Access: its auth calls need the Access session, so the browser gets the PIN prompt first. MCP and the OAuth routes are staging only (owner ruling 2026-10-09); the staging backend sets `MCP_OAUTH_ENABLED=true` for tbd's off-by-default switch, and the tunnel (`terraform/cloudflare/tunnel.tf`) routes the discovery documents to the backend and exactly `/mcp` to the MCP server. Out of git, once: Zero Trust enabled on the account (Free plan, team name) and the One-time PIN login method on. Token scope: `terraform/cloudflare-tokens`. Procedures: [runbooks.md](runbooks.md#cloudflare-access-on-tbd-staging).
- API tokens (INFRA-133): every account-owned token is managed by `terraform/cloudflare-tokens` (workspace
  `cloudflare-tokens`), except the bootstrap token that workspace runs with. Change a scope there, never in the
  dashboard: the next plan shows a hand edit as drift and the apply reverts it. User tokens (My Profile > API Tokens)
  are out of reach: an account-owned token cannot read or manage them. After the cutover no automation uses one.
  Procedures: [runbooks.md](runbooks.md#cloudflare-api-tokens).

### Worker and Snippet access (INFRA-98)

Cloudflare proves the zone, not the code. A Worker or Snippet that runs on thebetterdecision.com or ziftbook.com can set
`x-real-ip` on a subrequest to a hostname of the **same zone**, and Cloudflare forwards it to the node as
`CF-Connecting-IP`, which TBD trusts (rate limiting, `audit_events.ip_address`). A Worker on another zone or on
workers.dev cannot: Cloudflare replaces the value on cross-zone subrequests. The Hetzner tunnel hostnames
(`dev.thebetterdecision.com`, `dev.ziftbook.com`, INFRA-173; `app.ziftbook.com`, INFRA-181; `app.thebetterdecision.com`, INFRA-175) get the same header from
Cloudflare, so the same applies.

Workers custom domains, all attached by hand (not in Terraform or `wrangler.jsonc`, so no plan shows their drift). Each
one owns a read-only proxied DNS record that Cloudflare creates and deletes with it; a CNAME on the same name blocks
the attach. Recreate one with the Cloudflare API (`PUT /accounts/<account>/workers/domains`, body
`{"hostname": "<host>", "service": "<worker>"}`) or the Worker's Settings > Domains & Routes > Add > Custom domain.

| Hostname | Worker | Since |
|---|---|---|
| `ziftbook.com`, `www.ziftbook.com` | `ziftbook-landing` (www 301ed by the `ziftbook_redirects` zone rule before the Worker, INFRA-131; http by Always Use HTTPS and the Worker) | before 2026-10-04 |
| `thebetterdecision.com` | `tbd-landing` | INFRA-61. Not `www`: the `tbd_redirects` rule in `terraform/cloudflare` 301s it to the apex |

Who can put code on a zone and the token scopes: see the owner's private ops notes. Token scopes change only in `terraform/cloudflare-tokens`.

Rules:

- A CI deploy token is an account-owned token with the **Editor** role scoped to **its one Worker**, no
  Zone > Workers Routes, no Snippets, no Pages, stored as an environment secret limited to `main`. Product-scope
  Editor (the legacy "Workers Scripts: Edit") rewrites every Worker in the account, current and future, so either
  landing token could replace the other app's landing. Zone > Workers Routes > Write on a zone can route any hostname
  of it, node hostnames included: Cloudflare tokens cannot be limited by hostname.
- Custom domains are attached by the owner (or Terraform), not by CI, and are not declared in `wrangler.jsonc`: a
  per-Worker Editor deploys an existing Worker only while the deploy does not add, change or remove a route or
  custom domain, and Custom Domains do not support per-Worker roles yet. Ziftbook stopped declaring them in ziftbook#181 (INFRA-113).
- Residuals and gaps: see the owner's private ops notes.

## Grafana Cloud

Made by hand by the owner (INFRA-85); the cluster side is `clusters/hetzner/observability/`. Procedures:
[runbooks.md](runbooks.md#metrics-to-grafana-cloud-alloy).

| Item | Where | Notes |
|---|---|---|
| Stack (EU region) | grafana.com Cloud Portal, the org's stack | Its OTLP endpoint (`https://otlp-gateway-prod-eu-<n>.grafana.net/otlp`) and Instance ID are keys `otlp-endpoint` and `instance-id` of Secret `observability/grafana-cloud` |
| Access policy `k3s-alloy-metrics-write` | Grafana Cloud > Administration > Cloud access policies | Realm: the stack only. Scopes `metrics:write`, `traces:write` (added for INFRA-106) and `logs:write` (ZIF-89, Ziftbook logs); edit the policy, the existing token inherits it. Symptom of a missing scope: Alloy logs `401` or `403` from `otelcol.exporter.otlphttp` on `/v1/traces` or `/v1/logs` (those logs are dropped, not retried) |
| Logs retention 14 days | The stack's plan: Grafana Cloud Free keeps logs a fixed 14 days, no setting (owner ruling, ZIF-89) | Stack-wide. The Ziftbook privacy notices say Grafana Labs keeps server logs up to 14 days: a plan change (Pro keeps 30 days) needs the notices changed first. Symptom of a drift: Explore returns Ziftbook log lines older than 14 days |
| Token `alloy-platform-node` on that policy (named for the old Lightsail node; the same token runs on Hetzner) | Key `token` of Secret `observability/grafana-cloud` (`clusters/hetzner/observability/grafana-cloud.secret.yaml`, a whole new file each time) | No expiry; rotate on suspicion. Write-only: it cannot read or delete data |
| Folder `Platform` (uid `platform`), contact point `owner-email`, rule group `node-memory`, dashboard `Node memory` (uid `node-memory`) | The stack (INFRA-108), applied through the Grafana API from [`grafana/`](../grafana/) | The git files are the source; rules and contact point are API-provisioned, so the UI cannot edit them; dashboard UI edits are lost on the next apply. Apply procedure and alert table: [runbook](runbooks.md#memory-alerts). No service account or token is kept |
| Alert rule group `log-retention` (`LogRetentionStale`) | The stack (INFRA-130), applied through the Grafana API from [`grafana/log-retention.rules.json`](../grafana/log-retention.rules.json) | API-provisioned like `node-memory`; apply command in the [runbook](runbooks.md#request-log-retention). Apply after every change to the file. |
| Synthetic Monitoring checks `tbd-prod-health`, `ziftbook-prod-health` and alert rule group `synthetic-uptime` (`ProdEndpointDown`) | The stack (INFRA-172), applied through the Synthetic Monitoring API and the Grafana API from [`grafana/synthetic-*.json`](../grafana/) | HTTP checks every 2 min from 2 EU public probes; the SM access token is revoked after applying and not kept. Apply and free-tier math: [runbook](runbooks.md#synthetic-uptime). Applied and alert-tested 2026-10-20. `tbd-prod-health` targets `/health` since INF-09 (the database probes are not routed publicly): re-apply after that change |
| Alert rule groups `ziftbook-app` (`ZiftbookApiErrors`, `ZiftbookJobsGaveUp`, `ZiftbookRefundFailed`, `ZiftbookJobQueueStuck`) and `node-disk` (`NodeDiskHigh`) | The stack (ZIF-89), applied through the Grafana API from [`grafana/ziftbook-app.rules.json`](../grafana/ziftbook-app.rules.json) and [`grafana/node-disk.rules.json`](../grafana/node-disk.rules.json) | API-provisioned like `node-memory`; apply and the metric-name check: [runbook](runbooks.md#ziftbook-app-alerts). Apply after every change to the file. |
| Alert rule group `tbd-app` (`TbdCspReportsDropped`, `TbdCspReportSinkFailing`) | The stack, applied through the Grafana API from [`grafana/tbd-app.rules.json`](../grafana/tbd-app.rules.json) | API-provisioned like `node-memory`; apply and the metric-name check: [runbook](runbooks.md#tbd-app-alerts). Apply after every change to the file. |

## Cluster out-of-band material

| Item | Where | Notes |
|---|---|---|
| SOPS age private key | Owner's offline backup and `flux-system/sops-age` | Public key in `.sops.yaml`. Rotated once (2026-10-02). Never displayed in a shell or chat. Also a recipient of `ansible/*.sops.yaml`, but Ansible runs never need it |
| Ansible age key (INFRA-64) | Operator laptop, `~/.config/sops/age/ansible-hetzner.txt` (0600); public key `age17qhast...` in the `ansible/` rule of `.sops.yaml` | Decrypts only `ansible/*.sops.yaml` (the k3s token); runs set `SOPS_AGE_KEY_FILE` to it ([`ansible/README.md`](../ansible/README.md)). Lost or leaked: make a new key, swap its public key into `.sops.yaml`, run `sops updatekeys` on each `ansible/*.sops.yaml` with the owner key. INFRA-180 adds a CI recipient |
| `ghcr-pull` credentials | `clusters/hetzner/namespaces/<namespace>-ghcr-pull.secret.yaml` (tbd-staging on hetzner since INFRA-173) | GHCR read credential, the same in every namespace; expires with the token behind it |
| MySQL database and user for TBD staging | `tbd_staging` and `'tbd_staging'@'%'` in `data/mysql`, created by hand ([runbook](runbooks.md#mysql-database-per-app-environment), INFRA-67) | ALL on `tbd_staging` only, `MAX_USER_CONNECTIONS 20`. Its password is the one in `tbd-staging/tbd` `database-url`. The nightly grants dump carries the user, so a restore brings it back; the database itself is dumped once the `tbd-staging-mysql` set exists (INFRA-67 backup PRs) |
| TBD staging secrets | `tbd-staging/tbd` (generated for staging, no production value), `tbd-staging/tbd-captcha` (Turnstile widget `tbd-staging`, hostname `dev.thebetterdecision.com` only, Cloudflare dashboard > Turnstile), `tbd-staging/tbd-mailgun` (send-only key for `m.fjconsulting.dev`, staging only; optional in the Deployment, without it TBD only logs mail, INFRA-146), `tbd-staging/tbd-mcp` (key `jwt-secret-key`, random, the MCP server's own; it reads its database URL and API token HMAC key from `tbd` by key, INFRA-147) | Each is a whole new file, so only the public SOPS key is needed. A new `tbd` file means new JWT and encryption keys: staging users log in again and lose MFA and AI credentials |
| NetBird | Groups and policies are in Terraform (`terraform/netbird`); setup key and kubeconfig: see the Hetzner cluster row below | `owner-admin` token lasts 90 days from the mint (renewal and revoke-all: [bootstrap runbook](runbooks.md#hetzner-cluster-bootstrap) step 6). Setup key: step 4 |
| Postgres roles for Ziftbook | Jobs `ziftbook-bootstrap` (database `ziftbook`, staging, Hetzner cluster only since INFRA-173) and `ziftbook-prod-bootstrap` (database `ziftbook_prod`, INFRA-82) in `data`, both from a pinned commit of the Ziftbook repo (sha256 checked) | The migrations hardcode `ziftbook_app` and `ziftbook_migrate`, so production's own login roles `ziftbook_prod_app` and `ziftbook_prod_migrate` are members of them (no inherit) and `SET ROLE` to them at login; CONNECT on each database is limited to its own environment's logins (verified on a local Postgres 18 with the real migrations). A rotated staging password goes into `ziftbook-staging/ziftbook.secret.yaml`; production's passwords are in `data/postgres-ziftbook-prod` |
| Ziftbook production secrets | `data/postgres-ziftbook-prod`, `ziftbook-prod/ziftbook` (generated together), `namespaces/ziftbook-prod-ghcr-pull.secret.yaml` (staging's credential), `ziftbook-prod/ziftbook-mailgun` (optional, key `api-key` of the `m.ziftbook.com` sending key, INFRA-139) | Whole new files; [runbooks.md](runbooks.md#ziftbook-production-secrets). Only the pull secret needs the offline key. No mail from production until the Mailgun domain and key exist |
| Ziftbook worker heartbeat (ZIF-21) | Healthchecks.io checks `ziftbook-staging-worker` and `ziftbook-prod-worker` in the owner's account (period 1 minute, grace 5 minutes); each ping URL is Secret `ziftbook-healthcheck` (key `url`, `clusters/hetzner/ziftbook-<env>/ziftbook-healthcheck.secret.yaml`) | Optional in `worker.yaml`. Whole new file per change, so only the public SOPS key is needed; restart `deploy/worker` after. Alerts go to the account's integrations. [Runbook](runbooks.md#ziftbook-worker-heartbeat-healthchecksio) |
| R2 for Ziftbook photos (ZIF-133) | R2 enabled on the account by hand (payment method on file); buckets, CORS, lifecycle and custom domains in `terraform/cloudflare/r2.tf`; tokens `ziftbook-staging-storage` and `ziftbook-prod-storage` (R2 > Manage API tokens, Object Read & Write, that env's two buckets) made by hand; their keys in Secret `ziftbook-storage` (`access-key-id`, `secret-access-key`) per namespace | Whole new file per change, public SOPS key only. Rotate: new token, new file, restart backend and worker, delete the old token. [Runbook](runbooks.md#r2-for-ziftbook-photos) |
| Ziftbook Turnstile (ZIF-116) | Widgets `ziftbook-staging` (hostname `dev.ziftbook.com`) and `ziftbook-prod` (`app.ziftbook.com`), Cloudflare > Turnstile, outside Terraform (created through the API on 2026-10-18; the dashboard works too); keys in Secret `ziftbook-turnstile` (`site-key`, `secret`) per namespace | Optional in `backend.yaml` and `frontend.yaml`, but without it every public booking is refused. Whole new file per change, public SOPS key only. [Runbook](runbooks.md#ziftbook-turnstile-public-bookings) |
| Ziftbook Stripe (ZIF-158) | Stripe account, sandbox for staging: API keys and the webhook endpoint `https://dev.ziftbook.com/api/stripe/webhook`, made in the Stripe dashboard; keys in Secret `ziftbook-stripe` (`secret-key`, `webhook-secret`, `publishable-key`), staging only until go-live | Optional in `backend.yaml` and `worker.yaml` (secret key only): without it payments are off. Whole new file per change, public SOPS key only. [Runbook](runbooks.md#ziftbook-stripe-payments) |
| Backup upload key | Secret `data/backup-s3` | Access key of IAM user `k3s-backup-uploader` (from the `tbd-backups` stack); rotate per `terraform/tbd-backups/README.md` and re-encrypt |
| TBD app secret | `tbd-prod/tbd`, `clusters/hetzner/tbd-prod/tbd.secret.yaml` (INFRA-48, moved INFRA-175) | Values were carried over from the old hosting at the INFRA-48 cutover (same values, or logins and encrypted columns break); `database-url` is built from the `data/mysql` Secret. Keys: the `secretKeyRef` entries in `tbd-prod/backend.yaml` (`ai-credential-encryption-key-prev` is optional). A whole new file each time, so only the public key is needed. A rotated MySQL app password must be written here too |
| TBD smoke account | `tbd-prod/tbd-smoke`, `clusters/hetzner/tbd-prod/tbd-smoke.secret.yaml` (INFRA-48, moved INFRA-175), cloud-infra environment `tbd-prod` secrets `SMOKE_USERNAME` / `SMOKE_PASSWORD` (the post-deploy smoke, INFRA-114) | Production user for `scripts/smoke-test.sh` (account controls: owner private ops notes). Every store must hold the same values; the cluster copy is the readable one. Rotation and manual run: [runbooks.md](runbooks.md#tbd-smoke-account). Rotated 2026-10-04 |
| Mailgun domain `m.fjconsulting.dev` (EU, shared by all dev environments) | Mailgun dashboard > Sending > Domains; DNS in `terraform/cloudflare`; one sending key per environment (`ziftbook-staging`, `tbd-staging`) | Key `api-key` of Secret `ziftbook-mailgun` (`clusters/hetzner/ziftbook-staging/ziftbook-mailgun.secret.yaml`, a whole new file each time, so only the public SOPS key is needed); the worker reads it as optional. Rotate: create a new key in Mailgun, regenerate the file, restart `deploy/worker`, delete the old key |
| Mailgun domain `m.ziftbook.com` (EU, Ziftbook production) | Mailgun dashboard > Sending > Domains (created 2026-10-06 by API); DNS `ziftbook_mail` in `terraform/cloudflare`; sending key `ziftbook-prod` (send-only, this domain) | Key `api-key` of Secret `ziftbook-mailgun` (`clusters/hetzner/ziftbook-prod/ziftbook-mailgun.secret.yaml`, INFRA-139); the worker reads it as optional. Rotate as for the dev domain, then restart `deploy/worker` in `ziftbook-prod` |
| k3s `GOGC=50` | Ansible sets it (`extra_service_envs` in `ansible/group_vars/all.yml`, INFRA-64) in `/etc/systemd/system/k3s.service.env` and restores it on every run, upgrades included | Lowers the k3s process from about 846Mi to about 625Mi RSS for a little CPU (measured on the old Lightsail node, INFRA-80). Check: `kubectl get --raw /metrics \| grep '^go_gc_gogc_percent'` shows 50 |
| Flux | GitRepository `flux-system`, public GitHub over HTTPS, no deploy key | Interval 1 minute, Kustomization 10 minutes, `prune: true`, no health checks |
| Hetzner cluster (INFRA-171) | `flux-system/sops-age` (the same key), `cloudflared/cloudflared-token` (`clusters/hetzner/cloudflared/cloudflared-token.secret.yaml`, from the `cloudflare` workspace output `hetzner_tunnel_token`), NetBird setup key `hetzner-k3s` (group `k3s-hetzner`) in `clusters/hetzner/netbird/setup-key.secret.yaml`, NetBird policy `owner-to-hetzner-k3s` (`owner-devices` to group `k3s-hetzner`, TCP 6443 only; in Terraform, `terraform/netbird`, workspace `netbird` with env `NB_PAT` from the NetBird service user `terraform`), kubeconfig `~/.kube/hetzner` (server `hetzner-k3s.netbird.cloud`) | Written once by the [bootstrap runbook](runbooks.md#hetzner-cluster-bootstrap). A rotated tunnel token (Cloudflare dashboard or a replaced tunnel) needs step 3 again and `kubectl -n cloudflared rollout restart deploy/cloudflared` |

## Jira

Project `INFRA` on the `fjconsulting.atlassian.net` site, board 70, one-week sprints. The Atlassian GitHub
integration links PRs by key in the branch name or title. Smart commits (`KEY-nn #comment ...`) go in the
commit body only.

## Expiries and rotation

| What | When | Rotation |
|---|---|---|
| `GHCR_READ_TOKEN` (Mend) | about 2026-12-31 | Mend section |
| `ghcr-pull` credentials | with their token | Cluster section |
| NetBird `owner-admin` token | 90 days from the mint | [Bootstrap runbook](runbooks.md#hetzner-cluster-bootstrap) step 6 |
| `k3s-backup-uploader` access key | no expiry, rotate on suspicion | Cluster section |
| Grafana Cloud token `alloy-platform-node` | no expiry, rotate on suspicion | Grafana Cloud section |
| Cloudflare account-owned tokens (all managed by `cloudflare-tokens`) | per token; `expires_on` in `terraform/cloudflare-tokens` | [runbook](runbooks.md#cloudflare-api-tokens). A token Terraform made gets a replace run, its consumer is rewritten in the same apply; an imported one needs its consumer wired in Terraform first |
| Cloudflare bootstrap token, HCP Terraform team token `cloudflare-tokens` | no expiry, rotate on suspicion | [runbook](runbooks.md#cloudflare-api-tokens) |
