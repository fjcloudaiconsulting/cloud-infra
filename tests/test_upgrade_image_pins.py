"""Fences on the k3s upgrade Plans (K8S-02): their privileged images are pinned by digest and move with `version`.

system-upgrade-controller v0.20.2 appends `.status.latestVersion` (the version with - for +) only to an untagged
image; a tagged or digested one runs as written. So the pin in plans.yaml is what the upgrade Jobs pull, and Renovate
must move tag and digest in the same PR as `version`. Stdlib only; the YAML subset parser is test_tbd_manifests'.
"""
import fnmatch
import json
import re
import unittest

from test_tbd_manifests import ROOT, load_all

PLANS = ROOT / "clusters/hetzner/system-upgrade/plans.yaml"
RENOVATE = ROOT / "renovate.json"
IMAGE = "rancher/k3s-upgrade"
K3S = "k3s-io/k3s"
PINNED = re.compile(r"^(?P<name>[^:@]+):(?P<tag>[^:@]+)@sha256:(?P<digest>[0-9a-f]{64})$")


def plans():
    return [d for d in load_all(PLANS) if d.get("kind") == "Plan"]


def images(node, path=()):
    """(path, value) of every `image` key anywhere under node, so a container added later is covered too."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "image":
                yield ".".join(path + (k,)), v
            else:
                yield from images(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from images(v, path + (str(i),))


def plan_images():
    """(plan name, version, path, image) for every image of every Plan."""
    for doc in plans():
        for path, image in images(doc["spec"]):
            yield doc["metadata"]["name"], doc["spec"].get("version"), path, image


def js_regex(pattern):
    """A Renovate (JavaScript) regex as a Python one: named groups are (?<n>...) there."""
    return re.compile(pattern.replace("(?<", "(?P<"))


def renovate():
    return json.loads(RENOVATE.read_text())


def rules_naming(config, package):
    return [r for r in config["packageRules"] if package in (r.get("matchPackageNames") or [])]


class UpgradeImagesPinned(unittest.TestCase):
    def test_both_plans_set_prepare_and_upgrade_images(self):
        # Guards the tests below against passing on nothing.
        found = {(name, path) for name, _, path, _ in plan_images()}
        self.assertEqual(found, {(p, f"{c}.image") for p in ("k3s-app", "k3s-db") for c in ("prepare", "upgrade")})

    def test_every_image_is_pinned_by_digest(self):
        for name, _, path, image in plan_images():
            with self.subTest(plan=name, path=path):
                m = PINNED.match(image or "")
                self.assertIsNotNone(m, f"{image!r} is not {IMAGE}:<tag>@sha256:<64 hex>")
                self.assertEqual(m["name"], IMAGE)

    def test_every_tag_is_the_plan_version(self):
        for name, version, path, image in plan_images():
            with self.subTest(plan=name, path=path):
                self.assertRegex(version or "", r"^v\d+\.\d+\.\d+\+k3s\d+$")
                m = PINNED.match(image or "")
                self.assertIsNotNone(m, image)
                self.assertEqual(m["tag"], version.replace("+", "-"))

    def test_both_plans_share_one_version_and_one_digest(self):
        rows = list(plan_images())
        self.assertEqual(len({version for _, version, _, _ in rows}), 1, rows)
        digests = set()
        for *_, image in rows:
            m = PINNED.match(image or "")
            digests.add(m["digest"] if m else image)
        self.assertEqual(len(digests), 1, digests)


class RenovateMovesTheImageWithK3s(unittest.TestCase):
    def setUp(self):
        self.config = renovate()

    def test_the_kubernetes_manager_reads_the_plans(self):
        # The kubernetes manager extracts each `image:` line with its tag and digest; nothing else bumps them.
        rel = str(PLANS.relative_to(ROOT))
        patterns = self.config["kubernetes"]["managerFilePatterns"]
        self.assertTrue(any(js_regex(p.strip("/")).search(rel) for p in patterns), patterns)
        # fnmatch's * also crosses /, so this is stricter than Renovate's globs.
        self.assertFalse([g for g in self.config.get("ignorePaths", []) if fnmatch.fnmatch(rel, g)])

    def test_only_digest_only_updates_of_the_image_are_disabled(self):
        for rule in rules_naming(self.config, IMAGE):
            if rule.get("enabled") is False:
                self.assertEqual(rule.get("matchUpdateTypes"), ["digest"], rule)

    def test_the_image_shares_every_k3s_rule(self):
        # Group, release age, separateMinorPatch and the minor approval apply to both, so they land in one PR.
        k3s_rules = rules_naming(self.config, K3S)
        self.assertTrue(any(r.get("groupName") == "k3s" and r.get("minimumReleaseAge") for r in k3s_rules))
        for rule in k3s_rules:
            self.assertIn(IMAGE, rule["matchPackageNames"], rule)

    def test_the_image_versioning_reads_the_same_release_as_version(self):
        (tag_rule,) = [r for r in rules_naming(self.config, IMAGE) if "versioning" in r]
        self.assertEqual(tag_rule.get("matchDatasources"), ["docker"])
        tag_re = js_regex(tag_rule["versioning"].removeprefix("regex:"))
        (k3s_manager,) = [m for m in self.config["customManagers"] if "k3s" in m.get("description", "")]
        version_re = js_regex(k3s_manager["versioningTemplate"].removeprefix("regex:"))
        parts = ("major", "minor", "patch", "build")
        for name, version, path, image in plan_images():
            with self.subTest(plan=name, path=path):
                m = PINNED.match(image or "")
                self.assertIsNotNone(m, image)
                t, v = tag_re.match(m["tag"]), version_re.match(version)
                self.assertIsNotNone(t, m["tag"])
                self.assertIsNotNone(v, version)
                self.assertEqual([t[p] for p in parts], [v[p] for p in parts])
        # Release candidates and per-arch tags are not versions, so they are never proposed.
        for tag in ("v1.37.1-rc2-k3s1", "v1.36.5-k3s1-arm64", "latest"):
            self.assertIsNone(tag_re.match(tag), tag)


if __name__ == "__main__":
    unittest.main()
