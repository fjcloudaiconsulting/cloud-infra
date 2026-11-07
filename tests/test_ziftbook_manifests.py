"""Fences on the Ziftbook API and web in clusters/ (ziftbook-prod, ziftbook-staging) for 0.31.0.

Stdlib only; reuses the YAML subset parser of test_tbd_manifests.py.
"""
import unittest

from test_tbd_manifests import containers, manifests, where

APP_HOST = {"ziftbook-prod": "app.ziftbook.com", "ziftbook-staging": "dev.ziftbook.com"}
# ZIF_CSP_ENFORCE is not here: it is opt-in ("true" enforces, anything else is report-only), so setting it only
# tightens the web. A repeat first-run setup lifts ZIF_SETUP_TOKEN from this set for its PR (runbooks.md).
NEVER = {"ZIF_TURNSTILE_DISABLED", "ZIF_SETUP_TOKEN"}
# Erasure ledger (ZIF-58, INFRA-267): the env's bucket, per namespace wired so far (prod joins in its own PR), and the
# key variables with their key in Secret ziftbook-ledger-worker.
LEDGER_BUCKET = {"ziftbook-staging": "ziftbook-ledger-staging"}
LEDGER_KEYS = {"ZIF_ERASURE_LEDGER_ACCESS_KEY_ID": "access-key-id",
               "ZIF_ERASURE_LEDGER_SECRET_ACCESS_KEY": "secret-access-key"}


def container(ns, name):
    (hit,) = [(f, d) for f, d in manifests(ns) if d.get("kind") == "Deployment" and d["metadata"]["name"] == name]
    f, d = hit
    (c,) = [c for c in containers(d, init=False) if c["name"] == name]
    return f, d, c, {e["name"]: e for e in c.get("env") or []}


class ZiftbookApiAndWebEnv(unittest.TestCase):
    def test_no_container_sets_the_opt_out_switches(self):
        for ns in APP_HOST:
            for f, d in manifests(ns):
                for c in containers(d):
                    self.assertFalse({e["name"] for e in c.get("env") or []} & NEVER, where(f, d, c))

    def test_api_env(self):
        for ns, host in APP_HOST.items():
            f, d, c, env = container(ns, "backend")
            self.assertEqual(env["ZIF_APP_URL"].get("value"), f"https://{host}", where(f, d, c))
            self.assertEqual(env["ZIF_TRUSTED_PROXIES"].get("value"), "10.42.0.0/16", where(f, d, c))
            self.assertEqual(env["ZIF_TURNSTILE_SECRET"]["valueFrom"]["secretKeyRef"]["name"], "ziftbook-turnstile")
            self.assertNotIn("ZIF_MIGRATE_DATABASE_URL", env, where(f, d, c))

    def test_web_env(self):
        for ns, host in APP_HOST.items():
            f, d, c, env = container(ns, "frontend")
            self.assertEqual(env["ZIF_CLIENT_IP_HEADER"].get("value"), "cf-connecting-ip", where(f, d, c))
            self.assertEqual(env["ZIF_CSP_REPORT_URL"].get("value"), f"https://{host}/api/csp-reports", where(f, d, c))
            self.assertEqual(env["ZIF_TURNSTILE_SITE_KEY"]["valueFrom"]["secretKeyRef"]["name"], "ziftbook-turnstile")


class ZiftbookErasureLedger(unittest.TestCase):
    def test_ledger_keys_only_on_the_worker_from_its_own_secret(self):
        # The API never holds a ledger key, the operator's read-only key never sits on a Deployment (only the
        # short-lived reapply pod), and the worker's key variables read ziftbook-ledger-worker, never ziftbook-storage.
        for ns in APP_HOST:
            for f, d in manifests(ns):
                for c in containers(d):
                    for e in c.get("env") or []:
                        ref = (e.get("valueFrom") or {}).get("secretKeyRef") or {}
                        if e["name"] in LEDGER_KEYS or ref.get("name", "").startswith("ziftbook-ledger-"):
                            w = f"{where(f, d, c)} {e['name']}"
                            self.assertEqual((d["metadata"]["name"], c["name"]), ("worker", "worker"), w)
                            self.assertEqual(ref, {"name": "ziftbook-ledger-worker", "key": LEDGER_KEYS.get(e["name"])}, w)

    def test_wiring(self):
        for ns, bucket in LEDGER_BUCKET.items():
            f, d, c, env = container(ns, "worker")
            self.assertEqual(env["ZIF_ERASURE_LEDGER_BUCKET"].get("value"), bucket, where(f, d, c))
            self.assertLessEqual(set(LEDGER_KEYS), set(env), where(f, d, c))
            # The ledger bucket is EU: it answers only on the .eu endpoint.
            self.assertRegex(env["ZIF_S3_ENDPOINT"].get("value"),
                             r"^https://[0-9a-f]{32}\.eu\.r2\.cloudflarestorage\.com$", where(f, d, c))
            f, d, c, env = container(ns, "backend")
            self.assertEqual(env["ZIF_ERASURE_LEDGER_BUCKET"].get("value"), bucket, where(f, d, c))


if __name__ == "__main__":
    unittest.main()
