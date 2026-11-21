# Restoring the database dumps

The nightly `db-backup` CronJob writes one set per night to the backup bucket `db-backups`:
`<prefix>/YYYY/MM/DD/{<db>,grants,manifest}_<stamp>`, the dump and grants as `<name>.gpg`, encrypted to the owner's
backup key; the manifest names the files inside. The manifest
carries the table count and the SHA256 of the dump and grants files. A Postgres dump is a custom-format archive,
`<db>_<stamp>.dump`; MySQL dumps and every grants file are gzipped SQL, `.sql.gz`.

| prefix | database | written by |
|---|---|---|
| `tbd-mysql/` | MySQL `tbd` (TBD; sets from before INFRA-73 are empty and named `pfv2`) | `data/db-backup` on the Hetzner cluster (INFRA-175) |
| `tbd-staging-mysql/` | MySQL `tbd_staging` (TBD staging, INFRA-67) | `data/db-backup` on the Hetzner cluster (INFRA-173) |
| `ziftbook-postgres/` | Postgres `ziftbook` (Ziftbook staging) | `data/db-backup` on the Hetzner cluster (INFRA-173) |
| `ziftbook-prod-postgres/` | Postgres `ziftbook_prod` (Ziftbook production, INFRA-82) | `data/db-backup` on the Hetzner cluster (INFRA-181) |

Only the owner's backup key can read a dump: the uploader and the probe see ciphertext at most. Run everything from
the Mac that holds that key (docs/runbooks.md "Backups to R2"), with the cluster kubeconfig and a read-only token for the
bucket (R2 > Manage API tokens, Object Read, `db-backups` only; delete it after a drill). Each file is decrypted on the
Mac and streams straight into the pod; nothing lands on the Mac except the manifest. Never print table contents or the
grants file (it holds password hashes).

There are two targets:

