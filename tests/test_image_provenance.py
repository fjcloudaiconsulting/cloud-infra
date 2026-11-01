"""Image provenance check: gh stubbed, real git (INFRA-107). Stdlib only."""
import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / ".github/scripts/check-image-provenance.sh"
SAN = "https://github.com/fjcloudaiconsulting/.github/.github/workflows/build-image.yml@refs/tags/v1"
D1, D2 = "a" * 64, "b" * 64
TAG_COMMIT = "1" * 40  # what `gh api .../commits/refs/tags/vX` returns
# `gh api` answers only the exact tag path of the tags a test sets up, so a bare tag or `main` lookup fails.
# The stub plays the attestation: made on ATT_REF at ATT_COMMIT. Like gh it fails when a given flag disagrees,
# and, like gh, an omitted flag is not checked.
STUB = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
if [[ "$1" == api ]]; then
  [[ -z "${API_FAIL:-}" ]] && grep -qxF "$2" <<<"$API_PATHS" || exit 1
  [[ "$3 $4" == "--jq .sha" ]] || exit 2
  echo "$TAG_COMMIT"; exit 0
fi
[[ "$1 $2" == "attestation verify" ]] || exit 2
shift 2
while (($#)); do
  case "$1" in
    --source-ref) ref="$2"; shift ;;
    --source-digest) dig="$2"; shift ;;
  esac
  shift
done
[[ -z "${ref:-}" || "$ref" == "$ATT_REF" ]] && [[ -z "${dig:-}" || "$dig" == "$ATT_COMMIT" ]] || { echo "no matching attestation" >&2; exit 1; }
"""


def ref(tag="v1.0.0", digest=D1, img="backend", repo="tbd"):
    return f"ghcr.io/fjcloudaiconsulting/{repo}/{img}:{tag}" + (f"@sha256:{digest}" if digest else "")


def git(d, *a):
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)


def run(base_files, head_files, *, att_ref="refs/heads/main", att_commit=TAG_COMMIT,
        tags=(("tbd", "v1.0.0"), ("ziftbook", "v1.0.0")), api_fail=False):
    """base_files/head_files: {path: image ref, or a whole file when it holds a newline} under clusters/ in the base
    and head commits."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d) / "r"
        (d / "clusters").mkdir(parents=True)
        git(d, "init", "-q")
        for files, msg in ((base_files, "base"), (head_files, "head")):
            for n, r in files.items():
                (d / "clusters" / n).parent.mkdir(parents=True, exist_ok=True)
                (d / "clusters" / n).write_text(r if "\n" in r else f"image: {r}\n")
            git(d, "add", "clusters")
            git(d, "commit", "-q", "-m", msg)
            if msg == "base":
                git(d, "tag", "base")
        bin_ = d.parent / "bin"
        bin_.mkdir()
        (bin_ / "gh").write_text(STUB)
        (bin_ / "gh").chmod(0o755)
        r = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, cwd=d,
            env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "GH_LOG": str(d.parent / "log"), "BASE": "base",
                 "TAG_COMMIT": TAG_COMMIT, "ATT_REF": att_ref, "ATT_COMMIT": att_commit,
                 "API_PATHS": "\n".join(f"repos/fjcloudaiconsulting/{r}/commits/refs/tags/{t}" for r, t in tags),
                 "API_FAIL": "1" if api_fail else ""},
        )
        log = (d.parent / "log").read_text() if (d.parent / "log").exists() else ""
        return r, log


