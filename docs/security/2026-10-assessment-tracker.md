# Security assessment tracker: cloud-infra, October 2026

This is the shared record of the static security assessment of this repository (2026-10-24). It lists every finding,
the pull request that addresses it and what is left to do. Agents with Jira access use it to create, update and close
the Jira issues. Every agent working on this effort keeps it current, so the next one starts from the real state and
nobody needs a live conversation to catch up.

Last updated: 2026-10-25 by the cloud-infra security session, after opening the fix PRs (none merged yet).

## Disclosure note

This repository is public. So this file lists every finding by ID with a neutral title, its status and fix, and **no
exploit detail and no severity per open finding**. Fixed findings are described in their PRs. The full assessment
(affected code, impact and suggested fixes for every finding) is held privately by the owner and is not committed. Do
not paste exploit detail into public issues, PRs, PR comments or commit messages.

Findings already tracked by the TBD and Ziftbook assessments (for example tbd INF-01, INF-02, INF-04, INF-09, FE-01,
BE-13, DB-04 and Ziftbook ZB-08, ZB-25, ZB-31, owner actions 4, 6, 12, 14) stay in those trackers; this file only
cross-references them.

## Current state

- **Open for owner review:** the fix PRs #286, #287, #290, #295, #296, #297, #298, #299 and #300, plus the
  Postgres follow-up PR (number added when it opens). None is merged. Each PR description states what merging does
  in production and how to check it.
- **Related work by the other sessions, merged:** #275 and #280 (TBD edge and probes), #274 (TBD CORS), #277 and #288
  (Ziftbook photo containment and its staging revert), #278, #279, #283, #284, #285 (Ziftbook 0.31.0 prerequisites).
  Draft #289 restores photo uploads in production once production runs Ziftbook v0.31.0.
- **Waiting on the owner:** the decisions in the queue below, the merge order in "Merge plan", and the HCP Terraform
  applies that follow the Terraform PRs.
- **Next:** the owner merges in the order below; the Jira agent creates the INFRA issues and records the PRs.

## How to pick this up (any agent)

1. Read this whole file, then the handoff log at the end. The tables are a snapshot: confirm a PR's state with
   `gh pr view <n> -R fjcloudaiconsulting/cloud-infra --json state,mergedAt,mergeCommit` before acting on it.
2. Do your part (Jira, code, infrastructure checks, owner follow-ups).
3. Update this file in the same pass. You may change, add or remove anything:
   - change a row whose status, PR, Jira key or owner action moved;
   - add a row for a new finding or follow-up (next free ID in its layer, for example `K8S-11`);
   - remove a row or section that no longer applies, and say why in the log;
   - keep the counts table in step with the findings table.
4. Add an entry at the **top** of the handoff log: date, who you are, what you did, what you changed here, what comes
   next, and any questions for the owner. Do not rewrite other agents' entries; correct them in your own.
5. Ship the edit as a `docs(security): ...` PR from a fresh branch off `main`. First check that no other open PR edits
   this file; if one does, add to it or wait for it. The owner merges.
6. Keep it public-safe: neutral titles, no exploit detail, no secrets, no personal data.
7. Coordinate with the other sessions working in this repository (TBD, Ziftbook) in **PR comments**, so the history
   stays with the work.

## For the Jira agent

1. Project **INFRA**. One Jira issue per finding ID. Put the ID first in the summary
   (`[EDGE-02] Edge request body limits`) and add the label `security-assessment-2026-10-cloud-infra`, plus the layer in
   lower case (`k8s`, `terraform`, `hosts`, `ci`, `edge`, `data`). Search for an existing issue with that ID or label
   before creating one.
2. Priority and the full description come from the owner's private report (its section 9), not from this file.
3. Record the outcome on each issue: the status here, the PR link(s), the squash commit on `main` once merged, and the
   **Owner action** text as the remaining work.
4. Transition rules:
   - **Fixed**, and the Merged column shows a date: Done. Comment with the PR and its squash commit.
   - **Fixed** or **Partial**, PR open: In Review, linked to the PR.
   - **Partial**, PR merged: keep the issue open for the owner action, or close it and open a follow-up for the rest.
   - **Needs owner**: open, assigned to the owner.
   - **Cross-ref**: no INFRA issue; comment on the tracked issue if one exists.
   - A PR whose merge needs a window or a manual step stays In Review until the owner confirms the post-merge check.
5. Write each key back into the Jira column here, in a `docs(security): ...` PR.

## Pull requests

