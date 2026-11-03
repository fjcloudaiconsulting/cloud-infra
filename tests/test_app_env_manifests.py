"""Fences on the shape every app environment shares (INFRA-258): clusters/hetzner/<app>-prod and <app>-staging.

Each namespace directory holds its manifests in full: manifest_fence.py (GHA-03) allows no kustomize patches or
components there, so the line-based checks and Renovate read exactly what Flux applies. This file is the shared base
instead: what every app environment must look like, so a copy that drifts, or a new app environment, fails here.
Per-app fences stay in test_tbd_manifests.py, test_ziftbook_manifests.py and test_ziftbook_networkpolicy.py. Fenced
elsewhere for these namespaces too: Pod Security levels and ServiceAccount tokens (test_platform_hardening.py), egress
to the node network (test_network_policies.py), the OTLP endpoint per environment (test_telemetry_receiver.py).

Stdlib only; the manifest parser is test_tbd_manifests'.
"""
import re
import unittest

from test_tbd_manifests import CLUSTERS, containers, manifests, pod_spec, served, where, workloads

APP_NAMESPACES = ("tbd-prod", "tbd-staging", "ziftbook-prod", "ziftbook-staging")
ENVIRONMENT = {"prod": "production", "staging": "staging"}
CONTAINER_SECURITY = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                      "capabilities": {"drop": ["ALL"]}}
OTEL = [{"configMapRef": {"name": "otel"}}]


def objects(ns, kind):
    return {d["metadata"]["name"]: d for _, d in manifests(ns) if d.get("kind") == kind}


class AppNamespaces(unittest.TestCase):
    def test_every_app_environment_directory_is_listed(self):
        # The other fences loop over APP_NAMESPACES: a new <app>-<env> directory must join it.
        dirs = {p.name for p in CLUSTERS.glob("*/*")
                if p.is_dir() and re.fullmatch(r"[a-z0-9-]+-(?:prod|staging)", p.name)}
        self.assertEqual(dirs, set(APP_NAMESPACES))


class NetworkPolicyShape(unittest.TestCase):
    def test_default_deny_ingress(self):
        for ns in APP_NAMESPACES:
            pol = objects(ns, "NetworkPolicy").get("default-deny-ingress")
            self.assertIsNotNone(pol, f"{ns}: no default-deny-ingress policy")
            self.assertEqual(pol["spec"], {"podSelector": {}, "policyTypes": ["Ingress"]}, ns)

    def test_egress_policy_is_the_same_in_every_app_environment(self):
        specs = {ns: (objects(ns, "NetworkPolicy").get("egress-no-imds") or {}).get("spec") for ns in APP_NAMESPACES}
        first = specs[APP_NAMESPACES[0]]
        self.assertTrue(first, f"{APP_NAMESPACES[0]}: no egress-no-imds policy")
        for ns, spec in specs.items():
            self.assertEqual(spec, first, f"{ns}: egress-no-imds differs from {APP_NAMESPACES[0]}")


class WorkloadShape(unittest.TestCase):
    def test_pods(self):
        for ns in APP_NAMESPACES:
            self.assertTrue(workloads(ns), ns)
            for f, d in workloads(ns):
                what = f"{f.name} {d['kind']}/{d['metadata']['name']}"
                self.assertEqual(d["kind"], "Deployment", what)
                # Recreate: no surge pod, so a rollout fits the quota and never runs two versions side by side.
                self.assertEqual(d["spec"].get("strategy"), {"type": "Recreate"}, what)
                spec = pod_spec(d)
                self.assertIs(spec.get("automountServiceAccountToken"), False, what)
                psc = spec.get("securityContext") or {}
                self.assertEqual(set(psc), {"runAsUser", "runAsGroup", "runAsNonRoot", "seccompProfile"}, what)
                self.assertIs(psc["runAsNonRoot"], True, what)
                self.assertEqual(psc["seccompProfile"], {"type": "RuntimeDefault"}, what)
                self.assertTrue(isinstance(psc["runAsUser"], int) and psc["runAsUser"] > 0, what)
                self.assertEqual(psc["runAsGroup"], psc["runAsUser"], what)
                # A read-only root needs a writable /tmp.
                self.assertIn({"name": "tmp", "emptyDir": {}}, spec.get("volumes") or [], what)

    def test_containers(self):
        for ns in APP_NAMESPACES:
            for f, d in workloads(ns):
                for c in containers(d):
                    w = where(f, d, c)
                    self.assertEqual(c.get("securityContext"), CONTAINER_SECURITY, w)
                    self.assertIn({"name": "tmp", "mountPath": "/tmp"}, c.get("volumeMounts") or [], w)
                    # CPU and memory requests, and a memory limit only, as every app pod is written today.
                    res = c.get("resources") or {}
                    self.assertEqual(set(res.get("requests") or {}), {"cpu", "memory"}, w)
                    self.assertEqual(set(res.get("limits") or {}), {"memory"}, w)
                    if "envFrom" in c:
                        self.assertEqual(c["envFrom"], OTEL, f"{w}: envFrom reads the otel ConfigMap only")

    def test_every_served_container_has_a_readiness_probe(self):
        for ns in APP_NAMESPACES:
            seen = 0
            for f, d in served(ns):
                for c in containers(d, init=False):
                    seen += 1
                    self.assertTrue("readinessProbe" in c, where(f, d, c))
            self.assertGreaterEqual(seen, 2, f"{ns}: no served containers found")


class OtlpWiring(unittest.TestCase):
    def test_otel_config_map(self):
        keys = None
        for ns in APP_NAMESPACES:
            app, tier = ns.rsplit("-", 1)
            cm = objects(ns, "ConfigMap").get("otel")
            self.assertIsNotNone(cm, f"{ns}: no otel ConfigMap")
            data = cm["data"]
            self.assertEqual(data["OTEL_RESOURCE_ATTRIBUTES"],
                             f"service.namespace={app},deployment.environment.name={ENVIRONMENT[tier]}", ns)
            self.assertEqual(data["OTEL_EXPORTER_OTLP_PROTOCOL"], "http/protobuf", ns)
            self.assertEqual(data["OTEL_LOGS_EXPORTER"], "none", f"{ns}: logs leave as stdout JSON only")
            keys = keys or set(data)
            self.assertEqual(set(data), keys, f"{ns}: otel keys differ from the other app environments")

    def test_every_app_environment_reads_it(self):
        for ns in APP_NAMESPACES:
            readers = [c["name"] for _, d in workloads(ns) for c in containers(d) if c.get("envFrom") == OTEL]
            self.assertTrue(readers, f"{ns}: no container reads the otel ConfigMap")


if __name__ == "__main__":
    unittest.main()