class Provenance(unittest.TestCase):
    def test_valid_passes_and_verify_pins_identity(self):
        r, log = run({"a.yaml": ref(digest=""), "b.yaml": ref(digest="", repo="ziftbook", img="web")},
                     {"a.yaml": ref(), "b.yaml": ref(repo="ziftbook", img="web", digest=D2)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verified tbd/backend v1.0.0", r.stdout)
        self.assertIn("--repo fjcloudaiconsulting/tbd ", log)
        self.assertIn("--repo fjcloudaiconsulting/ziftbook ", log)
        self.assertIn(f"--cert-identity {SAN}", log)
        self.assertIn("--deny-self-hosted-runners", log)
        self.assertIn(f"oci://ghcr.io/fjcloudaiconsulting/tbd/backend@sha256:{D1}", log)

    def test_added_tag_only_ref_fails(self):
        r, log = run({"a.yaml": ref()}, {"a.yaml": ref("v1.1.0", "")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must be :vX.Y.Z@sha256", r.stderr)

    def test_every_added_ref_is_checked_not_only_the_first(self):
        r, _ = run({"a.yaml": ref(digest=""), "b.yaml": ref(img="frontend", digest="")},
                   {"a.yaml": ref(), "b.yaml": ref("v1.1.0", "", "frontend")})
        self.assertNotEqual(r.returncode, 0)

    def test_failed_tag_lookup_fails_and_never_verifies(self):
        r, log = run({"a.yaml": ref(digest="")}, {"a.yaml": ref()}, api_fail=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("attestation verify", log)

    def test_malformed_refs_fail(self):
        bad = [
            ref("v1.0.0-rc1"),
            ref(digest="A" * 64),
            ref(digest="a" * 63),
            ref() + "x",
            f"ghcr.io/fjcloudaiconsulting/tbd/backend@sha256:{D1}",
            ref().replace("ghcr.io", "GHCR.IO"),
            ref().replace("ghcr.io", "ghcr.io:443"),
            f"ghcr.io/fjcloudaiconsulting/x:v1.0.0",
        ]
        for b in bad:
            with self.subTest(ref=b):
                r, log = run({"a.yaml": ref(digest="")}, {"a.yaml": b})
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn("attestation verify", log)

    def test_branch_attestation_fails_on_source_ref(self):
        r, _ = run({"a.yaml": ref(digest="")}, {"a.yaml": ref()}, att_ref="refs/heads/evil")
        self.assertNotEqual(r.returncode, 0)

    def test_attestation_at_another_commit_fails_on_source_digest(self):
        r, _ = run({"a.yaml": ref(digest="")}, {"a.yaml": ref()}, att_commit="2" * 40)
        self.assertNotEqual(r.returncode, 0)

    def test_unchanged_ref_is_skipped(self):
        # A legacy tag-only ref untouched by the diff must not block, and gh is never called.
        legacy = ref("v0.9.0", "", "mcp")
        r, log = run({"a.yaml": legacy, "b.yaml": ref(digest="")}, {"a.yaml": legacy, "b.yaml": ref()})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("mcp", log)
        r, log = run({"a.yaml": legacy}, {"a.yaml": legacy, "b.yaml": "other: 1"})
        self.assertEqual((r.returncode, log), (0, ""))
        self.assertIn("no added app image refs", r.stdout)


def deployment(image, ns="tbd-staging"):
    return (f"apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: backend\n  namespace: {ns}\nspec:\n"
            f"  template:\n    spec:\n      containers:\n        - name: backend\n          image: {image}\n")


class CanonicalForm(unittest.TestCase):
    """GHA-03: shapes kustomize renders as an app image that the diff grep above does not see. Each one passes the
    check without manifest_fence.py, and with it fails before any lookup."""

    def assertFails(self, head, needle):
        base = {"hetzner/tbd-staging/backend.yaml": deployment(ref(digest=""))}
        r, log = run(base, {**base, **head})
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(needle, r.stderr)
        self.assertEqual(log, "")

    def test_escaped_ref_fails(self):
        self.assertFails({"hetzner/tbd-staging/backend.yaml": deployment(
            '"ghcr.io\\/fjcloudaiconsulting/tbd/backend:v1.1.0"')}, "escape in a quoted `image` value")

    def test_foreign_registry_in_an_app_namespace_fails(self):
        self.assertFails({"hetzner/tbd-staging/backend.yaml": deployment("registry.example/tbd/backend:v1.1.0")},
                         "image in tbd-staging must come from ghcr.io/fjcloudaiconsulting/tbd/")

    def test_kustomize_image_override_fails(self):
        self.assertFails({"hetzner/tbd-staging/kustomization.yaml":
                          "resources:\n  - backend.yaml\nimages:\n  - name: .*/backend\n"
                          "    newName: registry.example/tbd/backend\n"},
                         "kustomization key `images` outside flux-system and system-upgrade")

    def test_flux_image_override_outside_flux_system_fails(self):
        self.assertFails({"hetzner/tbd-staging/sync.yaml":
                          "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\nmetadata:\n  name: x\n"
                          "spec:\n  path: ./clusters/hetzner/tbd-staging\n  images:\n"
                          "    - name: .*/backend\n      newName: registry.example/x\n"},
                         "Flux objects live under flux-system")

    def test_api_version_and_kind_are_one_line_scalars(self):
        # A Flux Kustomization whose apiVersion is a block scalar, outside flux-system.
        flux = ("apiVersion: >-\n  kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\nmetadata:\n  name: x\n"
                "spec:\n  path: ./clusters/hetzner/tbd-staging\n  images:\n    - name: .*/backend\n"
                "      newName: registry.example/x\n")
        self.assertFails({"hetzner/data/sync.yaml": flux}, "`apiVersion` must be a one-line scalar")
        self.assertFails({"hetzner/data/x.yaml": "apiVersion: v1\nkind:\n  x: Secret\n"},
                         "`kind` must be a one-line scalar")

    def test_document_root_is_a_block_mapping(self):
        base = {"hetzner/tbd-prod/backend.yaml": deployment(ref(digest=""), "tbd-prod")}
        r, log = run(base, {**base, "hetzner/tbd-prod/kustomization.yaml": "{resources: [backend.yaml, job.txt]}\n",
                            "hetzner/tbd-prod/job.txt": "kind: Job\n"})
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a document must be a block mapping", r.stderr)
        self.assertEqual(log, "")
        self.assertFails({"hetzner/data/x.yaml": "- kind: Secret\n"}, "a document must be a block mapping")

    def test_vendored_exemption_is_only_directly_under_a_cluster(self):
        patches = "resources:\n  - plans.yaml\npatches:\n  - path: p.yaml\n"
        self.assertFails({"hetzner/data/system-upgrade/kustomization.yaml": patches,
                          "hetzner/data/system-upgrade/plans.yaml": "kind: ConfigMap\n"},
                         "kustomization key `patches` outside flux-system and system-upgrade")
        self.assertFails({"hetzner/data/flux-system/source.yaml":
                          "apiVersion: source.toolkit.fluxcd.io/v1\nkind: GitRepository\nmetadata:\n  name: x\n"},
                         "Flux objects live under flux-system")

    def test_override_in_a_vendored_dir_needs_a_digest(self):
        pinned = "images:\n  - name: rancher/system-upgrade-controller\n    newTag: v0.20.2\n"
        base = {"a.yaml": "other: 1"}
        r, log = run(base, {"hetzner/system-upgrade/kustomization.yaml": pinned + f"    digest: sha256:{D1}\n"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no added app image refs", r.stdout)
        r, log = run(base, {"hetzner/system-upgrade/kustomization.yaml": pinned})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("images entry without `digest: sha256:<64 hex>`", r.stderr)

    def test_repo_clusters_pass(self):
        # guard: the committed tree passes the fence this check runs first.
        r = subprocess.run(["python3", "-I", str(SCRIPT.parent / "manifest_fence.py"), "clusters"], capture_output=True,
                           text=True, cwd=SCRIPT.parent.parent.parent)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_renovate_bump_in_a_manifest_passes(self):
        # guard: the staging automerge shape, a Deployment whose image moves to :vX.Y.Z@sha256:<digest>.
        r, log = run({"hetzner/tbd-staging/backend.yaml": deployment(ref("v0.9.0", ""))},
                     {"hetzner/tbd-staging/backend.yaml": deployment(ref())})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verified tbd/backend v1.0.0", r.stdout)


if __name__ == "__main__":
    unittest.main()
