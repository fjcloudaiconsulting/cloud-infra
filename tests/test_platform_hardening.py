"""Fences on the platform layer: Flux's RBAC (clusters/hetzner/flux-system).

Stdlib only, plus `kubectl kustomize` (or `kustomize build`) for the directories that carry kustomize patches: the
render is what Flux applies, so these fences read it rather than the vendored upstream files. Without either tool the
render tests skip locally and fail in CI. Manifests are parsed with the YAML subset parser of test_tbd_manifests.py.
"""
import functools
import os
import re
import shutil
import subprocess
import unittest

from test_tbd_manifests import CLUSTERS, _Parser

FLUX_NS = "flux-system"
FLUX_CONTROLLERS = ("source-controller", "kustomize-controller")


# ---- helpers ------------------------------------------------------------------------------------------------------

def parse_docs(text, where):
    """Every document of a multi-document YAML string, CRDs left out (the subset parser does not need them)."""
    docs = []
    for part in re.split(r"(?m)^---[ \t]*$", text):
        if re.search(r"(?m)^kind: CustomResourceDefinition$", part):
            continue
        p = _Parser(part.split("\n"), where)
        doc = p.block(0)
        if p.peek() is not None:
            p.fail("unparsed content")
        if doc is not None:
            docs.append(doc)
    return docs


@functools.cache
def render(rel):
    """`kubectl kustomize clusters/hetzner/<rel>`: what Flux applies from that directory, as parsed documents."""
    for cmd in (["kubectl", "kustomize"], ["kustomize", "build"]):
        if shutil.which(cmd[0]):
            r = subprocess.run([*cmd, str(CLUSTERS / "hetzner" / rel)], capture_output=True, text=True)
            if r.returncode:
                raise AssertionError(f"{' '.join(cmd)} {rel} failed:\n{r.stderr}")
            return tuple(parse_docs(r.stdout, f"render of {rel}"))
    if os.environ.get("CI"):
        raise AssertionError("neither kubectl nor kustomize is on PATH: the render fences cannot run")
    raise unittest.SkipTest("neither kubectl nor kustomize is on PATH")


def of_kind(docs, *kinds):
    return [d for d in docs if d.get("kind") in kinds]


def name_of(doc):
    return (doc.get("metadata") or {}).get("name")


def grants(rule, group, resource, verb):
    """True when an RBAC rule allows `verb` on `resource` in API group `group` (wildcards included)."""
    return (any(g in ("*", group) for g in rule.get("apiGroups") or [])
            and any(r in ("*", resource) for r in rule.get("resources") or [])
            and any(v in ("*", verb) for v in rule.get("verbs") or []))


# ---- tests --------------------------------------------------------------------------------------------------------

class HelperSelfTest(unittest.TestCase):
    def test_grants(self):
        token = {"apiGroups": [""], "resources": ["serviceaccounts/token"], "verbs": ["create"]}
        self.assertTrue(grants(token, "", "serviceaccounts/token", "create"))
        self.assertTrue(grants({"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}, "", "serviceaccounts/token",
                               "create"))
        self.assertFalse(grants(token, "", "serviceaccounts", "create"))
        self.assertFalse(grants({"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["*"]}, "",
                                "serviceaccounts/token", "create"))
        self.assertFalse(grants({"nonResourceURLs": ["/livez/ping"], "verbs": ["head"]}, "", "serviceaccounts/token",
                                "create"))

    def test_parse_docs_skips_crds(self):
        text = "kind: CustomResourceDefinition\nspec:\n  x: [a, [b]]\n---\nkind: A\nmetadata:\n  name: a\n"
        self.assertEqual(parse_docs(text, "t"), [{"kind": "A", "metadata": {"name": "a"}}])


class FluxRbac(unittest.TestCase):
    """K8S-05: the Flux controllers mint no ServiceAccount tokens, and every binding names an account that exists."""

    def test_no_flux_controller_role_creates_serviceaccount_tokens(self):
        docs = render(FLUX_NS)
        roles = {name_of(d): d for d in of_kind(docs, "ClusterRole")}
        controllers = {(FLUX_NS, c) for c in FLUX_CONTROLLERS}
        checked = set()
        for b in of_kind(docs, "ClusterRoleBinding", "RoleBinding"):
            subjects = {(s.get("namespace"), s.get("name")) for s in b.get("subjects") or []
                        if s.get("kind") == "ServiceAccount"}
            if not subjects & controllers:
                continue
            ref = b["roleRef"]["name"]
            if ref not in roles:
                # kustomize-controller applies the whole tree as cluster-admin (Flux's model; K8S-01 tracks the
                # split): the only role bound to a controller that this directory does not define.
                self.assertEqual((ref, subjects & controllers), ("cluster-admin", {(FLUX_NS, "kustomize-controller")}),
                                 f"{b['kind']}/{name_of(b)} binds a controller to a role defined elsewhere")
                continue
            for rule in roles[ref].get("rules") or []:
                self.assertFalse(grants(rule, "", "serviceaccounts/token", "create"),
                                 f"ClusterRole/{ref} (bound by {name_of(b)}) grants serviceaccounts/token create")
            checked.add(ref)
        self.assertIn("crd-controller-flux-system", checked)

    def test_every_binding_subject_is_a_serviceaccount_defined_here(self):
        docs = render(FLUX_NS)
        accounts = {((d.get("metadata") or {}).get("namespace"), name_of(d)) for d in of_kind(docs, "ServiceAccount")}
        self.assertEqual(accounts, {(FLUX_NS, c) for c in FLUX_CONTROLLERS})
        bindings = of_kind(docs, "ClusterRoleBinding", "RoleBinding")
        self.assertGreaterEqual(len(bindings), 2)
        for b in bindings:
            for s in b.get("subjects") or []:
                self.assertEqual(s.get("kind"), "ServiceAccount", f"{name_of(b)}: {s}")
                self.assertIn((s.get("namespace"), s.get("name")), accounts, f"{name_of(b)}: {s} is not defined here")


if __name__ == "__main__":
    unittest.main()
