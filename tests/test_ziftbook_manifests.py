"""Fences on the Ziftbook API and web in clusters/ (ziftbook-prod, ziftbook-staging) for 0.31.0.

Stdlib only; reuses the YAML subset parser of test_tbd_manifests.py.
"""
import unittest

from test_tbd_manifests import containers, manifests, where

APP_HOST = {"ziftbook-prod": "app.ziftbook.com", "ziftbook-staging": "dev.ziftbook.com"}
NEVER = {"ZIF_TURNSTILE_DISABLED", "ZIF_CSP_ENFORCE", "ZIF_SETUP_TOKEN"}


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
        for ns in APP_HOST:
            f, d, c, env = container(ns, "frontend")
            self.assertEqual(env["ZIF_CLIENT_IP_HEADER"].get("value"), "cf-connecting-ip", where(f, d, c))
            self.assertEqual(env["ZIF_TURNSTILE_SITE_KEY"]["valueFrom"]["secretKeyRef"]["name"], "ziftbook-turnstile")


if __name__ == "__main__":
    unittest.main()
