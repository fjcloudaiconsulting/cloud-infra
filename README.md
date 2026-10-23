# cloud-infra

Infrastructure of FJ Consulting apps (TBD, Ziftbook). It runs on Hetzner (INFRA-164); AWS keeps the backup chain and account-level resources. App repos keep app code,
Dockerfiles and their own CI; everything about where and how the apps run lives here.

## Architecture

The end-to-end map (diagram, ingress, data, backups, access, monitoring, cost):
[`docs/architecture.md`](docs/architecture.md).

Settings that live outside git (GitHub Apps, rulesets, Renovate, HCP Terraform, AWS, Cloudflare, out-of-band
secrets) and what breaks when one is wrong: [`docs/configuration-map.md`](docs/configuration-map.md).

How to do the recurring changes (write or rotate a Secret, add a hostname or zone, add a dev environment's mail):
[`docs/runbooks.md`](docs/runbooks.md).

How a release reaches staging and then production, with diagrams: [`docs/release-flow.md`](docs/release-flow.md).

## Layout

| Path | What | Applied by |
|---|---|---|
| `terraform/<stack>/` | One Terraform root per HCP Terraform workspace (org `FlamaCorp`) | HCP Terraform VCS flow: plan on PR, apply on merge after approval in the TFC UI |
| `clusters/<cluster>/` | Kubernetes manifests | Flux, reconciling `main` |
| `aws/bootstrap/` | IAM trust and permission documents for the HCP Terraform roles (a workspace never manages its own role) | Root, once by CLI; see each stack's README |
| `grafana/` | Grafana Cloud alert rules, contact point and dashboard (API payloads) | By hand through the Grafana API after merge, see [runbooks](docs/runbooks.md#memory-alerts) |

## Rules

- Terraform runs through HCP Terraform only. CLI plan/apply is for debugging.
- Terraform version is pinned to the one in `.github/workflows/ci.yml`; workspaces use the same.
- Do not rename a workspace whose name is pinned in an AWS trust policy (e.g. `tbd-backups`). If a
  rename is ever unavoidable: add a trust statement for the new name, apply, rename, then remove the
  old statement. The same add-apply-remove order applies to any trust policy edit.
- Secrets never land in plain text: Terraform variables are sensitive TFC variables, Kubernetes
  secrets are SOPS-encrypted.

## Cluster access

kubectl and Flux reach the nodes over NetBird only (no public Kubernetes port). The peer is
`clusters/hetzner/netbird`; groups and policies are in `terraform/netbird`.

## Kubernetes secrets

A Secret lives in its own `clusters/**/<name>.secret.yaml`; `.sops.yaml` encrypts its `data` and
`stringData` to the cluster's age key, and CI fails on any Secret that is not encrypted. Flux
decrypts with the private key held in-cluster as `flux-system/sops-age`.

```sh
export SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt   # sops looks elsewhere on macOS
sops edit clusters/hetzner/<path>/<name>.secret.yaml   # later changes
```

A new Secret is encrypted from a pipe, never written as plaintext first: recipe in
[docs/runbooks.md](docs/runbooks.md). Encrypting needs only the public key in `.sops.yaml`; editing needs the private key (offline backup).

Bootstrap on a fresh cluster (once): create namespace `flux-system` and the Secret `sops-age` with the
private key under a name ending in `.agekey` (Flux ignores other names), typed at a silent prompt
so it stays out of shell history and scrollback (never a heredoc). A pasted trailing newline answers
the prompt empty, hence the length guard:

```sh
# Run it, wait for the prompt, then paste the AGE-SECRET-KEY line (74 chars); anything else changes nothing.
read -rsp 'Paste the key line, then Enter: ' K; K=${K%$'\r'}; echo; if [ ${#K} -eq 74 ]; then printf '%s\n' "$K" | sudo k3s kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=/dev/stdin; else echo "got ${#K} chars, expected 74: nothing changed"; fi; unset K
```

Then
`kubectl apply -f clusters/hetzner/flux-system/gotk-components.yaml`, wait for the controllers, and
`kubectl apply -f clusters/hetzner/flux-system/gotk-sync.yaml`.
Not `apply -k`: the encrypted Secrets in the folder fail kubectl validation; Flux applies them.

## Backups on AWS

AWS holds the backup chain (`terraform/tbd-backups`).

Work is tracked in Jira project [INFRA](https://fjconsulting.atlassian.net/jira/software/c/projects/INFRA/).
