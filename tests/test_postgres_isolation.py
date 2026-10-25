"""Fences on the boundary between Ziftbook staging and production on their shared Postgres (clusters/hetzner/data).

One server holds both environments' databases, and production's login roles SET ROLE to the roles staging logs in as
(postgres-bootstrap-prod.yaml), so the boundary is which login may open which database. The bootstrap Jobs' SQL is
read as text from their psql heredoc. Stdlib only; reuses the YAML subset parser of test_tbd_manifests.py.
"""
import re
import unittest

from test_tbd_manifests import CLUSTERS, load_all, pod_spec

DATA = CLUSTERS / "hetzner" / "data"
JOBS = {"staging": DATA / "postgres-bootstrap.yaml", "production": DATA / "postgres-bootstrap-prod.yaml"}
DATABASE = {"staging": "ziftbook", "production": "ziftbook_prod"}
LOGINS = {
    "staging": ("ziftbook_app", "ziftbook_migrate", "ziftbook_worker"),
    "production": ("ziftbook_prod_app", "ziftbook_prod_migrate", "ziftbook_prod_worker"),
}
CHECK = re.compile(r"(?s)DO \$check\$\n(.*)\n\$check\$;\Z")


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


if __name__ == "__main__":
    unittest.main()
