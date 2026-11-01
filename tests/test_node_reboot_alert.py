"""Fences on the node reboot-age alert (INFRA-219): Alloy's embedded node_exporter runs the stat collector, so
node_boot_time_seconds reaches Grafana Cloud as job="node", instance=<node>, and rule NodeRebootOverdue
(grafana/node-reboot.rules.json) mails the owner once a node has gone 35 days without a boot.

Stdlib only. config.alloy is read with the small parser below (labelled and plain blocks, `name = value` attributes,
values spanning lines inside brackets), which fails loudly on anything else. The node pipeline's relabel rules are
replayed on sample series, so a keep or drop rule added later that loses the metric fails here, and so does any
component on the way to the exporter that this test does not know.
"""
import json
import re
import unittest

from test_tbd_manifests import ROOT, load_all

ALLOY_DIR = ROOT / "clusters/hetzner/observability/alloy"
CONFIG = ALLOY_DIR / "config.alloy"
RULES = ROOT / "grafana/node-reboot.rules.json"
RUNBOOKS = ROOT / "docs/runbooks.md"
CONFIG_MAP = ROOT / "docs/configuration-map.md"
EXPORTER = "otelcol.exporter.otlphttp.grafana_cloud"
METRIC = "node_boot_time_seconds"
DAYS = 35
# The stat collector's other series (node_exporter's collector/stat_linux.go): dropped, nothing reads them.
STAT_EXTRAS = ("node_context_switches_total", "node_forks_total", "node_intr_total", "node_procs_blocked",
               "node_procs_running")
# Series the other node rules and dashboards read: the drop must leave them alone.
NODE_KEPT = ("up", "node_vmstat_oom_kill", "node_cpu_seconds_total", "node_memory_MemAvailable_bytes",
             "node_pressure_memory_waiting_seconds_total")
ENV = {"NODE_NAME": "node-1", "HOST_IP": "10.0.0.2"}


# ---- config.alloy -------------------------------------------------------------------------------------------------

def _skip_string(text, i):
    """Index just past the string literal starting at text[i] ("..." with escapes, or `...` raw)."""
    if text[i] == "`":
        return text.index("`", i + 1) + 1
    j = i + 1
    while text[j] != '"':
        if text[j] == "\n":
            raise ValueError("config.alloy: newline inside a string")
        j += 2 if text[j] == "\\" else 1
    return j + 1


def _strip_comments(text):
    out, i = [], 0
    while i < len(text):
        if text[i] in '"`':
            j = _skip_string(text, i)
            out.append(text[i:j])
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _end_of(text, i, closer=None):
    """Index past the value that starts at i: up to the bracket that closes depth 0 (closer) or the end of the line."""
    depth = 0
    while i < len(text):
        c = text[i]
        if c in '"`':
            i = _skip_string(text, i)
            continue
        if c in "[{(":
            depth += 1
        elif c in "]})":
            if depth == 0:
                if c == closer:
                    return i
                raise ValueError(f"config.alloy: unbalanced {c!r}")
            depth -= 1
        elif c == "\n" and depth == 0 and closer is None:
            return i
        i += 1
    if closer or depth:
        raise ValueError("config.alloy: unclosed bracket")
    return i


HEAD = re.compile(r'\s*([A-Za-z_][\w.]*)(?:\s+"([^"]*)")?\s*\{')
ATTR = re.compile(r"\s*([A-Za-z_]\w*)\s*=\s*")


def parse_body(text):
    """{"attrs": {name: raw value}, "blocks": [(name, label, body)]} for one block body (comments already removed)."""
    attrs, blocks, i = {}, [], 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        m = ATTR.match(text, i)
        if m:
            end = _end_of(text, m.end())
            attrs[m[1]] = text[m.end():end].strip()
            i = end
            continue
        m = HEAD.match(text, i)
        if not m:
            raise ValueError(f"config.alloy: not a block or an attribute: {text[i:i + 60]!r}")
        end = _end_of(text, m.end(), closer="}")
        blocks.append((m[1], m[2], parse_body(text[m.end():end])))
        i = end + 1
    return {"attrs": attrs, "blocks": blocks}


