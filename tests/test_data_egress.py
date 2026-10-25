"""Fences on egress from `data` (K8S-06): a default-deny covers every pod there, the database servers get no egress
rule (they only answer, and resolve no names), and only the backup and the Ziftbook bootstrap Jobs reach anything:
cluster DNS, the servers they use, and HTTPS to public addresses, never the node network or instance metadata.

Stdlib only: `python3 -m unittest discover -s tests`. Manifests are read with test_tbd_manifests' parser and the
policies with test_network_policies' helpers. The servers a pod uses are derived from its images, as there, so a new
client in `data` fails here until its egress policy follows.
"""
import ipaddress
import re
import unittest

from test_network_policies import (IMDS, cluster_dns, hetzner_network, named_namespace, pod_labels, policies,
                                   selects, service_backends)
from test_tbd_manifests import POD_CIDR, ROOT, containers, manifests, workloads

# k3s's default Service CIDR: ansible/group_vars/all.yml does not set service-cidr (a fence below keeps it so).
SERVICE_CIDR = "10.43.0.0/16"
# What a pod can route to that is not the internet: RFC 1918 (the node network, the pod and Service ranges), shared
# address space (NetBird's) and link-local (instance metadata).
NOT_PUBLIC = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
# Server image -> its Service and port in `data`.
SERVERS = {"mysql": ("mysql", 3306), "postgres": ("postgres", 5432)}
# The pods that fetch over HTTPS: the uploader (S3) and the bootstrap Jobs' fetch init container (bootstrap.sql).
HTTPS_CLIENTS = {"CronJob/db-backup", "Job/ziftbook-bootstrap", "Job/ziftbook-prod-bootstrap"}


# ---- helpers ------------------------------------------------------------------------------------------------------

def name(doc):
    return f"{doc['kind']}/{doc['metadata']['name']}"


def egress_policies():
    return {n: s for n, s in policies("data").items() if "Egress" in (s.get("policyTypes") or [])}


def egress_rules(labels):
    """The egress rules for a pod with `labels`: the union over every policy that selects it (Kubernetes adds them
    up). None when no egress policy selects the pod, which leaves its egress open."""
    selecting = [s for s in egress_policies().values() if selects(s["podSelector"], labels)]
    if not selecting:
        return None
    return [rule for s in selecting for rule in s.get("egress") or []]


def in_cluster(rules):
    """(namespace, pod labels, port, protocol) per in-cluster peer and port; a peer with no namespaceSelector is in
    `data`, and a rule without `ports` yields port None (every port)."""
    out = set()
    for rule in rules:
        for peer in rule.get("to") or []:
            if "ipBlock" in peer:
                continue
            ns = named_namespace(peer) if "namespaceSelector" in peer else "data"
            sel = peer.get("podSelector") or {}
            if set(sel) != {"matchLabels"} or not sel["matchLabels"]:
                raise AssertionError(f"peer does not name its pods by matchLabels: {peer}")
            for p in rule.get("ports") or [{}]:
                out.add((ns, frozenset(sel["matchLabels"].items()), p.get("port"), p.get("protocol", "TCP")))
    return out


def address_rules(rules):
    return [r for r in rules if any("ipBlock" in p for p in r.get("to") or [])]


def reaches_addresses(doc):
    """Whether a pod may open connections to addresses outside the cluster (open egress counts)."""
    rules = egress_rules(pod_labels(doc))
    return rules is None or bool(address_rules(rules))


def servers_used(doc):
    images = {(c.get("image") or "").split(":")[0].split("@")[0] for c in containers(doc)}
    return sorted(images & set(SERVERS))


# ---- tests --------------------------------------------------------------------------------------------------------

class EveryPodIsCovered(unittest.TestCase):
    """A pod no egress policy selects keeps open egress, so a default-deny selects every pod, future ones too."""

    def test_a_default_deny_selects_every_pod(self):
        deny = [n for n, s in egress_policies().items() if s["podSelector"] == {} and not s.get("egress")]
        self.assertTrue(deny, "data: no default-deny egress policy")

    def test_every_pod_template_is_selected(self):
        found = workloads("data")
        self.assertLessEqual({"StatefulSet/mysql", "StatefulSet/postgres", "CronJob/db-backup"},
                             {name(d) for _, d in found}, "the workload derivation found too little")
        for _, d in found:
            with self.subTest(workload=name(d)):
                self.assertIsNotNone(egress_rules(pod_labels(d)), "no egress policy selects it")


