"""Fences on the CI and Renovate inputs (GHA-09): third-party images and actions wait 7 days after a release before
Renovate proposes them, the org's own images and actions do not wait, and every `pip install` in a workflow installs
only hash-pinned requirements that Renovate keeps updated.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`. renovate.json's packageRules are evaluated
the way Renovate resolves the two options that matter here (minimumReleaseAge, minimumReleaseAgeBehaviour): rules
apply in order and a later match overrides an earlier one; a pattern is a minimatch glob (case-insensitive, `*` stops
at `/`, `**` does not) or a /regex/, and `!` negates it. A rule that sets either option with a matcher this file does
not model fails the tests instead of being guessed at. The org preset (`github>fjcloudaiconsulting/.github`) sets
neither option; it cannot be read offline.
"""
import json
import pathlib
import re
import shlex
import unittest

from test_tbd_manifests import load_all

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLUSTERS = ROOT / "clusters"
WORKFLOWS = ROOT / ".github" / "workflows"
RENOVATE = ROOT / "renovate.json"

WAIT = "7 days"
ORG_IMAGES = "ghcr.io/fjcloudaiconsulting/"
ORG_REPOS = "fjcloudaiconsulting/"
# Renovate reads release timestamps for Docker images from Docker Hub only (docs.renovatebot.com, key-concepts
# minimum-release-age); an image from any other registry has none.
DOCKER_HUB = {"docker.io", "index.docker.io", "registry-1.docker.io"}
# The update types Renovate ages. Pins and replacements have no release timestamp (it would hold them for ever) and a
# digest pin only freezes what already runs, so none of those may wait.
AGED = ("major", "minor", "patch", "digest")
UNAGED = ("pin", "pinDigest", "replacement")
BEHAVIOURS = {"timestamp-required", "timestamp-optional"}

MATCHERS = {
    "matchDatasources": "datasource",
    "matchManagers": "manager",
    "matchDepTypes": "depType",
    "matchPackageNames": "packageName",
    "matchDepNames": "depName",
    "matchUpdateTypes": "updateType",
    "matchFileNames": "fileName",
}
AGE_KEYS = ("minimumReleaseAge", "minimumReleaseAgeBehaviour")
IMAGE_LINE = re.compile(r"""^\s*-?\s*image:\s*['"]?([^\s'"#]+)""")
USES_LINE = re.compile(r"""^\s*(?:-\s+)?uses:\s*['"]?([^\s'"#@]+)@\S+""")

# Renovate's pip_requirements default managerFilePatterns (lib/modules/manager/pip_requirements/index.ts).
PIP_REQUIREMENTS = r"/(^|/)[\w-]*requirements([-._]\w+)?\.(txt|pip)$/"
# The python3 each runner label ships (actions/runner-images); the requirements are resolved for it.
RUNNER_PYTHON = {"ubuntu-26.04": "3.14"}
PIP_VALUE_OPTIONS = {"-r", "--requirement", "-c", "--constraint", "--only-binary", "--no-binary", "-i", "--index-url",
                     "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix", "--root", "--cache-dir"}
# One pinned package per logical line (extras and markers allowed), followed by at least one sha256.
REQUIREMENT = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]+\])?==[^\s;]+(\s*;[^-]+)?(\s+--hash=sha256:[0-9a-f]{64})+$")


# ---- Renovate rule evaluation -------------------------------------------------------------------------------------

def renovate():
    return json.loads(RENOVATE.read_text())


