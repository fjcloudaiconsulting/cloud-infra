"""The render check reads the objects Flux applies, not the files as written (GHA-03).

Stdlib only. The tests that render need kustomize ($KUSTOMIZE, or kustomize or kubectl on PATH). Without it they skip,
unless RENDER_CHECK_REQUIRED is set, as in CI's Kubernetes job, which installs the pinned kustomize: there they fail.
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github/scripts/check-rendered-manifests.py"
_spec = importlib.util.spec_from_file_location("check_rendered_manifests", SCRIPT)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

DIGEST = "@sha256:" + "a" * 64
ENC = "ENC[AES256_GCM,data:bWFj,iv:aXY=,tag:dGFn,type:str]"
PINNED = "ghcr.io/fjcloudaiconsulting/tbd/backend:v1.0.0" + DIGEST
SOPS = f"""\
sops:
  age:
    - recipient: age1example
      enc: |
        -----BEGIN AGE ENCRYPTED FILE-----
        -----END AGE ENCRYPTED FILE-----
  encrypted_regex: ^(data|stringData)$
  mac: {ENC}
  version: 3.13.3
"""
SYNC = """\
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: flux-system
  namespace: flux-system
spec:
  interval: 1m0s
  ref:
    branch: main
  url: https://github.com/example/repo
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: flux-system
  namespace: flux-system
spec:
  interval: 10m0s
  path: {path}
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
{extra}"""


def deployment(image, name="w", namespace=None, env=""):
    ns = f"\n  namespace: {namespace}" if namespace else ""
    return f"""\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {name}{ns}
spec:
  selector:
    matchLabels:
      app: {name}
  template:
    metadata:
      labels:
        app: {name}
    spec:
      containers:
        - name: c
          image: {image}{env}
