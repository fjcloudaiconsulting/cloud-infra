"""Fences on the supply chain itself: how app images are referenced under clusters/.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`.
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLUSTERS = ROOT / "clusters"
APP_REF = re.compile(r"ghcr\.io/fjcloudaiconsulting/[^\s\"'#,\]}]+", re.I)
PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")

# K8S-03: app image refs without a digest, as (file under clusters/, ref). They predate INFRA-107, and a digest added
# by hand would need an attestation those builds lack. The next tbd release bump replaces each with
# `:vX.Y.Z@sha256:<digest>` (renovate.json pins digests on a version bump) and empties this list: the staging bump
# first, then the tbd-prod PR, which must delete the whole list (test_tag_only_allowlist_only_shrinks fails until it
# does). Never add an entry.
TAG_ONLY = {
    ("hetzner/tbd-prod/backend.yaml", "ghcr.io/fjcloudaiconsulting/tbd/migrations:v0.302.0"),
    ("hetzner/tbd-prod/backend.yaml", "ghcr.io/fjcloudaiconsulting/tbd/backend:v0.302.0"),
    ("hetzner/tbd-prod/frontend.yaml", "ghcr.io/fjcloudaiconsulting/tbd/frontend:v0.302.0"),
    ("hetzner/tbd-prod/scheduler.yaml", "ghcr.io/fjcloudaiconsulting/tbd/backend:v0.302.0"),
    ("hetzner/tbd-staging/backend.yaml", "ghcr.io/fjcloudaiconsulting/tbd/migrations:v0.302.0"),
    ("hetzner/tbd-staging/backend.yaml", "ghcr.io/fjcloudaiconsulting/tbd/backend:v0.302.0"),
    ("hetzner/tbd-staging/frontend.yaml", "ghcr.io/fjcloudaiconsulting/tbd/frontend:v0.302.0"),
    ("hetzner/tbd-staging/mcp.yaml", "ghcr.io/fjcloudaiconsulting/tbd/mcp:v0.302.0"),
}


def app_refs(root=CLUSTERS):
    """{(file under root, ref)} for every app image ref in any file under root, comments included."""
    out = set()
    for f in sorted(root.rglob("*")):
        if f.is_file():
            for ref in APP_REF.findall(f.read_bytes().decode("utf-8", "replace")):
                out.add((f.relative_to(root).as_posix(), ref))
    return out


def unpinned(refs, allowed):
    """Refs without a digest that the allowlist does not name."""
    return sorted(r for r in refs if not PINNED.search(r[1]) and r not in allowed)


def image(ref):
    return re.split(r"[:@]", ref, maxsplit=1)[0].lower()


def stale(refs, allowed):
    """Problems with the allowlist: an entry whose ref is gone and was not replaced by a pinned ref of the same image
    in the same file, or a list whose every entry has been pinned (it must then be deleted)."""
    out, pinned = [], 0
    for f, ref in sorted(allowed):
        if (f, ref) in refs:
            continue
        if any(rf == f and image(r) == image(ref) and PINNED.search(r) for rf, r in refs):
            pinned += 1
        else:
            out.append(f"{f}: {ref} is gone; delete its TAG_ONLY entry")
    if allowed and pinned == len(allowed):
        out.append("every TAG_ONLY ref is pinned now: delete TAG_ONLY")
    return out


class AppImagesPinnedByDigest(unittest.TestCase):
    """K8S-03: a GHCR tag can be re-pointed; a digest cannot."""

    def test_app_image_refs_carry_a_digest(self):
        refs = app_refs()
        self.assertGreater(len(refs), 10)
        self.assertEqual(unpinned(refs, TAG_ONLY), [], "add @sha256:<digest> (the allowlist only shrinks)")

    def test_tag_only_allowlist_only_shrinks(self):
        self.assertEqual(stale(app_refs(), TAG_ONLY), [])

    def test_fence_logic(self):
        tag = ("a/x.yaml", "ghcr.io/fjcloudaiconsulting/tbd/backend:v1.0.0")
        pin = ("a/x.yaml", "ghcr.io/fjcloudaiconsulting/tbd/backend:v1.1.0@sha256:" + "a" * 64)
        other = ("b/y.yaml", "ghcr.io/fjcloudaiconsulting/tbd/frontend:v1.0.0")
        # A tag-only ref outside the allowlist fails; a pinned one, or an allowlisted one, passes.
        self.assertEqual(unpinned({tag, pin}, set()), [tag])
        self.assertEqual(unpinned({tag, pin}, {tag}), [])
        self.assertEqual(unpinned({("a/x.yaml", pin[1][:-1])}, set()), [("a/x.yaml", pin[1][:-1])])
        # An entry whose ref vanished without a pinned replacement is stale (kills an allowlist that only grows).
        gone = [f"a/x.yaml: {tag[1]} is gone; delete its TAG_ONLY entry"]
        self.assertEqual(stale({other}, {tag, other}), gone)
        self.assertEqual(stale({("c/z.yaml", pin[1]), other}, {tag, other}), gone)  # pinned in another file
        # A release bump that pins one entry passes (Renovate's staging automerge cannot edit this file)...
        self.assertEqual(stale({pin, other}, {tag, other}), [])
        # ...until every entry is pinned: then the list must go.
        self.assertEqual(stale({pin}, {tag}), ["every TAG_ONLY ref is pinned now: delete TAG_ONLY"])
        self.assertEqual(stale(set(), set()), [])


class KubeconformSchemasPinned(unittest.TestCase):
    """GHA-09: kubeconform reads its schemas from commits named in ci.yml, so they change only with a repo change."""

    def locations(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        env = dict(re.findall(r"(?m)^ +([A-Z][A-Z0-9_]*): (\S+)$", ci))
        out = []
        for loc in re.findall(r"-schema-location\s+(\S+)", ci):
            loc = re.sub(r"\$\{?([A-Z][A-Z0-9_]*)\}?", lambda m: env.get(m.group(1), m.group(0)), loc.strip("\"'"))
            out.append(loc)
        return out

    def test_no_default_location(self):
        self.assertNotIn("default", self.locations())

    def test_every_location_is_pinned_to_a_commit(self):
        locs = self.locations()
        self.assertEqual(len(locs), 2, locs)
        for loc in locs:
            self.assertRegex(loc, r"^https://raw\.githubusercontent\.com/[\w.-]+/[\w.-]+/[0-9a-f]{40}/")
        # The same layout as kubeconform's default, at a fixed commit.
        self.assertTrue(any(re.fullmatch(
            r"https://raw\.githubusercontent\.com/yannh/kubernetes-json-schema/[0-9a-f]{40}/\{\{ ?\.NormalizedKubernetesVersion ?\}\}"
            r"-standalone\{\{ ?\.StrictSuffix ?\}\}/\{\{ ?\.ResourceKind ?\}\}\{\{ ?\.KindSuffix ?\}\}\.json", loc) for loc in locs), locs)
        self.assertTrue(any("/datreeio/CRDs-catalog/" in loc for loc in locs), locs)


# GHA-08: paths whose change reaches CI, cluster-admin, the backup chain or the hosts.
TIER0 = (".github/", "renovate.json", "clusters/hetzner/flux-system/", "clusters/hetzner/system-upgrade/",
         "clusters/hetzner/netbird/", "terraform/", "aws/", "ansible/", ".sops.yaml")
OWNER = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:/[A-Za-z0-9._-]+)?|[^@\s]+@[^@\s]+\.[a-z]+")


def codeowners_rules(text):
    """[(pattern, owners)] in file order; raises on syntax GitHub's CODEOWNERS does not accept."""
    rules = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split(" #")[0].strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        if pattern.startswith("!") or "[" in pattern or "\\" in pattern:
            raise ValueError(f"line {n}: negation, character ranges and escapes are not supported: {pattern}")
        bad = [o for o in owners if not OWNER.fullmatch(o)]
        if not owners or bad:
            raise ValueError(f"line {n}: not an owner: {bad or 'none'}")
        rules.append((pattern, owners))
    return rules


