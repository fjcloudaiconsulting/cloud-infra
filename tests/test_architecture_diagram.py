"""Every Terraform stack, Namespace and Flux object must be a node in docs/architecture.md's diagram (INFRA-70).

Edges stay a review item. Stdlib only; reuses the cluster walker of test_psa_default.py.
"""
import re
import unittest

from test_psa_default import cluster_docs
from test_tbd_manifests import ROOT

DOC = ROOT / "docs/architecture.md"
# A node or subgraph declaration: id["label"], id(["label"]), id[("label")], id[label], subgraph id["label"].
NODE = re.compile(r'([A-Za-z_]\w*)\s*[\[(]{1,2}(?:"([^"]*)"|([^\])"|]*))')


def mermaid(text):
    (block,) = re.findall(r"```mermaid\n(.*?)```", text, re.S)
    return block


def nodes(block):
    """Each node's id and label as one string; edge labels (|"..."|) are not nodes."""
    return [f"{m[1]} {m[2] or m[3]}" for m in NODE.finditer(block)]


def named(name, labels):
    return any(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", s) for s in labels)


def flux_objects():
    """Namespaces, Flux Kustomizations and HelmReleases in git (kustomize's own kustomization.yaml excluded)."""
    out = set()
    for _, d in cluster_docs():
        if d["kind"] == "Namespace" or (d["kind"] in ("Kustomization", "HelmRelease")
                                         and "toolkit.fluxcd.io/" in d.get("apiVersion", "")):
            out.add(d["metadata"]["name"])
    return out


class ArchitectureDiagram(unittest.TestCase):
    def setUp(self):
        self.labels = nodes(mermaid(DOC.read_text()))

    def test_every_terraform_stack_is_a_node(self):
        stacks = sorted(p.name for p in (ROOT / "terraform").iterdir() if p.is_dir())
        self.assertTrue(stacks)
        missing = [s for s in stacks if not named(f"terraform/{s}", self.labels)]
        self.assertEqual(missing, [], "add these stacks to the diagram in docs/architecture.md")

    def test_every_namespace_and_flux_object_is_a_node(self):
        names = flux_objects()
        self.assertIn("flux-system", names)
        missing = sorted(n for n in names if not named(n, self.labels))
        self.assertEqual(missing, [], "add these to the diagram in docs/architecture.md")

    def test_edge_labels_and_prefixes_do_not_count(self):
        labels = nodes('a["x (terraform/cloudflare-tokens)"] -->|"terraform/hetzner"| b(["data store"])')
        self.assertTrue(named("terraform/cloudflare-tokens", labels))
        self.assertFalse(named("terraform/cloudflare", labels))
        self.assertFalse(named("terraform/hetzner", labels))
        self.assertTrue(named("data", labels))


if __name__ == "__main__":
    unittest.main()
