# Ansible: Hetzner hosts and k3s HA (INFRA-64)

Terraform (`terraform/hetzner`) creates the servers, the private network and the database volume. Ansible does
everything on the hosts: base config, the volume mount on db-1, and k3s. There is no cloud-init user data.

| Host | Private IP | Role | k3s |
|------|-----------|------|-----|
| node-1 | 10.0.1.11 | app | server, label `role=app` |
| node-2 | 10.0.1.12 | app | server, label `role=app` |
| db-1 | 10.0.1.21 | db | server, label `role=db`, taint `role=db:NoSchedule`, volume at `/var/lib/rancher/k3s/storage` |

All three are k3s servers with embedded etcd (quorum 2 of 3), flannel `wireguard-native` over the private NIC.

`site.yml` runs four plays: refuse placeholder addresses, `node_base` on every host (swap, security-only
unattended upgrades without reboot, key-only SSH, the private NIC on DHCP via `/etc/netplan/60-private.yaml`
because a network attached after first boot is missing from cloud-init's netplan file), `db_storage` on db-1, then the k3s servers one at a time
(`serial: 1`, stop at the first failure, wait for the node Ready and etcd healthy after each, db-1 last).

## Prerequisites (the machine running Ansible)

- Run every command from this directory (`ansible/`), so `ansible.cfg` applies.
- ansible-core 2.21 (CI pins 2.21.5), the Python `netaddr` library (`k3s.orchestration` uses `ansible.utils.ipwrap`),
  `sops`, and the Ansible age key: `export SOPS_AGE_KEY_FILE=~/.config/sops/age/ansible-hetzner.txt`. The owner's
  cluster key is never needed for Ansible.
- `kubectl` on this machine. The k3s play checks it first: without it the collection silently skips fetching the
  admin kubeconfig, which the play reads the cluster CA from.
- `ansible-galaxy collection install -r requirements.yml` (installs into the git-ignored `collections/`).
- The deploy key at `~/.ssh/fjc-hetzner-deploy`, with its public half next to it (`.pub`; `ssh-keygen -y -f
  ~/.ssh/fjc-hetzner-deploy > ~/.ssh/fjc-hetzner-deploy.pub` recreates it). `node_base` reads it ([Root SSH
  keys](#root-ssh-keys)).
- `inventory.yaml` holds each server's primary IPv4 (they outlive server rebuilds). The first play refuses a
  `192.0.2.x` placeholder.

The collection handles the token in task arguments. The `k3s_server` role runs with `no_log` (so `-v` shows
`censored` for it) and the k3s play has `diff: false` (so `--diff` cannot print it). `-e k3s_no_log=false` lifts
`no_log` to debug a failing role; never share that output.

## The k3s token

`secrets.sops.yaml` holds `k3s_token`, encrypted to two age keys (`.sops.yaml`): the owner key and the Ansible key
(`~/.config/sops/age/ansible-hetzner.txt` on the operator laptop, which decrypts only `ansible/`). INFRA-180 adds a CI
recipient. It is decrypted lazily, only when a k3s task needs it. A lost Ansible key: make a new one, swap its public
key into `.sops.yaml`, then `sops updatekeys ansible/secrets.sops.yaml` with the owner key. A leaked one (Ansible or
owner key) also needs [the token rotated](#rotate-the-k3s-token-live): `updatekeys` keeps the same data key, and git
history keeps the old ciphertext. The token for a new cluster:
`openssl rand -hex 32 | sed 's/^/k3s_token: /' | sops encrypt --filename-override ansible/secrets.sops.yaml > ansible/secrets.sops.yaml`
from the repo root. Never print it.

## Rotate the k3s token (live)

`k3s token rotate` replaces the server token on a running cluster (k3s added it in 2023; step 1 checks the running
version has it). Run it after a lost or leaked age key that decrypts `ansible/` (the Ansible key, or the owner key,
which is also the in-cluster Flux key), a leaked deploy SSH key, a node disk, backup or etcd snapshot out of your
hands, or any cluster-admin incident. It does not change the cluster CA or its keys: if a rogue server may have
joined or the bootstrap data was read, that is a CA rotation (`k3s certificate rotate-ca`), not this.

From the repo root on a new branch, with `SOPS_AGE_KEY_FILE` set as above. Nothing below prints the token.

1. Every server Ready, nothing cordoned, no other k3s or reboot run going. `N1=<node-1 public IPv4>`, then
   `ssh root@$N1 'k3s token rotate --help >/dev/null && echo supported'` and
   `ssh root@$N1 k3s etcd-snapshot save --name pre-token-rotate`. Snapshots from before the rotation need the old
   token to restore; git history keeps it.
2. The new token, into the SOPS file (through a temp file, so a failed encrypt leaves the old one):

   ```sh
   F=ansible/secrets.sops.yaml
   openssl rand -hex 32 | sed 's/^/k3s_token: /' \
     | sops encrypt --filename-override "$F" --input-type yaml --output-type yaml /dev/stdin > "$F.new" && mv "$F.new" "$F"
   grep -c 'k3s_token: ENC\[AES256' "$F"   # 1
   ```

3. Rotate on node-1: the new token goes over ssh stdin, the old one is the server's own token file. The new value is
   in that command's arguments on node-1 while it runs.

   ```sh
   sops decrypt --extract '["k3s_token"]' "$F" \
     | ssh root@$N1 'T=$(cat); [ -n "$T" ] && K3S_TOKEN=$(cat /var/lib/rancher/k3s/server/token) k3s token rotate --new-token "$T"'
   ```

4. Right away, from `ansible/` on the same branch: `ansible-playbook site.yml -e k3s_reconfigure=true`. It writes the
   new token into each server's `config.yaml` and restarts the servers one at a time behind the quorum guard. Until
   this run reaches a server, that server must not restart: with the old token it cannot read the cluster's bootstrap
   data and does not start (rerun this step to fix it).
5. PR the new `secrets.sops.yaml` with the `PLAY RECAP` and merge it straight away: until then `main` still has the old
   token, so run nothing k3s-related from `main`.

## Root SSH keys

`node_base` makes root's `authorized_keys` on every node exactly the deploy keys: the public key next to
`ansible_ssh_private_key_file` (`~/.ssh/fjc-hetzner-deploy.pub`) plus `deploy_ssh_extra_public_keys` in
`group_vars/all.yml`. Any other key goes on the next run. The `hetzner` workspace variable `deploy_ssh_public_key`
only seeds a new server. Before writing, the play stops if a key is not one line of `ssh-ed25519` or
`sk-ssh-ed25519@openssh.com` (FIDO2), or if the key it connects with is not already accepted on that host.

Read the change first, the first time and whenever the list changes: `ansible-playbook site.yml --check --diff`. The
`authorized_keys` diff shows public keys only; every removed line must be a key you mean to drop. If root SSH is lost
anyway, use the Hetzner console ([break-glass](../docs/runbooks.md#hetzner-nodes-ansible-rebuild-break-glass)).

Rotate the deploy key (after a leak, also [rotate the k3s token](#rotate-the-k3s-token-live): root on a node can read
it):

1. `ssh-keygen -t ed25519 -f ~/.ssh/fjc-hetzner-deploy.new` (`-t ed25519-sk` for a FIDO2 key), add the content of
   `~/.ssh/fjc-hetzner-deploy.new.pub` to `deploy_ssh_extra_public_keys`, merge, run `site.yml` (check first). Both
   keys now work.
2. Move the new pair over the old one (`mv ~/.ssh/fjc-hetzner-deploy.new ~/.ssh/fjc-hetzner-deploy`, the same for
   `.pub`), empty `deploy_ssh_extra_public_keys`, set the workspace variable `deploy_ssh_public_key` to the new public
   key (its apply replaces only the Hetzner key object, no server), merge, run `site.yml`: the old key is gone from
   every node.

## Split brain: bootstrap versus join

The collection gives `cluster-init` to the first host of the `server` group (node-1) and points the others at
`api_endpoint`. `cluster-init` on an empty disk starts a brand-new etcd cluster, so a rebuilt node-1 run that way
would start a second cluster beside the live one. Here the collection's choice is overridden (our `server_config_yaml`
is merged last):

- `k3s_bootstrap` (default `false`). Only `-e k3s_bootstrap=true` gives node-1 `cluster-init: true`. The play refuses
  it unless every server is in the run and none has etcd data (`/var/lib/rancher/k3s/server/db/etcd`).
- Otherwise every server, node-1 included, has `cluster-init: false` and `server: https://<k3s_join_peer>:6443`:
  node-1 joins through node-2, node-2 and db-1 through node-1. A server without etcd data first checks that its peer's
  API answers, and stops if not (k3s would otherwise wait forever to join).
- To join through a different live peer (say node-1 is the rebuilt one and node-2 is also down), pass
  `-e k3s_join_peer=10.0.1.21`.

The first bootstrap runs the three servers as one batch, because the collection's own check waits until every server
has joined, which a one-host batch can never satisfy on an empty cluster. Every later run is `serial: 1`.

The collection restarts k3s on every server it runs on, one at a time (a short API blip per node), so the play runs it
only for a bootstrap, a server without etcd data (new or rebuilt) or `-e k3s_reconfigure=true`. Any other run skips k3s
and restarts nothing.

Quorum guard: before such a run restarts a server that has etcd data, the play checks that every server is in the run (none
failed or was unreachable in an earlier play, none left out by `--limit`) and that `kubectl get nodes` shows every
server Ready. Otherwise it stops before any restart, since restarting one member while another is down leaves etcd
at 1 of 3. So a `k3s_reconfigure` run covers all three servers; only a rebuilt node (no etcd data yet) runs alone.
After each restart it waits for that node's own `/readyz/etcd`, then for the node to be Ready.

## First bootstrap

1. `terraform/hetzner` applied; the volume `fjc-db` is attached to db-1.
2. The primary IPv4s in `inventory.yaml` match `terraform/hetzner`.
3. Accept each host key only after checking it (this first run sends the k3s token). In the Hetzner Cloud Console,
   open the server's Console: at first boot cloud-init prints its fingerprints under `BEGIN SSH HOST KEY
   FINGERPRINTS`. Then `ssh -i ~/.ssh/fjc-hetzner-deploy root@<ip> true` and answer `yes` only if the `ED25519`
   fingerprint it shows is the console's. No fingerprint on the console any more: Rescue > Reset root password, log
   in there, `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, then `passwd -l root`.
4. From `main` at the merged SHA:

   ```sh
   ansible-playbook site.yml --check --diff
   ansible-playbook site.yml -e k3s_bootstrap=true
   ```

5. Check: `ssh root@<node-1> k3s kubectl get nodes -o wide` shows three Ready nodes; db-1 carries the taint.
   The play fetches the admin kubeconfig to `~/.kube/fjc-hetzner.yaml`, writes its cluster CA to
   `~/.kube/hetzner-ca.crt` and deletes it: its client certificate is `system:masters`, which no RBAC change revokes.
   Day-to-day access is the owner token over NetBird ([bootstrap runbook](../docs/runbooks.md#hetzner-cluster-bootstrap)
   step 6, which uses that CA file); break-glass is `ssh root@<server> k3s kubectl ...`.

## Steady-state run

1. Merge the change first.
2. `git switch main && git pull` and note the SHA.
3. `ansible-playbook site.yml --check --diff`, read it.
4. `ansible-playbook site.yml` (no `-e k3s_bootstrap`). It leaves k3s alone on every server that already has etcd
   data, so a re-run changes nothing (`changed=0`). The pinned collection restarts every running server whenever it
   runs, so it runs only for a bootstrap, a new or rebuilt node, or a k3s settings change
   (`group_vars` k3s keys): then add `-e k3s_reconfigure=true`, which restarts the servers one at a time behind the
   quorum guard. k3s versions move through system-upgrade-controller (INFRA-179), not this playbook.
5. Paste the `PLAY RECAP` into the PR with the SHA.

## API audit log

Each k3s server writes an API audit log to `/var/lib/rancher/k3s/server/logs/audit.log` (at most 5 files of 100 MB,
none older than 30 days). The policy is [`files/k3s-audit-policy.yaml`](files/k3s-audit-policy.yaml), installed on
every run as `/etc/rancher/k3s/audit-policy.yaml`: metadata (who, what, when, never a value) for Secrets, ConfigMaps,
token requests and token reviews; the request too for exec, attach, port-forward, RBAC changes and node changes. The
API server reads both only at start, so the log begins, and a policy change applies, after a `-e k3s_reconfigure=true`
run (each server restarts in turn behind the quorum guard). A server logs only the requests it served, so read all
three:

    ssh root@<server> tail -n 200 /var/lib/rancher/k3s/server/logs/audit.log \
      | jq -c '{t: .requestReceivedTimestamp, user: .user.username, verb, res: .objectRef.resource, ns: .objectRef.namespace, name: .objectRef.name}'

The log stays on the node; nothing ships it (or the SSH journal) elsewhere yet, so a root intruder can delete it.

## Pod Security defaults

The API server applies Pod Security defaults from [`files/k3s-pod-security.yaml`](files/k3s-pod-security.yaml),
installed on every run as `/etc/rancher/k3s/pod-security.yaml`: enforce `baseline`, warn and audit `restricted`, for
any namespace without a `pod-security.kubernetes.io/<mode>` label of its own for that mode. A namespace label always
wins, so the namespaces under `clusters/hetzner/` keep the enforce level they set; the enforce default reaches
`default`, `kube-public`, `kube-node-lease`, `flux-system` while its kustomization does not label it, and any namespace
made by hand. Only `kube-system` is exempt: k3s's add-ons and the local-path helper pods (hostPath) run there. Warn and
audit at `restricted` add apply warnings and audit annotations where a namespace sets only an enforce label; they
refuse nothing.

Like the audit policy, it takes effect on a `-e k3s_reconfigure=true` run (each server restarts in turn; one run
covers both). Before that run, list the namespaces and have the API server name the pods `baseline` would refuse in
each one without an enforce label (a dry run, nothing changes):

    kubectl get ns -L pod-security.kubernetes.io/enforce,pod-security.kubernetes.io/warn,pod-security.kubernetes.io/audit
    for ns in $(kubectl get ns -o json | jq -r '.items[] | select(.metadata.labels["pod-security.kubernetes.io/enforce"] == null) | .metadata.name' | grep -vx kube-system); do
      kubectl label --dry-run=server ns "$ns" pod-security.kubernetes.io/enforce=baseline
    done

Expect only `namespace/<ns> labeled` lines. `Warning: existing pods in namespace "<ns>" violate ...` names pods the
default would refuse once they are recreated: give that namespace its own enforce label in git first, then run. After
the run, `grep -c admission-control-config-file /etc/rancher/k3s/config.yaml` prints 1 on each server, and a
host-network pod in `default` is refused (a dry run again):

    kubectl -n default run psa-check --image=busybox --restart=Never --dry-run=server \
      --overrides='{"apiVersion":"v1","spec":{"hostNetwork":true}}'   # Forbidden: violates PodSecurity "baseline:latest"

To back out, remove the `admission-control-config-file` line in `group_vars/all.yml` and run with
`-e k3s_reconfigure=true` again. If a server's API server does not come back, the run stops there and the quorum
guard refuses the next run: on that server delete the line from `/etc/rancher/k3s/config.yaml` and
`systemctl restart k3s` (`journalctl -u k3s` shows why it failed), then fix git.

## Rolling reboot

unattended-upgrades installs kernel updates but never reboots (`node_base`), so a node runs the old kernel until it is
rebooted. `reboot.yml` (INFRA-192) reboots only the servers with `/var/run/reboot-required`, one at a time, in
inventory order (db-1 last): quorum and Ready checks first (as in `site.yml`), drain, reboot, wait for Ready and
`/readyz/etcd` on every server, uncordon. Prod keeps serving from the other app node during node-1 or node-2 (PDBs
and two replicas, INFRA-177). db-1 holds MySQL and Postgres, so its reboot is a few minutes of full outage for both
apps: run it in a quiet hour, not across the nightly dumps (02:00 UTC).

Cadence: monthly, or as soon as a node has `/var/run/reboot-required` (`kubectl get nodes -o wide` shows the running
kernel; a node behind the others is the hint).

1. `ansible-playbook reboot.yml --check` lists which servers need it (the reboot itself is skipped in check mode).
2. `ansible-playbook reboot.yml` with every server in the run (`--limit` is refused: the quorum check needs them all).
   `-e reboot_force=true` reboots every server, for a drill.
3. After it: `kubectl get nodes -o wide` (same kernel everywhere, all Ready, none SchedulingDisabled), then the
   post-deploy smoke for `tbd-prod` and `ziftbook-prod` (Actions > Post-deploy Smoke > Run workflow).

A run that stops halfway leaves that node cordoned: fix the cause, `kubectl uncordon <node>`, then rerun (finished
nodes are skipped, they no longer need a reboot). It also stops if a node asks for a reboot again right after booting (the daily
upgrade timer installed a newer kernel): rerun it later.

## Rebuilding one node

Rebuild means Terraform replaces the server (`terraform apply -replace='hcloud_server.node["<name>"]'` via a TFC run);
the primary IP and private IP stay. Etcd keeps quorum with two of three servers, so rebuild one node at a time and
wait for it to be Ready before touching another.

The order matters: stop k3s on the old node before deleting its Node object. Deleting the Node removes its etcd
member and its node-password secret; a k3s that is still running (or restarts) on the old node re-registers and
rejoins as a stale member, and the rebuilt node then hits a node-password conflict.

1. From a working node: `k3s kubectl drain <name> --ignore-daemonsets --delete-emptydir-data --timeout=5m`. On an app
   node it times out on the PDBs (the evicted replicas wait Pending for this node, docs/runbooks.md "Drain or lose an
   app node"); the other replicas serve, so go on.
2. On the OLD node: `systemctl disable --now k3s && /usr/local/bin/k3s-killall.sh`.
3. From a working node: `k3s kubectl delete node <name>`.
4. Replace the server in TFC.
5. `ssh-keygen -R <public ip>`, accept the new host key only after checking it against the console ([First
   bootstrap](#first-bootstrap) step 3), then `ansible-playbook site.yml --limit <name>` (never with
   `k3s_bootstrap`). The rebuilt node has no etcd data, so the quorum guard lets it run alone; it joins a live peer.

If that run fails after k3s started on the new node, the node now has etcd data: a plain rerun skips k3s on it and
then times out (about 8 min) waiting for its etcd and Ready, and a `k3s_reconfigure` rerun is refused by the quorum
guard. Reset it instead:

1. db-1 only, FIRST: `systemctl stop k3s && /usr/local/bin/k3s-killall.sh && umount /var/lib/rancher/k3s/storage`,
   and check `findmnt /var/lib/rancher/k3s/storage` prints nothing. `k3s-uninstall.sh` deletes everything under the
   k3s data dir, including the contents of a volume still mounted inside it: the database files.
2. On the new node: `/usr/local/bin/k3s-uninstall.sh`.
3. If it registered (`k3s kubectl get nodes` on a working node lists it): `k3s kubectl delete node <name>` from a
   working node.
4. Rerun `ansible-playbook site.yml --limit <name>` (on db-1 this mounts the volume again before k3s is installed).

### App node (node-2)

The steps above, nothing more.

### node-1

The same. node-1 has no etcd data after the rebuild, so it joins through node-2 (`k3s_join_peer`); it never
re-initialises the cluster.

### db-1

The volume holds the database files and survives the rebuild; it must be unmounted cleanly before the old server
goes away.

1. Scale the database StatefulSets in `data` to 0 and wait for the pods to go.
2. Drain db-1 (step 1 above).
3. On the old db-1: `systemctl disable --now k3s && /usr/local/bin/k3s-killall.sh`, then
   `umount /var/lib/rancher/k3s/storage` (`findmnt /var/lib/rancher/k3s/storage` prints nothing).
4. `k3s kubectl delete node db-1` from a working node.
5. Replace the server in TFC (the attachment follows the new server).
6. `ssh-keygen -R <public ip>`, accept the new host key only after checking it against the console ([First
   bootstrap](#first-bootstrap) step 3), then `ansible-playbook site.yml --limit db-1`. The play finds the volume, mounts it by
   its filesystem UUID and installs the k3s mount guard before k3s is installed.
7. Scale the StatefulSets back up; the local-path volumes are pinned to node `db-1` and come back with their data.

## Volume-missing drill (db-1)

What it proves: without the volume, k3s on db-1 does not start, so the databases cannot initialise empty data dirs
on the root disk.

1. Scale the database StatefulSets in `data` to 0, drain db-1, then on db-1
   `systemctl stop k3s && /usr/local/bin/k3s-killall.sh` and `umount /var/lib/rancher/k3s/storage`. Keep the Node object.
2. Detach the volume (Hetzner console or `hcloud volume detach fjc-db`).
3. `systemctl start k3s` fails with a dependency error after the device timeout (about 90 s);
   `systemctl status k3s` names `var-lib-rancher-k3s-storage.mount`.
4. `ansible-playbook site.yml --limit db-1 --check` stops at "Exactly one volume is attached".
5. A reboot in this state stops in emergency mode (the fstab entry has no `nofail`, on purpose); use the Hetzner
   console.
6. Re-attach the volume (`hcloud volume attach fjc-db --server db-1`, automount off), `mount -a`, `systemctl start k3s`,
   uncordon db-1, scale the StatefulSets back up.
