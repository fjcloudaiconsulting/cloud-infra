#!/usr/bin/env python3
"""A prod image may not be newer than staging's (INFRA-89, release contract section 8).

For every directory named <app>-prod under the clusters root, each ghcr.io/fjcloudaiconsulting image
must also appear in the <app>-staging directory (in any cluster tree), and its vX.Y.Z tag must be <= the LOWEST tag
staging has for that image's repo (a release is one unit: a lagging staging image bounds prod).
Older is allowed (rollback).
"Deployed in staging" means "on this tree under <app>-staging": static, no cluster or network access.
No app is named here, so a new <app>-prod/<app>-staging pair is covered with no change.
Limits: it reads the checked-out tree, so one PR that bumps staging and prod together passes (review
catches it), and ordering is not proof staging ran that exact tag; live health is the post-deploy smoke's job.
A ref inside a comment counts as a real ref.
Fails closed: prod without a staging dir or image, a tag that is not plain vX.Y.Z (an @sha256:<64 hex> pin is stripped
first), no prod dir at all, and a tree outside manifest_fence.py's canonical form (GHA-03): that fence also keeps
every image in an <app>-prod or <app>-staging namespace on ghcr.io/fjcloudaiconsulting/<app>/, in that namespace's
directory, unescaped and untouched by kustomize image overrides.

Usage: check-prod-tags.py [clusters-dir]   (default: clusters)
"""
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import manifest_fence  # noqa: E402

REF = re.compile(r"ghcr\.io/fjcloudaiconsulting/([^:@\s\"']+)(?::([^\s\"']+))?")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")  # optional pin after the tag (INFRA-107)
SEMVER = re.compile(r"v(\d+)\.(\d+)\.(\d+)$")


def refs(d, errs):
    """{repo/image: [(tag, file)]} for every yaml under d; non-vX.Y.Z tags are errors."""
    out = {}
    for f in sorted(d.rglob("*")):
        if f.suffix.lower() not in (".yaml", ".yml") or not f.is_file():
            continue
        for image, tag in REF.findall(f.read_bytes().decode("utf-8", "replace")):  # the fence reports non-UTF-8
            tag = DIGEST.sub("", tag)
            if SEMVER.match(tag):
                out.setdefault(image, []).append((tag, f))
            else:
                errs.append(f"{f}: {image}:{tag} is not a plain vX.Y.Z tag (untagged or digest refs included)")
    return out


def ver(tag):
    return tuple(int(x) for x in SEMVER.match(tag).groups())


def main(root):
    errs = [f"{p}: {m}" for p, m in manifest_fence.problems(root)]
    prods = sorted(pathlib.Path(root).rglob("*-prod"))
    prods = [p for p in prods if p.is_dir()]
    if not prods:
        errs.append(f"no <app>-prod directory under {root}")
    for prod in prods:
        sname = prod.name[: -len("prod")] + "staging"
        # Staging may live in another cluster tree than prod (Hetzner migration): any <app>-staging under the root.
        stgs = [d for d in sorted(pathlib.Path(root).rglob(sname)) if d.is_dir()]
        pr = refs(prod, errs)
        if not pr:
            errs.append(f"{prod}: no ghcr.io/fjcloudaiconsulting images")
        if not stgs:
            errs.append(f"{prod}: {sname} does not exist; prod needs a staging that runs the tag first")
            continue
        sr = {}
        for stg in stgs:
            for image, v in refs(stg, errs).items():
                sr.setdefault(image, []).extend(v)
        for image, uses in pr.items():
            if image not in sr:
                errs.append(f"{prod}: {image} is not in {sname}")
                continue
            repo = image.split("/")[0]
            low = min((t for i, v in sr.items() if i.split("/")[0] == repo for t, _ in v), key=ver)
            for tag, f in uses:
                if ver(tag) > ver(low):
                    errs.append(f"{f}: {image}:{tag} is newer than {sname} ({low}); wait for the staging bump on main, then update the branch")
        print(f"checked {prod}")
    for e in errs:
        print(f"::error::{e}" if os.environ.get("GITHUB_ACTIONS") else e, file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "clusters"))
