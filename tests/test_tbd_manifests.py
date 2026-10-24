"""Fences on the TBD workloads in clusters/ (tbd-prod, tbd-staging).

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`. The manifests are read with the small
YAML subset parser below (block mappings and sequences, plain and quoted scalars, block scalars, `[a, b]` and `{}`),
which fails loudly on anything else rather than guessing.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLUSTERS = ROOT / "clusters"
TBD_NAMESPACES = ("tbd-prod", "tbd-staging")
TBD_IMAGE = re.compile(r"^ghcr\.io/fjcloudaiconsulting/tbd/([a-z0-9-]+)[:@]")
# k3s's default cluster CIDR: ansible/group_vars/all.yml does not set cluster-cidr (a fence below keeps it so).
POD_CIDR = "10.42.0.0/16"
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob")


# ---- YAML subset --------------------------------------------------------------------------------------------------

class _Block:
    def __init__(self, style):
        self.style = style


def _split_flow(s):
    parts, cur, q = [], "", None
    for c in s:
        if q:
            cur += c
            if c == q:
                q = None
        elif c in "'\"":
            q = c
            cur += c
        elif c == ",":
            parts.append(cur)
            cur = ""
        elif c in "[]{}":
            raise ValueError(f"nested flow collections are not supported: [{s}]")
        else:
            cur += c
    parts.append(cur)
    return [p for p in (x.strip() for x in parts) if p]


def _value(raw):
    """(value, nested): nested is True when the value is the block on the following lines."""
    s = raw.strip()
    if not s or s.startswith("#"):
        return None, True
    if s[0] in "\"'":
        q, i, out = s[0], 1, []
        while i < len(s):
            c = s[i]
            if q == '"' and c == "\\" and i + 1 < len(s):
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\", "/": "/"}.get(s[i + 1], "\\" + s[i + 1]))
                i += 2
                continue
            if c == q:
                if q == "'" and s[i + 1:i + 2] == "'":
                    out.append("'")
                    i += 2
                    continue
                rest = s[i + 1:].strip()
                if rest and not rest.startswith("#"):
                    raise ValueError(f"text after a quoted scalar: {raw!r}")
                return "".join(out), False
            out.append(c)
            i += 1
        raise ValueError(f"unterminated quoted scalar: {raw!r}")
    s = re.split(r"\s#", s, maxsplit=1)[0].strip()
    if s.startswith("["):
        if not s.endswith("]"):
            raise ValueError(f"multi-line flow sequences are not supported: {raw!r}")
        return [_value(p)[0] for p in _split_flow(s[1:-1])], False
    if s.startswith("{"):
        if s.replace(" ", "") != "{}":
            raise ValueError(f"flow mappings other than {{}} are not supported: {raw!r}")
        return {}, False
    if s[0] in "|>":
        return _Block(s), False
    if s[0] in "&*!":
        raise ValueError(f"anchors, aliases and tags are not supported: {raw!r}")
    if s in ("true", "false"):
        return s == "true", False
    if s in ("null", "~"):
        return None, False
    if re.fullmatch(r"-?\d+", s):
        return int(s), False
    return s, False


def _key(content):
    """(key, rest) for a `key: value` line, or None."""
    m = re.match(r'^("(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\')\s*:(?:\s(.*))?$', content)
    if m:
        return _value(m.group(1))[0], m.group(2) or ""
    m = re.match(r"^([^\s\"'#\[\]{}|>&*!-][^#]*?|-[^\s#][^#]*?)\s*:(?:\s(.*))?$", content)
    if m and ": " not in m.group(1):
        return m.group(1), m.group(2) or ""
    return None


def _is_item(content):
    return content == "-" or content.startswith("- ")


class _Parser:
    def __init__(self, lines, where):
        self.lines, self.i, self.where = lines, 0, where

    def fail(self, msg):
        raise ValueError(f"{self.where}:{self.i + 1}: {msg}")

    def peek(self):
        while self.i < len(self.lines):
            line = self.lines[self.i]
            body = line.lstrip(" ")
            if body.strip() and not body.startswith("#"):
                if body.startswith("\t"):
                    self.fail("tab indentation")
                return len(line) - len(body), body.rstrip()
            self.i += 1
        return None

    def block(self, indent):
        t = self.peek()
        if t is None or t[0] < indent:
            return None
        return self.seq(t[0]) if _is_item(t[1]) else self.map(t[0])

    def seq(self, ind):
        out = []
        while True:
            t = self.peek()
            if t is None or t[0] < ind:
                return out
            if t[0] > ind or not _is_item(t[1]):
                if t[0] == ind:
                    return out
                self.fail("bad indentation in a sequence")
            rest = t[1][1:].lstrip(" ")
            col = ind + len(t[1]) - len(rest)
            if not rest or rest.startswith("#"):
                self.i += 1
                out.append(self.block(ind + 1))
            elif _is_item(rest) or _key(rest):
                self.lines[self.i] = " " * col + rest
                out.append(self.block(col))
            else:
                v, _ = _value(rest)
                self.i += 1
                out.append(self.block_scalar(ind) if isinstance(v, _Block) else v)

    def map(self, ind):
        out = {}
        while True:
            t = self.peek()
            if t is None or t[0] < ind or (t[0] == ind and _is_item(t[1])):
                return out
            if t[0] > ind:
                self.fail("bad indentation in a mapping")
            kv = _key(t[1])
            if kv is None:
                self.fail(f"not a key: {t[1]!r}")
            key, rest = kv
            if key in out:
                self.fail(f"duplicate key {key!r}")
            self.i += 1
            v, nested = _value(rest)
            if nested:
                t2 = self.peek()
                if t2 is not None and (t2[0] > ind or (t2[0] == ind and _is_item(t2[1]))):
                    v = self.block(t2[0])
            elif isinstance(v, _Block):
                v = self.block_scalar(ind)
            elif isinstance(v, str) and rest.strip()[0] not in "\"'[":
                v = self.plain_continuation(ind, v)
            out[key] = v

    def plain_continuation(self, ind, v):
        # A plain scalar continues on more-indented lines (folded with a space), as upstream CRDs write descriptions.
        while True:
            t = self.peek()
            if t is None or t[0] <= ind:
                return v
            v = f"{v} {re.split(r'\s#', t[1], maxsplit=1)[0].strip()}"
            self.i += 1

    def block_scalar(self, ind):
        body = []
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= ind:
                break
            body.append(line)
            self.i += 1
        while body and not body[-1].strip():
            body.pop()
        cut = min((len(x) - len(x.lstrip(" ")) for x in body if x.strip()), default=0)
        return "\n".join(x[cut:] for x in body) + "\n"


def load_all(path):
    docs, cur = [], []
    for line in path.read_text().split("\n") + ["---"]:
        if line.rstrip() == "---":
            p = _Parser(cur, path)
            doc = p.block(0)
            if p.peek() is not None:
                p.fail("unparsed content")
            if doc is not None:
                docs.append(doc)
            cur = []
        else:
            cur.append(line)
    return docs


# ---- helpers ------------------------------------------------------------------------------------------------------

def hand_written():
    """Every YAML file under clusters/ except SOPS Secrets, Flux's generated components and CRD-only files."""
    for f in sorted(CLUSTERS.rglob("*.yaml")):
        if f.name.endswith(".secret.yaml") or f.name == "gotk-components.yaml":
            continue
        if set(re.findall(r"(?m)^kind: (\S+)", f.read_text())) == {"CustomResourceDefinition"}:
            continue
        yield f


