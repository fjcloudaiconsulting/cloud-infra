"""Fences on the NetworkPolicies between namespaces: which pods reach the database servers in `data` (K8S-04), what
the app namespaces may reach outside the pod network (K8S-07), and which pods and ports cloudflared reaches (EDGE-07).

Stdlib only: `python3 -m unittest discover -s tests`. Manifests are read with test_tbd_manifests' parser, Terraform
with test_tbd_edge's helpers. The sets the policies must admit are derived from the workload manifests and the
Terraform, so a new database client, tunnel route or renumbered network fails here until its policy follows.
"""
import ipaddress
import re
import unittest

from test_tbd_edge import TF, resource_body, tunnel_rules
from test_tbd_manifests import POD_CIDR, containers, manifests, workloads

APP_NAMESPACES = ("tbd-prod", "tbd-staging", "ziftbook-prod", "ziftbook-staging")
IMDS = ipaddress.ip_network("169.254.169.254/32")
NS_LABEL = "kubernetes.io/metadata.name"
# The server each app's namespaces use. The URLs sit in SOPS Secrets, so the engine is not read from them.
SERVER = {"tbd": "mysql", "ziftbook": "postgres"}
# Env names and Secret keys that carry a database URL: DATABASE_URL, ZIF_DATABASE_URL, ZIF_MIGRATE_DATABASE_URL,
# database-url, migrate-database-url.
DB_URL = re.compile(r"database[-_]url", re.I)
# The only form a tunnel origin takes in terraform/cloudflare/tunnel.tf.
TUNNEL_SERVICE = re.compile(r"^http://([a-z0-9-]+)\.([a-z0-9-]+)\.svc\.cluster\.local:(\d+)$")


# ---- helpers ------------------------------------------------------------------------------------------------------

def policies(namespace):
    return {d["metadata"]["name"]: d["spec"] for _, d in manifests(namespace) if d.get("kind") == "NetworkPolicy"}


def template(doc):
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return spec.get("template") or {}


def pod_labels(doc):
    labels = dict(((template(doc).get("metadata") or {}).get("labels")) or {})
    if doc.get("kind") == "Job":
        labels["batch.kubernetes.io/job-name"] = doc["metadata"]["name"]  # set by the Job controller
    return labels


def selects(selector, labels):
    """Kubernetes label-selector semantics for matchLabels and matchExpressions; {} selects everything."""
    if set(selector) - {"matchLabels", "matchExpressions"}:
        raise AssertionError(f"unknown selector fields: {selector}")
    if any(labels.get(k) != v for k, v in (selector.get("matchLabels") or {}).items()):
        return False
    for e in selector.get("matchExpressions") or []:
        key, op, values = e["key"], e["operator"], e.get("values") or []
        ok = {"In": lambda: labels.get(key) in values, "NotIn": lambda: labels.get(key) not in values,
              "Exists": lambda: key in labels, "DoesNotExist": lambda: key not in labels}[op]()
        if not ok:
            return False
    return True


def app_allowlist(selector):
    """The `app` values a podSelector admits. Only a closed list on `app` is accepted: `app: x`, or `app In [...]`."""
    labels, exprs = selector.get("matchLabels") or {}, selector.get("matchExpressions") or []
    if set(selector) - {"matchLabels", "matchExpressions"}:
        raise AssertionError(f"unknown selector fields: {selector}")
    if labels and not exprs and set(labels) == {"app"}:
        return {labels["app"]}
    if exprs and not labels and len(exprs) == 1 and exprs[0].get("key") == "app" and exprs[0].get("operator") == "In":
        return set(exprs[0]["values"])
    raise AssertionError(f"podSelector is not a closed list of app labels: {selector}")


def named_namespace(peer):
    """The single namespace a peer's namespaceSelector names; anything wider fails."""
    sel = peer["namespaceSelector"]
    if set(sel) != {"matchLabels"} or set(sel["matchLabels"]) != {NS_LABEL}:
        raise AssertionError(f"namespaceSelector does not name one namespace: {sel}")
    return sel["matchLabels"][NS_LABEL]