def components():
    """{"<type>.<label>": body} for every labelled component of config.alloy."""
    out = {}
    for name, label, body in parse_body(_strip_comments(CONFIG.read_text()))["blocks"]:
        if label is not None:
            out[f"{name}.{label}"] = body
    return out


def value(raw):
    """A string literal, a list of them, or sys.env("X") (from ENV); anything else fails loudly."""
    raw = raw.strip()
    m = re.fullmatch(r'sys\.env\("(\w+)"\)', raw)
    if m:
        return ENV[m[1]]
    if raw.startswith("`") and raw.endswith("`"):
        return raw[1:-1]
    if raw.startswith('"'):
        return json.loads(raw)
    if raw.startswith("["):
        return json.loads(raw)
    raise ValueError(f"config.alloy: cannot evaluate {raw!r}")


def refs(raw, suffix):
    return re.findall(r"([A-Za-z_][\w.]*)\." + suffix + r"\b", raw)


def rules(body):
    return [r for name, _, r in body["blocks"] if name == "rule"]


def relabel(rule_bodies, labels):
    """Prometheus relabelling of one label set; None when a rule drops it."""
    labels = dict(labels)
    for r in rule_bodies:
        a = {k: value(v) for k, v in r["attrs"].items()}
        action = a.get("action", "replace")
        regex = re.compile(a.get("regex", "(.*)"))
        joined = a.get("separator", ";").join(labels.get(s, "") for s in a.get("source_labels", []))
        m = regex.fullmatch(joined)
        if action == "keep":
            if not m:
                return None
        elif action == "drop":
            if m:
                return None
        elif action == "replace":
            if m:
                new = re.sub(r"\$(?:\{(\w+)\}|(\w+))", lambda g: m.group(int(g[1] or g[2])) or "",
                             a.get("replacement", "$1"))
                if new:
                    labels[a["target_label"]] = new
                else:
                    labels.pop(a["target_label"], None)
        elif action == "labeldrop":
            labels = {k: v for k, v in labels.items() if not regex.fullmatch(k)}
        else:
            raise AssertionError(f"relabel action {action!r}: teach this test")
    return labels


class NodePipeline:
    """The node scrape: exporter target, its discovery.relabel, the scrape, then the forward_to chain."""

    def __init__(self):
        self.c = components()
        self.exporter = self.c['prometheus.exporter.unix.node']
        found = [cid for cid, b in self.c.items() if cid.startswith("discovery.relabel.")
                 and refs(b["attrs"].get("targets", ""), "targets") == ["prometheus.exporter.unix.node"]]
        assert len(found) == 1, f"want one discovery.relabel of the node exporter, found {found}"
        self.target_relabel = found[0]
        found = [cid for cid, b in self.c.items() if cid.startswith("prometheus.scrape.")
                 and refs(b["attrs"].get("targets", ""), "output") == [self.target_relabel]]
        assert len(found) == 1, f"want one scrape of {self.target_relabel}, found {found}"
        self.scrape = self.c[found[0]]

    def target_labels(self):
        # The exporter's target, as Alloy builds it; the scrape attaches every label not starting with __.
        target = {"__address__": "alloy-unix", "job": "integrations/unix", "instance": "alloy-abcde"}
        out = relabel(rules(self.c[self.target_relabel]), target)
        return {k: v for k, v in out.items() if not k.startswith("__")}

    def series(self, name):
        """The labels of series `name` when it reaches otelcol.receiver.prometheus, or None if a relabel drops it,
        and the receiver it reaches."""
        labels = {"__name__": name, **self.target_labels()}
        forward = refs(self.scrape["attrs"]["forward_to"], "receiver")
        while True:
            assert len(forward) == 1, f"node series fan out to {forward}: teach this test"
            cid = forward[0]
            if cid.startswith("otelcol.receiver.prometheus."):
                return labels, cid
            assert cid.startswith("prometheus.relabel."), f"{cid} on the node path: teach this test"
            labels = relabel(rules(self.c[cid]), labels)
            if labels is None:
                return None, cid
            forward = refs(self.c[cid]["attrs"]["forward_to"], "receiver")

    def otel_path(self, cid):
        """Components from cid to the exporter along metrics outputs; fails on a branch or an unknown component."""
        path = [cid]
        while path[-1] != EXPORTER:
            body = self.c[path[-1]]
            outputs = [b for name, _, b in body["blocks"] if name == "output"]
            assert len(outputs) == 1, f"{path[-1]}: want one output block"
            nxt = refs(outputs[0]["attrs"].get("metrics", ""), "input")
            assert len(nxt) == 1, f"{path[-1]} sends node metrics to {nxt}: teach this test"
            assert nxt[0] not in path, f"cycle at {nxt[0]}"
            path.append(nxt[0])
        return path


