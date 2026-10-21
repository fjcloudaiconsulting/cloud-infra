#!/usr/bin/env bash
# Image provenance (INFRA-107): every app image ref a change ADDS under clusters/ must be
# `:vX.Y.Z@sha256:<digest>` and the digest must carry a build-image attestation made on main, at the
# commit that tag vX.Y.Z points to. Refs unchanged from BASE or removed are ignored (legacy refs do not block).
#
# Inputs (env): BASE (git ref to diff against), GH_TOKEN. Needs `gh` and a checkout with BASE's history.
# Extraction is deliberately broad (any case, optional :443, any path under the org); the strict regex below then
# rejects every spelling that is not the canonical one. Fails CLOSED: any parse, API or verify failure exits non-zero.
set -euo pipefail

ORG=fjcloudaiconsulting
SAN="https://github.com/$ORG/.github/.github/workflows/build-image.yml@refs/tags/v1"
: "${BASE:?BASE is required}"

diff="$(git diff --no-renames --text -U0 "$BASE...HEAD" -- clusters)"
refs="$(grep -E '^\+' <<<"$diff" | grep -vE '^\+\+\+' \
        | grep -oiE "ghcr\.io(:443)?/$ORG/[^\"[:space:]]+" | sort -u || true)"
if [[ -z "$refs" ]]; then echo "no added app image refs"; exit 0; fi

while IFS= read -r ref; do
  re="^ghcr\.io/$ORG/([^/]+)/([^:@]+):(v[0-9]+\.[0-9]+\.[0-9]+)@sha256:([0-9a-f]{64})$"
  [[ "$ref" =~ $re ]] || { echo "image ref must be :vX.Y.Z@sha256:<64 hex>: $ref" >&2; exit 1; }
  repo="${BASH_REMATCH[1]}"; img="${BASH_REMATCH[2]}"; tag="${BASH_REMATCH[3]}"; digest="${BASH_REMATCH[4]}"
  # refs/tags/ explicitly: a branch named like the tag would otherwise win, and app repos protect only refs/tags/v*.
  commit="$(gh api "repos/$ORG/$repo/commits/refs/tags/$tag" --jq .sha)"
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "no commit for $repo $tag: '$commit'" >&2; exit 1; }
  gh attestation verify "oci://ghcr.io/$ORG/$repo/$img@sha256:$digest" --repo "$ORG/$repo" \
    --cert-identity "$SAN" --source-ref refs/heads/main --source-digest "$commit" --deny-self-hosted-runners >/dev/null \
    || { echo "attestation check failed: $ref" >&2; exit 1; }
  echo "verified $repo/$img $tag ($digest)"
done <<<"$refs"
