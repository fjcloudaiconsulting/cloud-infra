"""Fences on the boundary between Ziftbook staging and production on their shared Postgres (clusters/hetzner/data).

One server holds both environments' databases, and production's login roles SET ROLE to the roles staging logs in as
(postgres-bootstrap-prod.yaml), so the boundary is which login may open which database, and who may take the
server's connection slots. The bootstrap Jobs' SQL is read as text from their psql heredoc. Stdlib only; reuses the
YAML subset parser of test_tbd_manifests.py.
"""
import re
import unittest

from test_tbd_manifests import CLUSTERS, load_all, manifests, pod_spec

DATA = CLUSTERS / "hetzner" / "data"
JOBS = {"staging": DATA / "postgres-bootstrap.yaml", "production": DATA / "postgres-bootstrap-prod.yaml"}
DATABASE = {"staging": "ziftbook", "production": "ziftbook_prod"}
NAMESPACE = {"staging": "ziftbook-staging", "production": "ziftbook-prod"}
LOGINS = {
    "staging": ("ziftbook_app", "ziftbook_migrate", "ziftbook_worker"),
    "production": ("ziftbook_prod_app", "ziftbook_prod_migrate", "ziftbook_prod_worker"),
}
CHECK = re.compile(r"(?s)DO \$check\$\n(.*)\n\$check\$;\Z")
# Connections per pod at full pool, from the Ziftbook code of the running release (app/main.py, app/setup.py,
# app/worker.py, migrations/env.py): the API's pool of 5 + 10 overflow and its one-off start-up check, the worker's 21
# with no overflow, one per migrate init container (every API and worker pod runs one), and the admin CLI's one.
API_PER_POD, WORKER_PER_POD, MIGRATE_PER_POD, ADMIN_CLI = 16, 21, 1, 1
# What a warm pool keeps open: the API its 5 (overflow closes on return), the worker its 21.
API_STANDING, WORKER_STANDING = 5, 21
SUPERUSER_RESERVED_DEFAULT = 3
# Kubernetes evicts a lost node's pods after 300 s (the default not-ready/unreachable tolerations): their sessions
# must be gone by then, or the replacement pods find their role's cap taken.
NODE_LOSS_EVICTION_SECONDS = 300


def job_container(env):
    (doc,) = load_all(JOBS[env])
    (c,) = pod_spec(doc)["containers"]
    return c


def job_sql(env):
    """The SQL the Job pipes to psql, without comments or blank lines."""
    command = job_container(env).get("command") or []
    m = re.search(r"(?s)<<'SQL'\n(.*?)\nSQL\n", command[-1] if command else "")
    if not m:
        raise AssertionError(f"{JOBS[env].name}: no psql heredoc in the command")
    lines = (re.sub(r"\s*--.*$", "", line).rstrip() for line in m.group(1).split("\n"))
    return [line for line in lines if line.strip()]


def transaction(env):
    """The statements between BEGIN; and the COMMIT; that must end the script."""
    sql = job_sql(env)
    if "BEGIN;" not in sql or sql[-1] != "COMMIT;":
        raise AssertionError(f"{JOBS[env].name}: the SQL is not one transaction ending in COMMIT")
    return "\n".join(sql[sql.index("BEGIN;") + 1:-1])


def check_block(env):
    m = CHECK.search(transaction(env))
    if not m:
        raise AssertionError(f"{JOBS[env].name}: the transaction does not end with the DO $check$ block")
    return m.group(1)