"""


def secret(body, name="s"):
    return f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: {name}\n{body}"


def kustomize_needed(cls):
    if rc.kustomize_command() or os.environ.get("RENDER_CHECK_REQUIRED"):
        return cls  # with RENDER_CHECK_REQUIRED and no kustomize, the renders fail and so do the tests
    return unittest.skip("no kustomize: set KUSTOMIZE, or put kustomize or kubectl on PATH (CI installs it)")(cls)


class Tree:
    """A clusters/ root in a temp dir: clusters/c/flux-system (a Kustomization applying `path`) plus files."""

    def __init__(self, files, path="./clusters/c", extra=""):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name, "clusters")
        files = {"c/flux-system/kustomization.yaml": "resources:\n  - gotk-sync.yaml\n",
                 "c/flux-system/gotk-sync.yaml": SYNC.format(path=path, extra=textwrap.indent(extra, "  ")),
                 **files}
        for name, body in files.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)

    def __enter__(self):
        return self.root

    def __exit__(self, *exc):
        self.tmp.cleanup()


def problems(files, exempt=frozenset(), **kw):
    with Tree(files, **kw) as root:
        return rc.check(root, exempt=set(exempt))


class Base(unittest.TestCase):
    def assertFails(self, files, needle, **kw):
        out = problems(files, **kw)
        self.assertTrue(any(needle in p for p in out), f"{needle!r} not in {out}")
        return out

    def assertPasses(self, files, **kw):
        self.assertEqual(problems(files, **kw), [])


@kustomize_needed
class BypassShapesFail(Base):
    """Source shapes whose rendered object differs from their text (GHA-03): each must fail."""

    def test_tagged_kind_secret(self):
        self.assertFails({"c/app/s.yaml": "apiVersion: v1\nkind: !!str Secret\nmetadata:\n  name: s\n"
                                          "stringData:\n  password: plaintext\n"}, "Secret s: no sops metadata")

    def test_flow_style_secret(self):
        flow = "{apiVersion: v1, kind: Secret, metadata: {name: s}, data: {k: cGxhaW4=}}\n"
        self.assertFails({"c/app/s.yaml": flow}, "Secret s: no sops metadata")

    def test_quoted_and_escaped_kind(self):
        for kind in ('"Secret"', "'Secret'", r'"Secr\x65t"', r'"Secr\u0065t"'):
            with self.subTest(kind):
                self.assertFails({"c/app/s.yaml": f"apiVersion: v1\nkind: {kind}\nmetadata:\n  name: s\n"
                                                  "stringData:\n  k: v\n"}, "Secret s: no sops metadata")

    def test_list_wrapped_secret(self):
        self.assertFails({"c/app/s.yaml": "apiVersion: v1\nkind: List\nitems:\n  - apiVersion: v1\n    kind: Secret\n"
                                          "    metadata:\n      name: s\n    stringData:\n      k: v\n"},
                         "Secret s: no sops metadata")

    def test_secret_generator(self):
        out = self.assertFails({"c/gen/kustomization.yaml": "secretGenerator:\n  - name: s\n    literals:\n"
                                                            "      - password=plaintext\n"}, "no sops metadata")
        self.assertTrue(any("c/gen/kustomization.yaml: Secret s-" in p for p in out), out)

    def test_plaintext_value_in_an_encrypted_secret(self):
        self.assertFails({"c/app/s.secret.yaml": secret(f"data:\n  a: {ENC}\n  b: cGxhaW4=\n" + SOPS)},
                         "data.b is not encrypted")

    def test_patch_adds_plaintext_to_an_encrypted_secret(self):
        self.assertFails({"c/app/kustomization.yaml": "resources:\n  - s.secret.yaml\npatches:\n  - patch: |\n"
                                                      "      apiVersion: v1\n      kind: Secret\n      metadata:\n"
                                                      "        name: s\n      stringData:\n        k: plaintext\n",
                          "c/app/s.secret.yaml": secret(f"data:\n  a: {ENC}\n" + SOPS)},
                         "stringData.k is not encrypted")

    def test_images_override_swaps_a_pinned_image_for_a_tag(self):
        self.assertFails({"c/app/kustomization.yaml": "resources:\n  - d.yaml\nimages:\n"
                                                      "  - name: ghcr.io/fjcloudaiconsulting/tbd/backend\n"
                                                      "    newTag: v2.0.0\n",
                          "c/app/d.yaml": deployment(PINNED)},
                         "ghcr.io/fjcloudaiconsulting/tbd/backend:v2.0.0 is not pinned by @sha256")

    def test_patch_swaps_an_image(self):
        self.assertFails({"c/app/kustomization.yaml": "resources:\n  - d.yaml\npatches:\n  - target:\n"
                                                      "      kind: Deployment\n    patch: |\n      - op: replace\n"
                                                      "        path: /spec/template/spec/containers/0/image\n"
                                                      "        value: quay.io/x/y:latest\n",
                          "c/app/d.yaml": deployment(PINNED)}, "quay.io/x/y:latest is not from")

    def test_escaped_image_ref(self):
        # \x2F renders to "/" and the ref is tag-only; \/ is no escape to go-yaml, so kustomize fails.
        self.assertFails({"c/app/d.yaml": deployment(r'"ghcr.io\x2Ffjcloudaiconsulting/tbd/backend:v1.0.0"')},
                         "ghcr.io/fjcloudaiconsulting/tbd/backend:v1.0.0 is not pinned by @sha256")
        slash = r'"ghcr.io\/fjcloudaiconsulting/tbd/backend:v1.0.0' + DIGEST + '"'
        self.assertFails({"c/app/d.yaml": deployment(slash)}, "kustomize build failed")

    def test_foreign_or_malformed_image_ref(self):
        for ref, needle in (("quay.io/x/y" + DIGEST, "is not from"), ("ghcr.io:443/fjcloudaiconsulting/tbd/b" + DIGEST,
                            "is not from"), ("GHCR.io/x/y" + DIGEST, "is not an image reference"),
                            ("ghcr.io/x/y:v1@sha512:" + "a" * 128, "is not an image reference")):
            with self.subTest(ref):
                self.assertFails({"c/app/d.yaml": deployment(ref)}, needle)

    def test_init_and_ephemeral_containers_and_cronjobs(self):
        cron = textwrap.dedent("""\
            apiVersion: batch/v1
            kind: CronJob
            metadata:
              name: j
            spec:
              schedule: "0 1 * * *"
              jobTemplate:
                spec:
                  template:
                    spec:
                      restartPolicy: Never
                      containers:
                        - name: c
                          image: busybox:1
            """)
        pod = textwrap.dedent(f"""\
            apiVersion: v1
            kind: Pod
            metadata:
              name: p
            spec:
              containers:
                - name: c
                  image: {PINNED}
              initContainers:
                - name: i
                  image: busybox:2
              ephemeralContainers:
                - name: e
                  image: busybox:3
            """)
        out = problems({"c/app/cron.yaml": cron, "c/app/pod.yaml": pod})
        for needle in ("jobTemplate.spec.template.spec.containers[0]: busybox:1 is not pinned",
                       "initContainers[0]: busybox:2 is not pinned", "ephemeralContainers[0]: busybox:3 is not pinned"):
            self.assertTrue(any(needle in p for p in out), f"{needle!r} not in {out}")

    def test_plan_images(self):
        plan = ("apiVersion: upgrade.cattle.io/v1\nkind: Plan\nmetadata:\n  name: p\n  namespace: system-upgrade\n"
                "spec:\n  version: v1.36.5+k3s1\n  prepare:\n    image: rancher/k3s-upgrade\n"
                "  upgrade:\n    image: rancher/k3s-upgrade:v1.36.5-k3s1" + DIGEST + "\n")
        out = self.assertFails({"c/system-upgrade/plans.yaml": plan}, "spec.prepare: rancher/k3s-upgrade is not pinned")
        self.assertFalse(any("spec.upgrade" in p for p in out), out)

    def test_app_namespace_runs_only_its_app_images(self):
        self.assertFails({"c/tbd-prod/d.yaml": deployment("ghcr.io/fjcloudaiconsulting/ziftbook/backend:v1" + DIGEST,
                                                          namespace="tbd-prod")},
                         "in tbd-prod is not from ghcr.io/fjcloudaiconsulting/tbd/")

    def test_password_in_an_env_url(self):
        env = "\n          env:\n            - name: DB\n              value: postgres://app:hunter2@db:5432/app"
        self.assertFails({"c/app/d.yaml": deployment(PINNED, env=env)}, "env DB holds a password in a URL")
        ok = "\n          env:\n            - name: DB\n              value: postgres://app:$(DB_PASSWORD)@db/app"
        self.assertPasses({"c/app/d.yaml": deployment(PINNED, env=ok)})

    def test_remote_base(self):
        for entry in ("https://github.com/example/repo//base?ref=v1", "github.com/example/repo/base?ref=v1",
                      "git@github.com:example/repo.git"):
            with self.subTest(entry):
                self.assertFails({"c/app/kustomization.yaml": f"resources:\n  - {entry}\n"}, "remote resources entry")

    def test_render_is_offline(self):
        # The pre-read takes block lists; the render itself has no network, so a flow list fails as well.
        flow = 'resources: ["https://github.com/example/repo//b?ref=v1"]\n'
        out = self.assertFails({"c/app/kustomization.yaml": flow}, "kustomize build failed")
        self.assertTrue(any("127.0.0.1:9" in p or "not allowed" in p for p in out), out)

    def test_flux_fields_that_change_the_render(self):
        for extra in ("images:\n  - name: x\n    newTag: v2\n", "postBuild:\n  substitute:\n    TAG: v2\n",
                      "targetNamespace: other\n", "patches:\n  - patch: |\n      {}\n"):
            with self.subTest(extra):
                self.assertFails({}, "this check does not emulate it", extra=extra)

    def test_flux_source_elsewhere(self):
        sync = SYNC.format(path="./clusters/c", extra="")
        old = "    kind: GitRepository\n    name: flux-system\n"
        self.assertIn(old, sync)
        for new in ("    kind: OCIRepository\n    name: flux-system\n", "    kind: GitRepository\n    name: other\n"):
            with self.subTest(new):
                self.assertFails({"c/flux-system/gotk-sync.yaml": sync.replace(old, new)},
                                 "its source is not this repository")

    def test_flux_path_outside_the_root(self):
        self.assertFails({}, "is not a directory under clusters/", path="./elsewhere")


@kustomize_needed
class RendersWhatFluxApplies(Base):
    def test_encrypted_secret_and_pinned_image_pass(self):
        self.assertPasses({"c/app/s.secret.yaml": secret(f"data:\n  a: {ENC}\n  empty: \"\"\n" + SOPS),
                           "c/app/d.yaml": deployment(PINNED),
                           "c/tbd-prod/d.yaml": deployment(PINNED, namespace="tbd-prod")})

    def test_generated_kustomization_matches_flux(self):
        # Flux reads *.yaml and *.yml (case-sensitive) and treats a directory with a kustomization file as one entry.
        plain = "stringData:\n  k: plaintext\n"
        files = {"c/a.yaml": secret(plain, "a"), "c/sub/b.yml": secret(plain, "b"), "c/c.json": secret(plain, "c"),
                 "c/d.YAML": secret(plain, "d"), "c/kz/kustomization.yaml": "resources:\n  - e.yaml\n",
                 "c/kz/e.yaml": secret(plain, "e"), "c/kz/f.yaml": secret(plain, "f"),
                 "c/kz/sub/g.yaml": secret(plain, "g"), "c/notes.md": "x: y\n"}
        out = problems(files)
        self.assertEqual(sorted(re.findall(r"Secret (\w):", " ".join(out))), ["a", "b", "e"], out)

    def test_symlinks_are_left_out(self):
        # A source artifact holds no symlinks, so Flux never applies one; the text fence refuses them anyway.
        with Tree({"c/app/d.yaml": deployment(PINNED)}) as root:
            outside = root.parent / "outside.yaml"
            outside.write_text(secret("stringData:\n  k: plaintext\n"))
            (root / "c/app/link.yaml").symlink_to(outside)
            self.assertEqual(rc.check(root, exempt=set()), [])

    def test_nested_flux_kustomizations_are_followed(self):
        nested = SYNC.format(path="./clusters/c/more", extra="").split("---\n")[1]
        nested = nested.replace("  name: flux-system\n  namespace", "  name: more\n  namespace")
        self.assertIn("name: more", nested)
        out = self.assertFails({"c/apps/more.yaml": nested, "c/more/s.yaml": secret("stringData:\n  k: plaintext\n")},
                               "Secret s: no sops metadata", path="./clusters/c/apps")
        self.assertEqual(len([p for p in out if "Secret s:" in p]), 1, out)

    def test_tag_only_exemption_names_a_file(self):
        ref = "ghcr.io/fjcloudaiconsulting/tbd/backend:v1.0.0"
        files = {"c/app/a.yaml": deployment(ref, "a"), "c/app/b.yaml": deployment(ref, "b")}
        out = problems(files, exempt={("c/app/a.yaml", ref)})
        self.assertEqual(len(out), 1, out)
        self.assertIn("c/app/b.yaml: Deployment b:", out[0])

    def test_origin_annotations_cannot_be_forged(self):
        ref = "ghcr.io/fjcloudaiconsulting/tbd/backend:v1.0.0"
        forged = deployment(ref, "b").replace("  name: b\n", "  name: b\n  annotations:\n    config.kubernetes.io/"
                                                             "origin: |\n      path: ../clusters/c/app/a.yaml\n", 1)
        out = problems({"c/app/b.yaml": forged}, exempt={("c/app/a.yaml", ref)})
        self.assertTrue(any("c/app/b.yaml: Deployment b:" in p for p in out), out)

    def test_tag_only_list_is_read_from_the_ci_fences(self):
        with tempfile.TemporaryDirectory() as t:
            p = pathlib.Path(t, "f.py")
            p.write_text("import os\nTAG_ONLY = {\n    ('a/b.yaml', 'r:v1'),\n}\nos.exit(1)\n")
            self.assertEqual(rc.tag_only(p), {("a/b.yaml", "r:v1")})
            p.write_text("X = 1\n")
            self.assertEqual(rc.tag_only(p), set())  # deleted once the TBD refs are pinned


@kustomize_needed
class RepoTree(unittest.TestCase):
    def test_repo_clusters_pass(self):
        r = subprocess.run([sys.executable, "-I", str(SCRIPT), str(ROOT / "clusters")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_awaiting_digest_only_shrinks(self):
        # With only TAG_ONLY exempt, what fails is exactly the AWAITING_DIGEST refs; once a K8S-02 pin lands, its
        # entry is no longer rendered: delete it.
        out = rc.check(ROOT / "clusters", exempt=rc.tag_only())
        found = [re.fullmatch(r"(\S+): .*: (\S+) is not pinned by @sha256", p) for p in out]
        self.assertTrue(all(found), out)
        unpinned = {m.groups() for m in found}
        for entry in sorted(rc.AWAITING_DIGEST):
            with self.subTest(entry):
                self.assertIn(entry, unpinned, "no longer rendered tag-only: delete its AWAITING_DIGEST entry")
        self.assertEqual(unpinned, rc.AWAITING_DIGEST)


@kustomize_needed
class ReaderAgreesWithKustomize(unittest.TestCase):
    """kustomize writes these values in every scalar style it has; reading its output must give them back."""

    maxDiff = None

    VALUES = ["", " ", "plain", "yes", "No", "null", "~", "1.0", "0x1F", "1e3", "true", "-", "- x", "-x", "? x", ": x",
              "a: b", "a #b", "a#b", "#x", "'", "''", '"', "it's", "a\\b", "\\", "tab\there", "\ttab", "x\n", "\n",
              "\n\n", "a\n\nb\n\n\n", " lead", "trail ", "x  \n  y", " \n  x", "é ✓ 𝄞", "\x00\x01\x1b\ufeff", "\xa0",
              "---", "...", "@x", "`x", "%x", "!x", "&x", "*x", "|", ">", "{a: b}", "[a]", "x" * 300,
              ("word " * 40).strip(), "word  " * 30, "a 'b' " * 30 + ": c # d", "multi\nline with a very long second "
              "line that goes past eighty columns and keeps going, yes it does\n  indented\n", "ENC[AES256_GCM,"
              "data:" + "A" * 200 + ",iv:x,tag:y,type:str]"]

    def test_values_round_trip(self):
        data = {f"k{i}": v for i, v in enumerate(self.VALUES)}
        data.update({"quoted key": "x", "8080": "port", "a/b.c_d-e": "x", "": "empty key"})
        obj = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "m", "labels": {"y": "n"}},
               "data": data, "list": [[], {}, [["a"], {"b": None}], None, True, False, [{"c": [1]}]]}
        with tempfile.TemporaryDirectory() as t:
            d = pathlib.Path(t, "clusters/c")
            d.mkdir(parents=True)
            (d / "m.json").write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            (d / "kustomization.yaml").write_text('{"resources": ["m.json"]}')
            rendered = rc.render(d.parent, pathlib.PurePosixPath("c"), pathlib.Path(t, "base"))
        got = rendered[0][1]
        del got["metadata"]["annotations"]
        obj["list"][-1] = [{"c": ["1"]}]  # numbers stay strings: no check reads one
        self.assertEqual(got, obj)


class Reader(unittest.TestCase):
    """The reader takes kustomize's form and refuses any other YAML."""

    def test_kustomize_forms(self):
        text = textwrap.dedent("""\
            a: plain words that go on
              over a second line
            b: 'single ''quoted'' that
              folds'
            c: "double \\x41 \\
              joined\\ \\ two"
            d: |2-
               lead
              x
            e:
            - - n
            - k: v
              l:
              - 1
            - {}
            - []
            "q k": null
            f: |+
              keep

            ---
            z: ~
            """)
        self.assertEqual(rc.parse(text), [
            {"a": "plain words that go on over a second line", "b": "single 'quoted' that folds",
             "c": "double A joined  two", "d": " lead\nx", "e": [["n"], {"k": "v", "l": ["1"]}, {}, []],
             "q k": None, "f": "keep\n\n"}, {"z": None}])

    def test_refuses_other_yaml(self):
        for text in ("a: &x b\n", "a: *x\n", "a: !!str b\n", "a: {b: c}\n", "a: [b]\n", "? a\n: b\n", "# c\na: b\n",
                     "a: b # c\n", "a:\n\tb: c\n", "a: 'x\u2028y'\n", "a: x\ry\n", " a: b\n", "a: b\n\nc: d\n",
                     "a: b\n  c: d\n", "a: b\na: c\n", "a: |\n  x\n b: c\n", 'a: "\\/"\n', "a: >\n  x\n", "a:\nb: c\n",
                     "a: 'open\n"):
            with self.subTest(text):
                with self.assertRaises(rc.RenderError):
                    rc.parse(text)

    def test_a_document_that_is_not_a_mapping_fails(self):
        self.assertEqual(rc.object_problems("f.yaml", rc.parse("- a\n")[0], set()),
                         ["f.yaml: a rendered document is not a mapping"])


class CiRunsTheRenderCheck(unittest.TestCase):
    def test_kubernetes_job_pins_kustomize_and_requires_the_render_tests(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        job = ci[ci.index("\n  kubernetes:"):]
        self.assertRegex(job, r"\n      KUSTOMIZE_VERSION: v\d+\.\d+\.\d+\n")
        self.assertRegex(job, r"\n      KUSTOMIZE_SHA256: [0-9a-f]{64}\n")
        self.assertIn('echo "$KUSTOMIZE_SHA256  kustomize.tgz" | sha256sum -c -', job)
        self.assertIn("RENDER_CHECK_REQUIRED=1", job)
        self.assertIn("check-rendered-manifests.py clusters", job)
        self.assertIn("-p test_render_check.py", job)


if __name__ == "__main__":
    unittest.main()
