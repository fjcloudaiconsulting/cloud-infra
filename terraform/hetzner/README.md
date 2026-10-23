# `hetzner`

The Hetzner Cloud project (INFRA-164, INFRA-169): three CAX21 (ARM64) servers in `nbg1` on a private
network, a firewall, and the volume that holds the databases. The servers have no `user_data`: Ansible
configures them afterwards from a static inventory (see the `nodes` output).

| Resource | Notes |
|---|---|
| `hcloud_ssh_key` `fjc-hetzner-deploy` | `var.deploy_ssh_public_key`, the key Ansible uses |
| `hcloud_network` + subnet | `10.0.0.0/16`, subnet `10.0.1.0/24` (cloud, zone `eu-central`) |
| `hcloud_placement_group` | `spread`, so the three nodes land on different hosts |
| `hcloud_firewall` | Inbound TCP 22 from `var.ssh_allowed_cidrs` only. Apps come in through Cloudflare Tunnel (outbound only). Hetzner firewalls do not filter the private network, which k3s uses |
| `hcloud_primary_ip` x3 | IPv4, `auto_delete = false`: addresses survive a server rebuild |
| `hcloud_server` x3 | `node-1` (app, `10.0.1.11`), `node-2` (app, `10.0.1.12`), `db-1` (db, `10.0.1.21`); `ubuntu-24.04`, IPv4 only (GHCR needs it) |
| `hcloud_volume` `fjc-db` + attachment | 40 GB ext4 on `db-1`, not automounted |

## Workspace

HCP Terraform `FlamaCorp/hetzner`, VCS flow against this repo, working directory `terraform/hetzner`. Plan runs on
the PR; apply runs on merge after approval in the UI. Created by the owner (not by Terraform).

- Env `HCLOUD_TOKEN` (**sensitive**): a project token, held only in the workspace, set by the owner.
- Variable `ssh_allowed_cidrs` (HCL list, not sensitive), for example `["203.0.113.7/32"]`. If the owner's public IP
  changes, update it here and re-apply, or Ansible loses SSH (the Hetzner console still works).
- Variable `deploy_ssh_public_key` (string, not sensitive): the `ssh-ed25519 ...` public key.

`location` and `server_type` have defaults. Both no-default variables are validated at plan time.

## Protected

- The volume has `delete_protection` and `prevent_destroy`: the databases live there. Hetzner backups do not cover
  volumes; the S3 dumps are the backup. Ansible mounts it with a guard, never formats it blindly.
- Primary IPs are not deleted with their server.
- Changing the SSH key or image does not replace a node (`ignore_changes`).

## Rebuild one node

`terraform taint 'hcloud_server.node["node-1"]'` (or "Replace resources" on a run), then apply. The server is
recreated with the same primary IP and private IP and Ansible reconfigures it. For `db-1` the volume attachment is
recreated and the volume itself survives; re-run the Ansible mount step. Before replacing `db-1`, drain/stop k3s and
the DBs and unmount the volume: the attachment is destroyed (hot detach) before the server is deleted.