def manifests(namespace):
    """Every non-Secret document in clusters/*/<namespace>/, as (file, doc)."""
    out = []
    for f in sorted(CLUSTERS.glob(f"*/{namespace}/*.yaml")):
        if f.name.endswith(".secret.yaml"):
            continue
        out.extend((f, d) for d in load_all(f))
    return out


def pod_spec(doc):
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return ((spec.get("template") or {}).get("spec")) or {}


def workloads(namespace):
    return [(f, d) for f, d in manifests(namespace) if d.get("kind") in WORKLOAD_KINDS]


def containers(doc, init=True):
    spec = pod_spec(doc)
    return (spec.get("initContainers") or [] if init else []) + (spec.get("containers") or [])


def tbd_image(container):
    m = TBD_IMAGE.match(container.get("image") or "")
    return m.group(1) if m else None


def env_value(container, name):
    """The literal value of env `name`, KeyError when unset."""
    for e in container.get("env") or []:
        if e.get("name") == name:
            return e.get("value")
    raise KeyError(name)


def where(f, doc, c):
    return f"{f.relative_to(ROOT)} {doc['kind']}/{doc['metadata']['name']} container {c['name']}"


# ---- tests --------------------------------------------------------------------------------------------------------

class ParserSelfTest(unittest.TestCase):
    def test_subset(self):
        import tempfile
        text = (
            "# c\na: 1\nb: \"x # y\"  # z\nc: [p, 'q, r']\nd: {}\ne:\n  - name: n\n    value: v\n  - plain\n"
            "f: |\n  line # kept\n\n  two\ng:\n- s\n---\nh: http://x:1/y\n"
        )
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "x.yaml")
            p.write_text(text)
            docs = load_all(p)
        self.assertEqual(docs, [
            {"a": 1, "b": "x # y", "c": ["p", "q, r"], "d": {}, "e": [{"name": "n", "value": "v"}, "plain"],
             "f": "line # kept\n\ntwo\n", "g": ["s"]},
            {"h": "http://x:1/y"},
        ])

    def test_every_cluster_manifest_parses(self):
        # The fences below are only as good as the parser: it must read every hand-written manifest.
        n = 0
        for f in hand_written():
            with self.subTest(f=str(f.relative_to(ROOT))):
                n += len(load_all(f))
        self.assertGreater(n, 50)