def server_settings():
    """The `-c name=value` args of the Postgres StatefulSet."""
    (ss,) = [d for d in load_all(DATA / "postgres.yaml") if d.get("kind") == "StatefulSet"]
    (c,) = pod_spec(ss)["containers"]
    args = c.get("args") or []
    if args[::2] != ["-c"] * (len(args) // 2) or len(args) % 2:
        raise AssertionError(f"postgres.yaml: args are not -c pairs: {args}")
    return dict(value.split("=", 1) for value in args[1::2])


def seconds(value):
    m = re.fullmatch(r"(\d+)(s|min)?", value)
    if not m:
        raise AssertionError(f"not a time in s or min: {value}")
    return int(m.group(1)) * (60 if m.group(2) == "min" else 1)


def deployment_pods(env, name, surge=True):
    """The pods of a Deployment that run at once: its replicas, doubled when a rollout may surge."""
    (d,) = [d for _, d in manifests(NAMESPACE[env]) if d.get("kind") == "Deployment" and d["metadata"]["name"] == name]
    replicas = d["spec"].get("replicas", 1)
    recreate = (d["spec"].get("strategy") or {}).get("type") == "Recreate"
    return replicas * (1 if recreate or not surge else 2)


def full_pools(env):
    """Each environment's connections at full pools, by the shared role they run as."""
    api, worker = deployment_pods(env, "backend"), deployment_pods(env, "worker")
    return {"ziftbook_app": API_PER_POD * api, "ziftbook_worker": WORKER_PER_POD * worker,
            "ziftbook_migrate": MIGRATE_PER_POD * (api + worker) + ADMIN_CLI}


def standing_pools(env):
    return (API_STANDING * deployment_pods(env, "backend", surge=False)
            + WORKER_STANDING * deployment_pods(env, "worker", surge=False))


def caps(env):
    """The CONNECTION LIMITs a Job's transaction sets."""
    return {role: int(n) for role, n in re.findall(r"(?m)^ALTER ROLE (\w+) CONNECTION LIMIT (\d+);$", transaction(env))}


def staging_caps():
    return caps("staging")


def reservation_clause(body, negated):
    """The roles a check clause tests for (NOT) holding pg_use_reserved_connections."""
    m = re.search(r"WHERE rolname IN \(([^)]*)\)\n\s*AND " + ("NOT " if negated else "")
                  + r"pg_has_role\(oid, 'pg_use_reserved_connections', 'USAGE'\);", body)
    return set(re.findall(r"'(\w+)'", m.group(1))) if m else set()


class EnvironmentBoundary(unittest.TestCase):
    """Each environment's logins open only its own database, checked at the end of both Jobs (DATA-02)."""

    def test_bootstrap_runs_inside_the_transaction(self):
        for env in JOBS:
            sql = job_sql(env)
            self.assertIn(r"\i /work/bootstrap.sql", sql, env)
            self.assertLess(sql.index("BEGIN;"), sql.index(r"\i /work/bootstrap.sql"), env)
            self.assertEqual(sql[-1], "COMMIT;", env)

    def test_both_jobs_end_with_the_same_check(self):
        self.assertEqual(check_block("staging"), check_block("production"))

    def test_the_check_covers_every_login_against_the_other_database(self):
        body = check_block("production")
        pairs = set(re.findall(r"\('(\w+)', '(\w+)'\)", body))
        crossed = {(role, DATABASE["production"]) for role in LOGINS["staging"]}
        crossed |= {(role, DATABASE["staging"]) for role in LOGINS["production"]}
        self.assertEqual(pairs, crossed)
        self.assertIn("has_database_privilege(r.oid, d.oid, 'CONNECT')", body)
        self.assertRegex(body, r"IF crossed IS NOT NULL THEN\n\s*RAISE EXCEPTION ")

    def test_production_job_closes_the_maintenance_databases(self):
        self.assertRegex(transaction("production"), r"(?m)^REVOKE CONNECT ON DATABASE postgres, template1 FROM PUBLIC;$")

    def test_kubernetes_hands_the_scripts_over_as_written(self):
        # Kubernetes expands $(NAME) in a container's command and args and turns $$ into $, which breaks a $$ quote.
        for env in JOBS:
            c = job_container(env)
            for part in (c.get("command") or []) + (c.get("args") or []):
                for token in ("$$", "$("):
                    self.assertFalse(token in part, f"{JOBS[env].name}: {token} in the psql command")


class ConnectionSlots(unittest.TestCase):
    """Staging's logins cannot take the connection slots production needs (DATA-03)."""

    def test_both_jobs_cap_every_staging_login_at_its_full_pool(self):
        # Both: bootstrap.sql creates the shared roles uncapped, and either Job may run first or alone.
        need = full_pools("staging")
        self.assertEqual(caps("production"), caps("staging"), "the two Jobs set different caps")
        self.assertEqual(set(caps("staging")), set(LOGINS["staging"]))
        for role, cap in caps("staging").items():
            self.assertGreaterEqual(cap, need[role], f"{role}: capped below staging's full pool")

    def test_only_production_logins_may_take_the_reserved_slots(self):
        self.assertGreater(int(server_settings().get("reserved_connections", "0")), 0, "postgres.yaml")
        m = re.search(r"(?m)^GRANT pg_use_reserved_connections TO ([\w, ]+)\n\s*WITH INHERIT TRUE;$",
                      transaction("production"))
        self.assertIsNotNone(m, "the production Job does not grant pg_use_reserved_connections, inherited")
        self.assertEqual({r.strip() for r in m.group(1).split(",")}, set(LOGINS["production"]))

    def test_the_check_requires_the_caps_and_the_reservation(self):
        body = check_block("production")
        self.assertEqual({role: int(n) for role, n in re.findall(r"\('(\w+)', (\d+)\)", body)}, caps("staging"),
                         "the check's caps differ from the ones the Jobs set")
        self.assertRegex(body, r"WHERE r\.rolconnlimit < 0 OR r\.rolconnlimit > c\.cap;\n\s*IF uncapped IS NOT NULL THEN"
                               r"\n\s*RAISE EXCEPTION ")
        self.assertEqual(reservation_clause(body, negated=False), set(LOGINS["staging"]),
                         "the check does not refuse the reserved slots to every staging login")
        self.assertEqual(reservation_clause(body, negated=True), set(LOGINS["production"]),
                         "the check does not require the reserved slots for every production login")

    def test_connection_budget(self):
        s = server_settings()
        total, reserved = int(s["max_connections"]), int(s.get("reserved_connections", "0"))
        superuser = int(s.get("superuser_reserved_connections", SUPERUSER_RESERVED_DEFAULT))
        self.assertEqual(set(staging_caps()), set(LOGINS["staging"]), "a staging login has no cap to budget")
        self.assertGreater(reserved, 0, "no slots reserved for production")
        caps = sum(staging_caps().values())
        self.assertLessEqual(caps + reserved + superuser, total, "staging's caps reach into the reserved slots")
        self.assertLessEqual(superuser + caps + sum(full_pools("production").values()), total,
                             "production's full pools do not fit beside staging at its caps")
        # Postgres refuses logins without pg_use_reserved_connections once fewer than reserved + superuser slots are
        # free, whoever holds the rest: a larger reservation refuses staging below its caps.
        self.assertLessEqual(superuser + caps + standing_pools("production") + reserved, total,
                             "the reservation refuses staging below its caps beside production's standing pools")

    def test_a_slot_is_released_soon_by_a_stalled_or_lost_client(self):
        s = server_settings()
        self.assertLessEqual(seconds(s.get("authentication_timeout", "1min")), 10)
        idle, interval, count = (int(s.get(f"tcp_keepalives_{k}", "0")) for k in ("idle", "interval", "count"))
        self.assertTrue(idle and interval and count, "postgres.yaml: tcp_keepalives_idle, _interval and _count")
        self.assertLess(idle + interval * count, NODE_LOSS_EVICTION_SECONDS)


if __name__ == "__main__":
    unittest.main()
