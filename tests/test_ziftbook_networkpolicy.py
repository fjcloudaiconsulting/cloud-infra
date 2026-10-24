"""Fences on who may reach the Ziftbook pods (ZIF-217): only the tunnel reaches the web, only the web reaches the API,
nothing reaches the worker, everything else is denied.

Stdlib only: `python3 -m unittest discover -s tests`. The manifest parser is test_tbd_manifests'. That file also
fences every Service under clusters/ to ClusterIP (no NodePort or LoadBalancer), so it is not repeated here.
"""
import unittest

from test_tbd_manifests import manifests

NAMESPACES = ("ziftbook-staging", "ziftbook-prod")
TUNNEL = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "cloudflared"}},
          "podSelector": {"matchLabels": {"app": "cloudflared"}}}
WEB = {"podSelector": {"matchLabels": {"app": "frontend"}}}


def policies(ns):
    return {d["metadata"]["name"]: d["spec"] for _, d in manifests(ns) if d.get("kind") == "NetworkPolicy"}


def selected(spec):
    return ((spec.get("podSelector") or {}).get("matchLabels") or {}).get("app")


class ZiftbookIngress(unittest.TestCase):
    def test_default_deny_ingress(self):
        for ns in NAMESPACES:
            deny = [s for s in policies(ns).values() if s.get("podSelector") == {}
                    and "Ingress" in s["policyTypes"] and not s.get("ingress")]
            self.assertTrue(deny, f"{ns}: no default-deny ingress policy")

    def test_web_only_from_the_tunnel_API_only_from_the_web_worker_from_nothing(self):
        want = {"frontend": ([TUNNEL], [{"port": 3000}]), "backend": ([WEB], [{"port": 8000}])}
        for ns in NAMESPACES:
            by_app = {}
            for name, s in policies(ns).items():
                if "Ingress" not in s["policyTypes"]:
                    continue
                if s.get("podSelector") == {}:
                    self.assertFalse(s.get("ingress"), f"{ns} {name}: ingress rule for every pod")
                else:
                    # An allow policy for any other app (the worker included) would open a way in.
                    self.assertIn(selected(s), want, f"{ns} {name}: ingress policy for {selected(s)}")
                    by_app.setdefault(selected(s), []).append(s)
            for app, (frm, ports) in want.items():
                self.assertEqual(len(by_app.get(app, [])), 1, f"{ns}: want exactly one policy for {app}")
                rules = by_app[app][0]["ingress"]
                self.assertEqual([(r.get("from"), r.get("ports")) for r in rules], [(frm, ports)], f"{ns} {app}")


if __name__ == "__main__":
    unittest.main()
