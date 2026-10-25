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

- **Drill:** throwaway servers in a scratch namespace (steps 1-5). Run it every quarter, before any one-way door
  that relies on the backups and after a backup format change, then record it
  ([runbooks.md](../../../docs/runbooks.md#restore-drill)).
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
  aws s3 cp "s3://$B/$key" - | kubectl -n "$NS" exec -i "$POD" -- sh -c "cat >/tmp/$k.sql.gz"
  kubectl -n "$NS" exec "$POD" -- sh -c "echo '$sha  /tmp/$k.sql.gz' | sha256sum -c && gzip -t /tmp/$k.sql.gz"
done
```

Expect `/tmp/dump.sql.gz: OK` and `/tmp/grants.sql.gz: OK`. Anything else: stop, try an older night.

The manifest's SHA256 proves the file is the copy the manifest names, not who wrote it: the same
identity writes the dump, its hash and the manifest, so a set written while the cluster was
compromised can still check out here (DATA-04). This is why step 1 prefers a set from before the
incident, MySQL replays with `--binary-mode`, and the Postgres files are scanned before replay.

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

**Postgres** (`ziftbook-postgres`, `ziftbook-prod-postgres`). `psql` acts on a backslash meta-command
anywhere in a line (`\!` runs a shell command), and as superuser can reach the server's files
(`COPY ... FROM/TO PROGRAM` or a path, `lo_import`, `pg_read_file`) and load code (`CREATE EXTENSION`,
untrusted languages). The scan below is a guard against statements a `pg_dump`/`pg_dumpall` backup
should never contain, not a guarantee, so still choose a set from before the incident (step 1).
Custom-format dumps restored by a non-superuser role are planned to replace it (#272).
Scan the grants and the dump, each in its own mode, and abort if either is refused. Load the scanner
into the pod (its quotes do not survive a nested `sh -c`):

```bash
kubectl -n "$NS" exec -i "$POD" -- sh -c 'cat >/tmp/scan.awk' <<'AWK'
# BEGIN pg-restore-scan — tests/test_restore_runbook.py extracts and exercises this program.
# A guard, not a guarantee: it refuses a gunzipped Postgres backup that carries anything psql would
# act on beyond what pg_dump/pg_dumpall 18 emit for these databases. Select the scan with -v scan=grants
# (pg_dumpall --globals-only: a fixed allowlist of line shapes) or -v scan=dump (pg_dump --create: a
# quote-aware lexer). Custom-format dumps restored by a non-superuser role are planned to replace it
# (#272). Exit non-zero on the first refusal.
function refuse(why) { printf "SCAN REFUSED (%s): %s\n", why, $0 > "/dev/stderr"; bad = 1; exit 1 }
function refusel(why, ln) { printf "SCAN REFUSED (%s): %s\n", why, ln > "/dev/stderr"; bad = 1; exit 1 }

# ---- grants: an allowlist over pg_dumpall --globals-only (18) --------------------------------------
# Role/identifier: an unquoted name or a double-quoted identifier.
function roleok(s) { return (s ~ /^("[^"]*"|[A-Za-z_][A-Za-z0-9_$]*)$/) }
function alter_with_ok(line,   name, attrs, n, i, a, tok) {
  # ALTER ROLE <role> WITH <attrs>; — attrs are a fixed keyword vocabulary; SUPERUSER only for postgres.
  if (!match(line, /^ALTER ROLE ("[^"]*"|[A-Za-z_][A-Za-z0-9_$]*) WITH /)) return 0
  name = substr(line, 1, RLENGTH); sub(/^ALTER ROLE /, "", name); sub(/ WITH $/, "", name)
  attrs = substr(line, RLENGTH + 1)
  if (attrs !~ /;[ \t]*$/) return 0
  gsub(/'[^']*'/, "", attrs)                        # drop the SCRAM password / VALID UNTIL literal
  gsub(/;[ \t]*$/, "", attrs)
  n = split(attrs, a, /[ \t]+/)
  for (i = 1; i <= n; i++) {
    tok = a[i]
    if (tok == "") continue
    if (tok == "SUPERUSER") { if (name != "postgres") return 0; continue }
    if (tok ~ /^(NOSUPERUSER|INHERIT|NOINHERIT|CREATEROLE|NOCREATEROLE|CREATEDB|NOCREATEDB|LOGIN|NOLOGIN|REPLICATION|NOREPLICATION|BYPASSRLS|NOBYPASSRLS|PASSWORD|NULL|CONNECTION|LIMIT|VALID|UNTIL)$/) continue
    if (tok ~ /^[0-9]+$/) continue                  # CONNECTION LIMIT n
    return 0
  }
  return 1
}
function grants_line_ok(line) {
  if (line ~ /^[ \t]*$/) return 1
  if (line ~ /^--/) return 1
  if (line ~ /^\\(un)?restrict[ \t]+[^ \t]+[ \t]*$/) return 1
  if (line ~ /^SET [A-Za-z_][A-Za-z0-9_]* = [^;]*;[ \t]*$/) return 1
  if (line ~ /^CREATE ROLE ("[^"]*"|[A-Za-z_][A-Za-z0-9_$]*);[ \t]*$/) return 1
  if (line ~ /^ALTER ROLE ("[^"]*"|[A-Za-z_][A-Za-z0-9_$]*) SET [A-Za-z_]+ (TO|=) [^;]*;[ \t]*$/) return 1
  if (line ~ /^GRANT ("[^"]*"|[A-Za-z_][A-Za-z0-9_$]*) TO ("[^"]*"|[A-Za-z_][A-Za-z0-9_$]*) WITH INHERIT (TRUE|FALSE)( SET (TRUE|FALSE))?( ADMIN (TRUE|FALSE))?( GRANTED BY ("[^"]*"|[A-Za-z_][A-Za-z0-9_$]*))?;[ \t]*$/) return 1
  if (alter_with_ok(line)) return 1
  return 0
}

# ---- dump: a quote-aware lexer over pg_dump --create ----------------------------------------------
# State persists across lines: 0 code, sq '…', dq "…", dollar $tag$…$tag$, bc /*…*/ (nested), copy data.
function dump_line(line,   i, n, c, c2, rest, t, trimmed) {
  if (st == "copy") { if (line == "\\.") { st = 0 } ; return }   # data rows are opaque to psql
  if (st == 0) {
    trimmed = line; sub(/^[ \t]+/, "", trimmed)
    if (trimmed ~ /^\\/) {                                       # a whole-line meta-command
      if (trimmed ~ /^\\(connect|restrict|unrestrict)([ \t]|$)/) return
      refusel("backslash meta-command in SQL", line)
    }
    if (trimmed ~ /^COPY[ \t]/) {                                # a COPY header at a statement boundary
      if (trimmed ~ /^COPY[ \t]+.*[ \t]FROM[ \t]+stdin;[ \t]*$/ && trimmed !~ /PROGRAM/) { st = "copy"; return }
      refusel("unexpected COPY form (only FROM stdin is written by pg_dump)", line)
    }
  }
  n = length(line); i = 1
  while (i <= n) {
    c = substr(line, i, 1); c2 = substr(line, i, 2)
    if (st == "bc") {
      if (c2 == "*/") { bcd--; i += 2; if (bcd == 0) st = 0; continue }
      if (c2 == "/*") { bcd++; i += 2; continue }
      i++; continue
    }
    if (st == "sq") {
      if (c == "'") { if (substr(line, i + 1, 1) == "'") { i += 2; continue } st = 0; i++; continue }
      i++; continue
    }
    if (st == "dq") {
      if (c == "\"") { if (substr(line, i + 1, 1) == "\"") { i += 2; continue } st = 0; i++; continue }
      i++; continue
    }
    if (st == "dollar") {
      if (substr(line, i, length(dtag) + 2) == "$" dtag "$") { i += length(dtag) + 2; st = 0; continue }
      i++; continue
    }
    # st == 0 (code)
    if (c2 == "--") { return }                                   # line comment to EOL
    if (c2 == "/*") { st = "bc"; bcd = 1; i += 2; continue }
    if (c == "'") { st = "sq"; i++; continue }
    if (c == "\"") { st = "dq"; i++; continue }
    if (c == "$") {
      t = dollar_tag(line, i)
      if (t != "\037") { dtag = t; st = "dollar"; i += length(t) + 2; continue }
      code = code "$"; i++; continue
    }
    if (c == "\\") { refusel("backslash meta-command in SQL", line) }   # any other backslash in code
    code = code c; i++
  }
  code = code "\n"
}
# Return the dollar-quote tag opening at position i ("" for $$), or "\037" if this $ is not a tag open.
function dollar_tag(line, i,   j, ch, tag) {
  j = i + 1; tag = ""
  if (substr(line, j, 1) == "$") return ""
  if (substr(line, j, 1) ~ /[A-Za-z_]/) {
    while (substr(line, j, 1) ~ /[A-Za-z0-9_]/) { tag = tag substr(line, j, 1); j++ }
    if (substr(line, j, 1) == "$") return tag
  }
  return "\037"
}
function dump_finish(   norm, t) {
  norm = tolower(code); gsub(/[ \t\n]+/, " ", norm)
  if (norm ~ /from program/ || norm ~ /to program/) refusel("COPY ... PROGRAM", "(dump)")
  if (norm ~ /alter system/) refusel("ALTER SYSTEM", "(dump)")
  if (norm ~ /lo_import|lo_export/) refusel("large-object file access", "(dump)")
  if (norm ~ /pg_read_file|pg_read_binary_file|pg_ls_dir/) refusel("server-side file read", "(dump)")
  if (norm ~ /create language/) refusel("CREATE LANGUAGE", "(dump)")
  if (norm ~ /language plpython|language plperlu|plpython3u|plpythonu|untrusted/) refusel("untrusted procedural language", "(dump)")
  if (norm ~ /dblink/) refusel("dblink", "(dump)")
  # CREATE EXTENSION only for the extensions these databases legitimately use (btree_gist).
  t = norm
  gsub(/create extension if not exists btree_gist|create extension btree_gist/, "", t)
  if (t ~ /create extension/) refusel("CREATE EXTENSION other than btree_gist", "(dump)")
  # SUPERUSER / ALTER ROLE postgres: allowed only on the canonical postgres self line.
  t = norm; gsub(/nosuperuser/, "", t); gsub(/alter role postgres with superuser/, "", t)
  if (t ~ /superuser/) refusel("SUPERUSER grant", "(dump)")
  t = norm; gsub(/alter role postgres with superuser/, "", t)
  if (t ~ /alter role postgres/) refusel("ALTER ROLE postgres", "(dump)")
}

BEGIN { if (scan != "grants" && scan != "dump") { print "scan must be grants or dump" > "/dev/stderr"; exit 2 } }
scan == "grants" { if (!grants_line_ok($0)) refuse("line not in the globals allowlist"); next }
scan == "dump"   { dump_line($0) }
END { if (scan == "dump" && !bad) dump_finish() }
# END pg-restore-scan
AWK
kubectl -n "$NS" exec "$POD" -- sh -c '
  gzip -dc /tmp/grants.sql.gz | awk -v scan=grants -f /tmp/scan.awk || { echo "!! refused grants; do not replay" >&2; exit 1; }
  gzip -dc /tmp/dump.sql.gz   | awk -v scan=dump   -f /tmp/scan.awk || { echo "!! refused dump; do not replay"   >&2; exit 1; }
' && echo "scan passed"
```

Replay only after `scan passed`. `postgres` already exists on the target, so its `CREATE ROLE` line is
dropped before replay and every statement runs under `ON_ERROR_STOP`, which stops on the first real
error. The dump carries its own `CREATE DATABASE`.

```bash
kubectl -n "$NS" exec "$POD" -- sh -c '
  gzip -dc /tmp/grants.sql.gz | grep -v "^CREATE ROLE postgres;$" | psql -U postgres -Xq -v ON_ERROR_STOP=1 -d postgres &&
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
