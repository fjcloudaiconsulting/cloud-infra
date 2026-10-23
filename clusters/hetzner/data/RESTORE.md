# Restoring the database dumps

The nightly `db-backup` CronJob writes one set per night to
`tbd-mysql-backups-884686184019`: `<prefix>/YYYY/MM/DD/{<db>,grants,manifest}_<stamp>`. The manifest
carries the table count and the SHA256 of the dump and grants files.

| prefix | database | written by |
|---|---|---|
| `tbd-mysql/` | MySQL `tbd` (TBD; sets from before INFRA-73 are empty and named `pfv2`) | `data/db-backup` on the Hetzner cluster (INFRA-175) |
| `tbd-staging-mysql/` | MySQL `tbd_staging` (TBD staging, INFRA-67) | `data/db-backup` on the Hetzner cluster (INFRA-173) |
| `ziftbook-postgres/` | Postgres `ziftbook` (Ziftbook staging) | `data/db-backup` on the Hetzner cluster (INFRA-173) |
| `ziftbook-prod-postgres/` | Postgres `ziftbook_prod` (Ziftbook production, INFRA-82) | `data/db-backup` on the Hetzner cluster (INFRA-181) |

Only an account admin can read the dumps: the uploaders are put-only and the probe is list-only
(`terraform/tbd-backups`). Run everything from a Mac with admin AWS credentials (private ops notes) and the NetBird
kubeconfig. Dumps stream from S3 straight into the pod; nothing lands on the Mac except the
manifest. Never print table contents or the grants file (it holds password hashes).

There are two targets:

- **Drill:** throwaway servers in a scratch namespace (steps 1-5). Run it before any one-way door
  that relies on the backups and after a backup format change.
- **Real restore:** the `data` StatefulSet pod after its volume was lost (step 6).

## 1. Pick a set

```bash
cd "$(mktemp -d)"   # keeps manifest.json out of the repo
export KUBECONFIG=~/.kube/hetzner AWS_PROFILE=fjc   # every set lives on Hetzner
B=tbd-mysql-backups-884686184019
PREFIX=tbd-mysql   # or tbd-staging-mysql, ziftbook-postgres, ziftbook-prod-postgres
# Ziftbook only: the database, the app namespace and the bootstrap Job that go with the set.
ZDB=ziftbook ZNS=ziftbook-staging ZJOB=ziftbook-bootstrap                 # ziftbook-postgres
# ZDB=ziftbook_prod ZNS=ziftbook-prod ZJOB=ziftbook-prod-bootstrap        # ziftbook-prod-postgres
M=$(aws s3api list-objects-v2 --bucket "$B" --prefix "$PREFIX/" \
  --query 'reverse(sort_by(Contents[?contains(Key, `/manifest_`)], &LastModified))[0].Key' --output text)
aws s3 cp "s3://$B/$M" manifest.json && jq . manifest.json   # date, tables, both keys and SHA256s
```

For an older night, set `M` to that night's manifest key. For a real restore, check that `date`
predates the loss and `tables` is above 0: a run during the outage can upload a valid but empty set.

## 2. Scratch servers (drill only)

```bash
NS=restore-drill
kubectl create namespace "$NS"
kubectl label namespace "$NS" pod-security.kubernetes.io/enforce=baseline
openssl rand -hex 24 | tr -d '\n' | kubectl -n "$NS" create secret generic root --from-file=password=/dev/stdin
kubectl apply -n "$NS" -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-ingress
spec:
  podSelector: {}
  policyTypes: [Ingress]
---
apiVersion: v1
kind: Pod
metadata:
  name: mysql
spec:
  containers:
    - name: mysql
      image: mysql:8.4.11@sha256:6ea90827b1100f8f2ae306a539f86d2c264a26ed435a2a9f75551dd5c3aeb242
      env:
        - name: MYSQL_ROOT_HOST
          value: localhost
        - name: MYSQL_ROOT_PASSWORD
          valueFrom: {secretKeyRef: {name: root, key: password}}
      resources:
        limits: {memory: 768Mi}
---
apiVersion: v1
kind: Pod
metadata:
  name: postgres
spec:
  containers:
    - name: postgres
      image: postgres:18.6@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336
      env:
        - name: POSTGRES_PASSWORD
          valueFrom: {secretKeyRef: {name: root, key: password}}
      resources:
        limits: {memory: 512Mi}
EOF
# The entrypoints run a socket-only server first; TCP answers only once the real server is up.
until kubectl -n "$NS" exec mysql -- mysqladmin ping -h127.0.0.1 --silent 2>/dev/null; do sleep 5; done
until kubectl -n "$NS" exec postgres -- pg_isready -qh 127.0.0.1 2>/dev/null; do sleep 5; done
```

