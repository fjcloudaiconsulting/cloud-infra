#!/usr/bin/env bash
# Release drift (INFRA-38): open/update/close ONE issue when an app repo's latest GitHub
# release has been absent from clusters/ for GRACE_DAYS or more.
#
# Inputs (env): GH_TOKEN, GH_REPO, GRACE_DAYS, RUN_URL; optional CLUSTERS_DIR (default clusters),
# WATCH_REPOS (default "ziftbook tbd"; refs of other repos are ignored), NOW_EPOCH. Needs `gh`.
#
# Fails CLOSED: any parse or API problem exits non-zero WITHOUT touching the issue, so a broken
# probe can never read as "no drift" and close it. Depends on the app repos being public
# (GITHUB_TOKEN reads their releases); the run goes red if one goes private.
set -euo pipefail

DIR="${CLUSTERS_DIR:-clusters}"
WATCH="${WATCH_REPOS-ziftbook tbd}"
NOW="${NOW_EPOCH:-$(date +%s)}"
title="[release-drift] app release not in clusters/"
drift=""

[[ "$GRACE_DAYS" =~ ^[0-9]+$ ]] || { echo "bad grace_days" >&2; exit 1; }

# Every app image ref, loose on the tag so rc tags and digest suffixes are seen, then validated.
all="$(grep -rhoE --include='*.yaml' 'ghcr\.io/fjcloudaiconsulting/[^/]+/[^:"[:space:]]+:[^"[:space:]]+' "$DIR" || true)"
# Only watched repos are checked (placeholder pins of unwatched ones would be false drift).
pat="$(printf '%s' "$WATCH" | tr -s ' ' '|')"
refs="$(grep -E "ghcr\.io/fjcloudaiconsulting/($pat)/" <<<"$all" || true)"
echo "ignoring unwatched repos: $(grep -vE "ghcr\.io/fjcloudaiconsulting/($pat)/" <<<"$all" | sed -E 's#ghcr\.io/fjcloudaiconsulting/([^/]+)/.*#\1#' | sort -u | tr '\n' ' ')"
[[ -n "$refs" ]] || { echo "no watched app image refs in $DIR" >&2; exit 1; }
bad="$(printf '%s\n' "$refs" | grep -vE ':v[0-9]+\.[0-9]+\.[0-9]+$' || true)"
[[ -z "$bad" ]] || { printf 'image tag is not a plain vX.Y.Z:\n%s\n' "$bad" >&2; exit 1; }

# repo<TAB>lowest deployed tag; built into a variable so a failure aborts the run.
list="$(printf '%s\n' "$refs" | sed -E 's#ghcr\.io/fjcloudaiconsulting/([^/]+)/[^:]+:#\1\t#' \
        | sort -t$'\t' -k1,1 -k2,2V | awk -F'\t' '!s[$1]++')"
[[ -n "$list" ]] || { echo "empty repo list" >&2; exit 1; }
for r in $WATCH; do
  grep -q "^$r"$'\t' <<<"$list" || { echo "watched repo $r missing from $DIR" >&2; exit 1; }
done

while IFS=$'\t' read -r repo cur; do
  rel="$(gh api "repos/fjcloudaiconsulting/$repo/releases/latest" --jq '[.tag_name,.published_at]|@tsv')"
  latest="${rel%%$'\t'*}"; pub="${rel#*$'\t'}"
  [[ "$latest" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "unexpected release tag '$latest' for $repo" >&2; exit 1; }
  [[ "$latest" == "$cur" ]] && continue
  [[ "$(printf '%s\n%s\n' "$cur" "$latest" | sort -V | tail -1)" == "$latest" ]] || continue
  age=$(( ( NOW - $(date -d "$pub" +%s) ) / 86400 ))
  if (( age >= GRACE_DAYS )); then
    drift+="- \`$repo\`: clusters/ has \`$cur\`, latest release \`$latest\` (${age}d old)"$'\n'
  fi
done <<<"$list"

# Our own issue only: the repo is public, so match the exact title AND the Actions bot as author. The author filter
# works only through search.
issues="$(gh issue list --state open --author app/github-actions --search "\"$title\" in:title" \
          --json number,title --jq '.[] | [.number, .title] | @tsv')" || { echo "issue lookup failed" >&2; exit 1; }
existing="$(awk -F'\t' -v t="$title" '$2 == t { print $1; exit }' <<<"$issues")"
if [[ -z "$drift" ]]; then
  echo "no drift"
  if [[ -n "$existing" ]]; then gh issue close "$existing" --comment "Drift cleared ($RUN_URL)."; fi
  exit 0
fi
printf '%s' "$drift"
body="$(printf 'Newer releases not in `clusters/` for at least %s days. Merge the Renovate bump PR, or check the Dependency Dashboard and `GHCR_READ_TOKEN` if there is none (docs/configuration-map.md).\n\n%s\nRun: %s' "$GRACE_DAYS" "$drift" "$RUN_URL")"
if [[ -n "$existing" ]]; then gh issue edit "$existing" --body "$body"
else gh issue create --title "$title" --body "$body"; fi
