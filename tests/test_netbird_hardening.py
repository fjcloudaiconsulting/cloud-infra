"""Fences on the NetBird peer in clusters/hetzner/netbird/ (the owner's kubectl path, INFRA-66).

Stdlib only; reuses the YAML subset parser of test_tbd_manifests.py.
"""
import unittest

from test_tbd_manifests import manifests

NS = "netbird"
PSA = "pod-security.kubernetes.io/"


def namespace():
    (doc,) = [d for _, d in manifests(NS) if d.get("kind") == "Namespace" and d["metadata"]["name"] == NS]
    return doc


class PodSecurityLabels(unittest.TestCase):
    """K8S-09: the peer runs hostNetwork, so enforce stays privileged; warn and audit report anything past baseline."""

    def test_enforce_stays_privileged(self):
        self.assertEqual(namespace()["metadata"]["labels"].get(PSA + "enforce"), "privileged")

    def test_warn_and_audit_at_baseline(self):
        labels = namespace()["metadata"]["labels"]
        for mode in ("warn", "audit"):
            self.assertEqual(labels.get(PSA + mode), "baseline", mode)


if __name__ == "__main__":
    unittest.main()
