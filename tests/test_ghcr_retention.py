"""GHCR retention (INFRA-92): fake HTTP layer only, no network. Stdlib only."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import pathlib
import re
import tempfile
import unittest
import urllib.parse

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / ".github/scripts/ghcr-retention.py"
spec = importlib.util.spec_from_file_location("ghcr_retention", SCRIPT)
gr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gr)

NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone.utc)
TOKEN = "REGISTRYBEARER-s3cret"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_IMG = "application/vnd.oci.image.manifest.v1+json"
ZB, ZM, TB = "ziftbook/backend", "ziftbook/migrations", "tbd/backend"
ORG = "/orgs/fjcloudaiconsulting/packages/container/"


def dg(n):
    return "sha256:" + f"{n:064x}"


class World:
    """Fake GitHub API + GHCR registry. add() registers a version and its manifest."""

    def __init__(self, page_size=100):
        self.pkgs = {p: [] for p in gr.PACKAGES}
        self.manifests = {}  # (pkg, digest) -> (status, body dict)
        self.reqs = []  # (method, url, headers)
        self.deleted = []
        self.page_size = page_size
        self.count_script = []  # values returned by the next count reads, then the truth
        self.on_get_version = {}  # id -> fn(version) run before a single-version GET answers
        self.delete_status = 204

    def add(self, pkg, vid, tags, age, children=None, kind=None, tagsmissing=False, manifest=True):
        v = {"id": vid, "name": dg(vid), "created_at": (NOW - dt.timedelta(days=age)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "metadata": {"container": {"tags": list(tags)}}}
        if tagsmissing:
            v["metadata"] = {"container": {}}
        self.pkgs[pkg].append(v)
        if manifest:
            if children is not None:
                body = {"mediaType": OCI_INDEX, "manifests": [{"digest": dg(c)} for c in children]}
            else:
                body = {"mediaType": kind or OCI_IMG}
            self.manifests[(pkg, dg(vid))] = (200, body)
        return v

    def http(self, method, url, headers):
        self.reqs.append((method, url, dict(headers)))
        u = urllib.parse.urlparse(url)
        if u.netloc == "ghcr.io" and u.path == "/token":
            return 200, {}, json.dumps({"token": TOKEN}).encode()
        m = re.match(r"/v2/fjcloudaiconsulting/(.+)/manifests/(sha256:[0-9a-f]{64})$", u.path)
        if u.netloc == "ghcr.io" and m:
            st, body = self.manifests.get((m[1], m[2]), (404, {}))
            return st, {}, json.dumps(body).encode()
        m = re.match(r"/orgs/fjcloudaiconsulting/packages/container/([^/]+)(?:/versions(?:/(\d+))?)?$", u.path)
        pkg = urllib.parse.unquote(m[1])
        vs = self.pkgs[pkg]
        if m[2]:
            v = next(x for x in vs if x["id"] == int(m[2]))
            if method == "DELETE":
                self.deleted.append(v["id"])
                if self.delete_status == 204:
                    vs.remove(v)
                return self.delete_status, {}, b""
            if v["id"] in self.on_get_version:
                self.on_get_version[v["id"]](v)
            return 200, {}, json.dumps(v).encode()
        if "/versions" not in u.path:
            n = self.count_script.pop(0) if self.count_script else len(vs)
            return 200, {}, json.dumps({"version_count": n}).encode()
        page = int(urllib.parse.parse_qs(u.query).get("page", ["1"])[0])
        chunk = vs[(page - 1) * self.page_size: page * self.page_size]
        last = max(1, -(-len(vs) // self.page_size))
        link = lambda n, rel: f'<https://api.github.com{u.path}?per_page=100&page={n}>; rel="{rel}"'
        rels = ([link(page - 1, "prev")] if page > 1 else []) + ([link(page + 1, "next")] if page < last else []) \
            + ([link(last, "last")] if page < last else []) + ([link(1, "first")] if page > 1 else [])
        return 200, {"Link": ", ".join(rels)} if rels else {}, json.dumps(chunk).encode()

    def deletes(self):
        return [r for r in self.reqs if r[0] == "DELETE"]


def run(world, clusters=None, **env):
    e = {"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "schedule", "GITHUB_TOKEN": "ghs_x",
         "DELETE_APPS": "ziftbook,tbd", "MODE": "", "APP": "", "MAX_DELETES": ""}
    e.update(env)
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = gr.main(env=e, http=world.http, now=NOW, clusters=clusters or d)
    return rc, out.getvalue(), err.getvalue()


def clusters_with(text):
    d = tempfile.TemporaryDirectory()
    p = pathlib.Path(d.name) / "hetzner" / "x-prod"
    p.mkdir(parents=True)
    (p / "a.yaml").write_text(text)
    return d


class Selection(unittest.TestCase):
    def test_f1_old_untagged_children_of_v_index_kept(self):
        """Kills the literal 'delete untagged > 7 d' rule that breaks released images."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 2, [], 100)
        w.add(ZB, 10, ["v1.0.0"], 100, children=[1, 2])
        w.add(ZB, 3, [], 100)  # orphan: the control that deletes still happen
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (0, [3]))

    def test_f2_children_follow_their_index(self):
        """Kills protect-only-v-children and never-delete-children: young sha- index keeps its children, old one loses them."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 10, ["sha-aaaaaaa"], 5, children=[1])
        w.add(ZB, 2, [], 100)
        w.add(ZB, 20, ["sha-bbbbbbb"], 40, children=[2])
        rc, _, _ = run(w)
        self.assertEqual((rc, sorted(w.deleted)), (0, [2, 20]))

    def test_f3_shared_child_kept_by_v_index(self):
        """Kills a per-old-index delete: a child shared by an old sha- index and a v index must stay."""
        w = World()
        w.add(ZB, 1, [], 100)  # shared
        w.add(ZB, 2, [], 100)  # only the old sha- index
        w.add(ZB, 10, ["v1.0.0"], 100, children=[1])
        w.add(ZB, 20, ["sha-bbbbbbb"], 40, children=[1, 2])
        rc, _, _ = run(w)
        self.assertEqual((rc, sorted(w.deleted)), (0, [2, 20]))

    def test_f4_nested_index_and_attestation_entry_kept(self):
        """Kills a non-recursive or platform-filtered walk: nested index and an unknown/unknown entry stay."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 2, [], 100)  # attestation-style entry
        w.add(ZB, 5, [], 100, children=[1, 2])  # nested index
        w.add(ZB, 10, ["v1.0.0"], 100, children=[5])
        w.manifests[(ZB, dg(5))][1]["manifests"][1]["platform"] = {"architecture": "unknown", "os": "unknown"}
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (0, []))

    def test_f5_any_non_sha_tag_is_a_root(self):
        """Kills first-tag classification and v-only roots: [v, sha-], latest and major.minor survive."""
        w = World()
        w.add(ZB, 1, ["v1.2.3", "sha-abc1234"], 90)
        w.add(ZB, 5, ["sha-abc1234", "v1.2.3"], 90)  # sha- first: tags[0] classification deletes it
        w.add(ZB, 2, ["latest"], 90)
        w.add(ZB, 3, ["0.20"], 90)
        w.add(ZB, 4, ["sha-abc1234"], 90)  # control
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (0, [4]))

    def test_f6_boundaries_with_injected_now(self):
        """Kills off-by-one: exactly 30.0 d sha- and 7.0 d untagged are kept, a hair older is deleted."""
        w = World()
        w.add(ZB, 1, ["sha-aaaaaaa"], 30.0)
        w.add(ZB, 2, ["sha-bbbbbbb"], 30.01)
        w.add(ZB, 3, [], 7.0)
        w.add(ZB, 4, [], 7.01)
        rc, _, _ = run(w)
        self.assertEqual((rc, sorted(w.deleted)), (0, [2, 4]))

    def test_f10_pinned_digest_kept_both_forms(self):
        """Kills ignoring clusters/ pins: old untagged digests pinned as <pkg>:tag@sha and <pkg>@sha stay."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 2, [], 100)
        w.add(ZB, 3, [], 100)
        d = clusters_with(f"image: ghcr.io/fjcloudaiconsulting/{ZB}:v1.0.0@{dg(1)}\nimage: ghcr.io/fjcloudaiconsulting/{ZB}@{dg(2)}\n")
        self.addCleanup(d.cleanup)
        rc, _, _ = run(w, clusters=d.name)
        self.assertEqual((rc, w.deleted), (0, [3]))

    def test_f10_pinned_digest_absent_aborts(self):
        """Kills silently skipping a pin the registry no longer lists."""
        w = World()
        w.add(ZB, 3, [], 100)
        d = clusters_with(f"image: ghcr.io/fjcloudaiconsulting/{ZB}:v1.0.0@{dg(99)}\n")
        self.addCleanup(d.cleanup)
        rc, _, _ = run(w, clusters=d.name)
        self.assertEqual((rc, w.deleted), (1, []))


class Listing(unittest.TestCase):
    def test_f7_three_pages_all_seen(self):
        """Kills reading only the first page."""
        w = World(page_size=2)
        for i in range(1, 6):
            w.add(ZB, i, [], 100)
        rc, _, _ = run(w)
        self.assertEqual((rc, sorted(w.deleted)), (0, [1, 2, 3, 4, 5]))

    def test_first_url_regardless_of_rel_parser_aborts_not_hangs(self):
        """Kills a Link parser that follows the first <url> (prev on the last page loops) and a missing page bound."""
        w = World(page_size=2)
        for i in range(1, 6):
            w.add(ZB, i, [], 100)
        orig = gr.NEXT
        gr.NEXT = re.compile(r'<([^>]+)>')
        self.addCleanup(setattr, gr, "NEXT", orig)
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (1, []))
        self.assertLess(len(w.reqs), 100)

    def test_f7_count_mismatch_before_or_after_aborts(self):
        """Kills reading version_count once (or not at all): a drift before OR after listing aborts."""
        for script in ([99, 99, 99, 99], [1, 99, 1, 99]):
            w = World()
            w.add(ZB, 1, [], 100)
            orig = w.http

            def http(method, url, headers, w=w, orig=orig, script=script):
                if ZB.replace("/", "%2F") in url and "/versions" not in url and method == "GET" and script:
                    return 200, {}, json.dumps({"version_count": script.pop(0)}).encode()
                return orig(method, url, headers)
            w.http = http
            rc, _, _ = run(w)
            self.assertEqual((rc, w.deleted), (1, []), script)

    def test_f7_transient_mismatch_retries_once(self):
        """Kills aborting on the first mismatch (spec: retry the whole listing once)."""
        w = World()
        w.add(ZB, 1, [], 100)
        orig = w.http
        seq = [5]

        def http(method, url, headers):
            if ZB.replace("/", "%2F") in url and "/versions" not in url and method == "GET" and seq:
                return 200, {}, json.dumps({"version_count": seq.pop(0)}).encode()
            return orig(method, url, headers)
        w.http = http
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (0, [1]))

    def test_f9_missing_tags_key_aborts(self):
        """Kills .get('tags', []) defaulting a version with no tags field to untagged."""
        w = World()
        w.add(ZB, 1, [], 100, tagsmissing=True)
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (1, []))


class Aborts(unittest.TestCase):
    def last_package_world(self):
        w = World()
        w.add(ZB, 1, [], 100)  # deletable in an earlier package
        w.add(ZM, 2, ["v1.0.0"], 100)
        return w

    def test_f8_manifest_500_on_last_package_root_aborts_everything(self):
        """Kills skip-on-error and per-package delete: zero DELETEs anywhere when the last package fails."""
        w = self.last_package_world()
        w.manifests[(ZM, dg(2))] = (500, {})
        rc, _, _ = run(w, APP="all")
        self.assertEqual((rc, w.deleted), (1, []))

    def test_f8_unknown_media_type_aborts_everything(self):
        """Kills treating an unknown manifest media type as a leaf image."""
        w = self.last_package_world()
        w.manifests[(ZM, dg(2))] = (200, {"mediaType": "application/x-weird"})
        rc, _, _ = run(w, APP="all")
        self.assertEqual((rc, w.deleted), (1, []))

    def test_f8_child_missing_from_listing_aborts(self):
        """Kills ignoring a child digest the version listing does not have."""
        w = self.last_package_world()
        w.manifests[(ZM, dg(2))] = (200, {"mediaType": OCI_INDEX, "manifests": [{"digest": dg(77)}]})
        rc, _, _ = run(w, APP="all")
        self.assertEqual((rc, w.deleted), (1, []))

    def test_f8_manifest_requests_carry_all_four_accept_types(self):
        """Kills a manifest GET without Accept (GHCR answers 404) or with only the index types."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 10, ["v1.0.0"], 100, children=[])
        run(w)
        ms = [r for r in w.reqs if "/manifests/" in r[1]]
        self.assertTrue(ms)
        for _, _, h in ms:
            for t in (OCI_INDEX, "application/vnd.docker.distribution.manifest.list.v2+json", OCI_IMG,
                      "application/vnd.docker.distribution.manifest.v2+json"):
                self.assertIn(t, h["Accept"])

    def test_f12_cap_is_per_deleting_package_overflow_in_last_aborts_everything(self):
        """Kills a global total or running cap: ZB 1 + ZM 3 deletes, cap 2 aborts with zero DELETEs; cap 3 passes."""
        w = World()
        w.add(ZB, 1, [], 100)
        for i in (2, 3, 4):
            w.add(ZM, i, [], 100)
        rc, _, _ = run(w, MAX_DELETES="2")
        self.assertEqual((rc, w.deleted), (1, []))
        rc, _, _ = run(w, MAX_DELETES="3")
        self.assertEqual((rc, sorted(w.deleted)), (0, [1, 2, 3, 4]))

    def test_f12_dry_run_package_never_counts_toward_cap(self):
        """Kills counting dry-run packages: a huge tbd dry-run plan must not abort ziftbook's delete."""
        w = World()
        w.add(ZB, 1, [], 100)
        for i in range(10, 16):
            w.add(TB, i, [], 100)
        rc, _, _ = run(w, DELETE_APPS="ziftbook", MAX_DELETES="2")
        self.assertEqual((rc, w.deleted), (0, [1]))

    def test_default_cap_is_400_per_package(self):
        """Kills the old 300 default: 400 deletes pass with MAX_DELETES unset, 401 abort."""
        for n, want in ((400, 0), (401, 1)):
            w = World(page_size=100)
            for i in range(1, n + 1):
                w.add(ZB, i, [], 100)
            rc, _, _ = run(w, DELETE_APPS="ziftbook", APP="ziftbook")
            self.assertEqual((rc, len(w.deleted)), (want, n if want == 0 else 0), n)

    def test_f12_bad_max_deletes_rejected(self):
        """Kills int() without validation: 0, abc, -1 and 1.5 are all rejected."""
        for bad in ("0", "abc", "-1", "1.5", "\u00b2", "5\n"):
            w = World()
            w.add(ZB, 1, [], 100)
            rc, _, err = run(w, MAX_DELETES=bad, GITHUB_ACTIONS="true")
            self.assertEqual((rc, w.deleted, w.reqs), (1, [], []), bad)
            self.assertTrue(err.startswith("::error::"), bad)

    def test_f17_bearer_never_printed_on_500_path(self):
        """Kills logging headers or token responses: the bearer appears only in its ::add-mask:: line."""
        w = self.last_package_world()
        w.manifests[(ZM, dg(2))] = (500, {})
        with tempfile.TemporaryDirectory() as d:
            summ = pathlib.Path(d) / "s.md"
            rc, out, err = run(w, GITHUB_STEP_SUMMARY=str(summ), GITHUB_ACTIONS="true")
            text = out + err + (summ.read_text() if summ.exists() else "")
        self.assertEqual(rc, 1)
        self.assertIn(f"::add-mask::{TOKEN}", out)
        self.assertNotIn(TOKEN, re.sub(r"::add-mask::.*", "", text))

    def test_add_mask_only_inside_github_actions(self):
        """Kills printing workflow commands locally: no ::add-mask:: and no token without GITHUB_ACTIONS."""
        w = self.last_package_world()
        rc, out, err = run(w)
        self.assertEqual(rc, 0)
        self.assertNotIn("::add-mask::", out + err)
        self.assertNotIn(TOKEN, out + err)

    def test_candidate_manifest_non_200_aborts(self):
        """Kills deleting a version whose own manifest could not be read."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 2, [], 100)
        w.manifests[(ZB, dg(2))] = (404, {})
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (1, []))

    def test_duplicate_id_bad_digest_bad_created_at_abort(self):
        """Kills trusting the listing: duplicate id, malformed digest and unparseable created_at all abort."""
        def dup(w):
            w.pkgs[ZB].append(dict(w.pkgs[ZB][0]))

        def digest(w):
            w.pkgs[ZB][0]["name"] = "sha256:short"

        def created(w):
            w.pkgs[ZB][0]["created_at"] = "yesterday"
        for brk in (dup, digest, created):
            w = World()
            w.add(ZB, 1, [], 100)
            w.add(ZB, 2, [], 100)
            brk(w)
            rc, _, _ = run(w)
            self.assertEqual((rc, w.deleted), (1, []), brk.__name__)

    def test_no_assert_statements_in_script(self):
        """Kills guards that vanish under python -O: the script must not rely on assert."""
        import ast
        tree = ast.parse(SCRIPT.read_text())
        self.assertEqual([n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)], [])


class DeletePhase(unittest.TestCase):
    def test_f11_dry_run_variants_send_no_delete(self):
        """Kills any path that deletes when it should only plan: dispatch dry-run, empty DELETE_APPS, non-main ref, push."""
        cases = [dict(GITHUB_EVENT_NAME="workflow_dispatch", MODE="dry-run"), dict(DELETE_APPS=""),
                 dict(GITHUB_EVENT_NAME="workflow_dispatch", MODE="delete", GITHUB_REF="refs/heads/feat"),
                 dict(GITHUB_EVENT_NAME="push"), dict(GITHUB_EVENT_NAME="workflow_dispatch", MODE="")]
        for env in cases:
            w = World()
            w.add(ZB, 1, [], 100)
            rc, out, _ = run(w, **env)
            self.assertEqual((rc, w.deletes()), (0, []), env)
            self.assertIn(dg(1), out)  # the dry run still lists it

    def test_f11_only_apps_in_delete_apps_are_deleted(self):
        """Kills a global switch: ziftbook enabled, tbd stays dry; dispatch mode=delete works."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(TB, 2, [], 100)
        rc, _, _ = run(w, DELETE_APPS="ziftbook", GITHUB_EVENT_NAME="workflow_dispatch", MODE="delete")
        self.assertEqual((rc, w.deleted), (0, [1]))

    def test_f13_indexes_deleted_before_children(self):
        """Kills sort-by-id: the children have LOWER ids than their index, the index must still go first."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 2, [], 100)
        w.add(ZB, 50, ["sha-aaaaaaa"], 40, children=[1, 2])
        w.add(ZB, 60, [], 100)
        rc, _, _ = run(w)
        self.assertEqual(rc, 0)
        self.assertEqual(w.deleted[0], 50)
        self.assertEqual(sorted(w.deleted), [1, 2, 50, 60])

    def test_nested_index_deleted_outermost_first_even_with_lower_inner_id(self):
        """Kills index-before-leaf only: inner index (id 5) is the outer's (id 50) child and must go after it, before leaves."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 5, [], 100, children=[1])
        w.add(ZB, 50, ["sha-aaaaaaa"], 40, children=[5])
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (0, [50, 5, 1]))

    def test_f14_changed_tags_stop_all_further_deletes(self):
        """Kills skipping only the changed version: deleting its children would break the new release."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 2, [], 100)
        w.add(ZB, 50, ["sha-aaaaaaa"], 40, children=[1, 2])
        w.on_get_version[50] = lambda v: v["metadata"]["container"]["tags"].append("v9.9.9")
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (1, []))

    def test_f14_change_on_second_index_stops_after_first(self):
        """Kills continue-on-change when the change is not the first DELETE."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 2, [], 100)
        w.add(ZB, 50, ["sha-aaaaaaa"], 40, children=[1])
        w.add(ZB, 51, ["sha-bbbbbbb"], 41, children=[2])
        w.on_get_version[51] = lambda v: v["metadata"]["container"]["tags"].append("v9.9.9")
        rc, _, _ = run(w)
        self.assertEqual((rc, w.deleted), (1, [50]))

    def test_first_non_204_stops(self):
        """Kills carrying on after a refused DELETE."""
        w = World()
        w.add(ZB, 1, [], 100)
        w.add(ZB, 2, [], 100)
        w.delete_status = 403
        rc, _, _ = run(w)
        self.assertEqual((rc, len(w.deletes())), (1, 1))

    def test_f15_single_app_makes_no_requests_to_the_other(self):
        """Kills listing every package regardless of the app input."""
        w = World()
        w.add(ZB, 1, [], 100)
        rc, _, _ = run(w, APP="ziftbook")
        self.assertEqual(rc, 0)
        self.assertTrue(w.reqs)
        self.assertFalse([r for r in w.reqs if re.search(r"fjcloudaiconsulting(/|%2F)?tbd|container/tbd", r[1])])


