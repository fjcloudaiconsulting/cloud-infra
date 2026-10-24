"""Fences on the platform layer: Flux's RBAC and images (clusters/hetzner/flux-system), the upgrade controller's
images (clusters/hetzner/system-upgrade) and ServiceAccount tokens in every pod under clusters/.

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

from test_tbd_manifests import ROOT, CLUSTERS, WORKLOAD_KINDS, _Parser, containers, hand_written, load_all, pod_spec

FLUX_NS = "flux-system"
FLUX_CONTROLLERS = ("source-controller", "kustomize-controller")
APP_NAMESPACES = ("tbd-prod", "tbd-staging", "ziftbook-prod", "ziftbook-staging")
# repo:tag@sha256:<64 hex>: the tag for people, the digest for the kubelet.
PINNED = re.compile(r"^[a-z0-9][a-z0-9./_-]*:[\w][\w.-]*@sha256:[0-9a-f]{64}$")
# The only ServiceAccounts whose pods talk to the API and so mount a token (namespace, name): Alloy's discovery, the
# upgrade controller and the two Flux controllers.
TOKEN_ALLOWLIST = {("observability", "alloy"), ("system-upgrade", "system-upgrade"),
                   *((FLUX_NS, c) for c in FLUX_CONTROLLERS)}


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


def namespace_of(doc):
    return (doc.get("metadata") or {}).get("namespace")


def cluster_docs():
    """(where, doc) for every hand-written document under clusters/, plus the rendered flux-system: the Flux
    controllers' pods are only in its generated file, and the render is what runs."""
    out = []
    for f in hand_written():
        out.extend((str(f.relative_to(ROOT)), d) for d in load_all(f))
    out.extend(("render of flux-system", d) for d in render(FLUX_NS))
    return out


def label(doc, key):
    return ((doc.get("metadata") or {}).get("labels") or {}).get(key)


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

    def test_pinned(self):
        digest = "sha256:" + "0" * 64
        self.assertRegex(f"ghcr.io/fluxcd/source-controller:v1.9.6@{digest}", PINNED)
        self.assertRegex(f"rancher/kubectl:v1.36.2@{digest}", PINNED)
        for ref in ("rancher/kubectl:v1.36.2", f"rancher/kubectl@{digest}", f"rancher/kubectl:v1@{digest[:-1]}",
                    f"rancher/kubectl:v1@sha256:{'0' * 64}x", f"rancher/kubectl:v1@sha512:{'0' * 64}"):
            self.assertNotRegex(ref, PINNED)

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


class PinnedImages(unittest.TestCase):
    """K8S-02: the Flux controllers, the upgrade controller and the upgrade Jobs' kubectl run by tag and digest."""

    def test_every_flux_image_is_pinned_by_digest(self):
        pods = of_kind(render(FLUX_NS), *WORKLOAD_KINDS)
        self.assertEqual(sorted(name_of(d) for d in pods), sorted(FLUX_CONTROLLERS))
        for d in pods:
            for c in containers(d):
                self.assertRegex(c.get("image") or "", PINNED, f"{d['kind']}/{name_of(d)} container {c['name']}")

    def test_flux_pins_follow_the_vendored_version(self):
        # A regenerated gotk-components.yaml with new controller tags must move the images: entries in the same change,
        # or the render would run the old controllers against the new CRDs.
        text = (CLUSTERS / "hetzner/flux-system/gotk-components.yaml").read_text()
        vendored = dict(re.findall(r"(?m)^\s+image: (ghcr\.io/fluxcd/[a-z-]+):(\S+)$", text))
        self.assertEqual(set(vendored), {f"ghcr.io/fluxcd/{c}" for c in FLUX_CONTROLLERS})
        for d in of_kind(render(FLUX_NS), *WORKLOAD_KINDS):
            for c in containers(d):
                repo, tag = c["image"].split("@")[0].rsplit(":", 1)
                self.assertEqual(tag, vendored[repo], f"{name_of(d)}: images: tag differs from gotk-components.yaml")

    def test_upgrade_controller_and_kubectl_images_are_pinned_by_digest(self):
        docs = render("system-upgrade")
        env = [d for d in of_kind(docs, "ConfigMap") if name_of(d) == "default-controller-env"]
        self.assertEqual(len(env), 1)
        # The drain container of every upgrade Job runs this image with the controller's ServiceAccount.
        self.assertRegex(env[0]["data"]["SYSTEM_UPGRADE_JOB_KUBECTL_IMAGE"], PINNED)
        pods = of_kind(docs, *WORKLOAD_KINDS)
        self.assertTrue(pods)
        for d in pods:
            for c in containers(d):
                self.assertRegex(c.get("image") or "", PINNED, f"{d['kind']}/{name_of(d)} container {c['name']}")


class ServiceAccountTokens(unittest.TestCase):
    """K8S-06: a pod gets an API token only when its ServiceAccount is one that needs the API."""

    def test_default_accounts_mount_no_token(self):
        accounts = {(namespace_of(d), name_of(d)): d for _, d in cluster_docs() if d.get("kind") == "ServiceAccount"}
        for ns in ("data", *APP_NAMESPACES):
            sa = accounts.get((ns, "default"))
            self.assertIsNotNone(sa, f"{ns}: no default ServiceAccount in git")
            self.assertIs(sa.get("automountServiceAccountToken"), False, f"{ns}/default")
            # Flux must never delete a namespace's default account (removing the file would otherwise prune it).
            self.assertEqual((sa["metadata"].get("annotations") or {}).get("kustomize.toolkit.fluxcd.io/prune"),
                             "disabled", f"{ns}/default")

    def test_no_pod_mounts_a_token_unless_allowlisted(self):
        docs = cluster_docs()
        no_automount = {(namespace_of(d), name_of(d)) for _, d in docs
                        if d.get("kind") == "ServiceAccount" and d.get("automountServiceAccountToken") is False}
        seen = 0
        for where, d in docs:
            if d.get("kind") not in WORKLOAD_KINDS:
                continue
            seen += 1
            spec, ns = pod_spec(d), namespace_of(d)
            what = f"{where} {d['kind']}/{name_of(d)}"
            self.assertTrue(ns, f"{what}: no namespace")
            sa = spec.get("serviceAccountName") or spec.get("serviceAccount") or "default"
            mount = spec.get("automountServiceAccountToken")
            if mount is False or (ns, sa) in TOKEN_ALLOWLIST:
                continue
            self.assertIsNone(mount, f"{what}: sets automountServiceAccountToken true for {ns}/{sa}")
            self.assertIn((ns, sa), no_automount, f"{what}: mounts the token of {ns}/{sa}")
        self.assertGreater(seen, 15)


if __name__ == "__main__":
    unittest.main()
