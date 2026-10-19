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
| Staging image bump sits on a `renovate/ziftbook-staging-*` or `renovate/tbd-staging-*` branch and never merges, or shows up as a PR | Renovate app is not a bypass actor on the cloud-infra ruleset, or CI did not run on the branch | [Rulesets](#branch-protection), `ci.yml` push filter | Ruleset > Bypass list > Renovate (app) > Always. CI must report `Terraform checks` and `Kubernetes checks` on the branch |
| No app image bumps, Dependency Dashboard lists `ghcr.io` lookup failures (other dependencies still get PRs) | `GHCR_READ_TOKEN` (Mend org secret) expired or revoked | [Mend Renovate](#mend-renovate) | New classic PAT with only `read:packages`, owned by GitHub user `flamarion` (the username is fixed in `renovate.json` hostRules); replace the Mend secret, tick "run again" on the dashboard |
| `ImagePullBackOff` in `ziftbook-prod`, `ziftbook-staging`, `tbd-staging` or `tbd-prod` | The `ghcr-pull` credential expired or was revoked | `clusters/*/namespaces/*-ghcr-pull.secret.yaml` | Re-encrypt a new GHCR read credential into every file (see [Cluster](#cluster-out-of-band-material)) |
| Hetzner: `cloudflared` pods in `CreateContainerConfigError`, tunnel `hetzner-k3s` DOWN | Secret `cloudflared/cloudflared-token` missing, or the tunnel was replaced (new token) | `kubectl -n cloudflared describe pod`, Zero Trust > Networks > Tunnels | [Bootstrap runbook](runbooks.md#hetzner-cluster-bootstrap) step 3, merge, restart the Deployment |
| `kubectl`/`flux` cannot connect | NetBird client not connected, or the `owner-admin` token expired | [`clusters/hetzner/netbird/`](../clusters/hetzner/netbird/) | Connect NetBird; renew the token per the [bootstrap runbook](runbooks.md#hetzner-cluster-bootstrap) |
| A pod or Job in `ziftbook-staging` or `tbd-staging` is never created (`FailedCreate`, "exceeded quota: staging-class-only") | Every pod there must set `priorityClassName: staging`; the quota rejects any other, Jobs and CronJobs included | `kubectl -n <ns> get events` | Add `priorityClassName: staging` and explicit memory requests that fit the `memory` quota |
| Flux shows the Kustomization Ready but a workload is down | `flux-system` has no `healthChecks`: pod, quota or PSA failures do not turn it red | `kubectl -n <ns> get pods,events` | Fix the manifest. Namespace limits: [`namespaces/ziftbook-staging.yaml`](../clusters/hetzner/namespaces/ziftbook-staging.yaml) |
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
| `cloudflare` plan fails with Cloudflare `Authentication error` | Token `tf: cloudflare workspace` was deleted, rolled in the dashboard, or the variable was overwritten by hand | Workspace `cloudflare-tokens` | A deleted token: plan and apply there, it is recreated and the variable rewritten. Rolled, or the variable overwritten: neither shows in a plan, so start a run with Replace resources = `cloudflare_account_token.cloudflare_workspace`. Change token scopes only in `terraform/cloudflare-tokens` |
| Cloudflare 429 page (error 1015) on sign-in, sign-up, password reset, MFA or an invite link, or a 429 to an MCP client's OAuth token or registration call (TBD, INFRA-147), or the Ziftbook post-deploy smoke failing with 429 on `/api/health/dependencies` (shared CI egress address) | The edge rate limit (INFRA-123): more than 20 requests in 10 s from one IP to the zone's auth paths, which on ziftbook.com include `GET /api/session` and `/api/health/dependencies` (INFRA-140) and on thebetterdecision.com `/api/v1/oauth/token` and `/api/v1/oauth/register` (INFRA-147). Blocks last 10 s. Many users behind one NAT share the counter | `terraform/cloudflare/rate_limit.tf`, Cloudflare Security > Analytics of the zone | Wait 10 s. If real users trip it, raise `requests_per_period` or drop a chatty path in a PR |
| `thebetterdecision.com` does not resolve, or `www.thebetterdecision.com` 301s to an apex that does not answer | The apex custom domain was detached from `tbd-landing` (its DNS record goes with it), or the Worker was deleted | Workers & Pages > `tbd-landing` > Settings > Domains & Routes; [Worker and Snippet access](#worker-and-snippet-access-infra-98) | Re-attach `thebetterdecision.com` to `tbd-landing` (API or dashboard, same section). A deleted Worker: re-run tbd's `Deploy Apex Landing` workflow first |
| A PR has no `Terraform Cloud/FlamaCorp/<ws>` check | Workspace missing, or its trigger path was not touched. Re-running GitHub checks does not trigger a plan | HCP Terraform workspace | An absent check is not a pass. Push a change under the stack's directory |
| `aws` says the token has expired, aws-mcp tools missing | SSO session expired (8 h) | n/a | Owner runs `aws sso login --profile fjc` |
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
| 8 | Renovate fast-forwards the branch into `main` without a PR | Renovate app in the `main protection` bypass list |
| 9 | Flux applies the commit; Deployments use `Recreate` | `sops-age`, `ghcr-pull` |
| 10 | CI step `prod tags not ahead of staging` (`check-prod-tags.py`, in `Kubernetes checks`) fails a PR whose `<app>-prod` image tags are newer than that app's `<app>-staging` tags on the tree (the lowest of them, so a staging-only image such as `tbd/mcp` left on an older tag blocks the prod bump: tbd must promote mcp on every release) | Required check `Kubernetes checks` in the `main protection` ruleset (no ruleset change); covers any `<app>-prod` dir with no code change |

Step 10 is advice for the owner, not a lock: admins bypass the ruleset (merging a red check is possible) and Renovate's fast-forward bypasses it too, so the same step also runs on every push to `main` and goes red after the fact. It compares tags in git, not what staging actually runs (the post-deploy smoke and the staging `/health` do that), and a PR that bumps staging and prod together passes it (review). A red check means "wait for the staging bump on `main`, then update the branch".

Drift watch: workflow `release-drift-probe.yml` (daily) opens or updates one `[release-drift]` issue when an app's latest GitHub release has been absent from `clusters/` for 2+ days (production bump PR unmerged, or Renovate never opened it), and closes it when clear. No credentials; it reads the app repos' releases because GHCR is private. It depends on `tbd` and `ziftbook` staying public repos (GITHUB_TOKEN reads); the run goes red if either goes private. Only repos in `WATCH_REPOS` (default `ziftbook tbd`, tbd since the INFRA-48 cutover) are checked.

## GitHub

Org `fjcloudaiconsulting` is on the **Free** plan: no org-level rulesets (the API answers 403), and
private repositories cannot have branch protection, so every repo that must be protected is public.

### Branch protection

| Repo | Mechanism | Rules |
|---|---|---|
| cloud-infra | Ruleset `main protection` (id 24298796) | PR with 1 approval (squash only), linear history, no force-push or deletion, required checks `Terraform checks` and `Kubernetes checks` (strict). Bypass: org admin, repository admin role, **Renovate app (id 2740, Always, added 2026-10-03)** |
| .github | Ruleset `main protection` (id 24298798) | PR with 1 approval (squash only), linear history, no force-push or deletion, no required checks. Bypass: org admin, repository admin role. Tags: see [Tag protection](#tag-protection) |
| app-template | Ruleset `main protection` (id 24384611) | Same as `.github`: PR with 1 approval, linear history, no required checks |
| ziftbook | Classic branch protection | 1 review, strict checks `Backend Checks`, `Frontend Checks`, `pr-title / check`, admins not enforced. **Force-push and deletion of `main` are allowed** |
| tbd | Classic branch protection | 1 review, checks `Backend Checks`, `Frontend Checks`, admins enforced |

### Tag protection

Git release tags `v*` are immutable by policy. Each repo below has a ruleset `tag protection` on
`refs/tags/v*`: restrict creations, updates and deletions, block force pushes. Only the bypass actors
can create a `v*` tag, so the release App must stay on the list of every app repo or the next release
fails at the tag (see the symptom table). The App bypass is only as strong as the `release`
environment that holds its key (`main` only, see [Per-repo settings](#per-repo-settings)) and the
`main` branch protection. These rulesets cover git refs only, not the GHCR `vX.Y.Z` image tags.

| Repo | Ruleset id | Bypass (Always) |
|---|---|---|
| .github | 24413637 (INFRA-76) | Org admin, `fjcloudaiconsulting-release` app (id 5166919, added 2026-10-10, INFRA-161): the `publish` workflow creates `vX.Y.Z` and moves `v1` when the owner merges the Release PR |
| tbd | 24454860 (INFRA-94, 2026-10-04) | Org admin, `fjcloudaiconsulting-release` app (id 5166919) |
| ziftbook | 24454865 (INFRA-94, 2026-10-04) | Org admin, `fjcloudaiconsulting-release` app (id 5166919) |
| app-template | 24454868 (INFRA-94, 2026-10-04) | Org admin, `fjcloudaiconsulting-release` app (id 5166919). Repos made from the template get only its files: each new app repo needs this ruleset, the App installed and the `release` environment ([Per-repo settings](#per-repo-settings)) |

### GitHub Apps

Org settings > GitHub Apps (installations). Permission changes ask the org owner to accept on the
installation, and the grant applies to **every repository in the installation**.

| App | Repos | Notes |
|---|---|---|
| `fjcloudaiconsulting-release` | selected: each app repo on the shared release flow (Ziftbook, app-template, tbd from INFRA-42), and `.github` (2026-10-10, INFRA-161) | Permissions: contents, issues, pull requests write; metadata read; **workflows write** (added 2026-10-03, INFRA-78). Private key held by the owner and stored only as the `release` environment secret. In `.github` the key moves `v1` by design: a release there happens only when the owner merges the Release PR (`main` protected, 1 approving review). Keep cloud-infra out of the selection (`renovate` and `claude` already hold contents and workflows write there) |
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
- Dependabot vulnerability alerts (`gh api -i repos/<r>/vulnerability-alerts`: 204 on, 404 off): on in `.github`, cloud-infra and ziftbook; **off in tbd** as of 2026-10-04 (INFRA-109, owner to enable). Renovate's security-fix PRs, which skip grouping and dashboard approval, only fire where they are on. Dependabot security updates (`repos/<r>/automated-security-fixes`) are off in all four (Renovate opens the fixes).
- Actions default workflow permission is read-only everywhere checked. "Allow GitHub Actions to create and approve pull requests" is off in cloud-infra, tbd and ziftbook (turned off 2026-10-04; release-please uses the release App token, nothing approves with `GITHUB_TOKEN`). It is still on in app-template.
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

Hosted Mend app, Free plan, repos selected at
`https://github.com/organizations/fjcloudaiconsulting/settings/installations/167278400`.

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
| `cloudflare-tokens` (INFRA-133) | `terraform/cloudflare-tokens` | Env `CLOUDFLARE_API_TOKEN` (**sensitive**): the bootstrap token, the one hand-made Cloudflare token (account-owned, Account API Tokens: Edit only, stored by the owner with a script that checks it first). It can mint any token, so treat it as account admin: only this workspace holds it, and the workspace does not manage it. Env `TFE_TOKEN` (**sensitive**): token of team `cloudflare-tokens`, custom access on workspace `cloudflare` only (variables write, runs read). Its state holds the value of every token it creates: remote state sharing off, destroy plans off, auto-apply off. Any in-repo branch whose PR touches `terraform/cloudflare-tokens/**` plans with these credentials (a `data "external"` there could read them), so only the owner and his agents push there, and Renovate needs dashboard approval for it; HCP Terraform never plans fork PRs |
| `tbd-backups` | `terraform/tbd-backups` | OIDC, two roles (plan, provisioner); variable `aws_account_id` |
| `hetzner` (INFRA-169) | `terraform/hetzner` | Env `HCLOUD_TOKEN` (**sensitive**): Hetzner project `fj-platform` token `tfc-hetzner` (Read & Write), held only here. Variables `ssh_allowed_cidrs` (HCL list of IPv4 CIDRs allowed on port 22) and `deploy_ssh_public_key` (`ssh-ed25519 ...`, not sensitive). Both have no default, so a plan without them fails. The Hetzner project and a second token `claude-readonly` (Read; owner's local hcloud context `fj-platform-ro`) are also hand-made |

The org also holds workspaces `tbd` (repo tbd, `infra/terraform`) and `fjconsulting-website`, which belong to those repos. CI pins Terraform 1.16.6; check the workspaces' Terraform version matches after a bump.

Never set `TFC_AWS_RUN_ROLE_ARN` on `tbd-backups`: it would give unapproved PR plans the apply role. The IAM documents are
in [`aws/bootstrap/`](../aws/bootstrap/); root creates the roles once and a workspace never manages its
own role.

## AWS (account 884686184019)

- Local access is IAM Identity Center (organization instance, eu-central-1, INFRA-10): user `flamarion`, permission set
  `AdministratorAccess` (8 h sessions, MFA on every sign-in), profile `fjc` (`aws sso login --profile fjc`), which the
  aws-mcp server also uses (`AWS_MCP_PROXY_PROFILES=fjc`). Root has MFA, no access keys, and is kept for root-only tasks
  in the console.
- Created by hand once (root): OIDC provider `app.terraform.io` and the `tfc-*` roles. Everything else in
  the backup chain is managed by the `tbd-backups` stack: bucket `tbd-mysql-backups-884686184019` (Object
  Lock), KMS key, the uploader users and keys, OIDC provider `token.actions.githubusercontent.com`, role
  `github-actions-backup-probe`. Details: `terraform/tbd-backups/README.md`.
- Lightsail decommissioned 2026-10-14 (INFRA-176): the node, its firewall, alarms, budget, SNS topics, health check and the `aws-platform` workspace are gone. The CloudTrail trail `platform-management` was destroyed too (owner ruling 2026-10-14): the account keeps no trail, only the 90-day CloudTrail Event history. The log bucket `fjc-platform-cloudtrail-884686184019` is kept, unmanaged and no longer written to; its lifecycle rule expires the old logs.
- Cost alerts, made by hand (ZIF-37, 2026-10-18; Terraform would need budgets and ce permissions on the `tfc-backups-*` roles,
  a root re-mint): budget `aws-monthly-usage`, $3 a month on unblended cost before credits and refunds, mails the owner when
  the actual or the forecast passes $3. Expected spend is about $1 a month, nearly all the KMS key ($1); the backup bucket adds
  cents. October 2026 already passed $3 (Lightsail ran until 10-14), so it mails once for October; November is the first
  real month. Cost Anomaly Detection: monitor
  `Default-Services-Monitor` (per service), subscription `Default-Services-Subscription` mails the owner daily for any anomaly
  of $1 or more (was $100 and 40%). Symptom of a drift: no mail when the month passes $3; check Billing > Budgets.

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
- Cloudflare Access on `dev.thebetterdecision.com` (INFRA-117, `terraform/cloudflare/access.tf`): email one-time PIN, allow-list the owner, bypass only `/health` and `/robots.txt` for the post-deploy smoke, and the MCP OAuth paths `/mcp`, `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource` and `/api/v1/oauth/*` (INFRA-147: an MCP client cannot pass the PIN). The consent page `/oauth/authorize` stays behind Access: its auth calls need the Access session, so the browser gets the PIN prompt first. MCP and the OAuth routes are staging only (owner ruling 2026-10-09): production gets no route, no MCP and no bypass until a dedicated security review and pen test; the staging backend sets `MCP_OAUTH_ENABLED=true` for tbd's off-by-default switch, and the tunnel (`terraform/cloudflare/tunnel.tf`) routes the discovery documents to the backend and exactly `/mcp` to the MCP server. Out of git, once: Zero Trust enabled on the account (Free plan, team name) and the One-time PIN login method on. Scope: the `cloudflare` workspace token holds account "Access: Apps and Policies Write" (`cloudflare-tokens`), which covers every Access app in the account (Cloudflare has no narrower group). Procedures: [runbooks.md](runbooks.md#cloudflare-access-on-tbd-staging).
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

Read 2026-10-04 (API, read-only), all four zones of the account (thebetterdecision.com, ziftbook.com,
fjconsulting.dev, yetanothergrower.com): no Workers routes, no Snippets, no snippet rules. Workers custom domains:
ziftbook.com and www.ziftbook.com, both `ziftbook-landing`. One Worker script, `ziftbook-landing`, which makes no
outbound fetch (only its assets binding). One Pages project, `fjconsulting-website-dev` on fjconsulting.dev (no node
hostname in that zone). One account member, the owner (Super Administrator).

Workers custom domains, all attached by hand (not in Terraform or `wrangler.jsonc`, so no plan shows their drift). Each
one owns a read-only proxied DNS record that Cloudflare creates and deletes with it; a CNAME on the same name blocks
the attach. Recreate one with the Cloudflare API (`PUT /accounts/<account>/workers/domains`, body
`{"hostname": "<host>", "service": "<worker>"}`) or the Worker's Settings > Domains & Routes > Add > Custom domain.

| Hostname | Worker | Since |
|---|---|---|
| `ziftbook.com`, `www.ziftbook.com` | `ziftbook-landing` (www 301ed by the `ziftbook_redirects` zone rule before the Worker, INFRA-131; http by Always Use HTTPS and the Worker) | before 2026-10-04 |
| `thebetterdecision.com` | `tbd-landing` | INFRA-61. Not `www`: the `tbd_redirects` rule in `terraform/cloudflare` 301s it to the apex |

Re-read 2026-10-06 (API, read-only): still no Workers routes on thebetterdecision.com, ziftbook.com and fjconsulting.dev (yetanothergrower.com too: no routes, no Snippets); Workers custom domains are
ziftbook.com and www.ziftbook.com (`ziftbook-landing`) and thebetterdecision.com (`tbd-landing`, attached by the owner,
INFRA-61); scripts `tbd-landing` and `ziftbook-landing` only; the Snippets endpoints return nothing on the four zones. Both
landing deploys ran green with the per-Worker tokens (ziftbook `landing.yml` and tbd `apex-deploy.yml`, 2026-10-05), and
neither repo holds a repo-level `CLOUDFLARE_API_TOKEN` any more. The Cloudflare MCP grant cannot read API tokens (9109), so
token scopes are the ones `cloudflare-tokens` applied (runs of 2026-10-05, all applied).

Who can put code on a zone (a Pages project with a custom domain on an app zone counts the same as a Worker).
Scopes are what the dashboard shows (names only), as of 2026-10-05:

| Principal | Held in | Can do today | Scopes |
|---|---|---|---|
| Owner | dashboard, `wrangler login` | everything | Super Administrator |
| Bootstrap token (INFRA-133) | HCP Terraform workspace `cloudflare-tokens`, env var `CLOUDFLARE_API_TOKEN`; that workspace's PR plans run provider code with it | mints, edits and deletes any account-owned token, so indirectly everything | Account API Tokens Write only. The only hand-made Cloudflare token |
| `cloudflare` workspace token | HCP Terraform, env var `CLOUDFLARE_API_TOKEN`; every PR plan runs provider code with it | whatever its policies allow; Terraform manages no Worker, route, Snippet or Pages project | `tf: cloudflare workspace`, made by `cloudflare-tokens` (INFRA-133; policies in `terraform/cloudflare-tokens/main.tf`): thebetterdecision.com: Zone Write, Zone Settings Write, DNS Write, SSL and Certificates Write, Zone WAF Write, Dynamic URL Redirects Write (dashboard: Single Redirect, INFRA-61); ziftbook.com: the same with Zone Read instead of Zone Write (Dynamic URL Redirects Write since INFRA-131); fjconsulting.dev: Zone Read, DNS Write; account: Access: Apps and Policies Write (INFRA-117), Cloudflare One Connector: cloudflared Write (INFRA-165), Workers R2 Storage Write (ZIF-133: buckets, CORS, lifecycle, custom domains; no narrower group can create a bucket, and it can also read, write and delete objects in every bucket, so Ziftbook's photos are in this token's reach; the apps use their own bucket-scoped tokens). No other zone, no Workers Scripts or Workers Routes, so it cannot add a Worker route. In use since 2026-10-05; the hand-made `cloudflare-tfc` it replaced is deleted |
| Ziftbook `CLOUDFLARE_API_TOKEN` | ziftbook `landing` environment secret, `main` only (2026-10-05; the old repo-level secret is deleted) | deploys `ziftbook-landing`; no longer touches its custom domains (ziftbook#181) | Individual Workers Editor on `ziftbook-landing` only; token `ziftbook-landing deploy`, adopted by `cloudflare-tokens` (INFRA-133 import) |
| tbd `CLOUDFLARE_API_TOKEN` (INFRA-60) | tbd `landing` environment secret, `main` only (2026-10-05) | deploys `tbd-landing` (the apex since INFRA-61, and its workers.dev preview); never touches its custom domain | Individual Workers Editor on `tbd-landing` only; token `tbd-landing deploy`, adopted by `cloudflare-tokens` (INFRA-133 import) |
| Cloudflare MCP OAuth grant (Claude sessions) | My Profile > Access Management > Connected Applications | full access, granted by the owner 2026-10-05 (it can deploy Workers and change zones); still 9109 on the API token lists | full |

Rules:

- A CI deploy token is an account-owned token with the **Editor** role scoped to **its one Worker**, no
  Zone > Workers Routes, no Snippets, no Pages, stored as an environment secret limited to `main`. Product-scope
  Editor (the legacy "Workers Scripts: Edit") rewrites every Worker in the account, current and future, so either
  landing token could replace the other app's landing. Zone > Workers Routes > Write on a zone can route any hostname
  of it, node hostnames included: Cloudflare tokens cannot be limited by hostname.
- Custom domains are attached by the owner (or Terraform), not by CI, and are not declared in `wrangler.jsonc`: a
  per-Worker Editor deploys an existing Worker only while the deploy does not add, change or remove a route or
  custom domain, and Custom Domains do not support per-Worker roles yet. Ziftbook stopped declaring them in ziftbook#181 (INFRA-113).
- Residual, which no token narrowing removes: a landing Worker attached to an app zone runs in that zone, so its
  code can spoof `CF-Connecting-IP` towards that zone's node hostnames. Today `ziftbook-landing` -> `app.ziftbook.com`
  (production) and `dev.ziftbook.com` (staging), both through the tunnel; today `tbd-landing` (apex) -> `app.thebetterdecision.com` (production, through the tunnel since INFRA-175). Who can change
  that code: whoever merges to the app repo's `main`, or holds its deploy token (an environment secret limited to `main`). Impact: a forged IP in TBD's audit log and rate-limit buckets; the same
  token could already serve any page on the apex, which is the bigger risk. Owner decision on INFRA-98, 2026-10-06:
  narrow, do not accept. Worker deploy token narrowing is complete for account-owned tokens (user-owned tokens are unreadable by agents: 9109; rules above, no consumer needs more than its own Worker); what is
  left is this code-level residual. An edge WAF rule was tried and does not catch it (applied and tested 2026-10-07, then
  removed; result on INFRA-136). Owner ruling 2026-10-08: not accepted, fix tracked in INFRA-145. The Pages tokens `fjconsulting-website-*` hold Pages Write
  on the whole account (Cloudflare has no per-project Pages role); as we understand it (untested) a custom domain on an app zone would also need DNS
  rights there, which they lack.
- No scheduled probe for new routes or Snippets: once the deploy tokens are per-Worker, only the owner, the bootstrap token (by minting one) and a write-scoped MCP grant can add one, and a probe cannot see the code-level residual above.

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
| Alert rule group `log-retention` (`LogRetentionStale`) | The stack (INFRA-130), applied through the Grafana API from [`grafana/log-retention.rules.json`](../grafana/log-retention.rules.json) | API-provisioned like `node-memory`; apply command in the [runbook](runbooks.md#request-log-retention). Until applied, a stalled log-retention job raises no alert |
| Synthetic Monitoring checks `tbd-prod-health`, `ziftbook-prod-health` and alert rule group `synthetic-uptime` (`ProdEndpointDown`) | The stack (INFRA-172), applied through the Synthetic Monitoring API and the Grafana API from [`grafana/synthetic-*.json`](../grafana/) | HTTP checks every 2 min from 2 EU public probes; the SM access token is revoked after applying and not kept. Apply and free-tier math: [runbook](runbooks.md#synthetic-uptime). Until applied there is no external uptime alert |
| Alert rule groups `ziftbook-app` (`ZiftbookApiErrors`, `ZiftbookJobsGaveUp`, `ZiftbookJobQueueStuck`) and `node-disk` (`NodeDiskHigh`) | The stack (ZIF-89), applied through the Grafana API from [`grafana/ziftbook-app.rules.json`](../grafana/ziftbook-app.rules.json) and [`grafana/node-disk.rules.json`](../grafana/node-disk.rules.json) | API-provisioned like `node-memory`; apply and the metric-name check: [runbook](runbooks.md#ziftbook-app-alerts). Until applied, Ziftbook production 5xx bursts, given-up jobs, a stuck queue and a filling disk raise no alert |

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
| Backup upload key | Secret `data/backup-s3` | Access key of IAM user `k3s-backup-uploader` (from the `tbd-backups` stack); rotate per `terraform/tbd-backups/README.md` and re-encrypt |
| TBD app secret | `tbd-prod/tbd`, `clusters/hetzner/tbd-prod/tbd.secret.yaml` (INFRA-48, moved INFRA-175) | Values were carried over from the old hosting at the INFRA-48 cutover (same values, or logins and encrypted columns break); `database-url` is built from the `data/mysql` Secret (`redis-url` is unused since tbd 0.296.0 and goes with the next rotation). Keys: the `secretKeyRef` entries in `tbd-prod/backend.yaml` (`ai-credential-encryption-key-prev` is optional). A whole new file each time, so only the public key is needed. A rotated MySQL app password must be written here too |
| TBD smoke account | `tbd-prod/tbd-smoke`, `clusters/hetzner/tbd-prod/tbd-smoke.secret.yaml` (INFRA-48, moved INFRA-175), cloud-infra environment `tbd-prod` secrets `SMOKE_USERNAME` / `SMOKE_PASSWORD` (the post-deploy smoke, INFRA-114) | Production user for `scripts/smoke-test.sh`: active, email verified, no MFA (TBD-371). Every store must hold the same values; the cluster copy is the readable one. Rotation and manual run: [runbooks.md](runbooks.md#tbd-smoke-account). Rotated 2026-10-04 |
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
| SSO session, profile `fjc` | 8 h | `aws sso login --profile fjc` |