class ParserSelfTest(unittest.TestCase):
    def test_reads_the_node_exporter_and_the_cadvisor_relabel(self):
        c = components()
        self.assertIn("prometheus.exporter.unix.node", c)
        self.assertIn(EXPORTER, c)
        self.assertGreaterEqual(len(rules(c["prometheus.relabel.cadvisor"])), 5)

    def test_relabel_semantics(self):
        keep = {"attrs": {"source_labels": '["__name__"]', "regex": '"a|b"', "action": '"keep"'}, "blocks": []}
        drop = {"attrs": {"source_labels": '["__name__", "x"]', "regex": '"a;1"', "action": '"drop"'}, "blocks": []}
        repl = {"attrs": {"target_label": '"job"', "replacement": '"node"'}, "blocks": []}
        self.assertIsNone(relabel([keep], {"__name__": "c"}))
        self.assertIsNone(relabel([drop], {"__name__": "a", "x": "1"}))
        self.assertEqual(relabel([keep, drop], {"__name__": "a", "x": "2"}), {"__name__": "a", "x": "2"})
        self.assertEqual(relabel([repl], {"job": "j"}), {"job": "node"})

    def test_fails_loudly(self):
        with self.assertRaises(ValueError):
            parse_body('a = [1,\n')
        with self.assertRaises(ValueError):
            value("concat(1)")


class BootTimeReachesGrafanaCloud(unittest.TestCase):
    def setUp(self):
        self.p = NodePipeline()

    def test_stat_collector_enabled(self):
        self.assertIn("stat", value(self.p.exporter["attrs"]["set_collectors"]))

    def test_stat_reads_the_host_proc_already_mounted(self):
        self.assertEqual(value(self.p.exporter["attrs"]["procfs_path"]), "/host/proc")
        ds = [d for d in load_all(ALLOY_DIR / "alloy.yaml") if d.get("kind") == "DaemonSet"][0]
        spec = ds["spec"]["template"]["spec"]
        (mount,) = [m for m in spec["containers"][0]["volumeMounts"] if m["mountPath"] == "/host/proc"]
        self.assertIs(mount["readOnly"], True)
        (vol,) = [v for v in spec["volumes"] if v["name"] == mount["name"]]
        self.assertEqual(vol["hostPath"]["path"], "/proc")

    def test_boot_time_survives_the_relabel_path_as_job_node_per_node(self):
        labels, receiver = self.p.series(METRIC)
        self.assertEqual(labels, {"__name__": METRIC, "job": "node", "instance": ENV["NODE_NAME"]})
        path = self.p.otel_path(receiver)
        # Only the batch processor between the receiver and the exporter: nothing there filters or renames.
        self.assertEqual([cid.rsplit(".", 1)[0] for cid in path[1:-1]], ["otelcol.processor.batch"])

    def test_the_other_stat_series_are_dropped(self):
        for name in STAT_EXTRAS:
            self.assertIsNone(self.p.series(name)[0], name)

    def test_the_drop_leaves_the_other_node_series(self):
        for name in NODE_KEPT:
            self.assertIsNotNone(self.p.series(name)[0], name)