def owners_of(rules, path):
    """Owners of a repo path by the last matching rule (anchored patterns only: `/dir/` or `/file`)."""
    found = []
    for pattern, owners in rules:
        if not pattern.startswith("/") or "*" in pattern:
            raise ValueError(f"use an anchored literal pattern here: {pattern}")
        p = pattern[1:]
        if path == p or (p.endswith("/") and path.startswith(p)):
            found = owners
    return found


class CodeOwners(unittest.TestCase):
    """GHA-08: the owner reviews every change to a tier-0 path once the main ruleset requires code-owner review."""

    def test_tier0_paths_are_owned(self):
        rules = codeowners_rules((ROOT / ".github/CODEOWNERS").read_text())
        for path in TIER0:
            with self.subTest(path):
                self.assertTrue((ROOT / path).exists(), f"{path} no longer exists: update TIER0 and CODEOWNERS")
                probe = path + "x" if path.endswith("/") else path
                self.assertEqual(owners_of(rules, probe), ["@flamarion"])

    def test_parser_rejects_what_github_ignores(self):
        for line in ("!/x/ @flamarion", "/x/[ab] @flamarion", "/x/", "/x/ flamarion"):
            with self.subTest(line):
                with self.assertRaises(ValueError):
                    codeowners_rules(line)
        rules = codeowners_rules("# c\n/a/ @flamarion\n/a/b @org/team  # note\n")
        self.assertEqual(owners_of(rules, "a/x"), ["@flamarion"])
        self.assertEqual(owners_of(rules, "a/b"), ["@org/team"])  # the last match wins
        self.assertEqual(owners_of(rules, "b"), [])


if __name__ == "__main__":
    unittest.main()