| PR | Branch | Findings | What merging does | State |
|---|---|---|---|---|
| #286 | `security/upgrade-image-pins` | K8S-02 | Flux updates the two upgrade Plans; no job runs (merge while no k3s roll is in progress) | Open |
| #287 | `security/network-policies` | K8S-04, K8S-07, EDGE-07 | Flux applies NetworkPolicies at once (merge in a window, with the checks in the PR) | Open |
| #290 | `security/edge-rules` | EDGE-02, EDGE-05 (TBD part), EDGE-06 | Nothing until the owner approves the `cloudflare` apply | Open |
| #295 | `security/platform-hardening` | K8S-05, K8S-02 (Flux, kubectl), K8S-06, K8S-09 | Flux restarts its two controllers; new pods in `data` follow the stricter profile | Open |
| #296 | `security/telemetry-receiver` | EDGE-04 | Rolls the Alloy and log-retention DaemonSets; staging telemetry resumes after the next staging restart | Open |
| #297 | `security/restore-safety` | DATA-01 | Documentation and CI only | Open |
| #298 | `security/hosts-hardening` | ANS-01, ANS-02, ANS-03, ANS-04, ANS-05, ANS-06, ANS-09 | Nothing until the owner runs Ansible; the `hetzner` workspace plans | Open |
| #299 | `security/terraform-iam-fences` | TF-01, TF-02, TF-05, TF-08 | CI at once; the `tbd-backups` plan-role change applies only after the owner approves | Open |
| #300 | `security/ci-fences` | GHA-03, K8S-03, GHA-07, GHA-09, GHA-08 | CI only | Open |
| (pending) | `security/postgres-isolation` | DATA-02, DATA-03 | Restarts Postgres (window) and re-runs both bootstrap Jobs | In progress |

## Findings

Status values: **Fixed** (the PR fixes it), **Partial** (the PR fixes part; the owner action is the rest), **Needs
owner** (a decision or a setting outside this repo), **In progress** (a PR is being prepared), **Cross-ref** (tracked
elsewhere), **Accepted** (no change).

| ID | Title | Status | PR | Merged | Commit | Owner action | Jira |
|---|---|---|---|---|---|---|---|
| TF-01 | Terraform plan-time credentials | Partial | #299 | | | Review HCP Terraform run settings and per-phase credentials (private report) | |
| TF-02 | Backup apply role self-management | Partial | #299 | | | Decide the permissions-boundary change (private report) | |
| TF-03 | Secrets kept in Terraform state | Needs owner | | | | Restrict state access in HCP Terraform | |
| TF-04 | Cloudflare workspace token scope | Needs owner | | | | Decide on splitting the account-scoped permissions | |
| TF-05 | Backup plan role read scope | Fixed | #299 | | | Simulate the policy before approving the `tbd-backups` apply (README) | |
| TF-06 | NetBird group membership binding | Needs owner | | | | Provide peer IDs; enable peer approval and posture checks | |
| TF-07 | Identifiers committed to the repository | Cross-ref | | | | Tracked as INFRA-233 | |
| TF-08 | Lock-file platform hashes and version constraint | Fixed | #299 | | | Hash the `cloudflare-tokens` lock for both platforms on its next bump | |
| K8S-01 | Per-tenant Flux reconciliation | Needs owner | | | | Decide the Flux tenancy restructure (after the Ziftbook release) | |
| K8S-02 | Digest pins for upgrade and controller images | Fixed | #286, #295 | | | Merge #286 while no k3s roll is in progress | |
| K8S-03 | Digest pins for TBD images | Partial | #300, #301 | | | Merge the tbd v0.303.1 bumps (staging, then #301) | |
| K8S-04 | Database NetworkPolicy peers | Fixed | #287 | | | Pre- and post-merge checks in #287 | |
| K8S-05 | Flux controller RBAC | Fixed | #295 | | | | |
| K8S-06 | Service-account token mounting | Fixed | #295 | | | | |
| K8S-07 | App-namespace egress to node addresses | Fixed | #287 | | | | |
| K8S-08 | NetBird pod security context | Needs owner | | | | Rehearse with a console session open | |
| K8S-09 | Pod Security levels | Partial | #295 | | | Add the warn/audit labels to the `netbird` namespace | |
| K8S-10 | Documentation corrections | Partial | #286, #295 | | | | |
| ANS-01 | Host network filtering | Partial | #298 | | | Decide the host firewall rollout; narrow the SSH allowlist to /24 or longer | |
| ANS-02 | Kubernetes API audit logging | Partial | #298 | | | Run `site.yml -e k3s_reconfigure=true`; decide log shipping | |
| ANS-03 | Root SSH key management | Fixed | #298 | | | Run `site.yml --check --diff`, then `site.yml` | |
| ANS-04 | Cluster-admin credential lifetime | Partial | #298 | | | Shorten the owner token lifetime | |
| ANS-05 | k3s token rotation procedure | Fixed | #298 | | | | |
| ANS-06 | k3s installer verification | Partial | #298 | | | Decide on pinning the binary (airgap path) | |
| ANS-07 | Pending reboot alerting | Needs owner | | | | | |
| ANS-08 | k3s hardening-guide options | Needs owner | | | | | |
| ANS-09 | Host key verification at bootstrap | Fixed | #298 | | | | |
| GHA-01 | Automated commits to `main` | Needs owner | | | | Decide the Renovate automerge model (private report) | |
| GHA-02 | Shared preset and signer identity pinning | Needs owner | | | | Decide with the `.github` release flow (cross-ref Ziftbook owner action 14) | |
| GHA-03 | CI checks for secrets and images | Fixed | #300 | | | | |
| GHA-04 | GHCR retention permissions | Needs owner | | | | Decide where retention runs before any Admin grant | |
| GHA-05 | Third-party GitHub App access | Needs owner | | | | Review app installations and selections | |
| GHA-06 | Organisation 2FA and bypass settings | Needs owner | | | | Require 2FA; review bypass modes | |
| GHA-07 | Public workflow log content | Partial | #300 | | | | |
| GHA-08 | Repository security settings | Partial | #300 | | | Enable code-owner review and the settings listed in the PR | |
| GHA-09 | CI input pinning and release age | Partial | #300 | | | Release age for images and actions (with #273) | |
| EDGE-01 | Access control for the Ziftbook staging host | Needs owner | | | | Decide the Access application and its bypasses | |
| EDGE-02 | Edge request body limits | Fixed | #290 | | | Approve the `cloudflare` apply; post-apply checks in #290 | |
| EDGE-03 | Email sender authentication for the product domains | Needs owner | | | | Decide the mail-policy ramp | |
| EDGE-04 | Telemetry receiver isolation and limits | Partial | #296 | | | Pin the infrastructure alert rules to pipeline labels (Grafana) | |
| EDGE-05 | Edge rate-limit path coverage | Fixed | #290, #283 | | | | |
| EDGE-06 | TBD staging Access bypass | Fixed | #290 | | | Apply the Access change in two steps | |
| EDGE-07 | Tunnel connector egress | Fixed | #287 | | | | |
| EDGE-08 | DNS zone hygiene | Needs owner | | | | | |
| EDGE-09 | Data processor inventory | Accepted | | | | Keep the privacy notices current | |
| DATA-01 | Restore procedure | Partial | #297 | | | Decide the backup format change after #272 | |
| DATA-02 | Postgres role isolation between environments | In progress | (pending) | | | | |
| DATA-03 | Postgres connection limits per environment | In progress | (pending) | | | | |
| DATA-04 | Backup provenance and an independent copy | Needs owner | | | | Decide the second copy and retention | |
| DATA-05 | Restore drill freshness signal | Needs owner | | | | | |
| DATA-06 | Database and backup credential separation | Needs owner | | | | | |
| DATA-07 | Encryption for database data at rest and in transit | Partial | #272 | | | #272 keeps dumps in memory; decide on the rest | |

