"""Fences on Alloy's OTLP receivers (EDGE-04): one receiver per source (production apps 4318, staging apps 4319, the
log-retention heartbeat 4320), each with a body cap. App data passes a memory limiter, a transform that sets
deployment.environment.name from the receiver, and a filter on the platform's own series names before export; the
NetworkPolicy admits each source to its own port only.

Stdlib only. config.alloy is read with the small parser below (blocks, `name = value` attributes, values spanning lines
inside brackets: what `alloy fmt` writes), which fails loudly on anything else; the manifests with the YAML subset
parser of test_tbd_manifests.
"""
import json
import pathlib
import re
import unittest
import urllib.parse

from test_tbd_manifests import load_all

ROOT = pathlib.Path(__file__).resolve().parent.parent
HETZNER = ROOT / "clusters/hetzner"
ALLOY_DIR = HETZNER / "observability/alloy"
CONFIG = ALLOY_DIR / "config.alloy"
HEARTBEAT_SCRIPT = HETZNER / "observability/log-retention/log-retention.sh"
EXPORTER = "otelcol.exporter.otlphttp.grafana_cloud"
PORTS = {"production": 4318, "staging": 4319, "heartbeat": 4320}
PROD_NAMESPACES = {"tbd-prod", "ziftbook-prod"}
STAGING_NAMESPACES = {"tbd-staging", "ziftbook-staging"}
LOG_RETENTION_PEER = {"podSelector": {"matchLabels": {"app": "log-retention"}}}
ENV_KEY = "deployment.environment.name"


# ---- config.alloy -------------------------------------------------------------------------------------------------

def _blank_strings(s):
    """s with every string literal's content blanked, so brackets and // inside strings do not count."""
    return re.sub(r'"(?:[^"\\\n]|\\.)*"|`[^`]*`', lambda m: m[0][0] + " " * (len(m[0]) - 2) + m[0][-1], s)


def _code(line):
    """The line without its // comment, stripped."""
    cut = _blank_strings(line).find("//")
    return (line[:cut] if cut >= 0 else line).strip()


def _depth(value):
    b = _blank_strings(value)
    return sum(b.count(c) for c in "[{(") - sum(b.count(c) for c in "]})")


def parse_alloy(text):
    root = {"name": "", "label": None, "attrs": {}, "blocks": []}
    stack, lines, i = [root], text.split("\n"), 0
    while i < len(lines):
        line = _code(lines[i])
        i += 1
        if not line:
            continue
        if line == "}":
            stack.pop()
            if not stack:
                raise ValueError(f"config.alloy:{i}: unbalanced }}")
            continue
        m = re.fullmatch(r'([A-Za-z_][\w.]*)(?:\s+"([^"]*)")?\s*\{', line)
        if m:
            blk = {"name": m[1], "label": m[2], "attrs": {}, "blocks": []}
            stack[-1]["blocks"].append(blk)
            stack.append(blk)
            continue
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*(.+)", line)
        if not m:
            raise ValueError(f"config.alloy:{i}: not a block or an attribute: {line!r}")
        value = m[2]
        while _depth(value) > 0:
            if i >= len(lines):
                raise ValueError(f"config.alloy: unterminated value of {m[1]}")
            value += "\n" + _code(lines[i])
            i += 1
        stack[-1]["attrs"][m[1]] = value
    if len(stack) != 1:
        raise ValueError("config.alloy: unclosed block")
    return root


