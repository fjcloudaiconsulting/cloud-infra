"""Prod image tags must not be newer than staging's (INFRA-89). Stdlib only."""
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / ".github/scripts/check-prod-tags.py"


def img(repo, name, tag):
    return f"          image: ghcr.io/fjcloudaiconsulting/{repo}/{name}:{tag}\n"


def run(layout):
    """layout: {dir under the clusters root: {file: text}}."""
    with tempfile.TemporaryDirectory() as d:
        for rel, files in layout.items():
            (pathlib.Path(d) / rel).mkdir(parents=True)
            for n, t in files.items():
                (pathlib.Path(d) / rel / n).write_text(t)
        return subprocess.run(["python3", "-I", str(SCRIPT), d], capture_output=True, text=True)


def tbd(prod, stg):
    return {"p/tbd-prod": {"a.yaml": "".join(img("tbd", n, t) for n, t in prod)},
            "p/tbd-staging": {"a.yaml": "".join(img("tbd", n, t) for n, t in stg)}}


class ProdTags(unittest.TestCase):
    def test_staging_in_another_cluster_tree_passes(self):
        # Migration: prod in one tree, staging in another (INFRA-166).
        r = run({"platform/tbd-prod": {"a.yaml": img("tbd", "backend", "v0.2.0")},
                 "hetzner/tbd-staging": {"a.yaml": img("tbd", "backend", "v0.2.0")}})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_staging_name_must_match_exactly(self):
        # xtbd-staging is not tbd's staging (kills a suffix match).
        r = run({"platform/tbd-prod": {"a.yaml": img("tbd", "backend", "v0.2.0")},
                 "hetzner/xtbd-staging": {"a.yaml": img("tbd", "backend", "v0.2.0")}})
        self.assertEqual(r.returncode, 1)
        self.assertIn("tbd-staging does not exist", r.stderr)

    def test_staging_in_two_trees_bounds_prod_by_the_lowest(self):
        # Either tree may hold the lower tag (kills taking only the first or only the last).
        for lo, hi in (("platform", "hetzner"), ("hetzner", "platform")):
            r = run({"a/tbd-prod": {"a.yaml": img("tbd", "backend", "v0.3.0")},
                     f"{lo}/tbd-staging": {"a.yaml": img("tbd", "backend", "v0.2.0")},
                     f"{hi}/tbd-staging": {"a.yaml": img("tbd", "backend", "v0.3.0")}})
            self.assertEqual(r.returncode, 1, (lo, r.stderr))

    def test_staging_missing_in_all_trees_fails(self):
        r = run({"platform/tbd-prod": {"a.yaml": img("tbd", "backend", "v0.2.0")},
                 "hetzner/ziftbook-staging": {"a.yaml": img("ziftbook", "backend", "v0.2.0")}})
        self.assertEqual(r.returncode, 1)
        self.assertIn("tbd-staging does not exist", r.stderr)

    def test_equal_passes(self):
        self.assertEqual(run(tbd([("backend", "v0.2.0")], [("backend", "v0.2.0")])).returncode, 0)

    def test_prod_ahead_fails(self):
        r = run(tbd([("backend", "v0.3.0")], [("backend", "v0.2.0")]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("backend", r.stderr)

    def test_version_order_is_numeric(self):
        # v0.10.0 is newer than v0.9.0 (kills string comparison).
        self.assertEqual(run(tbd([("backend", "v0.10.0")], [("backend", "v0.9.0")])).returncode, 1)
        self.assertEqual(run(tbd([("backend", "v0.9.0")], [("backend", "v0.10.0")])).returncode, 0)

    def test_prod_older_passes_for_rollback(self):
        self.assertEqual(run(tbd([("backend", "v0.1.0")], [("backend", "v0.2.0")])).returncode, 0)

    def test_lagging_staging_ref_bounds_prod(self):
        # staging runs backend v0.2.0 in one manifest and v0.3.0 in another: v0.3.0 is not safe (kills "max").
        r = run(tbd([("backend", "v0.3.0")], [("backend", "v0.2.0"), ("backend", "v0.3.0")]))
        self.assertEqual(r.returncode, 1)

    def test_lagging_staging_image_bounds_whole_repo(self):
        # staging frontend lags: prod backend v0.3.0 is not a fully staged release (kills per-image comparison).
        r = run(tbd([("backend", "v0.3.0")], [("backend", "v0.3.0"), ("frontend", "v0.2.0")]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("backend", r.stderr)

    def test_image_missing_in_staging_fails(self):
        r = run(tbd([("backend", "v0.2.0"), ("scheduler", "v0.2.0")], [("backend", "v0.2.0")]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("scheduler", r.stderr)

    def test_prod_without_staging_dir_fails(self):
        # Fail closed: a new <app>-prod must come with its staging (kills "skip apps without staging").
        r = run({"p/zift-prod": {"a.yaml": img("zift", "api", "v1.0.0")}})
        self.assertEqual(r.returncode, 1)
        self.assertIn("zift-staging", r.stderr)

    def test_any_app_is_covered(self):
        # No app name is hard-coded: a second app is checked without a code change.
        layout = tbd([("backend", "v0.2.0")], [("backend", "v0.2.0")])
        layout["p/zift-prod"] = {"a.yaml": img("zift", "api", "v1.1.0")}
        layout["p/zift-staging"] = {"a.yaml": img("zift", "api", "v1.0.0")}
        r = run(layout)
        self.assertEqual(r.returncode, 1)
        self.assertIn("zift/api", r.stderr)

    def test_lowest_staging_tag_is_numeric(self):
        # staging v0.10.0 and v0.9.0: the lowest is v0.9.0, so prod v0.10.0 fails (kills string min).
        r = run(tbd([("backend", "v0.10.0")], [("backend", "v0.10.0"), ("frontend", "v0.9.0")]))
        self.assertEqual(r.returncode, 1)

    def test_non_semver_prod_tag_fails(self):
        # One good image beside the bad one, so only the tag check can fail it.
        r = run(tbd([("backend", "v0.2.0"), ("frontend", "latest")], [("backend", "v0.2.0"), ("frontend", "v0.2.0")]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a plain", r.stderr)

    def test_non_semver_staging_tag_fails(self):
        r = run(tbd([("backend", "v0.2.0")], [("backend", "v0.2.0"), ("frontend", "sha-abc1234")]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a plain", r.stderr)

    def test_yml_and_nested_files_are_scanned(self):
        # Flux and Renovate also take .yml, and subdirs (kills "*.yaml only" and glob instead of rglob).
        for rel, name in (("p/tbd-prod", "a.yml"), ("p/tbd-prod/sub", "a.yaml")):
            layout = tbd([("backend", "v0.2.0")], [("backend", "v0.2.0")])
            layout[rel] = {**layout.get(rel, {}), name: img("tbd", "backend", "v0.9.0")}
            self.assertEqual(run(layout).returncode, 1, rel + name)

    def test_digest_pin_is_stripped_before_comparing(self):
        # v0.2.0@sha256:<64 hex> compares as v0.2.0 on either side (INFRA-107).
        pin = "@sha256:" + "a" * 64
        self.assertEqual(run(tbd([("backend", "v0.2.0" + pin)], [("backend", "v0.2.0" + pin)])).returncode, 0)
        self.assertEqual(run(tbd([("backend", "v0.2.0" + pin)], [("backend", "v0.2.0")])).returncode, 0)
        r = run(tbd([("backend", "v0.3.0" + pin)], [("backend", "v0.2.0" + pin)]))
        self.assertEqual(r.returncode, 1, r.stderr)

    def test_untagged_digest_and_prerelease_prod_refs_fail(self):
        # Kills a missing-tag skip and a SEMVER without `$` (v0.2.0-rc.1, v0.2.0@sha256 read as v0.2.0).
        for ref in ("ghcr.io/fjcloudaiconsulting/tbd/backend\n", "ghcr.io/fjcloudaiconsulting/tbd/backend:v0.2.0-rc.1\n",
                    "ghcr.io/fjcloudaiconsulting/tbd/backend:v0.2.0@sha256:abc\n"):
            layout = tbd([("backend", "v0.2.0")], [("backend", "v0.2.0")])
            layout["p/tbd-prod"]["b.yaml"] = "image: " + ref
            self.assertEqual(run(layout).returncode, 1, ref)

    def test_other_repos_in_staging_do_not_bound_prod(self):
        # Kills dropping the repo filter from the lowest-tag computation.
        layout = tbd([("backend", "v0.3.0")], [("backend", "v0.3.0")])
        layout["p/tbd-staging"]["b.yaml"] = img("other", "x", "v0.1.0")
        self.assertEqual(run(layout).returncode, 0)

    def test_no_prod_dir_is_an_error(self):
        # Never a silent pass if the layout changes under the check.
        self.assertEqual(run({"p/tbd-staging": {"a.yaml": img("tbd", "backend", "v0.2.0")}}).returncode, 1)

    def test_prod_dir_without_images_fails(self):
        r = run({"p/tbd-prod": {"a.yaml": "kind: Service\n"}, "p/tbd-staging": {"a.yaml": img("tbd", "backend", "v0.2.0")}})
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