class Guard(unittest.TestCase):
    def test_f16_wrapper_refuses_package_delete_and_writes(self):
        """Kills an unguarded transport: DELETE on a package path and POST/PUT/PATCH never reach the network."""
        sent = []
        g = gr.guarded(lambda m, u, h: sent.append((m, u)) or (204, {}, b""))
        base = "https://api.github.com" + ORG
        for m, u in (("DELETE", base + "ziftbook%2Fbackend"), ("DELETE", base + "ziftbook%2Fbackend/versions"),
                     ("DELETE", base + "ziftbook%2Fbackend/versions/12x"), ("POST", base + "ziftbook%2Fbackend/versions/1"),
                     ("PUT", base + "x"), ("PATCH", base + "x"),
                     ("DELETE", "https://api.github.com/orgs/other/packages/container/a/versions/1")):
            with self.assertRaises(Exception, msg=(m, u)):
                g(m, u, {})
        self.assertEqual(sent, [])
        self.assertEqual(g("DELETE", base + "ziftbook%2Fbackend/versions/12", {})[0], 204)

    def test_delete_url_is_fullmatched_against_known_packages(self):
        """Kills prefix/path-only matching: traversal, unknown package, query, fragment, trailing newline are refused."""
        sent = []
        g = gr.guarded(lambda m, u, h: sent.append((m, u)) or (204, {}, b""))
        base = "https://api.github.com" + ORG
        for u in (base + "..%2Fx/versions/1", base + "../versions/1", base + "other%2Fbackend/versions/1",
                  base + "ziftbook%2Fbackend/versions/1?a=b", base + "ziftbook%2Fbackend/versions/1#f",
                  base + "ziftbook%2Fbackend/versions/1\n"):
            with self.assertRaises(gr.Abort, msg=u):
                g("DELETE", u, {})
        self.assertEqual(sent, [])

    def test_get_only_api_github_and_ghcr_hosts(self):
        """Kills an open GET: another host (or scheme) never leaves the process."""
        sent = []
        g = gr.guarded(lambda m, u, h: sent.append(u) or (200, {}, b"{}"))
        for u in ("https://evil.example/x", "http://api.github.com/x", "https://api.github.com.evil.example/x"):
            with self.assertRaises(gr.Abort, msg=u):
                g("GET", u, {})
        self.assertEqual(sent, [])
        g("GET", "https://ghcr.io/token", {})
        g("GET", "https://api.github.com/orgs/x", {})

    def test_transport_does_not_follow_redirects(self):
        """Kills following a 3xx (a redirect could carry the bearer elsewhere): the 302 comes back as a status."""
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/elsewhere" else 302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.assertEqual(gr.urlopen_http("GET", f"http://127.0.0.1:{srv.server_port}/", {})[0], 302)


if __name__ == "__main__":
    unittest.main()