def strings(value):
    """The string literals in a value, in order: "..." unescaped, `...` raw."""
    out = []
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|`([^`]*)`', value):
        out.append(m[2] if m[2] is not None else json.loads(f'"{m[1]}"'))
    return out


def sub(block, name):
    return [b for b in block["blocks"] if b["name"] == name]


class Pipeline:
    def __init__(self, text):
        self.components = {f"{b['name']}.{b['label']}": b for b in parse_alloy(text)["blocks"] if b["label"]}

    def next(self, cid, signal):
        out = sub(self.components[cid], "output")
        refs = re.findall(r"([A-Za-z_][\w.]*)\.input\b", out[0]["attrs"].get(signal, "")) if out else []
        for r in refs:
            if r not in self.components:
                raise AssertionError(f"{cid} {signal} -> {r}: no such component")
        return refs

    def paths(self, cid, signal, seen=()):
        """Every path from cid along `signal` outputs, as lists of component ids (cid first)."""
        nxt = self.next(cid, signal)
        if not nxt:
            return [[cid]]
        if cid in seen:
            raise AssertionError(f"cycle at {cid}")
        return [[cid] + p for n in nxt for p in self.paths(n, signal, seen + (cid,))]

    def receiver(self, port):
        found = [cid for cid, b in self.components.items() if b["name"] == "otelcol.receiver.otlp"
                 and any(strings(h["attrs"].get("endpoint", "")) == [f"0.0.0.0:{port}"] for h in sub(b, "http"))]
        if len(found) != 1:
            raise AssertionError(f"want one OTLP receiver on {port}, found {found}")
        return found[0]

    def statements(self, cid, kind):
        """(context, statement) for every `<kind>_statements` block of a transform."""
        return [(strings(s["attrs"]["context"])[0], st) for s in sub(self.components[cid], f"{kind}_statements")
                for st in strings(s["attrs"]["statements"])]


def size(value):
    """'4MiB', '64KiB', '256Mi', '200MiB' -> bytes."""
    m = re.fullmatch(r"(\d+)(Ki|Mi|Gi)B?", value.strip().strip('"'))
    if not m:
        raise ValueError(f"unsupported size {value!r}")
    return int(m[1]) * {"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30}[m[2]]


def ottl_filter(conditions):
    """A name predicate for filter `metric` conditions of the forms IsMatch(name, "re") and not IsMatch(name, "re"),
    which are all this config uses (the filter drops a metric when any condition is true)."""
    preds = []
    for c in conditions:
        m = re.fullmatch(r'(not\s+)?IsMatch\(name,\s*"((?:[^"\\]|\\.)*)"\)', c.strip())
        n = re.fullmatch(r'name\s*!=\s*"((?:[^"\\]|\\.)*)"', c.strip())
        if m:
            rx, neg = re.compile(json.loads(f'"{m[2]}"')), bool(m[1])
            preds.append(lambda s, rx=rx, neg=neg: (rx.search(s) is None) if neg else (rx.search(s) is not None))
        elif n:
            preds.append(lambda s, want=json.loads(f'"{n[1]}"'): s != want)
        else:
            raise AssertionError(f"unsupported filter condition, extend the test: {c}")
    return lambda name: any(p(name) for p in preds)


# ---- what the platform alerts read --------------------------------------------------------------------------------

PROMQL_WORDS = {"by", "without", "on", "ignoring", "group_left", "group_right", "offset", "and", "or", "unless", "bool"}


def selectors(expr):
    """(metric name, label matchers) for every vector selector of a PromQL expression."""
    expr = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", expr)
    expr = re.sub(r"\[[^\]]*\]", " ", expr)
    out = []
    for m in re.finditer(r'\{[^}]*\}|"(?:[^"\\]|\\.)*"|(?<![\w$.])([a-zA-Z_:][\w:]*)(\s*\{[^}]*\})?(\s*\()?', expr):
        if m[1] and not m[3] and m[1] not in PROMQL_WORDS:
            out.append((m[1], m[2] or ""))
    return out


def grafana_selectors():
    def exprs(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "expr" and isinstance(v, str):
                    yield v
                else:
                    yield from exprs(v)
        elif isinstance(node, list):
            for v in node:
                yield from exprs(v)

    files = sorted((ROOT / "grafana").glob("*.rules.json")) + sorted((ROOT / "grafana").glob("*.dashboard.json"))
    return [s for f in files for e in exprs(json.loads(f.read_text())) for s in selectors(e)]


def expand(alternative):
    """One concrete name for a regex alternative such as container_fs_(usage|limit)_bytes."""
    return re.sub(r"\(([^()|]*)(?:\|[^()]*)?\)\??", r"\1", alternative)


def top_level_alternatives(rx):
    parts, d, cur = [], 0, ""
    for c in rx:
        d += c == "("
        d -= c == ")"
        if c == "|" and d == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    return parts + [cur]


def platform_series(pipeline):
    """Names of the series the collector itself produces or the heartbeat sends, and those the platform's rules and
    dashboards read without an environment label (app series are always selected by deployment_environment_name)."""
    names = {"up", "scrape_duration_seconds", "scrape_samples_scraped", "scrape_samples_post_metric_relabeling",
             "scrape_series_added", "node_cpu_seconds_total", "node_memory_MemAvailable_bytes", "node_load1"}
    keep = [r for r in pipeline.components["prometheus.relabel.cadvisor"]["blocks"]
            if r["name"] == "rule" and strings(r["attrs"].get("action", "")) == ["keep"]][0]
    rx = strings(keep["attrs"]["regex"])[0]
    for alt in top_level_alternatives(rx):
        name = expand(alt)
        assert re.fullmatch(rx, name), (alt, name)
        names.add(name)
    names |= set(re.findall(r'\\"name\\":\\"([a-z_]+)\\"', HEARTBEAT_SCRIPT.read_text()))
    names |= {n for n, sel in grafana_selectors() if "deployment_environment_name" not in sel}
    return names


APP_SERIES = {"http.server.request.duration", "http.server.active_requests", "tbd.security.csp_reports",
              "tbd.process.up", "ziftbook.job.runs", "ziftbook.job.queue.oldest_age", "ziftbook.payment.outcomes",
              "uptime", "nodejs.eventloop.delay", "containers.created", "upload.bytes"}


def variants(name):
    """Spellings an app could send that Grafana Cloud would store under `name` (or that look like it)."""
    return {name, name.replace("_", "."), name.replace("_", "-"), name.upper(), "_" + name, "." + name}


# ---- manifests ----------------------------------------------------------------------------------------------------

def alloy_docs():
    return {d["kind"]: d for d in load_all(ALLOY_DIR / "alloy.yaml")}


def alloy_container(docs):
    return [c for c in docs["DaemonSet"]["spec"]["template"]["spec"]["containers"] if c["name"] == "alloy"][0]


def env(container, name):
    return [e["value"] for e in container.get("env") or [] if e["name"] == name][0]


def otel_endpoint(namespace):
    docs = [d for d in load_all(HETZNER / namespace / "otel.yaml") if d.get("kind") == "ConfigMap"]
    return urllib.parse.urlsplit(docs[0]["data"]["OTEL_EXPORTER_OTLP_ENDPOINT"])


# ---- tests --------------------------------------------------------------------------------------------------------

class ParserSelfTest(unittest.TestCase):
    def test_subset(self):
        root = parse_alloy('a "x" {\n\tb = "v" // c\n\tc = [\n\t\t`s("//")`,\n\t]\n\n\td {\n\t\te = 1\n\t}\n}\n')
        a = root["blocks"][0]
        self.assertEqual((a["name"], a["label"], strings(a["attrs"]["b"])), ("a", "x", ["v"]))
        self.assertEqual(strings(a["attrs"]["c"]), ['s("//")'])
        self.assertEqual(sub(a, "d")[0]["attrs"], {"e": "1"})
        with self.assertRaises(ValueError):
            parse_alloy("a {\n\tb\n}\n")
        self.assertEqual(selectors('max by (instance) (up{job="node"}[5m] offset 1h) * 1e9 or time() - x'),
                         [("up", '{job="node"}'), ("x", "")])

    def test_config_parses(self):
        self.assertIn(EXPORTER, Pipeline(CONFIG.read_text()).components)


class AppReceivers(unittest.TestCase):
    def setUp(self):
        self.p = Pipeline(CONFIG.read_text())

    def app_paths(self, env_name):
        rid = self.p.receiver(PORTS[env_name])
        return rid, {s: self.p.paths(rid, s) for s in ("metrics", "traces")}

    def test_each_app_receiver_caps_the_body(self):
        for env_name in ("production", "staging"):
            rid = self.p.receiver(PORTS[env_name])
            http = sub(self.p.components[rid], "http")[0]
            self.assertIn("max_request_body_size", http["attrs"], f"{rid}: the default is 20MiB")
            cap = size(strings(http["attrs"]["max_request_body_size"])[0])
            self.assertTrue(0 < cap <= 4 << 20, f"{rid}: {cap} bytes; raise only with the apps' batch sizes in hand")

    def test_app_data_passes_the_memory_limiter_the_filter_and_the_scrub_before_export(self):
        for env_name in ("production", "staging"):
            rid, paths = self.app_paths(env_name)
            self.assertEqual(sub(self.p.components[rid], "output")[0]["attrs"].keys(), {"metrics", "traces"}, rid)
            for signal, ps in paths.items():
                for path in ps:
                    where = f"{env_name} {signal}: {' -> '.join(path)}"
                    self.assertEqual(path[-1], EXPORTER, where)
                    self.assertTrue(path[1].startswith("otelcol.processor.memory_limiter."), f"{where}: limiter first")
                    self.assertTrue(any(c.startswith("otelcol.processor.filter.") for c in path), where)
                    self.assertIn("otelcol.processor.transform.scrub", path, where)

    def test_memory_limiters_sit_under_gomemlimit_and_the_container_limit(self):
        docs = alloy_docs()
        c = alloy_container(docs)
        gomem, ceiling = size(env(c, "GOMEMLIMIT")), size(c["resources"]["limits"]["memory"])
        for env_name in ("production", "staging"):
            _, paths = self.app_paths(env_name)
            for path in paths["metrics"] + paths["traces"]:
                self.assertTrue(path[1].startswith("otelcol.processor.memory_limiter."), f"{env_name}: {path}")
                lim = self.p.components[path[1]]["attrs"]
                limit, spike = size(strings(lim["limit"])[0]), size(strings(lim["spike_limit"])[0])
                self.assertTrue(0 < spike < limit < gomem <= ceiling, f"{path[1]}: {spike} {limit} {gomem} {ceiling}")
                self.assertEqual(strings(lim["check_interval"]), ["1s"], path[1])

    def test_each_receiver_forces_its_own_environment(self):
        for env_name in ("production", "staging"):
            _, paths = self.app_paths(env_name)
            for kind, signal, inner in (("metric", "metrics", "datapoint"), ("trace", "traces", "span")):
                for path in paths[signal]:
                    st = [s for cid in path if cid.startswith("otelcol.processor.transform.")
                          for s in self.p.statements(cid, kind)]
                    sets = [(ctx, s) for ctx, s in st if ENV_KEY in s and s.startswith("set(")]
                    self.assertEqual(sets, [("resource", f'set(attributes["{ENV_KEY}"], "{env_name}")')],
                                     f"{env_name} {signal}: {path}")
                    for ctx in ("resource", inner):
                        rxs = [re.compile(strings(s)[0]) for c, s in st if c == ctx and s.startswith("delete_matching_keys(")]
                        for spelling in (ENV_KEY, "deployment_environment_name", "deployment-environment-name"):
                            self.assertTrue(any(r.search(spelling) for r in rxs),
                                            f"{env_name} {signal}: {ctx} {spelling} survives")
                    # The delete runs before the set on the resource, or it would delete the forced value.
                    res = [s for c, s in st if c == "resource" and (ENV_KEY in s or "environment" in s)]
                    self.assertTrue(res and res[-1].startswith("set("), f"{env_name} {signal}: {res}")

    def test_filter_drops_platform_series_names_and_keeps_app_ones(self):
        names = platform_series(self.p)
        for must in ("up", "machine_memory_bytes", "probe_success", "log_retention_last_success_timestamp_seconds",
                     "container_memory_working_set_bytes", "node_vmstat_oom_kill"):
            self.assertIn(must, names, "the derivation from config.alloy and grafana/ lost a name")
        for env_name in ("production", "staging"):
            _, paths = self.app_paths(env_name)
            for path in paths["metrics"]:
                conds = [c for cid in path if cid.startswith("otelcol.processor.filter.")
                         for m in sub(self.p.components[cid], "metrics") for c in strings(m["attrs"].get("metric", ""))]
                dropped = ottl_filter(conds)
                for n in sorted(names):
                    for v in sorted(variants(n)):
                        self.assertTrue(dropped(v), f"{env_name}: {v!r} reaches export")
                for v in ("", ".", "_", "--"):
                    self.assertTrue(dropped(v), f"{env_name}: a name without letters ({v!r}) reaches export")
                for n in sorted(APP_SERIES | {s for s, sel in grafana_selectors() if "deployment_environment_name" in sel}):
                    self.assertFalse(dropped(n), f"{env_name}: app series {n} dropped")


class HeartbeatReceiver(unittest.TestCase):
    def test_heartbeat_url_points_at_its_own_receiver(self):
        docs = [d for d in load_all(HETZNER / "observability/log-retention/daemonset.yaml") if d["kind"] == "DaemonSet"]
        c = docs[0]["spec"]["template"]["spec"]["containers"][0]
        url = urllib.parse.urlsplit(env(c, "HEARTBEAT_URL"))
        self.assertEqual((url.hostname, url.port, url.path), ("alloy.observability.svc", PORTS["heartbeat"], "/v1/metrics"))

    def test_heartbeat_receiver_takes_the_heartbeat_metric_only(self):
        p = Pipeline(CONFIG.read_text())
        rid = p.receiver(PORTS["heartbeat"])
        http = sub(p.components[rid], "http")[0]
        self.assertLessEqual(size(strings(http["attrs"]["max_request_body_size"])[0]), 64 << 10, rid)
        self.assertEqual(sub(p.components[rid], "output")[0]["attrs"].keys(), {"metrics"}, f"{rid}: metrics only")
        beat = re.findall(r'\\"name\\":\\"([a-z_]+)\\"', HEARTBEAT_SCRIPT.read_text())
        self.assertEqual(beat, ["log_retention_last_success_timestamp_seconds"])
        for path in p.paths(rid, "metrics"):
            self.assertEqual(path[-1], EXPORTER, path)
            conds = [c for cid in path if cid.startswith("otelcol.processor.filter.")
                     for m in sub(p.components[cid], "metrics") for c in strings(m["attrs"].get("metric", ""))]
            dropped = ottl_filter(conds)
            self.assertFalse(dropped(beat[0]), path)
            for other in ("up", "http.server.request.duration", "log_retention_x"):
                self.assertTrue(dropped(other), f"{other} passes {path}")


class Network(unittest.TestCase):
    def setUp(self):
        self.docs = alloy_docs()
        self.rules = self.docs["NetworkPolicy"]["spec"]["ingress"]

    @staticmethod
    def namespaces(rule):
        return {((p.get("namespaceSelector") or {}).get("matchLabels") or {}).get("kubernetes.io/metadata.name")
                for p in rule.get("from") or []}

    def test_policy_covers_the_alloy_pods_and_every_rule_names_its_ports(self):
        self.assertIn(self.docs["NetworkPolicy"]["spec"]["podSelector"], ({}, {"matchLabels": {"app": "alloy"}}))
        for r in self.rules:
            self.assertTrue(r.get("ports"), f"a rule without ports admits every port: {r}")
            self.assertTrue(r.get("from"), f"a rule without peers admits everyone: {r}")
            for peer in r["from"]:
                self.assertNotIn("ipBlock", peer, r)

    def test_production_port_admits_the_production_namespaces_only(self):
        rules = [r for r in self.rules if any(p["port"] == PORTS["production"] for p in r["ports"])]
        self.assertTrue(rules)
        for r in rules:
            self.assertEqual(self.namespaces(r), PROD_NAMESPACES, r)
            for peer in r["from"]:
                self.assertEqual(peer.keys(), {"namespaceSelector"}, r)
            self.assertFalse(any(ns and ns.endswith("-staging") for ns in self.namespaces(r)), r)

    def test_staging_port_admits_the_staging_namespaces_only(self):
        rules = [r for r in self.rules if any(p["port"] == PORTS["staging"] for p in r["ports"])]
        self.assertTrue(rules)
        for r in rules:
            self.assertEqual(self.namespaces(r), STAGING_NAMESPACES, r)
            self.assertEqual([p["port"] for p in r["ports"]], [PORTS["staging"]], r)
        for r in self.rules:
            if self.namespaces(r) & STAGING_NAMESPACES:
                self.assertEqual([p["port"] for p in r["ports"]], [PORTS["staging"]], f"staging reaches more: {r}")

    def test_heartbeat_port_admits_log_retention_only(self):
        rules = [r for r in self.rules if any(p["port"] == PORTS["heartbeat"] for p in r["ports"])]
        self.assertTrue(rules)
        for r in rules:
            self.assertEqual(r["from"], [LOG_RETENTION_PEER], r)
            self.assertEqual([p["port"] for p in r["ports"]], [PORTS["heartbeat"]], r)

    def test_ports_agree_across_config_daemonset_service_and_policy(self):
        p = Pipeline(CONFIG.read_text())
        receivers = {int(strings(h["attrs"]["endpoint"])[0].rsplit(":", 1)[1])
                     for b in p.components.values() if b["name"] == "otelcol.receiver.otlp" for h in sub(b, "http")}
        self.assertEqual(receivers, set(PORTS.values()))
        container = {cp["name"]: cp["containerPort"] for cp in alloy_container(self.docs)["ports"]}
        service = {sp["port"]: container[sp["targetPort"]] for sp in self.docs["Service"]["spec"]["ports"]}
        self.assertEqual(set(service), receivers)
        self.assertTrue(all(k == v for k, v in service.items()), service)
        self.assertEqual({pp["port"] for r in self.rules for pp in r["ports"]}, receivers)

    def test_apps_send_to_their_environment_port(self):
        for ns in sorted(PROD_NAMESPACES | STAGING_NAMESPACES):
            url = otel_endpoint(ns)
            want = PORTS["staging"] if ns in STAGING_NAMESPACES else PORTS["production"]
            self.assertEqual((url.scheme, url.hostname, url.port), ("http", "alloy.observability.svc", want), ns)


if __name__ == "__main__":
    unittest.main()
