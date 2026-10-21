"""Image provenance check: gh stubbed, real git (INFRA-107). Stdlib only."""
import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / ".github/scripts/check-image-provenance.sh"
SAN = "https://github.com/fjcloudaiconsulting/.github/.github/workflows/build-image.yml@refs/tags/v1"
D1, D2 = "a" * 64, "b" * 64
TAG_COMMIT = "1" * 40  # what `gh api .../commits/vX` returns
# The stub plays the attestation: made on ATT_REF at ATT_COMMIT. Like gh it fails when a flag disagrees.
STUB = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
if [[ "$1" == api ]]; then echo "$TAG_COMMIT"; exit 0; fi
[[ "$1 $2" == "attestation verify" ]] || exit 2
shift 2
while (($#)); do
  case "$1" in
    --source-ref) ref="$2"; shift ;;
    --source-digest) dig="$2"; shift ;;
  esac
  shift
done
[[ "${ref:-}" == "$ATT_REF" && "${dig:-}" == "$ATT_COMMIT" ]] || { echo "no matching attestation" >&2; exit 1; }
"""


def ref(tag="v1.0.0", digest=D1, img="backend"):
    return f"ghcr.io/fjcloudaiconsulting/tbd/{img}:{tag}" + (f"@sha256:{digest}" if digest else "")


def git(d, *a):
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)


def run(base_files, head_files, *, att_ref="refs/heads/main", att_commit=TAG_COMMIT):
    """base_files/head_files: {name: image ref} written under clusters/ in the base and head commits."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d) / "r"
        (d / "clusters").mkdir(parents=True)
        git(d, "init", "-q")
        for files, msg in ((base_files, "base"), (head_files, "head")):
            for n, r in files.items():
                (d / "clusters" / n).write_text(f"image: {r}\n")
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
                 "TAG_COMMIT": TAG_COMMIT, "ATT_REF": att_ref, "ATT_COMMIT": att_commit},
        )
        log = (d.parent / "log").read_text() if (d.parent / "log").exists() else ""
        return r, log


class Provenance(unittest.TestCase):
    def test_valid_passes_and_verify_pins_identity(self):
        r, log = run({"a.yaml": ref(digest="")}, {"a.yaml": ref()})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verified tbd/backend v1.0.0", r.stdout)
        self.assertIn(f"--cert-identity {SAN}", log)
        self.assertIn("--deny-self-hosted-runners", log)
        self.assertIn(f"oci://ghcr.io/fjcloudaiconsulting/tbd/backend@sha256:{D1}", log)

    def test_added_tag_only_ref_fails(self):
        r, log = run({"a.yaml": ref()}, {"a.yaml": ref("v1.1.0", "")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must be :vX.Y.Z@sha256", r.stderr)

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


if __name__ == "__main__":
    unittest.main()