## Counts

| Status | Findings |
|---|---|
| Fixed (PR open) | 15 |
| Partial (PR open) | 15 |
| In progress | 2 |
| Needs owner | 18 |
| Cross-ref | 1 |
| Accepted | 1 |
| **Total** | **52** |

## Owner decision queue

1. HCP Terraform run settings and per-phase credentials (TF-01).
2. The Renovate automerge model and the Flux tenancy restructure (GHA-01, K8S-01).
3. Access for the Ziftbook staging host (EDGE-01).
4. The backup apply role's permissions boundary (TF-02).
5. Backups: second copy, retention and format (DATA-04, DATA-01).
6. Mail policy for the product domains (EDGE-03).
7. GitHub organisation settings: 2FA, app access, code-owner review, bypass modes (GHA-05, GHA-06, GHA-08).
8. GHCR retention design before any Admin grant (GHA-04).
9. Host firewall rollout and audit-log shipping (ANS-01, ANS-02).

## Merge plan

The owner merges, one PR at a time: `gh pr update-branch`, wait for every check on the new head, then
`gh pr merge --squash --admin --match-head-commit <sha>`. Each PR description has its own pre- and post-merge checks.

1. **No production effect:** #297, #299 (then simulate and approve the `tbd-backups` apply), #298 (then the Ansible
   runs in the order in its description), #290 (then approve the `cloudflare` apply).
2. **Routine Renovate first, to free its PR slots:** #251, #292, #294 (Terraform providers), #264 and #257 (before
   #296 and #287 respectively, same files), #263 (before #296), #248 (before #272).
3. **Flux, low risk:** #286 (no k3s roll in progress), #295.
4. **Flux, in a window with the checks:** #287, then #296.
5. **Database windows:** #265 (MySQL restart); #262 with the Postgres follow-up PR and #247 (Postgres restart and the
   bootstrap Jobs).
6. **Releases:** the tbd-staging v0.303.1 bump (automerges), then #301, then #300 (its tag-only list is then removed);
   Ziftbook #256 with #289, after the staging checks.

## Handoff log

Newest first.

### 2026-10-25: cloud-infra security session

- **Done:** the assessment (52 findings) and fix PRs #286, #287, #290, #295, #296, #297, #298, #299, #300; the
  Ziftbook photo-upload reverts #288 (since merged) and draft #289, prepared for the Ziftbook session; closed #291 and
  #293 on the owner's decision; this tracker.
- **Coordination:** with the TBD and Ziftbook sessions in PR comments (#283, #284, #256, #287, #288).
- **Next:** the Postgres follow-up PR; the owner's decisions and merges; the Jira agent creates the INFRA issues.
- **Questions for the owner:** the decision queue above.