def _pattern(p):
    m = re.fullmatch(r"/(.*)/(i?)", p)
    if m:
        return re.compile(m[1].replace("(?<", "(?P<"), re.I if m[2] else 0)
    out, i = "", 0
    while i < len(p):
        if p.startswith("**", i):
            out, i = out + ".*", i + 2
        elif p[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif p[i] == "?":
            out, i = out + "[^/]", i + 1
        elif p[i] in "[]{}":
            raise AssertionError(f"glob syntax this test does not model: {p!r}")
        else:
            out, i = out + re.escape(p[i]), i + 1
    return re.compile(rf"^{out}$", re.I)


def matches(value, patterns):
    """Renovate's matchRegexOrGlobList: a positive pattern must match if there is one, no negative one may."""
    if value is None:
        return False
    if patterns == ["*"]:
        return True
    pos = [p for p in patterns if not p.startswith("!")]
    neg = [p[1:] for p in patterns if p.startswith("!")]
    if pos and not any(_pattern(p).search(value) for p in pos):
        return False
    return not any(_pattern(p).search(value) for p in neg)


def release_age(config, dep):
    """(minimumReleaseAge, minimumReleaseAgeBehaviour) as Renovate resolves them for dep."""
    for key in ("major", "minor", "patch", "digest", "pin", "pinDigest", "replacement"):
        if any(k in (config.get(key) or {}) for k in AGE_KEYS):
            raise AssertionError(f"renovate.json `{key}` sets a release-age option; this test does not model it")
    age = config.get("minimumReleaseAge")
    behaviour = config.get("minimumReleaseAgeBehaviour", "timestamp-required")
    for i, rule in enumerate(config.get("packageRules", [])):
        if not any(k in rule for k in AGE_KEYS):
            continue
        unknown = [k for k in rule if k.startswith(("match", "exclude")) and k not in MATCHERS]
        if unknown:
            raise AssertionError(f"packageRules[{i}] sets a release-age option with unmodelled matchers {unknown}")
        if all(matches(dep.get(field), rule[key]) for key, field in MATCHERS.items() if key in rule):
            age = rule.get("minimumReleaseAge", age)
            behaviour = rule.get("minimumReleaseAgeBehaviour", behaviour)
    return age, behaviour


# ---- what Renovate reads ------------------------------------------------------------------------------------------

def image_name(ref):
    name = ref.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    return f"{head}/{last.split(':', 1)[0]}" if head else last.split(":", 1)[0]


def registry(name):
    first = name.split("/", 1)[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        return first.lower()
    return "docker.io"


def cluster_images(config):
    """[(file, manager, image name)] for every image Renovate reads under clusters/: `image:` lines (kubernetes
    manager, renovate.json's managerFilePatterns) and kustomization `images:` (kustomize manager), minus ignorePaths."""
    k8s = [_pattern(p) for p in config["kubernetes"]["managerFilePatterns"]]
    ignored = [_pattern(g) for g in config.get("ignorePaths", [])]
    out = []
    for f in sorted(CLUSTERS.rglob("*")):
        rel = f.relative_to(ROOT).as_posix()
        if not f.is_file() or f.suffix not in (".yaml", ".yml") or any(g.search(rel) for g in ignored):
            continue
        if any(p.search(rel) for p in k8s):
            for line in f.read_text().splitlines():
                m = IMAGE_LINE.match(line)
                if m and not line.lstrip().startswith("#"):
                    out.append((rel, "kubernetes", image_name(m[1])))
        if f.name == "kustomization.yaml":
            for doc in load_all(f):
                for image in doc.get("images") or []:
                    out.append((rel, "kustomize", image.get("newName") or image["name"]))
    return out


def workflow_actions():
    """[(file, package name, depType)] for every remote `uses:` in .github/workflows."""
    out = []
    for f in sorted(WORKFLOWS.glob("*.y*ml")):
        for line in f.read_text().splitlines():
            m = USES_LINE.match(line)
            if m and not m[1].startswith(("./", "docker://")):
                kind = "workflow" if "/.github/workflows/" in m[1] else "action"
                out.append((f.relative_to(ROOT).as_posix(), "/".join(m[1].split("/")[:2]), kind))
    return out


def image_dep(rel, manager, name, update_type):
    return {"datasource": "docker", "manager": manager, "packageName": name, "depName": name,
            "updateType": update_type, "fileName": rel}


def action_dep(rel, name, dep_type, update_type):
    return {"datasource": "github-tags", "manager": "github-actions", "depType": dep_type, "packageName": name,
            "depName": name, "updateType": update_type, "fileName": rel}


def pip_installs():
    """[(where, runs-on, working directory, argv after `install`)] for every `pip install` in a workflow `run:`."""
    out = []
    for f in sorted(WORKFLOWS.glob("*.y*ml")):
        (doc,) = load_all(f)
        top = ((doc.get("defaults") or {}).get("run") or {}).get("working-directory")
        for job_id, job in (doc.get("jobs") or {}).items():
            job_wd = ((job.get("defaults") or {}).get("run") or {}).get("working-directory", top)
            for i, step in enumerate(job.get("steps") or []):
                wd = step.get("working-directory", job_wd) or "."
                for line in str(step.get("run") or "").replace("\\\n", " ").splitlines():
                    if not re.search(r"\bpip3?\b", line):
                        continue
                    argv = shlex.split(line, comments=True)
                    for j, word in enumerate(argv[:-1]):
                        if re.search(r"(^|/)pip3?$", word) and argv[j + 1] == "install":
                            where = f"{f.relative_to(ROOT)} {job_id} step {step.get('name', i)}"
                            out.append((where, job.get("runs-on"), wd, argv[j + 2:]))
    return out


def requirement_files(argv):
    """The -r files of a pip install argv, and any positional argument (a package given on the command line)."""
    files, positional, i = [], [], 0
    while i < len(argv):
        word = argv[i]
        if word in ("-r", "--requirement"):
            files.append(argv[i + 1])
        elif word.startswith("--requirement="):
            files.append(word.split("=", 1)[1])
        elif not word.startswith("-"):
            positional.append(word)
        if word in PIP_VALUE_OPTIONS:
            i += 1
        i += 1
    return files, positional


def requirement_lines(path):
    """The logical lines of a requirements file (continuations joined), without comments and blank lines."""
    out = []
    for line in path.read_text().replace("\\\n", " ").splitlines():
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if line:
            out.append(line)
    return out


# ---- tests --------------------------------------------------------------------------------------------------------

class EvaluatorSelfTest(unittest.TestCase):
    def test_patterns(self):
        self.assertTrue(matches("ghcr.io/fjcloudaiconsulting/tbd/backend", ["ghcr.io/fjcloudaiconsulting/**"]))
        self.assertFalse(matches("ghcr.io/fjcloudaiconsulting/tbd/backend", ["!ghcr.io/FJCloudAIConsulting/**"]))
        self.assertTrue(matches("postgres", ["!ghcr.io/fjcloudaiconsulting/**"]))
        self.assertFalse(matches("a/b/c", ["a/*"]))
        self.assertTrue(matches("fjcloudaiconsulting/.github", ["fjcloudaiconsulting/**"]))
        self.assertTrue(matches("v1.2.3", ["/^v\\d+/"]))
        self.assertFalse(matches(None, ["!x"]))

    def test_later_rules_override_and_unknown_matchers_fail(self):
        config = {"packageRules": [
            {"matchDatasources": ["docker"], "minimumReleaseAge": "3 days"},
            {"matchPackageNames": ["x"], "minimumReleaseAge": None},
            {"matchPackageNames": ["y"], "groupName": "ignored: sets no release-age option"},
        ]}
        self.assertEqual(release_age(config, {"datasource": "docker", "packageName": "w"}),
                         ("3 days", "timestamp-required"))
        self.assertEqual(release_age(config, {"datasource": "docker", "packageName": "x"})[0], None)
        with self.assertRaises(AssertionError):
            release_age({"packageRules": [{"matchCurrentVersion": "1", "minimumReleaseAge": "1 day"}]}, {})

    def test_image_names_and_registries(self):
        self.assertEqual(image_name("mirror.gcr.io/cloudflare/cloudflared:1@sha256:ab"),
                         "mirror.gcr.io/cloudflare/cloudflared")
        self.assertEqual(image_name("localhost:5000/x:1"), "localhost:5000/x")
        self.assertEqual(registry("grafana/alloy"), "docker.io")
        self.assertEqual(registry("postgres"), "docker.io")
        self.assertEqual(registry("mirror.gcr.io/cloudflare/cloudflared"), "mirror.gcr.io")

    def test_pip_argv_and_requirement_lines(self):
        argv = shlex.split('--quiet --require-hashes --only-binary :all: -r a.txt --requirement=b.txt "x==1"')
        self.assertEqual(requirement_files(argv), (["a.txt", "b.txt"], ["x==1"]))
        self.assertEqual(requirement_files(["-c", "c.txt", "--index-url", "https://i", "-r", "r.txt"]), (["r.txt"], []))
        h = "--hash=sha256:" + "0" * 64
        self.assertRegex(f"a-b.c==1.0 {h} {h}", REQUIREMENT)
        self.assertRegex(f'x[y]==2 ; python_version < "3.15" {h}', REQUIREMENT)
        for bad in ("a==1", f"a>=1 {h}", f"-e . {h}", f"--index-url https://x {h}", f"a==1 --hash=md5:{'0' * 32}"):
            self.assertNotRegex(bad, REQUIREMENT)


class RenovateReleaseAge(unittest.TestCase):
    def setUp(self):
        self.config = renovate()
        images = cluster_images(self.config)
        self.org = [i for i in images if i[2].lower().startswith(ORG_IMAGES)]
        self.third_party = [i for i in images if not i[2].lower().startswith(ORG_IMAGES)]
        self.actions = workflow_actions()

    def test_collectors_find_the_inputs(self):
        # Guards the tests below against passing on nothing.
        self.assertTrue(self.org)
        self.assertTrue([i for i in self.third_party if registry(i[2]) in DOCKER_HUB])
        self.assertTrue([i for i in self.third_party if i[1] == "kustomize"])
        self.assertTrue(self.actions)

    def test_third_party_images_wait_seven_days(self):
        for rel, manager, name in self.third_party:
            # Without a release timestamp Renovate would hold the update for ever, so such registries go unaged.
            behaviour = "timestamp-required" if registry(name) in DOCKER_HUB else "timestamp-optional"
            for t in AGED:
                with self.subTest(file=rel, image=name, update=t):
                    self.assertEqual(release_age(self.config, image_dep(rel, manager, name, t)), (WAIT, behaviour))

    def test_third_party_actions_wait_seven_days(self):
        for rel, name, dep_type in self.actions + [(".github/workflows/x.yml", "example/reusable", "workflow")]:
            if name.lower().startswith(ORG_REPOS):
                continue
            for t in AGED:
                with self.subTest(file=rel, action=name, update=t):
                    self.assertEqual(release_age(self.config, action_dep(rel, name, dep_type, t)),
                                     (WAIT, "timestamp-required"))

    def test_updates_renovate_cannot_age_never_wait(self):
        # The general rules, seen through packages no dedicated rule names (the k3s group ages all its updates).
        for t in UNAGED:
            with self.subTest(update=t):
                dep = image_dep("clusters/hetzner/example/app.yaml", "kubernetes", "example/app", t)
                self.assertIsNone(release_age(self.config, dep)[0])
                dep = action_dep(".github/workflows/example.yml", "example/action", "action", t)
                self.assertIsNone(release_age(self.config, dep)[0])
        for t in AGED:
            # Runner labels are GitHub's images, not third-party releases, and not in scope.
            runner = {"datasource": "github-runners", "manager": "github-actions", "depType": "github-runner",
                      "packageName": "ubuntu", "depName": "ubuntu", "updateType": t,
                      "fileName": ".github/workflows/ci.yml"}
            self.assertIsNone(release_age(self.config, runner)[0])

    def test_org_images_and_actions_are_excluded_not_unmatched(self):
        # Staging follows releases at once (INFRA-36, INFRA-91): no rule may delay an org image in any file. The same
        # image under another owner waits, so the exclusion is what lets it through.
        for rel, manager, name in self.org:
            other = "ghcr.io/example/" + name[len(ORG_IMAGES):]
            for t in AGED + UNAGED:
                with self.subTest(file=rel, image=name, update=t):
                    self.assertIsNone(release_age(self.config, image_dep(rel, manager, name, t))[0])
            self.assertEqual(release_age(self.config, image_dep(rel, manager, other, "patch"))[0], WAIT, other)
        for dep_type in ("action", "workflow"):
            for t in AGED:
                with self.subTest(depType=dep_type, update=t):
                    dep = action_dep(".github/workflows/x.yml", "fjcloudaiconsulting/.github", dep_type, t)
                    self.assertIsNone(release_age(self.config, dep)[0])
                    dep["packageName"] = dep["depName"] = "example/.github"
                    self.assertEqual(release_age(self.config, dep)[0], WAIT)

    def test_k3s_waits_as_long(self):
        dep = {"datasource": "github-releases", "manager": "regex", "packageName": "k3s-io/k3s",
               "depName": "k3s-io/k3s", "updateType": "patch", "fileName": "clusters/hetzner/system-upgrade/plans.yaml"}
        self.assertEqual(release_age(self.config, dep), (WAIT, "timestamp-required"))

    def test_release_age_values_are_valid(self):
        # renovate-config-validator checks option names, not these values.
        for i, rule in enumerate(self.config["packageRules"]):
            if rule.get("minimumReleaseAge") is not None:
                self.assertRegex(rule["minimumReleaseAge"], r"^\d+ (minutes|hours|days|weeks)$", i)
            if "minimumReleaseAgeBehaviour" in rule:
                self.assertIn(rule["minimumReleaseAgeBehaviour"], BEHAVIOURS, i)


class PipInstallsArePinnedByHash(unittest.TestCase):
    def setUp(self):
        self.installs = pip_installs()
        # (where, runs-on, requirements file name as written, path) for every -r file of every install.
        self.files = [(where, runs_on, name, ROOT / wd / name)
                      for where, runs_on, wd, argv in self.installs for name in requirement_files(argv)[0]]

    def test_every_pip_install_requires_hashes_from_a_file(self):
        self.assertTrue(self.installs, "no pip install found: the parser lost them")
        for where, _, wd, argv in self.installs:
            with self.subTest(where):
                files, positional = requirement_files(argv)
                self.assertIn("--require-hashes", argv)
                # Wheels only: an sdist build runs its own code and fetches build dependencies without hashes.
                self.assertIn(":all:", [argv[k + 1] for k, w in enumerate(argv[:-1]) if w == "--only-binary"])
                self.assertEqual(positional, [], "a package named on the command line is not hash-checked")
                self.assertTrue(files)
                for name in files:
                    self.assertTrue((ROOT / wd / name).is_file(), f"{wd}/{name}")

    def test_every_requirement_is_pinned_and_hashed(self):
        self.assertTrue(self.files)
        for _, _, _, path in self.files:
            lines = requirement_lines(path) if path.is_file() else []
            self.assertTrue(lines, path)
            for line in lines:
                with self.subTest(file=path.relative_to(ROOT).as_posix(), line=line[:60]):
                    # Also refuses options (-r, -e, --index-url): every line is one pinned, hashed package.
                    self.assertRegex(line, REQUIREMENT)

    def test_requirements_resolve_for_the_runner_and_say_how_to_regenerate(self):
        self.assertTrue(self.files)
        for where, runs_on, name, path in self.files:
            with self.subTest(where=where, file=name):
                header = path.read_text().splitlines()[:2] if path.is_file() else []
                self.assertEqual(header[:1], ["# This file was autogenerated by uv via the following command:"])
                cmd = shlex.split(header[1].lstrip("# "))
                self.assertEqual(cmd[:3], ["uv", "pip", "compile"])
                self.assertIn("--generate-hashes", cmd)
                self.assertEqual(cmd[cmd.index("-o") + 1], name)
                self.assertTrue((path.parent / cmd[3]).is_file(), f"source file {cmd[3]}")
                self.assertIn(runs_on, RUNNER_PYTHON, "a new runner label: check its python3 and regenerate")
                self.assertEqual(cmd[cmd.index("--python-version") + 1], RUNNER_PYTHON[runs_on])

    def test_renovate_updates_the_pins_after_the_same_wait(self):
        self.assertTrue(self.files)
        config = renovate()
        patterns = (config.get("pip_requirements") or {}).get("managerFilePatterns") or [PIP_REQUIREMENTS]
        ignored = [_pattern(g) for g in config.get("ignorePaths", [])]
        for _, _, _, path in self.files:
            rel = path.relative_to(ROOT).as_posix()
            with self.subTest(file=rel):
                self.assertTrue(any(_pattern(p).search(rel) for p in patterns), patterns)
                self.assertFalse([g for g in ignored if g.search(rel)])
                for line in requirement_lines(path) if path.is_file() else []:
                    package = re.split(r"[\[=]", line, maxsplit=1)[0]
                    dep = {"datasource": "pypi", "manager": "pip_requirements", "packageName": package,
                           "depName": package, "updateType": "minor", "fileName": rel}
                    self.assertEqual(release_age(config, dep), (WAIT, "timestamp-required"), package)

if __name__ == "__main__":
    unittest.main()
