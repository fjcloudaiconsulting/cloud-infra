"""Every Secret under clusters/ is SOPS-encrypted (INFRA-23).

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`.
"""
import pathlib
import subprocess
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHECK = ROOT / ".github/scripts/check-sops-secrets.py"

SOPS_TAIL = """\
sops:
    age:
        - recipient: age1example
          enc: |
            -----BEGIN AGE ENCRYPTED FILE-----
            -----END AGE ENCRYPTED FILE-----
    encrypted_regex: ^(data|stringData)$
    mac: ENC[AES256_GCM,data:bWFj,iv:aXY=,tag:dGFn,type:str]
    version: 3.13.3
"""

SECRET_HEAD = """\
apiVersion: v1
kind: Secret
metadata:
    name: s
    namespace: flux-system
type: Opaque
"""


def run(files):
    with tempfile.TemporaryDirectory() as d:
        for name, body in files.items():
            p = pathlib.Path(d, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(body))
        return subprocess.run(
            ["python3", str(CHECK), d], capture_output=True, text=True
        )


class SopsSecrets(unittest.TestCase):
    def assertFails(self, files, needle):
        r = run(files)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(needle, r.stdout + r.stderr)

    def test_repo_clusters_pass(self):
        # guard: the committed tree (canary + Flux components) is clean.
        r = subprocess.run(
            ["python3", str(CHECK), str(ROOT / "clusters")], capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_encrypted_secret_passes(self):
        # guard: a correctly encrypted file, data and stringData both ENC.
        r = run({"a/x.secret.yaml": SECRET_HEAD
                 + "data:\n    k: ENC[AES256_GCM,data:eA==,type:str]\n"
                 + "stringData:\n    k2: ENC[AES256_GCM,data:eA==,type:str]\n"
                 + SOPS_TAIL})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_secret_file_without_sops_metadata_fails(self):
        # fence: kills a check that trusts the .secret.yaml name alone.
        self.assertFails(
            {"x.secret.yaml": SECRET_HEAD + "stringData:\n    k: ENC[fake]\n"},
            "no SOPS metadata",
        )

    def test_plaintext_value_with_sops_metadata_fails(self):
        # fence: kills a check that only looks for a sops: block
        # (e.g. a file encrypted with a wrong encrypted_regex, then edited).
        self.assertFails(
            {"x.secret.yaml": SECRET_HEAD
             + "stringData:\n    ok: ENC[AES256_GCM,data:eA==,type:str]\n    pw: hunter2\n"
             + SOPS_TAIL},
            "pw",
        )

    def test_block_scalar_value_fails(self):
        # fence: kills a value check that skips multi-line (|) values.
        self.assertFails(
            {"x.secret.yaml": SECRET_HEAD + "data:\n    k: |\n        aHVudGVyMg==\n" + SOPS_TAIL},
            "data.k is not encrypted",
        )

    def test_secret_outside_secret_yaml_fails(self):
        # fence: kills a check that only scans *.secret.yaml files.
        self.assertFails(
            {"app/deploy.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n---\n"
             + SECRET_HEAD + "stringData:\n  pw: hunter2\n"},
            "deploy.yaml: Secret outside",
        )

    def test_yml_extension_is_scanned(self):
        # fence: kills a scan limited to *.yaml.
        self.assertFails(
            {"app/s.yml": SECRET_HEAD + "stringData:\n  pw: hunter2\n"},
            "s.yml: Secret outside",
        )

    def test_upper_case_extension_is_scanned(self):
        # fence: kills a case-sensitive suffix filter.
        self.assertFails(
            {"app/S.YAML": SECRET_HEAD + "stringData:\n  pw: hunter2\n"},
            "S.YAML: Secret outside",
        )

    def test_sops_block_without_mac_fails(self):
        # fence: kills a metadata check that accepts any sops: block.
        tail = "".join(l + "\n" for l in SOPS_TAIL.splitlines() if "mac:" not in l)
        self.assertFails(
            {"x.secret.yaml": SECRET_HEAD + "stringData:\n    k: ENC[AES256_GCM,data:eA==,type:str]\n" + tail},
            "no SOPS metadata",
        )

    def test_quoted_or_commented_kind_fails(self):
        # fence: kills a kind match that requires a bare `Secret` at end of line.
        for kind in ('kind: "Secret"', "kind: 'Secret'", "kind: Secret  # db"):
            with self.subTest(kind=kind):
                self.assertFails(
                    {"a.yaml": SECRET_HEAD.replace("kind: Secret", kind) + "stringData:\n  pw: hunter2\n"},
                    "a.yaml: Secret outside",
                )

    def test_secret_nested_in_list_fails(self):
        # fence: kills a kind match anchored at column 0.
        self.assertFails(
            {"a.yaml": "apiVersion: v1\nkind: List\nitems:\n  - apiVersion: v1\n    kind: Secret\n"
             "    stringData:\n      pw: hunter2\n"},
            "a.yaml: Secret outside",
        )

    def test_json_secret_fails(self):
        # fence: kills a scan limited to YAML.
        self.assertFails(
            {"a.json": '{"apiVersion": "v1", "kind": "Secret", "stringData": {"pw": "hunter2"}}'},
            "a.json: Secret outside",
        )

    def test_secret_generator_fails(self):
        # fence: kills a check that only recognises `kind: Secret` (generators never say it).
        self.assertFails(
            {"kustomization.yaml": "resources: []\nsecretGenerator:\n  - name: s\n    literals:\n      - pw=hunter2\n"},
            "secretGenerator",
        )

    def test_flow_style_values_fail(self):
        # fence: kills a value check that only reads indented block entries.
        self.assertFails(
            {"x.secret.yaml": SECRET_HEAD + "stringData: {pw: hunter2}\n" + SOPS_TAIL},
            "stringData is not encrypted",
        )

    def test_column0_comment_does_not_end_block(self):
        # fence: kills a block parser that treats `# note: x` as a new top-level key.
        self.assertFails(
            {"x.secret.yaml": SECRET_HEAD + "stringData:\n# rotated: 2026\n    pw: hunter2\n" + SOPS_TAIL},
            "stringData.pw is not encrypted",
        )

    def test_repo_ansible_passes(self):
        # guard: the committed ansible/ secrets file is encrypted.
        r = subprocess.run(
            ["python3", str(CHECK), str(ROOT / "ansible")], capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_encrypted_sops_vars_pass(self):
        # guard: an Ansible vars file sops wrote, scalar and list values both ENC.
        r = run({"ansible/x.sops.yaml": "k3s_token: ENC[AES256_GCM,data:eA==,type:str]\n"
                 "keys:\n    - ENC[AES256_GCM,data:eA==,type:str]\n" + SOPS_TAIL})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_plaintext_sops_vars_fail(self):
        # fence: kills a check that only scans Kubernetes Secrets (a vars file has no kind:).
        self.assertFails(
            {"ansible/x.sops.yaml": "k3s_token: hunter2\n" + SOPS_TAIL},
            "k3s_token is not encrypted",
        )

    def test_plaintext_sops_list_item_fails(self):
        # fence: kills a value check that only reads `key: value` lines.
        self.assertFails(
            {"ansible/x.sops.yaml": "keys:\n    - ENC[AES256_GCM,data:eA==,type:str]\n    - hunter2\n" + SOPS_TAIL},
            "- is not encrypted",
        )

    def test_sops_vars_without_metadata_fail(self):
        # fence: kills a check that trusts the .sops.yaml name alone.
        self.assertFails(
            {"ansible/x.sops.yaml": "k3s_token: ENC[fake]\n"},
            "no SOPS metadata",
        )

    def test_default_roots_include_ansible(self):
        # fence: kills a default that scans clusters/ only.
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "ansible").mkdir()
            pathlib.Path(d, "ansible/x.sops.yaml").write_text("k3s_token: hunter2\n")
            r = subprocess.run(["python3", str(CHECK)], capture_output=True, text=True, cwd=d)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("x.sops.yaml", r.stdout)

    def test_only_root_collections_dir_is_skipped(self):
        # fence: kills an exclusion that skips any directory named collections.
        self.assertFails(
            {"c/collections/x.sops.yaml": "k3s_token: hunter2\n" + SOPS_TAIL},
            "k3s_token is not encrypted",
        )

    def test_root_collections_and_ansible_dirs_are_skipped(self):
        # guard: installed third-party collections under <root>/collections and <root>/.ansible.
        r = run({"collections/a/x.sops.yaml": "k3s_token: hunter2\n",
                 ".ansible/collections/b/y.sops.yaml": "k3s_token: hunter2\n"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_non_canonical_secret_shapes_fail(self):
        # GHA-03: each renders as a real Secret (or a plaintext credential) and passed the text match before
        # manifest_fence.py.
        pw = "stringData:\n  pw: hunter2\n"
        cases = {
            "tag": (SECRET_HEAD.replace("kind: Secret", "kind: !!str Secret") + pw, "YAML tag"),
            "escape": (SECRET_HEAD.replace("kind: Secret", 'kind: "Secr\\x65t"') + pw, "escape in a quoted `kind`"),
            "flow": ("{apiVersion: v1, kind: Secret, metadata: {name: s}, stringData: {pw: hunter2}}\n",
                     "flow collection holds `apiVersion`"),
            "list": ("apiVersion: v1\nkind: List\nitems:\n  - kind: Secret\n    apiVersion: v1\n"
                     "    stringData:\n      pw: hunter2\n", "kind List"),
            "block": (SECRET_HEAD.replace("kind: Secret", "kind: |-\n  Secret") + pw, "`kind` value must be a scalar"),
            "alias": ("apiVersion: v1\nx: &k Secret\nkind: *k\n" + pw, "YAML alias"),
            "quoted key": (SECRET_HEAD.replace("kind: Secret", "'kind': Secret") + pw, "quoted key"),
            "line separator": (SECRET_HEAD.replace("\n", "\u2028") + pw, "U+2028"),
            "env credential": ("apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    spec:\n      containers:\n"
                               "        - env:\n            - name: DATABASE_URL\n"
                               "              value: postgresql://app:hunter2@db:5432/app\n",
                               "password in a URL"),
        }
        for name, (body, needle) in cases.items():
            with self.subTest(name):
                self.assertFails({"app/deploy.yaml": body}, needle)

    def test_fence_reads_everything_kustomize_renders(self):
        # GHA-03 review: each layout passed the fence's first version.
        secret = SECRET_HEAD + "stringData:\n  pw: hunter2\n"
        url = "postgresql://app:hunter2@db/app"
        cm = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  annotations:\n    note: " + url + "\n---\n"
        gen = "configMapGenerator:\n  - name: c\n    {}:\n      - {}\n"
        cases = {
            # 1: a resource the checks never read.
            "resource": ({"app/kustomization.yaml": "resources:\n  - s.txt\n", "app/s.txt": secret},
                         "neither a directory nor a .yaml/.yml/.json file"),
            # 1: the SOPS check reads extension-less Kustomization files like the fence.
            "Kustomization": ({"app/Kustomization": "secretGenerator:\n  - name: s\n    literals:\n      - pw=x\n"},
                              "app/Kustomization: secretGenerator writes plaintext"),
            # 2: only an exact $(VAR) or ${VAR} is a placeholder.
            "placeholder": ({"app/deploy.yaml": "env:\n  - name: U\n    value: postgresql://app:$hunter2@db/app\n"},
                            "password in a URL"),
            # 3: an escape in any double-quoted value.
            "escape": ({"app/deploy.yaml": 'args:\n  - "postgresql://app:hunter2\\x40db/app"\n'},
                       "escape in a quoted value"),
            # 4: a *.secret.yaml is checked too; only ENC[...] values are skipped.
            "secret file": ({"x.secret.yaml": cm + SECRET_HEAD + "stringData:\n    k: ENC[AES256_GCM,data:eA==,type:str]\n"
                             + SOPS_TAIL}, "password in a URL"),
            # 5: configMapGenerator sources, in every form, and inside the tree.
            "generator file": ({"app/kustomization.yaml": gen.format("files", "app.ini"), "app/app.ini": url + "\n"},
                               "generator source app.ini line 1: password in a URL"),
            "generator key=file": ({"app/kustomization.yaml": gen.format("files", "k=app.ini"),
                                    "app/app.ini": url + "\n"}, "generator source app.ini line 1"),
            "generator env": ({"app/kustomization.yaml": gen.format("envs", "app.env"), "app/app.env": "U=" + url + "\n"},
                              "generator source app.env line 1"),
            "generator outside": ({"app/kustomization.yaml": gen.format("files", "../../x.ini")},
                                  "generator source ../../x.ini is not a file inside"),
            "generator flow": ({"app/kustomization.yaml": "configMapGenerator:\n  - name: c\n    files: [app.ini]\n",
                                "app/app.ini": url + "\n"}, "write `files` as a block list"),
        }
        for name, (files, needle) in cases.items():
            with self.subTest(name):
                self.assertFails(files, needle)

    def test_paths_and_credentials_in_their_exact_form(self):
        # GHA-03 second review: each layout passed the fence before this change.
        secret = SECRET_HEAD + "stringData:\n  pw: hunter2\n"
        url = "postgresql://app:hunter2@db/app"
        cases = {
            # 1: a path list entry is a plain or quoted one-line scalar, nothing else.
            "block resource": ({"app/kustomization.yaml": "resources:\n  - |-\n    s.txt\n", "app/s.txt": secret},
                               "write `resources` as a block list of one-line paths"),
            "multi-line resource": ({"app/kustomization.yaml": "resources:\n  - 's.\n    txt'\n"},
                                    "write `resources` as a block list of one-line paths"),
            "nested resource": ({"app/kustomization.yaml": "resources:\n  -\n    path: s.txt\n", "app/s.txt": secret},
                                "write `resources` as a block list of one-line paths"),
            "block generator file": ({"app/kustomization.yaml": "configMapGenerator:\n  - name: c\n    files:\n"
                                      "      - >-\n        app.ini\n", "app/app.ini": url + "\n"},
                                     "write `files` as a block list of one-line paths"),
            # 2: only a whole value in the full sops format is skipped, not ENC[...] inside a value.
            "fake ENC": ({"x.secret.yaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  annotations:\n"
                          "    note: postgresql://app:ENC[hunter2]@db/app\n---\n" + SECRET_HEAD
                          + "stringData:\n    k: ENC[AES256_GCM,data:eA==,type:str]\n" + SOPS_TAIL},
                         "line 6: password in a URL"),
            # 3: an empty user name.
            "empty user": ({"app/deploy.yaml": "env:\n  - name: R\n    value: redis://:hunter2@cache:6379\n"},
                           "password in a URL"),
        }
        for name, (files, needle) in cases.items():
            with self.subTest(name):
                self.assertFails(files, needle)

    def test_sops_values_in_the_full_format_pass(self):
        # guard: what sops writes, mac included, in a manifest and an Ansible vars file.
        enc = "ENC[AES256_GCM,data:aHVudGVy,iv:aXZpdml2aXZpdml2,tag:dGFndGFndGFndGFn,type:str]"
        tail = SOPS_TAIL.replace("ENC[AES256_GCM,data:bWFj,iv:aXY=,tag:dGFn,type:str]", enc)
        r = run({"x.secret.yaml": SECRET_HEAD + f"stringData:\n    url: {enc}\n" + tail,
                 "ansible/x.sops.yaml": f"k3s_token: {enc}\n" + tail})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_generator_sources_inside_the_tree_pass(self):
        # guard: the shapes observability/ uses (files: [key=]path), with clean sources.
        r = run({"app/kustomization.yaml": "configMapGenerator:\n  - name: c\n    files:\n      - a.sh\n      - k=b.conf\n",
                 "app/a.sh": "#!/bin/sh\necho ok\n", "app/b.conf": "url = https://example.invalid/x\n"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_url_with_a_password_placeholder_passes(self):
        # guard: Kubernetes expands $(VAR) from a Secret-backed env var; nothing literal is committed.
        r = run({"app/deploy.yaml": "env:\n  - name: DATABASE_URL\n    value: postgresql://app:$(DB_PASSWORD)@db/app\n"
                 "  - name: CACHE_URL\n    value: redis://app:${CACHE_PASSWORD}@cache\n"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_indented_secret_document_fails(self):
        # fence: the value check reads data/stringData at column 0; an indented document would skip it.
        body = "".join("  " + l + "\n" for l in (SECRET_HEAD + "stringData:\n  pw: hunter2\n").splitlines())
        self.assertFails({"x.secret.yaml": body + "---\n" + SOPS_TAIL}, "does not start at column 0")

    def test_ansible_sops_vars_are_fenced(self):
        # fence: the YAML rules cover the Ansible vars files too (a carriage return is a YAML line break).
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "ansible").mkdir()
            pathlib.Path(d, "ansible/x.sops.yaml").write_text(
                "k3s_token: ENC[AES256_GCM,data:eA==,type:str]\rplain: hunter2\n" + SOPS_TAIL)
            r = subprocess.run(["python3", str(CHECK), str(pathlib.Path(d, "ansible"))], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("U+000D", r.stdout)

    def test_ansible_tree_fences_only_sops_files(self):
        # guard: playbooks may use Ansible's own tags (!unsafe, !vault); only *.sops.yaml are read as SOPS files.
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "ansible").mkdir()
            pathlib.Path(d, "ansible/site.yml").write_text("- hosts: all\n  vars:\n    x: !unsafe '{{ y }}'\n")
            r = subprocess.run(["python3", str(CHECK), str(pathlib.Path(d, "ansible"))], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_empty_value_left_by_sops_passes(self):
        # guard (inverse defect): sops leaves "" unencrypted; that is not a leak.
        r = run({"x.secret.yaml": SECRET_HEAD
                 + "stringData:\n    k: ENC[AES256_GCM,data:eA==,type:str]\n    empty: \"\"\n" + SOPS_TAIL})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
