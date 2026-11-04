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


if __name__ == "__main__":
    unittest.main()