def reads_database_url(namespace, container):
    """Whether a container gets a database URL from env or envFrom (Secrets are read by reference only)."""
    configmaps = {d["metadata"]["name"]: d.get("data") or {} for _, d in manifests(namespace)
                  if d.get("kind") == "ConfigMap"}
    for e in container.get("env") or []:
        ref = (e.get("valueFrom") or {})
        key = (ref.get("secretKeyRef") or ref.get("configMapKeyRef") or {}).get("key") or ""
        if DB_URL.search(e.get("name") or "") or DB_URL.search(key):
            return True
    for src in container.get("envFrom") or []:
        if "secretRef" in src:
            raise AssertionError(f"{namespace} container {container['name']}: envFrom secretRef hides its key names; "
                                 "use env with secretKeyRef so this fence can see them")
        cm = (src.get("configMapRef") or {}).get("name")
        if cm is not None and any(DB_URL.search(k) for k in configmaps.get(cm, {})):
            return True
    return False


def database_clients(namespace):
    """The `app` labels of an app namespace's workloads whose pods (init containers included) read a database URL."""
    return {pod_labels(d).get("app") for _, d in workloads(namespace)
            if any(reads_database_url(namespace, c) for c in containers(d))}


def server_policy(server):
    """The `data` NetworkPolicy that selects the server's pods, by its `app` label."""
    found = [s for s in policies("data").values() if (s["podSelector"].get("matchLabels") or {}).get("app") == server]
    if len(found) != 1:
        raise AssertionError(f"want exactly one data NetworkPolicy for {server}, found {len(found)}")
    return found[0]


def ingress_peers(spec):
    return [p for rule in spec.get("ingress") or [] for p in rule.get("from") or []]


def hetzner_network():
    """The nodes' private network (`hcloud_network.main` ip_range) and the node addresses in `local.nodes`."""
    text = (TF / "hetzner/main.tf").read_text()
    m = re.search(r'(?m)^\s*ip_range\s*=\s*"([^"]+)"', resource_body(text, "hcloud_network", "main"))
    if not m:
        raise AssertionError("hcloud_network.main has no ip_range")
    nodes = [ipaddress.ip_address(ip) for ip in re.findall(r'\bip\s*=\s*"([^"]+)"', text)]
    if not nodes:
        raise AssertionError("no node addresses in terraform/hetzner/main.tf")
    return ipaddress.ip_network(m.group(1)), nodes


def tunnel_origins():
    """(namespace, Service, port) of every in-cluster origin the tunnel routes to; http_status rules are skipped."""
    out = set()
    for r in tunnel_rules():
        if r["service"].startswith("http_status:"):
            continue
        m = TUNNEL_SERVICE.match(r["service"])
        if not m:
            raise AssertionError(f"tunnel service is not http://<svc>.<ns>.svc.cluster.local:<port>: {r['service']}")
        out.add((m.group(2), m.group(1), int(m.group(3))))
    if not out:
        raise AssertionError("no tunnel origins found")
    return out


def service_backends(namespace, name, port):
    """For a Service port: the Service selector, and (pod labels, container port, protocol) per workload it selects."""
    svcs = [d for _, d in manifests(namespace) if d.get("kind") == "Service" and d["metadata"]["name"] == name]
    if len(svcs) != 1:
        raise AssertionError(f"want one Service {namespace}/{name}, found {len(svcs)}")
    spec = svcs[0]["spec"]
    sp = [p for p in spec.get("ports") or [] if p.get("port") == port]
    if len(sp) != 1:
        raise AssertionError(f"Service {namespace}/{name} has no port {port}")
    target, proto = sp[0].get("targetPort", port), sp[0].get("protocol", "TCP")
    out = []
    for _, d in workloads(namespace):
        labels = pod_labels(d)
        if not selects({"matchLabels": spec["selector"]}, labels):
            continue
        cport = target
        if isinstance(target, str):  # a named port: the container port of that name
            cport = next(p["containerPort"] for c in containers(d) for p in c.get("ports") or []
                         if p.get("name") == target)
        out.append((labels, cport, proto))
    if not out:
        raise AssertionError(f"Service {namespace}/{name} selects no workload")
    return spec["selector"], out


