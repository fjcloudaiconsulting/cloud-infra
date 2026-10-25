"""Fences on the restore drill (DATA-05): clusters/hetzner/data/RESTORE.md steps 1-5.

The drill must replay the backups into the database images production runs. RESTORE.md used to pin its own MySQL and
Postgres digests, which drifted from the manifests Renovate bumps. Step 2 now reads them from the `data` StatefulSets
(or, on a cluster without them, from their manifests), so no image bump ever has to touch RESTORE.md.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`.
"""
import os
import re
import subprocess
import unittest

from test_tbd_manifests import ROOT, containers, load_all

DATA = ROOT / "clusters" / "hetzner" / "data"
RESTORE = DATA / "RESTORE.md"
# The step 2 variable that carries each StatefulSet's image, by engine.
ENGINE_VARS = {"mysql": "MYSQL_IMAGE", "postgres": "PG_IMAGE"}
# A mysql or postgres image ref by tag or digest, with or without a registry path ("postgres://" is not one).
PINNED_REF = re.compile(r"(?<![\w.-])(?:[\w.-]+/)*(?:mysql|postgres)(?::\w[\w.-]*|@sha256:)")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def bash_blocks(text):
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def statefulset_image(name):
    """What step 2's jsonpath returns: the image of container `name` in StatefulSet `name` (data/<name>.yaml)."""
    images = [
        c.get("image")
        for d in load_all(DATA / f"{name}.yaml")
        if d.get("kind") == "StatefulSet" and d["metadata"]["name"] == name
        for c in containers(d, init=False)
        if c.get("name") == name
    ]
    if len(images) != 1:
        raise AssertionError(f"expected one container {name} in StatefulSet {name} in data/{name}.yaml, got {images}")
    return images[0]


class DrillTakesTheManifestImages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RESTORE.read_text()
        # The step 2 block: the one that creates the scratch servers.
        blocks = [b for b in bash_blocks(cls.text) if "kind: Pod" in b]
        cls.step2 = blocks[0] if len(blocks) == 1 else None

    def test_restore_md_pins_no_database_image(self):
        # Kills: a hard-coded ref that Renovate never bumps (the drift this replaces).
        self.assertEqual([], PINNED_REF.findall(self.text), "RESTORE.md pins a mysql/postgres image")
        self.assertEqual([], DIGEST.findall(self.text), "RESTORE.md carries an image digest")

    def test_every_drill_image_is_a_guarded_variable(self):
        self.assertIsNotNone(self.step2, "expected one bash block that creates the scratch Pods")
        images = re.findall(r"^\s*image:\s*(.+?)\s*$", self.step2, re.M)
        self.assertEqual(len(ENGINE_VARS), len(images), f"expected one image per engine, got {images}")
        allowed = {f'"${{{v}:?}}"' for v in ENGINE_VARS.values()}
        for value in images:
            # `:?` makes the shell refuse the apply when the lookup came back empty, rather than create a broken Pod.
            self.assertIn(value, allowed, f"drill image is not a variable from the StatefulSet lookup: {value}")
        self.assertEqual(allowed, set(images), "each engine's image must be used once")

    def test_each_variable_is_read_from_its_statefulset(self):
        self.assertIsNotNone(self.step2)
        lines = [ln for ln in self.step2.splitlines() if not ln.lstrip().startswith("#")]
        live = [ln for ln in lines if re.match(r"\s*img\(\)", ln)]
        self.assertEqual(1, len(live), f"expected one active img() definition, got {live}")
        # The running StatefulSet's pod template: what Flux applied from the manifest, not a copy of it.
        self.assertIn("kubectl -n data get", live[0])
        self.assertIn('jsonpath={.spec.template.spec.containers[?(@.name==\\"$1\\")].image}', live[0])
        self.assertRegex(live[0], r'statefulset "\$1"; }$')
        for engine, var in ENGINE_VARS.items():
            self.assertRegex(self.step2, rf"(?m)(^|\s){var}=\$\(img {engine}\)(\s|$)", f"{var} is not img {engine}")

    def test_the_lookup_names_resolve_in_the_manifests(self):
        # img <engine> reads container <engine> of StatefulSet <engine>: a rename in the manifest must fail here,
        # not as an empty image during a drill. Both resolve to a digest-pinned ref of their engine.
        for engine in ENGINE_VARS:
            with self.subTest(engine=engine):
                self.assertRegex(statefulset_image(engine), rf"^{engine}:[\w.-]+@sha256:[0-9a-f]{{64}}$")

    def test_the_pod_heredoc_expands_the_variables(self):
        # A quoted delimiter (<<'EOF', <<"EOF", <<\EOF) would hand the literal "${MYSQL_IMAGE:?}" to the cluster.
        self.assertIsNotNone(self.step2)
        opener = re.search(r"kubectl apply [^\n]*<<\s*(\S+)\s*$", self.step2, re.M)
        self.assertIsNotNone(opener, "the scratch servers are not applied from a heredoc")
        self.assertRegex(opener.group(1), r"^[A-Za-z_]+$", "the Pod heredoc delimiter is quoted")

    def test_the_off_cluster_lookup_reads_the_same_refs_from_the_manifests(self):
        # The commented alternative, run as written against this checkout, must print what the StatefulSets carry.
        self.assertIsNotNone(self.step2)
        alt = [ln.lstrip()[1:].strip() for ln in self.step2.splitlines() if re.match(r"\s*#\s*img\(\)", ln)]
        self.assertEqual(1, len(alt), f"expected one commented img() for clusters without the data StatefulSets: {alt}")
        self.assertIn('"$REPO/clusters/hetzner/data/$1.yaml"', alt[0])
        for engine in ENGINE_VARS:
            with self.subTest(engine=engine):
                r = subprocess.run(["bash", "-c", f'{alt[0]}\nimg "$1"', "bash", engine], capture_output=True,
                                   text=True, env={**os.environ, "REPO": str(ROOT)})
                self.assertEqual(0, r.returncode, r.stderr)
                self.assertEqual(statefulset_image(engine), r.stdout.strip())


if __name__ == "__main__":
    unittest.main()