class OriginOnlyThroughTheTunnel(unittest.TestCase):
    """INF-01: the tunnel is the only way in, only cloudflared reaches the backend and MCP pods, and their uvicorn
    trusts forwarded headers from the pod network only."""

    def test_no_listener_outside_the_tunnel(self):
        for f in hand_written():
            for d in load_all(f):
                kind = d.get("kind")
                name = f"{f.relative_to(ROOT)} {kind}/{(d.get('metadata') or {}).get('name')}"
                self.assertNotIn(kind, ("Ingress", "IngressRoute", "IngressRouteTCP", "Gateway", "HTTPRoute"), name)
                if kind == "Service":
                    self.assertIn((d.get("spec") or {}).get("type", "ClusterIP"), ("ClusterIP",), name)
                if kind in WORKLOAD_KINDS:
                    spec = pod_spec(d)
                    if spec.get("hostNetwork"):
                        self.assertEqual(d["metadata"].get("namespace"), "netbird", name)
                    for c in containers(d):
                        for p in c.get("ports") or []:
                            self.assertNotIn("hostPort", p, name)

    def test_k3s_runs_without_traefik_servicelb_or_a_custom_pod_cidr(self):
        cfg = (ROOT / "ansible/group_vars/all.yml").read_text()
        self.assertRegex(cfg, r'(?m)^  disable: \["traefik", "servicelb"\]$')
        self.assertNotIn("cluster-cidr", cfg, f"POD_CIDR ({POD_CIDR}) assumes the k3s default; update it and the args")

    def test_every_tbd_namespace_denies_ingress_by_default(self):
        for ns in TBD_NAMESPACES:
            pols = [d for _, d in manifests(ns) if d.get("kind") == "NetworkPolicy"]
            deny = [p for p in pols if p["spec"].get("podSelector") == {} and "Ingress" in p["spec"]["policyTypes"]
                    and not p["spec"].get("ingress")]
            self.assertTrue(deny, f"{ns}: no default-deny ingress policy")

    def test_only_cloudflared_reaches_backend_and_mcp(self):
        tunnel = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "cloudflared"}},
                  "podSelector": {"matchLabels": {"app": "cloudflared"}}}
        want = {"backend": 8000, "mcp": 8001}
        for ns in TBD_NAMESPACES:
            apps = {(d["spec"]["template"]["metadata"]["labels"] or {}).get("app")
                    for _, d in workloads(ns) if d.get("kind") == "Deployment"}
            for _, p in manifests(ns):
                if p.get("kind") != "NetworkPolicy":
                    continue
                sel = (p["spec"].get("podSelector") or {}).get("matchLabels") or {}
                rules = p["spec"].get("ingress") or []
                if sel.get("app") in want:
                    for r in rules:
                        self.assertEqual(r.get("from"), [tunnel], f"{ns} {p['metadata']['name']}")
                        self.assertEqual(r.get("ports"), [{"port": want[sel["app"]]}], f"{ns} {p['metadata']['name']}")
                elif not sel:
                    self.assertEqual(rules, [], f"{ns} {p['metadata']['name']}: ingress rule for every pod")
            for app in want:
                if app in apps:
                    self.assertTrue(any(((p["spec"].get("podSelector") or {}).get("matchLabels") or {}).get("app") == app
                                        for _, p in manifests(ns) if p.get("kind") == "NetworkPolicy"),
                                    f"{ns}: {app} has no NetworkPolicy of its own")

    def test_serving_tbd_api_trusts_forwarded_headers_from_the_pod_network_only(self):
        modules = {"backend": ("app.main:app", "8000"), "mcp": ("app.mcp_main:app", "8001")}
        seen = set()
        for ns in TBD_NAMESPACES:
            services = {tuple(sorted((d["spec"].get("selector") or {}).items()))
                        for _, d in manifests(ns) if d.get("kind") == "Service"}
            for f, d in workloads(ns):
                labels = tuple(sorted((d["spec"]["template"]["metadata"].get("labels") or {}).items()))
                if labels not in services:
                    continue  # nothing routes to it (the scheduler)
                for c in containers(d, init=False):
                    img = tbd_image(c)
                    if img not in modules:
                        continue
                    seen.add((ns, img))
                    module, port = modules[img]
                    # args replaces the image CMD (tbd backend/Dockerfile, Dockerfile.mcp) with the trust list narrowed.
                    self.assertEqual(c.get("args"), [
                        "uvicorn", module, "--host", "0.0.0.0", "--port", port, "--ws", "none",
                        "--proxy-headers", "--forwarded-allow-ips", POD_CIDR,
                    ], where(f, d, c))
                    self.assertNotIn("command", c, where(f, d, c))
        self.assertEqual(seen, {("tbd-prod", "backend"), ("tbd-staging", "backend"), ("tbd-staging", "mcp")})

    def test_no_tbd_container_sets_the_legacy_runtime_switch(self):
        for ns in TBD_NAMESPACES:
            for f, d in workloads(ns):
                for c in containers(d):
                    with self.assertRaises(KeyError, msg=where(f, d, c)):
                        env_value(c, "PFV_RUNTIME")


if __name__ == "__main__":
    unittest.main()
