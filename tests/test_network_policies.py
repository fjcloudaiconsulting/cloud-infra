"""Fences on the NetworkPolicies between namespaces: which pods reach the database servers in `data` (K8S-04).

Stdlib only: `python3 -m unittest discover -s tests`. Manifests are read with test_tbd_manifests' parser. The sets the
policies must admit are derived from the workload manifests, so a new database client or tunnel route fails here until
its policy follows.
"""
import re
import unittest

from test_tbd_manifests import containers, manifests, workloads

APP_NAMESPACES = ("tbd-prod", "tbd-staging", "ziftbook-prod", "ziftbook-staging")
NS_LABEL = "kubernetes.io/metadata.name"
# The server each app's namespaces use. The URLs sit in SOPS Secrets, so the engine is not read from them.
SERVER = {"tbd": "mysql", "ziftbook": "postgres"}
# Env names and Secret keys that carry a database URL: DATABASE_URL, ZIF_DATABASE_URL, ZIF_MIGRATE_DATABASE_URL,
# database-url, migrate-database-url.
DB_URL = re.compile(r"database[-_]url", re.I)


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
    """The `app` labels of the workloads in an app namespace whose pods (init containers included) read a database URL."""
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
                    admitted.setdefault(named_namespace(peer), set()).update(app_allowlist(peer.get("podSelector") or {}))
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


if __name__ == "__main__":
    unittest.main()