def cluster_dns():
    """The CoreDNS pod labels, from the kube-system PodDisruptionBudget that guards them."""
    pdbs = [d for _, d in manifests("kube-system") if d.get("kind") == "PodDisruptionBudget"
            and d["metadata"]["name"] == "coredns"]
    if len(pdbs) != 1:
        raise AssertionError("no coredns PodDisruptionBudget in kube-system")
    return pdbs[0]["spec"]["selector"]["matchLabels"]


def egress_allows(spec, namespace, labels, port, protocol):
    """Whether an egress policy lets its pods open (port, protocol) to a pod with `labels` in `namespace`: Kubernetes
    semantics, where a rule without `to` or `ports` allows every destination or port."""
    for rule in spec.get("egress") or []:
        ports = rule.get("ports")
        if ports and not any(p.get("port") == port and p.get("protocol", "TCP") == protocol for p in ports):
            continue
        peers = rule.get("to")
        if not peers:
            return True
        for peer in peers:
            if "ipBlock" in peer:
                continue
            ns_ok = selects(peer.get("namespaceSelector") or {}, {NS_LABEL: namespace}) \
                if "namespaceSelector" in peer else namespace == "cloudflared"
            if ns_ok and selects(peer.get("podSelector") or {}, labels):
                return True
    return False


def in_cluster_destinations(spec):
    """(peer, port, protocol) for every in-cluster peer of an egress policy, one per port (None: every port)."""
    out = []
    for rule in spec.get("egress") or []:
        for peer in rule.get("to") or []:
            if "ipBlock" in peer:
                continue
            for p in rule.get("ports") or [{}]:
                out.append((peer, p.get("port"), p.get("protocol", "TCP")))
    return out


# ---- tests --------------------------------------------------------------------------------------------------------

class DatabaseClientsOnly(unittest.TestCase):
    """K8S-04: from the app namespaces, each database server admits only the pods that hold a database URL (frontends
    hold none), and the backup and bootstrap pods inside `data` stay admitted."""

    def test_every_cross_namespace_peer_names_its_pods(self):
        for server in SERVER.values():
            for peer in ingress_peers(server_policy(server)):
                if "namespaceSelector" not in peer:
                    continue
                with self.subTest(server=server, peer=peer):
                    named_namespace(peer)
                    self.assertTrue(app_allowlist(peer.get("podSelector") or {}))

    def test_admitted_pods_are_exactly_the_database_clients(self):
        clients = {ns: database_clients(ns) for ns in APP_NAMESPACES}
        for ns in APP_NAMESPACES:
            # The derivation itself: every app's API reads a database URL, so an empty set means it broke.
            self.assertIn("backend", clients[ns], f"{ns}: no database client found")
        for app, server in SERVER.items():
            admitted = {}
            for peer in ingress_peers(server_policy(server)):
                if "namespaceSelector" in peer:
                    apps = app_allowlist(peer.get("podSelector") or {})
                    admitted.setdefault(named_namespace(peer), set()).update(apps)
            want = {ns: apps for ns, apps in clients.items() if ns.split("-")[0] == app}
            self.assertEqual(admitted, want, server)

    def test_backup_and_bootstrap_pods_in_data_stay_admitted(self):
        seen = set()
        for _, d in workloads("data"):
            if d.get("kind") == "StatefulSet":  # the servers themselves
                continue
            images = {(c.get("image") or "").split(":")[0] for c in containers(d)}
            for server in SERVER.values():
                if server not in images:
                    continue
                seen.add((d["metadata"]["name"], server))
                same_ns = [p for p in ingress_peers(server_policy(server)) if "namespaceSelector" not in p]
                self.assertTrue(any(selects(p["podSelector"], pod_labels(d)) for p in same_ns),
                                f"data {d['kind']}/{d['metadata']['name']} is not admitted to {server}")
        self.assertTrue({("db-backup", "mysql"), ("db-backup", "postgres")} <= seen, seen)


