"""Each app namespace's memory quota fits its Deployments' memory limits (clusters/hetzner/namespaces/<ns>.yaml).

A pod counts at the larger of its init containers and the sum of its app containers, times the replicas. Production
quotas hold twice that: pods on a lost node keep counting until the quota's 5-minute resync while their replacements
count too. Every Deployment uses Recreate, so a rollout needs no surge room. A limit raised without its quota fails
here instead of as a FailedCreate in the cluster.

Stdlib only; the manifest parser is test_tbd_manifests'.
"""
import re
import unittest

from test_tbd_manifests import CLUSTERS, containers, load_all, workloads

APP_NAMESPACES = ("tbd-prod", "tbd-staging", "ziftbook-prod", "ziftbook-staging")
UNITS = {"Mi": 1, "Gi": 1024}


def mebibytes(quantity):
    m = re.fullmatch(r"(\d+)(Mi|Gi)", str(quantity))
    if not m:
        raise AssertionError(f"memory quantity this test does not model: {quantity!r}")
    return int(m[1]) * UNITS[m[2]]


def limit(container):
    return mebibytes(container["resources"]["limits"]["memory"])


def quota(namespace):
    for doc in load_all(CLUSTERS / "hetzner" / "namespaces" / f"{namespace}.yaml"):
        if doc.get("kind") == "ResourceQuota" and doc["metadata"]["name"] == "memory":
            return mebibytes(doc["spec"]["hard"]["limits.memory"])
    raise AssertionError(f"{namespace}: no memory ResourceQuota")


def limits_needed(namespace):
    total = 0
    for _, doc in workloads(namespace):
        if doc["kind"] != "Deployment":
            raise AssertionError(f"{namespace}: {doc['kind']} {doc['metadata']['name']} is not modelled here")
        app = sum(limit(c) for c in containers(doc, init=False))
        init = max((limit(c) for c in containers(doc) if c not in containers(doc, init=False)), default=0)
        total += max(app, init) * int(doc["spec"].get("replicas", 1))
    return total * (2 if namespace.endswith("-prod") else 1)


class MemoryQuotasFitTheLimits(unittest.TestCase):
    def test_each_app_namespace_quota_holds_its_limits(self):
        for ns in APP_NAMESPACES:
            with self.subTest(namespace=ns):
                self.assertLessEqual(limits_needed(ns), quota(ns))


if __name__ == "__main__":
    unittest.main()
