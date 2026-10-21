"""Fence: a Grafana-side query error must not page (2026-10-20: ruler to Prometheus i/o timeout fired every such rule)."""
import json
import unittest
from pathlib import Path

G = Path(__file__).resolve().parents[1] / "grafana"


class GrafanaRules(unittest.TestCase):
    def test_query_errors_keep_last_state(self):
        rules = [r for p in sorted(G.glob("*.rules.json")) for r in json.loads(p.read_text())["rules"]]
        self.assertGreater(len(rules), 10)
        for r in rules:
            self.assertEqual(r["execErrState"], "KeepLast", r["title"])


if __name__ == "__main__":
    unittest.main()