The images are the ones `mysql.yaml`, [`clusters/hetzner/data/db-backup.yaml`](../../hetzner/data/db-backup.yaml) and [`clusters/hetzner/data/postgres.yaml`](../../hetzner/data/postgres.yaml) pin. Renovate bumps those manifests, not this file: if they differ, use the image from the manifest.

## 3. Copy and check the files

```bash
POD=mysql   # postgres for ziftbook-postgres and ziftbook-prod-postgres
# SQL on stdin, run as the database superuser over the pod's socket.
my() { kubectl -n "$NS" exec -i "$POD" -- sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -NB "$@"' sh "$@"; }
pg() { kubectl -n "$NS" exec -i "$POD" -- psql -U postgres -XAtq -v ON_ERROR_STOP=1 "$@"; }
for k in dump grants; do
  key=$(jq -r ".$k.key" manifest.json) sha=$(jq -r ".$k.sha256" manifest.json)
  aws s3 cp "s3://$B/$key" - | kubectl -n "$NS" exec -i "$POD" -- sh -c "cat >/tmp/$k.sql.gz"
  kubectl -n "$NS" exec "$POD" -- sh -c "echo '$sha  /tmp/$k.sql.gz' | sha256sum -c && gzip -t /tmp/$k.sql.gz"
done
```

Expect `/tmp/dump.sql.gz: OK` and `/tmp/grants.sql.gz: OK`. Anything else: stop, try an older night.

## 4. Restore

Grants first, so the data restore can reference the users.

**MySQL** (`tbd-mysql`, `tbd-staging-mysql`). The dump has no `CREATE DATABASE` or `USE` (dumped
without `--databases`), so a dump loads into `tbd` as is; the grants use
`CREATE USER IF NOT EXISTS`, so existing users keep their passwords. For `tbd-staging-mysql`, write `tbd_staging`
for every `tbd` database name in this step and in step 5, or the staging data lands in production's `tbd`.

```bash
kubectl -n "$NS" exec "$POD" -- sh -c 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
  gzip -dc /tmp/grants.sql.gz | mysql -uroot &&
  mysql -uroot -e "CREATE DATABASE IF NOT EXISTS tbd" &&
  gzip -dc /tmp/dump.sql.gz | mysql -uroot tbd && echo restored'
```

**Postgres** (`ziftbook-postgres`, `ziftbook-prod-postgres`). The globals recreate every role, so `role "postgres" already
exists` is the one expected error; the dump carries its own `CREATE DATABASE`.

```bash
kubectl -n "$NS" exec "$POD" -- sh -c '
  gzip -dc /tmp/grants.sql.gz | psql -U postgres -Xq 2>&1 | grep -E "ERROR|FATAL" | grep -v "role \"postgres\" already exists"
  gzip -dc /tmp/dump.sql.gz | psql -U postgres -Xq -v ON_ERROR_STOP=1 -d postgres >/dev/null && echo restored'
```

Expect only `restored`.

## 5. Verify, then remove the drill

Table count must equal the manifest's `tables`. Row counts per table are the record of the drill.

**MySQL:**

```bash
jq .tables manifest.json
my <<'SQL'
SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'tbd' AND table_type = 'BASE TABLE';
SELECT user FROM mysql.user WHERE user LIKE 'tbd%';
SQL
# Exact row count per table.
my <<'SQL' | my
SELECT CONCAT('SELECT ''', table_name, ''', COUNT(*) FROM tbd.`', table_name, '`;')
FROM information_schema.tables WHERE table_schema = 'tbd' AND table_type = 'BASE TABLE' ORDER BY table_name;
SQL
```

A round trip proves the rows match the dump byte for byte: re-dump with the backup's options and
compare the `INSERT` lines. Expect two identical hashes. A pair of `e3b0c442...` is the hash of
nothing: the set had no rows, so nothing was compared.

```bash
kubectl -n "$NS" exec "$POD" -- sh -c 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
  gzip -dc /tmp/dump.sql.gz | grep "^INSERT INTO" | sha256sum
  mysqldump -uroot --single-transaction --routines --triggers --events --quick --hex-blob tbd | grep "^INSERT INTO" | sha256sum'
```

