#!/usr/bin/env python3
"""GHCR retention (INFRA-92): prune the app images' container versions. Dry run unless every gate holds.

Policy: never delete a version with a non-sha- tag (v*, major.minor, latest, anything else); keep sha-<hex>
versions <= 30 days old and untagged versions <= 7 days old; keep every digest pinned under clusters/**; keep
everything reachable from those roots through the registry manifests (an untagged version is usually a platform
image of a kept multi-arch index). Everything else is deleted, outermost index first, then inner indexes, then leaf manifests.
Every package is planned before any DELETE: one abort anywhere means zero DELETEs.
GET only to api.github.com and ghcr.io, never following a redirect. Exit 1 on any abort.
Delete needs ALL of: GITHUB_REF == refs/heads/main; event schedule, or workflow_dispatch with MODE=delete;
the package's app listed in DELETE_APPS (comma separated, git-tracked in the workflow).
Env: GITHUB_TOKEN, GITHUB_REF, GITHUB_EVENT_NAME, MODE, APP (ziftbook|tbd|all), MAX_DELETES (default 400, per package, only packages that delete count),
DELETE_APPS, GITHUB_STEP_SUMMARY. Unverified: whether version_count counts soft-deleted versions (INFRA-92 spec).

Usage: ghcr-retention.py [clusters-dir]   (default: clusters)
"""
import base64
import datetime as dt
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ORG = "fjcloudaiconsulting"
API = "https://api.github.com"
PACKAGES = ["tbd/backend", "tbd/migrations", "tbd/frontend", "tbd/mcp",
            "ziftbook/backend", "ziftbook/frontend", "ziftbook/migrations"]
SHA_TAG = re.compile(r"sha-[0-9a-f]{7,}$")
V_TAG = re.compile(r"v\d")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}$")
DELETE_URL = re.compile(re.escape(API) + "/orgs/" + ORG + "/packages/container/(?:"
                        + "|".join(re.escape(urllib.parse.quote(p, safe="")) for p in PACKAGES) + r")/versions/\d+")
GET_HOSTS = ("api.github.com", "ghcr.io")
NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
IMAGE_TYPES = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
ACCEPT = ", ".join(INDEX_TYPES + IMAGE_TYPES)
SHA_DAYS, UNTAGGED_DAYS, DEFAULT_MAX = 30, 7, 400


class Abort(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx surfaces as an HTTPError, i.e. a non-200 answer


def urlopen_http(method, url, headers):
    """(status, headers, body) for one request; HTTP errors are answers, not exceptions. No redirects."""
    try:
        with urllib.request.build_opener(NoRedirect).open(urllib.request.Request(url, method=method, headers=headers), timeout=60) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def guarded(http):
    """Only GET, and DELETE of one version of an org container package, ever leave this process."""
    def call(method, url, headers):
        if method == "DELETE":
            if not DELETE_URL.fullmatch(url):
                raise Abort(f"refusing DELETE {url!r}")
        elif method != "GET":
            raise Abort(f"refusing {method}")
        else:
            u = urllib.parse.urlparse(url)
            if u.scheme != "https" or u.hostname not in GET_HOSTS:
                raise Abort(f"refusing GET {url!r}")
        return http(method, url, headers)
    return call


def header(h, name):
    return next((v for k, v in h.items() if k.lower() == name), "")


def get_json(http, url, headers, what):
    st, h, body = http("GET", url, headers)
    if st != 200:
        raise Abort(f"{what}: HTTP {st}")
    return h, json.loads(body)


def list_versions(http, api_h, pkg):
    base = f"{API}/orgs/{ORG}/packages/container/{urllib.parse.quote(pkg, safe='')}"
    for _ in range(2):  # a count drift while listing means versions moved: retry the whole listing once
        before = get_json(http, base, api_h, f"{pkg} count")[1]["version_count"]
        out, url, pages = [], base + "/versions?per_page=100", 0
        while url:
            pages += 1
            if pages > before + 2:  # a page holds at least one version, so more pages than the count allows: a loop or a lying Link header
                raise Abort(f"{pkg}: listing exceeds {pages - 1} pages for version_count {before}")
            h, page = get_json(http, url, api_h, f"{pkg} versions")
            out += page
            m = NEXT.search(header(h, "link"))
            url = m[1] if m else None
        after = get_json(http, base, api_h, f"{pkg} count")[1]["version_count"]
        if before == after == len(out):
            return out
    raise Abort(f"{pkg}: version_count {before}/{after} does not match the listing ({len(out)})")


def parse_versions(pkg, raw, now):
    seen, vs = set(), []
    for v in raw:
        tags = (v.get("metadata", {}).get("container", {})).get("tags")
        if tags is None:
            raise Abort(f"{pkg}: version {v.get('id')} has no metadata.container.tags")
        if v["id"] in seen or not DIGEST.match(v.get("name", "")):
            raise Abort(f"{pkg}: duplicate id or bad digest on version {v['id']}")
        seen.add(v["id"])
        try:
            age = (now - dt.datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))).total_seconds() / 86400
        except (KeyError, ValueError) as e:
            raise Abort(f"{pkg}: version {v['id']} created_at unusable: {e}")
        vs.append({"id": v["id"], "digest": v["name"], "tags": tags, "age": age})
    return vs


