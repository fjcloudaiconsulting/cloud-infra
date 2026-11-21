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

    def test_no_superadmin_alerts_on_a_zero_count_in_production(self):
        # The probe's count gauge, not its log line: only allowlisted TBD events are shipped (see the Loki fences below).
        rule = self.rules["tbd-no-superadmin"]
        self.assertEqual(rule["data"][0]["datasourceUid"], "grafanacloud-prom")
        self.assertIn('tbd_platform_active_superadmins{service_name="tbd-scheduler", '
                      'deployment_environment_name="production"}', rule["data"][0]["model"]["expr"])
        self.assertEqual(rule["data"][1]["model"]["conditions"][0]["evaluator"], {"type": "lt", "params": [0.5]})
        self.assertEqual(rule["labels"]["severity"], "critical")

    def test_stale_probe_outlasts_two_scheduler_ticks_and_alerts_when_absent(self):
        scheduler = (G.parent / "clusters/hetzner/tbd-prod/scheduler.yaml").read_text()
        tick = re.search(r"- name: SCHEDULER_TICK_SECONDS\s*\n\s+value: \"?(\d+)", scheduler)
        tick = int(tick.group(1)) if tick else 900  # tbd's default scheduler_tick_seconds
        rule = self.rules["tbd-superadmin-probe-stale"]
        threshold = rule["data"][1]["model"]["conditions"][0]["evaluator"]["params"][0]
        self.assertGreaterEqual(threshold, 2 * tick)
        # A missing series (probe never succeeded since the scheduler started) must read as stale, not as no data.
        self.assertIn("or on() vector(1e9)", rule["data"][0]["model"]["expr"])

ALLOY = (G.parent / "clusters/hetzner/observability/alloy/config.alloy").read_text()
LOKI = [q["model"]["expr"] for p in sorted(G.glob("*.rules.json")) for r in json.loads(p.read_text())["rules"]
        for q in r["data"] if q.get("datasourceUid") == "grafanacloud-logs"]


def read_namespaces(block):
    m = re.search(r'discovery\.kubernetes "%s" \{.*?names = \[([^\]]*)\]' % block, ALLOY, re.S)
    return set(re.findall(r'"([^"]+)"', m.group(1)))


class LokiRulesReadShippedLogs(unittest.TestCase):
    def test_every_log_rule_targets_a_namespace_alloy_reads(self):
        # fence: a log rule on a namespace Alloy does not read sees no data, and noDataState OK keeps it silent forever.
        shipped = read_namespaces("ziftbook_pods") | read_namespaces("tbd_pods")
        self.assertTrue(LOKI)
        for expr in LOKI:
            namespaces = set(re.findall(r'k8s_namespace_name="([^"]+)"', expr))
            for alternatives in re.findall(r'k8s_namespace_name=~"([^"]+)"', expr):
                namespaces |= set(alternatives.split("|"))
            self.assertTrue(namespaces, expr)
            self.assertLessEqual(namespaces, shipped, expr)

    def test_tbd_log_rules_match_only_allowlisted_events(self):
        # fence: TBD lines leave the node only when their msg is on the allowlist in config.alloy; a rule on any other
        # msg never sees data and stays silent.
        allowed = set(re.findall(r"'([^']+)'", re.search(r"contains\(\[([^\]]*)\], msg\)", ALLOY).group(1)))
        self.assertIn("plans.rollover.failed", allowed)
        tbd = [e for e in LOKI if 'service_namespace="tbd"' in e]
        self.assertTrue(tbd)
        for expr in tbd:
            events = {v for m in re.findall(r'\| msg=~?"([^"]+)"', expr) for v in m.split("|")}
            self.assertTrue(events, expr)
            self.assertLessEqual(events, allowed, expr)

    def test_tbd_logs_leave_only_through_the_allowlist(self):
        # fence: the TBD source feeds only the allowlist stage, which drops everything else and rewrites the line from
        # the named fields. Pointing the source at the Ziftbook pipeline would ship TBD records as they are.
        source = re.search(r'loki\.source\.kubernetes "tbd_pods" \{(.*?)\n\}', ALLOY, re.S).group(1)
        self.assertRegex(source, r"forward_to = \[loki\.process\.tbd_events\.receiver\]")
        stage = re.search(r'loki\.process "tbd_events" \{(.*?)\n\}', ALLOY, re.S).group(1)
        self.assertIn("drop_malformed = true", stage)
        self.assertRegex(stage, r'source\s*= "keep"\s*value\s*= "false"')
        self.assertRegex(stage, r'stage\.output \{\s*source = "line"\s*\}')
        self.assertNotIn("exc", re.search(r'line = "([^"]*)"', stage).group(1))
        self.assertEqual(1, len(re.findall(r"loki\.process\.tbd_events\.receiver", ALLOY)))


if __name__ == "__main__":
    unittest.main()