class ServersOnlyAnswer(unittest.TestCase):
    """MySQL and Postgres get no egress rule: kube-router passes the replies of an admitted connection (conntrack
    ESTABLISHED) whatever egress allows, and neither server looks up its clients' names."""

    def test_servers_have_no_egress_rule(self):
        servers = [d for _, d in workloads("data") if d.get("kind") == "StatefulSet"]
        self.assertEqual({d["metadata"]["name"] for d in servers}, set(SERVERS))
        for d in servers:
            with self.subTest(server=name(d)):
                self.assertEqual(egress_rules(pod_labels(d)), [])

    def test_servers_resolve_no_names(self):
        # The mysql image's my.cnf sets skip-name-resolve (and host-cache-size=0); tbd.cnf must not undo it.
        cnf = next(d["data"]["tbd.cnf"] for _, d in manifests("data")
                   if d.get("kind") == "ConfigMap" and d["metadata"]["name"] == "mysql")
        for line in cnf.splitlines():
            if re.search(r"(?i)name[-_]resolve", line) and not line.lstrip().startswith("#"):
                self.assertRegex(line.strip(), r"(?i)^skip[-_]name[-_]resolve(\s*=\s*(on|1|true))?$",
                                 "tbd.cnf turns client name lookups back on: MySQL would need DNS")
        # Postgres resolves only for log_hostname or a hostname in pg_hba (the image writes `host all all all`).
        pg = next(d for _, d in workloads("data") if name(d) == "StatefulSet/postgres")
        for arg in (a for c in containers(pg) for a in c.get("args") or []):
            self.assertNotRegex(str(arg), r"(?i)^(log_hostname\s*=\s*(on|true|yes|1)|hba_file\s*=)",
                                "Postgres would need DNS")


class ClientsReachTheirServersAndHttps(unittest.TestCase):
    """The backup and the bootstrap Jobs: cluster DNS, the servers their images talk to, and TCP 443 to public
    addresses only. Nothing reaches the Kubernetes API, a node, the app namespaces or instance metadata."""

    def clients(self):
        out = [d for _, d in workloads("data") if d.get("kind") != "StatefulSet"]
        self.assertTrue({name(d) for d in out} >= HTTPS_CLIENTS, "a listed HTTPS client is gone or renamed")
        return out

    def test_in_cluster_egress_is_exactly_dns_and_the_servers_used(self):
        dns = frozenset(cluster_dns().items())
        for d in self.clients():
            want = {("kube-system", dns, 53, proto) for proto in ("UDP", "TCP")}
            for image in servers_used(d):
                svc, port = SERVERS[image]
                selector, backends = service_backends("data", svc, port)
                want |= {("data", frozenset(selector.items()), cport, proto) for _, cport, proto in backends}
            with self.subTest(workload=name(d)):
                rules = egress_rules(pod_labels(d))
                self.assertIsNotNone(rules, "no egress policy selects it")
                self.assertEqual(in_cluster(rules), want)

    def test_only_the_https_clients_reach_addresses(self):
        self.clients()
        self.assertEqual({name(d) for _, d in workloads("data") if reaches_addresses(d)}, HTTPS_CLIENTS)

    def test_every_rule_names_its_destination_and_ports(self):
        for n, s in egress_policies().items():
            for rule in s.get("egress") or []:
                with self.subTest(policy=n, rule=rule):
                    self.assertTrue(rule.get("to"), "rule without `to` allows every destination")
                    self.assertTrue(rule.get("ports"), "rule without `ports` allows every port")

    def test_addresses_are_public_https_only(self):
        net, _ = hetzner_network()
        targets = [net, IMDS, ipaddress.ip_network(POD_CIDR), ipaddress.ip_network(SERVICE_CIDR)]
        targets += [ipaddress.ip_network(c) for c in NOT_PUBLIC]
        seen = 0
        for n, s in egress_policies().items():
            for rule in address_rules(s.get("egress") or []):
                with self.subTest(policy=n):
                    self.assertEqual(rule.get("ports"), [{"protocol": "TCP", "port": 443}])
                for peer in rule["to"]:
                    if "ipBlock" not in peer:
                        continue
                    seen += 1
                    cidr = ipaddress.ip_network(peer["ipBlock"]["cidr"])
                    excepted = [ipaddress.ip_network(e) for e in peer["ipBlock"].get("except") or []]
                    for target in targets:
                        if cidr.overlaps(target):
                            with self.subTest(policy=n, target=str(target)):
                                self.assertTrue(any(target.subnet_of(e) for e in excepted),
                                                f"ipBlock {cidr} does not except {target}")
        self.assertTrue(seen, "no ipBlock found: the fence checks nothing")

    def test_k3s_keeps_the_default_service_cidr(self):
        cfg = (ROOT / "ansible/group_vars/all.yml").read_text()
        self.assertNotIn("service-cidr", cfg, f"SERVICE_CIDR ({SERVICE_CIDR}) assumes the k3s default")


if __name__ == "__main__":
    unittest.main()