def pins(clusters, pkg):
    rx = re.compile(r"ghcr\.io/" + ORG + "/" + re.escape(pkg) + r"(?::[^\s@\"']+)?@(sha256:[0-9a-f]{64})")
    return {d for f in pathlib.Path(clusters).rglob("*") if f.is_file() and f.suffix.lower() in (".yaml", ".yml")
            for d in rx.findall(f.read_text())}


def plan_package(http, api_h, pkg, clusters, now, mask=False):
    vs = parse_versions(pkg, list_versions(http, api_h, pkg), now)
    by_digest = {v["digest"]: v for v in vs}
    pinned = pins(clusters, pkg)
    for d in pinned:
        if d not in by_digest:
            raise Abort(f"{pkg}: pinned digest {d} is not in the listing")
    reasons, roots = {}, []
    for v in vs:
        if any(not SHA_TAG.match(t) for t in v["tags"]):
            r = "tagged"
        elif v["digest"] in pinned:
            r = "pinned"
        elif v["tags"] and v["age"] <= SHA_DAYS:
            r = "young sha"
        elif not v["tags"] and v["age"] <= UNTAGGED_DAYS:
            r = "young untagged"
        else:
            continue
        reasons[v["id"]] = r
        roots.append(v)
    registry = {}  # digest -> manifest media type, filled by the walk

    def manifest(d):
        if d not in registry:
            st, _, body = http("GET", f"https://ghcr.io/v2/{ORG}/{pkg}/manifests/{d}", reg_h)
            if st != 200:
                raise Abort(f"{pkg}: manifest {d} HTTP {st}")
            j = json.loads(body)
            mt = j.get("mediaType")
            if mt not in INDEX_TYPES + IMAGE_TYPES:
                raise Abort(f"{pkg}: manifest {d} has unknown media type {mt}")
            registry[d] = (mt, [m["digest"] for m in j.get("manifests", [])] if mt in INDEX_TYPES else [])
        return registry[d]

    tok = get_json(http, f"https://ghcr.io/token?scope=repository:{ORG}/{pkg}:pull",
                   {"Authorization": "Basic " + base64.b64encode(f"x:{api_h['Authorization'].split()[1]}".encode()).decode()}, f"{pkg} token")[1]["token"]
    if mask:
        print(f"::add-mask::{tok}")
    reg_h = {"Authorization": f"Bearer {tok}", "Accept": ACCEPT}
    keep = {v["digest"] for v in roots}
    stack = [v["digest"] for v in roots]
    while stack:
        for c in manifest(stack.pop())[1]:
            if c not in by_digest:
                raise Abort(f"{pkg}: child {c} is not in the listing")
            if c not in keep:
                keep.add(c)
                stack.append(c)
    for v in vs:
        if v["digest"] in keep and v["id"] not in reasons:
            reasons[v["id"]] = "child"
    dele = [v for v in vs if v["digest"] not in keep]
    for v in dele:
        v["index"] = manifest(v["digest"])[0] in INDEX_TYPES
    if [v for v in dele if any(not SHA_TAG.match(t) for t in v["tags"])]:
        raise Abort(f"{pkg}: would delete a non-sha tag")
    if any(V_TAG.match(t) for v in vs for t in v["tags"]) and not any(V_TAG.match(t) for v in vs if v not in dele for t in v["tags"]):
        raise Abort(f"{pkg}: would leave no v-tagged version")
    ids = {v["digest"]: v for v in dele if v["index"]}
    depth = {}

    def level(v):  # deleted indexes above v, longest chain: outermost = 0
        if v["id"] not in depth:
            depth[v["id"]] = max((level(o) + 1 for o in ids.values() if v["digest"] in registry[o["digest"]][1]), default=0)
        return depth[v["id"]]
    return {"pkg": pkg, "reasons": reasons, "delete": sorted(dele, key=lambda v: level(v) if v["index"] else len(dele) + 1)}


