"""Fences on the restore drill (DATA-05): clusters/hetzner/data/RESTORE.md steps 1-5, and how fresh the last one is.

The drill must replay the backups into the database images production runs. RESTORE.md used to pin its own MySQL and
Postgres digests, which drifted from the manifests Renovate bumps. Step 2 now reads them from the `data` StatefulSets
(or, on a cluster without them, from their manifests), so no image bump ever has to touch RESTORE.md.

The backup probe proves the nightly sets exist, not that they restore. docs/restore-drill.md records the last
successful drill, and restore-drill-probe.yml (logic in .github/scripts/check-restore-drill.py) keeps one issue open
while that is never or more than 100 days ago.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`.
"""
import datetime as dt
import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

from test_tbd_manifests import ROOT, containers, load_all

DATA = ROOT / "clusters" / "hetzner" / "data"
RESTORE = DATA / "RESTORE.md"
RECORD = ROOT / "docs" / "restore-drill.md"
RUNBOOKS = ROOT / "docs" / "runbooks.md"
SCRIPT = ROOT / ".github" / "scripts" / "check-restore-drill.py"
WORKFLOW = ROOT / ".github" / "workflows" / "restore-drill-probe.yml"
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


# ---- the drill record and the probe's age logic --------------------------------------------------------------------