**Postgres:** table and row counts (no round trip). The table filter matches the backup's, which
skips tables owned by extensions.

```bash
jq .tables manifest.json
pg -d "$ZDB" <<'SQL'
SELECT count(*) FROM pg_tables t WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND NOT EXISTS
  (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.deptype = 'e'
    AND d.objid = format('%I.%I', t.schemaname, t.tablename)::regclass);
SELECT rolname FROM pg_roles WHERE rolname NOT LIKE 'pg\_%' ORDER BY 1;
SQL
pg -d "$ZDB" <<'SQL' | pg -d "$ZDB"
SELECT format('SELECT %L, count(*) FROM %I.%I;', schemaname || '.' || tablename, schemaname, tablename)
FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1;
SQL
```

Remove the drill (this deletes the restored data with it) and the manifest:

```bash
kubectl delete namespace "$NS" && rm manifest.json
```

## 6. Real restore into `data`

Not drilled: the drill covers steps 1-5 only. Restore only into an **empty** database (a fresh
volume). A MySQL dump replaces every table it contains (`DROP TABLE IF EXISTS`).

1. Stop everything that writes, and keep Flux from undoing it:

   ```bash
   flux suspend kustomization flux-system
   kubectl -n data patch cronjob db-backup -p '{"spec":{"suspend":true}}'
   ```

   Then scale the app Deployments in `tbd-prod`, `tbd-staging` (both use MySQL) or `$ZNS` (Ziftbook) to 0.
2. Steps 1 and 3 with `NS=data` and `POD=mysql-0` (or `postgres-0`, on the Hetzner cluster: `KUBECONFIG=~/.kube/hetzner`).
3. Gate: step 5's table-count query (the first statement of the `my` or `pg -d "$ZDB"` block) must
   print `0`. Anything else: stop and decide; never drop a database on reflex.
4. Postgres only: the entrypoint (`ziftbook`) or the bootstrap Job (`ziftbook_prod`) created an empty `$ZDB` that the dump's `CREATE DATABASE`
   would collide with. With the gate at `0`: `pg -d postgres -c "DROP DATABASE $ZDB"`. The other
   Ziftbook database on the same server stays as it is.
5. Step 4.
   - MySQL: the entrypoint and `10-backup-user.sh` created `tbd_app` and `tbd_backup` from the
     `mysql` secret, and `IF NOT EXISTS` leaves them as they are.
   - Postgres: the entrypoint creates only `postgres`, so the globals bring back every role **with
     its password as of that night**, `postgres` included. Make the secrets authoritative again:
     `kubectl -n data delete job "$ZJOB"` (Flux recreates it on resume and it resets the
     app passwords). If `admin-password` changed after that night, set it from
     the pod's env (the value never leaves the pod):

     ```bash
     kubectl -n data exec postgres-0 -- sh -c \
       'echo "ALTER ROLE postgres PASSWORD :'\''pw'\''" | psql -U postgres -Xq -v pw="$POSTGRES_PASSWORD"'
     ```
6. Step 5's checks, then remove the copies: `kubectl -n data exec "$POD" -- rm /tmp/dump.sql.gz /tmp/grants.sql.gz`.
7. Revoke what the dump brings back: sessions signed out after it, and links used or revoked after
   it. Everyone signs in again; owners resend open invites, and pending sign-ups and resets start over.
   Postgres (`POD=postgres-0`):

   ```bash
   pg -d "$ZDB" -c 'TRUNCATE sessions; DELETE FROM email_tokens; UPDATE invites SET token_hash = NULL'
   ```

   Jobs that ran after the dump run again (handlers are safe to repeat). Owners re-check their invite
   list afterwards: an invite whose `email.invite` job was still pending in the dump gets a fresh
   link when that job runs; every other open invite needs a resend.

   MySQL (`POD=mysql-0`; write `tbd_staging` for `tbd` with a staging set). From migration
   `089_sessions_to_mysql` on (INFRA-122), TBD keeps its sessions in MySQL; deleting the families
   also deletes their members (`ON DELETE CASCADE`). A set from before that migration has no such
   table: skip this.

   ```bash
   echo 'DELETE FROM auth_session_families;' | my tbd
   ```
8. Resume (`suspend` was set by hand, so Flux does not clear it):

   ```bash
   kubectl -n data patch cronjob db-backup -p '{"spec":{"suspend":false}}'
   flux resume kustomization flux-system
   ```

   Scale the app back up, and check that it logs in and that `kubectl -n data get job` shows the
   bootstrap Job Complete.