# ---- grafana/node-reboot.rules.json -------------------------------------------------------------------------------

def anchors(markdown):
    """GitHub's heading anchors for a Markdown file."""
    out = set()
    for line in markdown.splitlines():
        m = re.match(r"#{1,6}\s+(.*)", line)
        if m:
            out.add(re.sub(r"[^\w\- ]", "", m[1].strip().lower()).replace(" ", "-"))
    return out


class RebootRule(unittest.TestCase):
    def setUp(self):
        self.assertTrue(RULES.is_file(), f"{RULES.relative_to(ROOT)} is missing")
        self.group = json.loads(RULES.read_text())
        self.assertEqual(len(self.group["rules"]), 1)
        self.rule = self.group["rules"][0]
        self.data = {d["refId"]: d for d in self.rule["data"]}

    def test_group_follows_the_other_node_rules(self):
        r = self.rule
        self.assertEqual(self.group["title"], "node-reboot")
        self.assertEqual(self.group["interval"], 60)
        self.assertEqual((r["ruleGroup"], r["folderUID"], r["orgID"]), ("node-reboot", "platform", 1))
        self.assertEqual((r["uid"], r["title"]), ("node-reboot-overdue", "NodeRebootOverdue"))
        self.assertEqual(r["labels"], {"severity": "warning", "component": "node"})

    def test_queries_boot_time_per_node(self):
        a = self.data["A"]
        self.assertEqual(a["datasourceUid"], "grafanacloud-prom")
        self.assertIs(a["model"]["instant"], True)
        expr = a["model"]["expr"]
        self.assertIn(METRIC + '{job="node"}', expr)
        self.assertIn("by (instance)", expr)
        self.assertRegex(expr, r"^\(time\(\) - .*\) / 86400$", "A is the boot age in days")

    def test_threshold_is_35_days(self):
        b = self.data["B"]
        self.assertEqual(self.rule["condition"], "B")
        self.assertEqual((b["datasourceUid"], b["model"]["type"], b["model"]["expression"]),
                         ("__expr__", "threshold", "A"))
        (cond,) = b["model"]["conditions"]
        self.assertEqual(cond["evaluator"], {"type": "gt", "params": [DAYS]})

    def test_errors_and_gaps_do_not_page(self):
        # INFRA-201: a query error keeps the last state. A gap keeps it too: NodeMetricsAbsent reports a silent node.
        self.assertEqual(self.rule["execErrState"], "KeepLast")
        self.assertEqual(self.rule["noDataState"], "KeepLast")

    def test_mails_the_owner(self):
        contact = json.loads((ROOT / "grafana/contact-point.json").read_text())
        ns = self.rule["notification_settings"]
        self.assertEqual(ns["receiver"], contact["name"])
        self.assertEqual(ns["receiver"], "owner-email")
        self.assertEqual(ns["repeat_interval"], "24h")

    def test_summary_names_the_node_and_the_reboot_procedure(self):
        ann = self.rule["annotations"]
        self.assertIn("{{ $labels.instance }}", ann["summary"])
        self.assertIn("reboot.yml", ann["summary"])
        prefix = "https://github.com/fjcloudaiconsulting/cloud-infra/blob/main/docs/runbooks.md#"
        self.assertTrue(ann["runbook_url"].startswith(prefix), ann["runbook_url"])
        self.assertIn(ann["runbook_url"][len(prefix):], anchors(RUNBOOKS.read_text()))

    def test_runbook_and_configuration_map_name_the_file(self):
        apply = (f"api PUT /api/v1/provisioning/folder/{self.rule['folderUID']}/rule-groups/{self.group['title']} "
                 "grafana/node-reboot.rules.json")
        runbooks = RUNBOOKS.read_text()
        self.assertIn(apply, runbooks)
        self.assertIn("NodeRebootOverdue", runbooks)
        self.assertIn("grafana/node-reboot.rules.json", CONFIG_MAP.read_text())


if __name__ == "__main__":
    unittest.main()
