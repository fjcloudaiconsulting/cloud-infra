# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Single home for the infrastructure of FJ Consulting apps (Hetzner, epic INFRA-164; AWS keeps the backup chain and account-level resources): TBD (`fjcloudaiconsulting/tbd`, in prod on this
cluster since the 2026-10-04 cutover) and Ziftbook (`fjcloudaiconsulting/ziftbook`, pre-launch). App repos keep
app code, Dockerfiles and CI; this repo owns where and how they run. Work is tracked in Jira `INFRA`.

## Target architecture (decided 2026-10-01)

The Hetzner migration (epic INFRA-164) is done; the Lightsail node and the `aws-platform` stack were removed in INFRA-176.

- Three Hetzner CAX21 (ARM64) servers in nbg1 (`terraform/hetzner`), k3s HA configured by `ansible/`. No EKS, no RDS, no ALB/NAT.
- MySQL 8.4 (TBD), Postgres 18 (Ziftbook) run in-cluster as StatefulSets; nightly dumps go to the existing Object Lock bucket in AWS.
- Cloudflare proxies all app traffic to the Cloudflare Tunnel (`cloudflared`, the cluster's only ingress).
- Images live on GHCR, built by the app repos. Flux (source + kustomize controllers only) applies
  `clusters/hetzner/`. Secrets are SOPS + age.
- Namespaces (`clusters/hetzner/`): `tbd-staging`, `ziftbook-staging` (INFRA-173), `ziftbook-prod` (INFRA-181) and `tbd-prod` (INFRA-175), `data` (MySQL, Postgres, backups), `cloudflared` (the tunnel connectors), `observability` (Grafana Alloy, metrics to Grafana Cloud), `netbird` (owner kubectl access over NetBird), and `system-upgrade` (system-upgrade-controller and the two k3s Plans, INFRA-179); `kube-system/` holds only the CoreDNS replica count and PDB (INFRA-177).

## Layout and commands

- `terraform/<stack>/`: every directory directly under `terraform/` is a root stack, one per HCP Terraform workspace (org `FlamaCorp`). Plan runs on the PR, apply on
  merge after approval in the TFC UI. Local CLI only for debugging.
- `clusters/<cluster>/`: Kubernetes manifests reconciled by Flux.
- `ansible/`: host config and k3s HA for the Hetzner servers (INFRA-64), run by hand from that directory;
  runbook `ansible/README.md`. Secrets are `*.sops.yaml` there.
- `aws/bootstrap/`: IAM documents for the HCP Terraform OIDC roles, minted once by root (see each stack's
  README). A workspace never manages its own role.
- `grafana/`: Grafana Cloud alert rules, contact point and dashboard as API payloads; applied by hand after merge
  (runbooks.md "Memory alerts"). Nothing applies them automatically: keep git and the stack in step.
- CI (`.github/workflows/ci.yml`) runs, on PRs only when Terraform files changed (the job itself always reports, it is a required check), `terraform fmt -check -recursive -diff`, `init -backend=false && validate`
  for every stack, tflint (`.tflint.hcl`), and the Python tests (`python3 -m unittest discover -s tests`: backup probe, SOPS check). A second job runs kubeconform on
  `clusters/` and `.github/scripts/check-sops-secrets.py` (every Secret under `clusters/` and every `ansible/**/*.sops.yaml` must be SOPS-encrypted) and `.github/scripts/check-prod-tags.py` (every `<app>-prod` image tag <= that app's `<app>-staging` tags) and `.github/scripts/check-image-provenance.sh` (every app image ref a change adds is `:vX.Y.Z@sha256:<digest>` with a build-image attestation made on main at that tag's commit, INFRA-107). `.github/workflows/ansible.yml` runs `ansible-playbook --syntax-check` and `ansible-lint` (production profile) in `ansible/`, only when `ansible/**` changes. Locally: `terraform fmt -recursive`,
  `terraform -chdir=terraform/<stack> validate`, and `tflint --init && tflint --recursive --config "$PWD/.tflint.hcl"`.
- The Terraform version is pinned in `ci.yml`; keep workspaces on the same version.

## Constraints that are easy to break

- The TBD backup chain (bucket, KMS key, uploader, `tfc-backups-*` roles, OIDC providers) lives in AWS. The TFC role trust is pinned to workspace name `tbd-backups`:
  do not rename it. If a rename is ever unavoidable, or any trust statement must change, add the new
  statement, apply, then remove the old one; never edit in place. Account specifics: `docs/specs/private-ops-notes.md` (local, git-excluded).
- `thebetterdecision.com` is registered at Cloudflare Registrar (since 2026-10-06) and its DNS is served by Cloudflare
  (`terraform/cloudflare`); no Route 53 zone is left. The TBD apex is the Worker `tbd-landing`.
- AWS calls go through the aws-mcp server; the account is in `docs/specs/private-ops-notes.md` (local).

## Docs

Plans and specs are local only (`docs/specs/`, git-excluded). Shareable decisions go in PR bodies and Jira.

`docs/configuration-map.md` lists every setting made by hand outside git and a symptom table for when one breaks. Update it in the same PR as any such change.
`docs/runbooks.md` holds the step-by-step procedures (following Flux and rollouts, promoting a TBD release, the post-deploy smoke, Secrets, Ziftbook production secrets (with the worker's database role), the Ziftbook admin TOTP key, the Ziftbook rate-limit key, Ziftbook first-run setup, MySQL database per app environment, public hostnames, Mailgun, the Ziftbook worker heartbeat, R2 for Ziftbook photos, Ziftbook Turnstile, Ziftbook Stripe, TBD smoke account, Cloudflare Access on TBD staging, Cloudflare API tokens, metrics to Grafana Cloud, draining or losing an app node, upgrading k3s, node memory and the DB volume, Hetzner nodes (Ansible, rebuild, break-glass), the Hetzner cluster bootstrap); update it when a procedure changes.

<!-- claude-mem-lite:begin v1 -->
## claude-mem-lite — persistent memory

PreToolUse hooks already run `mem_recall` for past lessons before Read/Edit/Write. The calls worth making proactively:

| When | Call |
|------|------|
| Before Edit/Write | hook already recalled; if an injected `#NN` lesson changed what you did, add the bare tag `(#NN)` once at the end of the sentence describing that change (citing = adopting; uncited lessons decay; skip ones that did not apply). No other mention of memory ids, saves or the memory store in replies to the user |
| A recalled memory drives an answer or a design choice | check its claim in the code or `git log` first: `#NN` and `E#NN` rows are notes from past sessions, many written automatically, so they can be wrong, and they describe the code as it was. If the code disagrees, trust the code and replace the note: `mem_save(..., supersedes=[NN])`, or `supersedes=["E#NN"]` for an event |
| After fixing a non-trivial bug | `mem_save(type="bugfix", lesson_learned="<root cause + fix, only what this change's diff shows>", importance=2)` |
| After a non-obvious architecture decision | `mem_save(type="decision", lesson_learned="<constraint + tradeoff>")` |
| Deferring to a future session | `mem_defer({title, priority:1|2|3, detail})`; when fixed, add `closes_deferred=[N]` to `mem_save` |
| Looking up past work / history | `mem_search "keywords"` · `mem_recent` · `mem_timeline` |

Path cost is round-trips, not milliseconds: the PreToolUse hook above already recalls (0 calls) — prefer it. For an explicit query, if these `mem_*` tools are deferred behind ToolSearch this session, the Bash CLI `claude-mem-lite` is one call vs two (ToolSearch + call); the MCP server instructions carry the absolute path to use when it is not on PATH.

Full tool + CLI tables, citation/decay rules, and save discipline → `.claude/plugin_claude_mem_lite.md`
<!-- claude-mem-lite:end -->

## graphify

- **graphify** (`~/.claude/skills/graphify/SKILL.md`) - any input to knowledge graph. Trigger: `/graphify`.
  When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.
