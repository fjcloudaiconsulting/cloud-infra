"""Fence: a Grafana-side query error must not page (2026-10-20: ruler to Prometheus i/o timeout fired every such rule)."""
import json
import re
import unittest
from pathlib import Path

G = Path(__file__).resolve().parents[1] / "grafana"


class GrafanaRules(unittest.TestCase):
    def test_query_errors_keep_last_state(self):
        rules = [r for p in sorted(G.glob("*.rules.json")) for r in json.loads(p.read_text())["rules"]]
        self.assertGreater(len(rules), 10)
        for r in rules:
            self.assertEqual(r["execErrState"], "KeepLast", r["title"])

    def test_every_app_process_has_a_liveness_rule(self):
        """INFRA-200: each backend/MCP Deployment's tbd_process_up and each frontend's cAdvisor series is named in a rule."""
        exprs = " ".join(r["data"][0]["model"]["expr"] for p in G.glob("*.rules.json") for r in json.loads(p.read_text())["rules"])
        clusters = G.parent / "clusters" / "hetzner"
        envs = {"tbd-prod": "production", "tbd-staging": "staging"}
        # shortcut: one TBD container per file, read by regex; switch to test_tbd_manifests' parser if a file holds two.
        seen = 0
        for ns, env in envs.items():
            for f in sorted((clusters / ns).glob("*.yaml")):
                text = f.read_text()
                image = re.search(r"image: ghcr\.io/fjcloudaiconsulting/tbd/(backend|mcp)[:@]", text)
                if not image:
                    continue
                named = re.search(r"- name: OTEL_SERVICE_NAME.*\n\s+value: (\S+)", text)
                service = named.group(1) if named else {"backend": "tbd-api", "mcp": "tbd-mcp"}[image.group(1)]
                self.assertIn(f'tbd_process_up{{service_name="{service}", deployment_environment_name="{env}"}}', exprs, f)
                seen += 1
        self.assertEqual(seen, 4)
        frontends = sorted(clusters.glob("*/frontend.yaml"))
        self.assertEqual(len(frontends), 4)
        for f in frontends:
            self.assertIn(f'namespace="{f.parent.name}", container="frontend"', exprs, f)




class TbdSuperadminAlerts(unittest.TestCase):
    """A populated TBD install with no active superadmin locks everyone out of /admin; the scheduler's probe is the
    only detector, so its log line and the age of its last read both alert."""

    def setUp(self):
        rules = json.loads((G / "tbd-app.rules.json").read_text())["rules"]
        self.rules = {r["uid"]: r for r in rules}

    def test_no_superadmin_alerts_on_the_probe_error_in_production(self):
        expr = self.rules["tbd-no-superadmin"]["data"][0]["model"]["expr"]
        self.assertIn('k8s_namespace_name="tbd-prod"', expr)
        self.assertIn('level="ERROR" | msg="platform.superadmin.none"', expr)
        self.assertEqual(self.rules["tbd-no-superadmin"]["labels"]["severity"], "critical")

    def test_stale_probe_outlasts_two_scheduler_ticks_and_alerts_when_absent(self):
        scheduler = (G.parent / "clusters/hetzner/tbd-prod/scheduler.yaml").read_text()
        tick = re.search(r"- name: SCHEDULER_TICK_SECONDS\s*\n\s+value: \"?(\d+)", scheduler)
        tick = int(tick.group(1)) if tick else 900  # tbd's default scheduler_tick_seconds
        rule = self.rules["tbd-superadmin-probe-stale"]
        threshold = rule["data"][1]["model"]["conditions"][0]["evaluator"]["params"][0]
        self.assertGreaterEqual(threshold, 2 * tick)
        # A missing series (probe never succeeded since the scheduler started) must read as stale, not as no data.
        self.assertIn("or on() vector(1e9)", rule["data"][0]["model"]["expr"])

if __name__ == "__main__":
    unittest.main()