class AppEgressSkipsTheNodes(unittest.TestCase):
    """K8S-07: the app namespaces' egress to addresses (ipBlock) leaves out the node network and instance metadata.
    Pods, and Services (DNATed to pod addresses before the policy applies), stay reachable through a pod peer."""

    def test_node_network_holds_the_nodes_and_not_the_pods(self):
        net, nodes = hetzner_network()
        for ip in nodes:
            self.assertIn(ip, net)
        self.assertFalse(net.overlaps(ipaddress.ip_network(POD_CIDR)), "excepting the node network would cut pods")

    def test_every_app_namespace_excepts_the_node_network(self):
        net, _ = hetzner_network()
        for ns in APP_NAMESPACES:
            egress = [s for s in policies(ns).values() if "Egress" in s["policyTypes"]]
            self.assertTrue(any(s["podSelector"] == {} for s in egress), f"{ns}: no egress policy for every pod")
            for s in egress:
                for rule in s.get("egress") or []:
                    # A rule without `to` allows every address, the node network included.
                    self.assertTrue(rule.get("to"), f"{ns}: egress rule without `to`")
                    for peer in rule["to"]:
                        if "ipBlock" not in peer:
                            continue
                        cidr = ipaddress.ip_network(peer["ipBlock"]["cidr"])
                        excepted = [ipaddress.ip_network(e) for e in peer["ipBlock"].get("except") or []]
                        for target in (net, IMDS):
                            if cidr.overlaps(target):
                                self.assertTrue(any(target.subnet_of(e) for e in excepted),
                                                f"{ns}: ipBlock {cidr} does not except {target}")


class TunnelReachesItsOriginsOnly(unittest.TestCase):
    """EDGE-07: cloudflared reaches cluster DNS and exactly the pods and ports the tunnel config routes to, plus
    Cloudflare's edge; no egress rule leaves the destination or the port open."""

    def cloudflared(self):
        return policies("cloudflared")["cloudflared"]

    def test_every_egress_rule_names_its_ports_and_its_pods(self):
        for rule in self.cloudflared()["egress"]:
            with self.subTest(rule=rule):
                self.assertTrue(rule.get("to"), "rule without `to` allows every destination")
                self.assertTrue(rule.get("ports"), "rule without `ports` allows every port")
                for peer in rule["to"]:
                    if "ipBlock" in peer:
                        continue
                    named_namespace(peer)
                    self.assertTrue(peer.get("podSelector"), "peer without a podSelector admits every pod")

    def test_every_tunnel_origin_is_reachable(self):
        spec = self.cloudflared()
        for ns, name, port in sorted(tunnel_origins()):
            _, backends = service_backends(ns, name, port)
            for labels, cport, proto in backends:
                with self.subTest(origin=f"{name}.{ns}:{port}", pod=labels):
                    self.assertTrue(egress_allows(spec, ns, labels, cport, proto))
        for proto in ("UDP", "TCP"):
            self.assertTrue(egress_allows(spec, "kube-system", cluster_dns(), 53, proto), f"DNS over {proto}")

    def test_in_cluster_egress_is_exactly_dns_and_the_tunnel_origins(self):
        want = {("kube-system", frozenset(cluster_dns().items()), 53, proto) for proto in ("UDP", "TCP")}
        for ns, name, port in tunnel_origins():
            selector, backends = service_backends(ns, name, port)
            want |= {(ns, frozenset(selector.items()), cport, proto) for _, cport, proto in backends}
        got = {(named_namespace(peer), frozenset(((peer.get("podSelector") or {}).get("matchLabels") or {}).items()),
                p, pr) for peer, p, pr in in_cluster_destinations(self.cloudflared())}
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