- **Drill:** throwaway servers in a scratch namespace (steps 1-5). Run it every quarter, before any one-way door
  that relies on the backups and after a backup format change, then record it
  ([runbooks.md](../../../docs/runbooks.md#restore-drill)).
- **Real restore:** the `data` StatefulSet pod after its volume was lost (step 6).

## 1. Pick a set

```bash
cd "$(mktemp -d)"   # keeps manifest.json out of the repo
export KUBECONFIG=~/.kube/hetzner AWS_REGION=auto
read -rsp 'Access Key ID: ' AWS_ACCESS_KEY_ID; echo; read -rsp 'Secret Access Key: ' AWS_SECRET_ACCESS_KEY; echo
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
E=https://<account id>.eu.r2.cloudflarestorage.com B=db-backups
PREFIX=tbd-mysql   # or tbd-staging-mysql, ziftbook-postgres, ziftbook-prod-postgres
# Ziftbook only: the database, the app namespace, the bootstrap Job and the restore login that go with the set.
ZDB=ziftbook ZNS=ziftbook-staging ZJOB=ziftbook-bootstrap ZLOGIN=ziftbook_migrate                 # ziftbook-postgres
# ZDB=ziftbook_prod ZNS=ziftbook-prod ZJOB=ziftbook-prod-bootstrap ZLOGIN=ziftbook_prod_migrate   # ziftbook-prod-postgres
M=$(aws s3api list-objects-v2 --endpoint-url "$E" --bucket "$B" --prefix "$PREFIX/" \
  --query 'reverse(sort_by(Contents[?contains(Key, `/manifest_`)], &LastModified))[0].Key' --output text)
aws s3 cp --endpoint-url "$E" "s3://$B/$M" manifest.json && jq . manifest.json   # date, tables, both keys and SHA256s
```

For an older night, set `M` to that night's manifest key. For a real restore, check that `date`
predates the loss and `tables` is above 0: a run during the outage can upload a valid but empty set.
After an incident, pick the newest set written *before the incident began*, not the newest overall,
because any set written once the cluster was compromised may carry changes made during it.

## 2. Scratch servers (drill only)

```bash
NS=restore-drill
# The server images the data StatefulSets run (this file pins none, see below).
img() { kubectl -n data get -o "jsonpath={.spec.template.spec.containers[?(@.name==\"$1\")].image}" statefulset "$1"; }
# On a cluster without them, read the manifests in a checkout of main instead (REPO: its root):
# img() { awk -v n="$1" '$1 == "image:" && index($2, n ":") == 1 { print $2; exit }' "$REPO/clusters/hetzner/data/$1.yaml"; }
MYSQL_IMAGE=$(img mysql) PG_IMAGE=$(img postgres)
printf '%s\n' "$MYSQL_IMAGE" "$PG_IMAGE"   # the versions this drill proves
kubectl create namespace "$NS"
kubectl label namespace "$NS" pod-security.kubernetes.io/enforce=baseline
openssl rand -hex 24 | tr -d '\n' | kubectl -n "$NS" create secret generic root --from-file=password=/dev/stdin
# Unquoted EOF so the two images expand; `:?` refuses to create the pods if either is empty.
kubectl apply -n "$NS" -f - <<EOF
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
      image: "${MYSQL_IMAGE:?}"
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
      image: "${PG_IMAGE:?}"
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

The drill replays into the server versions production runs: `img` reads them from the `data` StatefulSets, which
Flux applies from [`mysql.yaml`](mysql.yaml) and [`postgres.yaml`](postgres.yaml). Renovate bumps those manifests
(and the dump clients in [`db-backup.yaml`](db-backup.yaml)), never this file, so it pins no image and cannot drift
from them; do not paste one in (`tests/test_restore_drill.py`).

## 3. Copy and check the files

```bash
POD=mysql   # postgres for ziftbook-postgres and ziftbook-prod-postgres
# SQL on stdin, run as the database superuser over the pod's socket. --binary-mode: the mysql client
# otherwise runs `\!` and `system` lines from whatever it reads, backup content included (DATA-01).
my() { kubectl -n "$NS" exec -i "$POD" -- sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --binary-mode -uroot -NB "$@"' sh "$@"; }
pg() { kubectl -n "$NS" exec -i "$POD" -- psql -U postgres -XAtq -v ON_ERROR_STOP=1 "$@"; }
for k in dump grants; do
  key=$(jq -r ".$k.key" manifest.json) sha=$(jq -r ".$k.sha256" manifest.json)
  f=/tmp/$k.${key#*.}   # the key's extension: sql.gz, or dump for a Postgres dump
  # Decrypted on the Mac (gpg asks for the backup key's passphrase once), never written to its disk.
  aws s3 cp --endpoint-url "$E" "s3://$B/$key.gpg" - | gpg --decrypt --quiet | kubectl -n "$NS" exec -i "$POD" -- sh -c "cat >$f"
  kubectl -n "$NS" exec "$POD" -- sh -c "echo '$sha  $f' | sha256sum -c && case $f in *.gz) gzip -t $f; esac"
done
```

Expect `/tmp/grants.sql.gz: OK` and `/tmp/dump.sql.gz: OK` (`/tmp/dump.dump: OK` for a Postgres
dump). Anything else: stop, try an older night.

The manifest's SHA256 proves the file is the copy the manifest names, not who wrote it: the same
identity writes the dump, its hash and the manifest, so a set written while the cluster was
compromised can still check out here (DATA-04). This is why step 1 prefers a set from before the
incident, MySQL replays with `--binary-mode`, and a Postgres dump is restored by a login that is not a
superuser.

## 4. Restore

Grants first, so the data restore can reference the users.

**MySQL** (`tbd-mysql`, `tbd-staging-mysql`). The dump has no `CREATE DATABASE` or `USE` (dumped
without `--databases`), so a dump loads into `tbd` as is; the grants use
`CREATE USER IF NOT EXISTS`, so existing users keep their passwords. For `tbd-staging-mysql`, write `tbd_staging`
for every `tbd` database name in this step and in step 5, or the staging data lands in production's `tbd`.

```bash
kubectl -n "$NS" exec "$POD" -- sh -c 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"
  gzip -dc /tmp/grants.sql.gz | mysql --binary-mode -uroot &&
  mysql --binary-mode -uroot -e "CREATE DATABASE IF NOT EXISTS tbd" &&
  gzip -dc /tmp/dump.sql.gz | mysql --binary-mode -uroot tbd && echo restored'
```

**Postgres** (`ziftbook-postgres`, `ziftbook-prod-postgres`). `pg_restore` loads the dump into an existing, empty
database, logged in as a Ziftbook role that is not a superuser: whatever the archive holds runs with the owner role's rights only, and
`pg_restore` runs no client commands. `--no-owner --role=ziftbook_migrate` makes `ziftbook_migrate` own every object,
as the migrations do; `--single-transaction --exit-on-error`: on any error nothing is restored. The roles, the
database and its grants come from git (the bootstrap Jobs), never from the backup: the grants file is not replayed.

Drill only: create on the scratch server what the bootstrap Jobs create (no passwords: the restore logs in over the
pod's socket).

```bash
ZLOGIN=ziftbook_migrate   # the drill logs in as the owner role itself
pg -d postgres <<SQL
CREATE ROLE ziftbook_migrate LOGIN;
CREATE ROLE ziftbook_app;
CREATE ROLE ziftbook_worker IN ROLE ziftbook_app;
CREATE DATABASE $ZDB;
GRANT CONNECT, CREATE, TEMPORARY ON DATABASE $ZDB TO ziftbook_migrate;
SQL
pg -d "$ZDB" -c 'GRANT CREATE ON SCHEMA public TO ziftbook_migrate'
```

Restore:

```bash
kubectl -n "$NS" exec "$POD" -- pg_restore -U "$ZLOGIN" --role=ziftbook_migrate --no-owner \
  --single-transaction --exit-on-error -d "$ZDB" /tmp/dump.dump && echo restored
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

Remove the drill (this deletes the restored data with it), the manifest and the token from the shell:

```bash
kubectl delete namespace "$NS" && rm manifest.json; unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
```

Then delete the read token in R2 > Manage API tokens.

Once both engines have passed, set the drill's date in [`docs/restore-drill.md`](../../../docs/restore-drill.md) in a PR.

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
4. Postgres only: the restore goes into the empty `$ZDB` and needs the roles and grants first. Rerun both
   bootstrap Jobs from a checkout of main (`REPO`: its root), staging's first (production's revokes CONNECT on
   `ziftbook`). They are idempotent and set every password from the Secrets; production's creates `ziftbook_prod`.
   Then the gate again for `$ZDB` (on a fresh volume `ziftbook_prod` exists only from here).

   ```bash
   for f in postgres-bootstrap postgres-bootstrap-prod; do
     j=$(awk '$1 == "name:" { print $2; exit }' "$REPO/clusters/hetzner/data/$f.yaml")
     kubectl -n data delete job "$j" --ignore-not-found
     kubectl apply -f "$REPO/clusters/hetzner/data/$f.yaml"
     kubectl -n data wait --for=condition=complete --timeout=10m "job/$j" || break
   done
   ```
   The other Ziftbook database on the same server stays as it is.
5. Step 4.
   - MySQL: the entrypoint and `10-backup-user.sh` created `tbd_app` and `tbd_backup` from the
     `mysql` secret, and `IF NOT EXISTS` leaves them as they are.
   - Postgres: the roles are the ones item 4's Jobs made, with the Secrets' passwords.
6. Step 5's checks, then remove the copies: `kubectl -n data exec "$POD" -- sh -c 'rm -f /tmp/dump.* /tmp/grants.sql.gz'`.
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

   Then revoke every TBD API token: personal tokens and OAuth grants share `api_tokens`, and one revoked after the
   dump would come back live. Users and MCP clients mint or authorize again. Again `tbd_staging` for a staging set;
   a set from before the table existed fails with "doesn't exist", which is safe to ignore.

   ```bash
   echo 'UPDATE api_tokens SET revoked_at = UTC_TIMESTAMP() WHERE revoked_at IS NULL;' | my tbd
   ```

   And revoke every open TBD invitation: one revoked after the dump would come back with a working link. Owners
   resend the invitations they still want.

   ```bash
   echo 'UPDATE invitations SET revoked_at = UTC_TIMESTAMP() WHERE accepted_at IS NULL AND revoked_at IS NULL;' | my tbd
   ```
8. Resume (`suspend` was set by hand, so Flux does not clear it):

   ```bash
   kubectl -n data patch cronjob db-backup -p '{"spec":{"suspend":false}}'
   flux resume kustomization flux-system
   ```

   Scale the app back up, and check that it logs in and that `kubectl -n data get job` shows the
   bootstrap Job Complete.