def delete_phase(http, api_h, plans):
    """Indexes first per package. Re-read each version first; changed tags stop everything."""
    for p in plans:
        base = f"{API}/orgs/{ORG}/packages/container/{urllib.parse.quote(p['pkg'], safe='')}/versions"
        for v in p["delete"]:
            now_tags = get_json(http, f"{base}/{v['id']}", api_h, f"{p['pkg']} version {v['id']}")[1]["metadata"]["container"]["tags"]
            if now_tags != v["tags"]:
                raise Abort(f"{p['pkg']}: version {v['id']} tags changed to {now_tags}; stopping all deletes")
            st = http("DELETE", f"{base}/{v['id']}", api_h)[0]
            if st != 204:
                raise Abort(f"{p['pkg']}: DELETE {v['id']} HTTP {st}; stopping")
            print(f"deleted {p['pkg']} version {v['id']} {v['digest']}")


def summary(plans, deleting):
    lines = []
    for p in plans:
        kept = {}
        for r in p["reasons"].values():
            kept[r] = kept.get(r, 0) + 1
        lines += [f"### {p['pkg']} ({'DELETE' if p['pkg'] in deleting else 'dry run'})", "",
                  "| kept | count |", "|---|---|"] + [f"| {r} | {n} |" for r, n in sorted(kept.items())]
        lines += [f"| to delete | {len(p['delete'])} |", ""]
        if p["delete"]:
            lines += ["| id | digest | tags | age days |", "|---|---|---|---|"]
            lines += [f"| {v['id']} | {v['digest']} | {','.join(v['tags']) or '-'} | {v['age']:.1f} |" for v in p["delete"]]
            lines.append("")
    return "\n".join(lines)


def main(env=None, http=None, now=None, clusters="clusters"):
    env = os.environ if env is None else env
    http = guarded(http or urlopen_http)
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        mx = env.get("MAX_DELETES") or str(DEFAULT_MAX)
        if not (re.fullmatch(r"[0-9]+", mx) and int(mx) > 0):
            raise Abort(f"MAX_DELETES must be a positive integer, got {mx!r}")
        app = env.get("APP") or "all"
        if app not in ("all", "tbd", "ziftbook"):
            raise Abort(f"APP must be ziftbook, tbd or all, got {app!r}")
        api_h = {"Authorization": f"Bearer {env['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"}
        enabled = (env.get("GITHUB_REF") == "refs/heads/main"
                   and (env.get("GITHUB_EVENT_NAME") == "schedule"
                        or (env.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and env.get("MODE") == "delete")))
        apps = {a.strip() for a in (env.get("DELETE_APPS") or "").split(",")}
        plans = [plan_package(http, api_h, p, clusters, now, bool(env.get("GITHUB_ACTIONS"))) for p in PACKAGES if app in ("all", p.split("/")[0])]
        deleting = {p["pkg"] for p in plans if enabled and p["pkg"].split("/")[0] in apps}
        for p in plans:
            if p["pkg"] in deleting and len(p["delete"]) > int(mx):
                raise Abort(f"{p['pkg']}: {len(p['delete'])} deletes exceed MAX_DELETES {mx}")
        text = summary(plans, deleting)
        print(text)
        if env.get("GITHUB_STEP_SUMMARY"):
            with open(env["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(text + "\n")
        delete_phase(http, api_h, [p for p in plans if p["pkg"] in deleting])
    except Abort as e:
        print(f"::error::{e}" if env.get("GITHUB_ACTIONS") else f"abort: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(clusters=sys.argv[1] if len(sys.argv) > 1 else "clusters"))