def load_probe():
    spec = importlib.util.spec_from_file_location("check_restore_drill", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TODAY = dt.date(2027, 3, 1)


def record(value):
    return f"# Restore drill record\n\nprose\n\nlast-successful-drill: {value}\n"


def ago(days):
    return (TODAY - dt.timedelta(days=days)).isoformat()


class RecordAndAge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probe = load_probe()

    def verdict(self, text):
        return self.probe.verdict(self.probe.parse_record(text), TODAY)[0]

    def test_the_committed_record_parses(self):
        # Kills: a record PR that the probe would read as could-not-run (CI runs this on every PR).
        last = self.probe.parse_record(RECORD.read_text())
        self.probe.verdict(last, dt.datetime.now(dt.timezone.utc).date())

    def test_never_is_overdue(self):
        self.assertEqual("overdue", self.verdict(record("never")))

    def test_the_limit_is_100_days(self):
        # 101 flags (kills "no limit"), 100 and 99 pass (kills `>=` and an off-by-one the other way).
        self.assertEqual("overdue", self.verdict(record(ago(101))))
        self.assertEqual("fresh", self.verdict(record(ago(100))))
        self.assertEqual("fresh", self.verdict(record(ago(99))))
        self.assertEqual("fresh", self.verdict(record(ago(0))))
        self.assertEqual(100, self.probe.MAX_AGE_DAYS)

    def test_a_future_date_is_refused(self):
        # A typo such as 2072 must not silence the probe for decades.
        with self.assertRaises(self.probe.RecordError):
            self.verdict(record((TODAY + dt.timedelta(days=1)).isoformat()))

    def test_a_malformed_record_is_refused(self):
        bad = {
            "no record line": "# Restore drill record\n",
            "two record lines": record("never") + "last-successful-drill: 2027-01-01\n",
            "not a calendar date": record("2027-02-30"),
            "not a date": record("yesterday"),
            "trailing text": record(f"{ago(1)} (INFRA-214)"),
            "other case": "Last-Successful-Drill: never\n",
        }
        for name, text in bad.items():
            with self.subTest(name), self.assertRaises(self.probe.RecordError):
                self.probe.parse_record(text)

    def test_the_runbook_names_the_record_and_its_format(self):
        # The runbook says which file and which line to change; that line must be what the probe reads.
        text = RUNBOOKS.read_text()
        section = re.search(r"\n## Restore drill\n(.*?)(?=\n## )", text, re.S)
        self.assertIsNotNone(section, "docs/runbooks.md has no 'Restore drill' section")
        self.assertIn("[`docs/restore-drill.md`](restore-drill.md)", section.group(1))
        fmt = re.search(r"`(last-successful-drill: YYYY-MM-DD)`", section.group(1))
        self.assertIsNotNone(fmt, "the runbook does not give the record line's format")
        self.assertEqual(TODAY, self.probe.parse_record(fmt.group(1).replace("YYYY-MM-DD", TODAY.isoformat())))


# ---- the probe's issue: one, ours, opened or updated when overdue, closed when fresh -------------------------------

TITLE = "[restore-drill] database restore drill is overdue"
BOT = "app/github-actions"
# Same contract as the release drift and backup probe stubs: honour --author only through --search, emit
# number<TAB>title for the @tsv --jq.
GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
if [[ "$1 $2" == "issue list" ]]; then
  [[ -z "${LIST_FAIL:-}" ]] || exit 1
  [[ "$*" == *--search* && "$*" == *in:title* ]] || exit 0
  if [[ "$*" == *@tsv* && "$*" == *.title* ]]; then fmt='$2 "\\t" $3'; else fmt='$2'; fi
  if [[ "$*" == *"--author app/github-actions"* ]]; then want=app/github-actions; else want=; fi
  printf '%s' "${ISSUES:-}" | awk -F'\\t' -v w="$want" '!w || $1 == w { print '"$fmt"' }'
elif [[ -n "${WRITE_FAIL:-}" ]]; then
  exit 1
fi
"""


def run_probe(text, *, issue=True, issues="", list_fail="", write_fail=""):
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        rec = d / "restore-drill.md"
        if text is not None:
            rec.write_text(text)
        (d / "bin").mkdir()
        gh = d / "bin" / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        args = [sys.executable, "-I", str(SCRIPT)] + (["--issue"] if issue else []) + [str(rec)]
        r = subprocess.run(args, capture_output=True, text=True, cwd=d, env={
            **os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}", "GH_LOG": str(d / "log"), "GH_REPO": "o/r",
            "RUN_URL": "u", "TODAY": TODAY.isoformat(), "ISSUES": issues, "LIST_FAIL": list_fail,
            "WRITE_FAIL": write_fail})
        log = (d / "log").read_text() if (d / "log").exists() else ""
        return r, log


def writes(log):
    return [ln for ln in log.splitlines() if re.match(r"issue (create|edit|close|comment)\b", ln)]


class ProbeIssue(unittest.TestCase):
    def test_never_drilled_opens_the_issue(self):
        r, log = run_probe(record("never"))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn(f"issue create --title {TITLE}", log)

    def test_101_days_opens_the_issue_and_99_days_does_not(self):
        self.assertIn("issue create", run_probe(record(ago(101)))[1])
        r, log = run_probe(record(ago(99)))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual([], writes(log))

    def test_the_open_issue_is_updated_not_duplicated(self):
        r, log = run_probe(record(ago(101)), issues=f"{BOT}\t7\t{TITLE}\n")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("issue edit 7 --body", log)
        self.assertNotIn("issue create", log)

    def test_a_recorded_drill_closes_the_open_issue(self):
        r, log = run_probe(record(ago(99)), issues=f"{BOT}\t7\t{TITLE}\n")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual(["issue close 7"], [w.split(" --")[0] for w in writes(log)])

    def test_an_outsider_issue_with_our_title_is_never_touched(self):
        # Public repo: an outsider's same-title issue must not be edited (overdue) or closed (fresh).
        outsider = f"mallory\t9\t{TITLE}\n"
        r, log = run_probe(record("never"), issues=outsider)
        self.assertIn("issue create", log)
        self.assertNotIn("issue edit 9", log)
        r, log = run_probe(record(ago(1)), issues=outsider)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual([], writes(log))

    def test_a_bot_issue_with_a_longer_title_is_not_ours(self):
        # Exact title, not prefix: a bot issue sharing only the prefix is not ours, so we open our own.
        other = f"{BOT}\t9\t{TITLE} (copy)\n"
        r, log = run_probe(record("never"), issues=other)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("issue create", log)
        self.assertNotIn("issue edit 9", log)
        r, log = run_probe(record(ago(1)), issues=other)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual([], writes(log))

    def test_an_unreadable_record_raises_the_issue_and_fails(self):
        for name, text in {"missing file": None, "malformed": record("soon"), "future": record("2099-01-01")}.items():
            with self.subTest(name):
                r, log = run_probe(text)
                self.assertEqual(2, r.returncode, r.stdout)
                self.assertIn("could not read the drill record", log)
                self.assertIn("issue create", log)

    def test_a_failed_lookup_touches_nothing(self):
        # Fails closed: a broken lookup must never read as "no issue" and open a duplicate or close the real one.
        for text in (record("never"), record(ago(1))):
            r, log = run_probe(text, issues=f"{BOT}\t7\t{TITLE}\n", list_fail="1")
            self.assertEqual(3, r.returncode, r.stdout)
            self.assertEqual([], writes(log))

    def test_a_failed_write_fails_the_run(self):
        r, _ = run_probe(record("never"), write_fail="1")
        self.assertEqual(3, r.returncode, r.stdout)

    def test_without_issue_it_only_reports(self):
        # A local run by hand must not open or close issues under the owner's name.
        for text, rc in ((record(ago(1)), 0), (record("never"), 1), (record("x"), 2)):
            r, log = run_probe(text, issue=False)
            self.assertEqual(rc, r.returncode, r.stdout)
            self.assertEqual("", log)

    def test_the_issue_carries_no_infrastructure_names(self):
        # Public issue (GHA-07): the verdict, the record file and the runbook only.
        _, log = run_probe(record("never"))
        self.assertIn("docs/restore-drill.md", log)
        self.assertNotRegex(log, r"(?i)\b\d{12}\b|s3://|arn:|bucket|amazonaws|backups-")


# ---- the workflow: pinned, least privilege, no expressions in run --------------------------------------------------

class ProbeWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = WORKFLOW.read_text()
        (cls.wf,) = load_all(WORKFLOW)
        cls.jobs = cls.wf["jobs"]

    def steps(self):
        return [(name, s) for name, job in self.jobs.items() for s in job.get("steps") or []]

    def test_triggers_are_schedule_dispatch_and_main_only(self):
        # No pull_request trigger: a PR context gets no issues: write. No dispatch inputs reach the script.
        on = self.wf["on"]
        self.assertEqual({"schedule", "workflow_dispatch", "push"}, set(on))
        self.assertTrue(on["schedule"] and all("cron" in s for s in on["schedule"]))
        self.assertIsNone(on["workflow_dispatch"])
        self.assertEqual(["main"], on["push"]["branches"])
        for path in on["push"]["paths"]:
            self.assertTrue((ROOT / path).is_file(), f"push path does not exist: {path}")
        self.assertIn("docs/restore-drill.md", on["push"]["paths"], "a merged record must close the issue at once")

    def test_permissions_are_least_privilege(self):
        self.assertEqual({"contents": "read"}, self.wf["permissions"])
        self.assertEqual({"contents": "read", "issues": "write"}, self.jobs["probe"]["permissions"])
        self.assertEqual({"actions": "write"}, self.jobs["keepalive"]["permissions"])
        self.assertEqual({"probe", "keepalive"}, set(self.jobs))

    def test_every_action_is_pinned_by_commit(self):
        uses = [s["uses"] for _, s in self.steps() if "uses" in s]
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
            self.assertRegex(self.raw, rf"uses: {re.escape(ref)} # v\d", f"{ref} has no version comment")
        for _, s in self.steps():
            if s.get("uses", "").startswith("actions/checkout@"):
                self.assertIs(False, (s.get("with") or {}).get("persist-credentials"))

    def test_no_expression_in_run(self):
        # Every value reaches a command through env:, never spliced into the script text.
        for job, s in self.steps():
            self.assertNotIn("${{", s.get("run", ""), f"{job}: expression in run: {s.get('run')}")

    def test_the_probe_runs_this_script_on_the_record(self):
        # A wrong path reads as could-not-run every week.
        runs = [s["run"] for _, s in self.steps() if "check-restore-drill.py" in s.get("run", "")]
        self.assertEqual(["python3 -I .github/scripts/check-restore-drill.py --issue docs/restore-drill.md"], runs)

    def test_a_keepalive_job_re_enables_this_workflow(self):
        # Kills: no keepalive, so 60 quiet days in this public repo disable a 100-day signal before it fires.
        (step,) = self.jobs["keepalive"]["steps"]
        self.assertEqual('gh api -X PUT "repos/${GH_REPO}/actions/workflows/restore-drill-probe.yml/enable"', step["run"])


if __name__ == "__main__":
    unittest.main()
