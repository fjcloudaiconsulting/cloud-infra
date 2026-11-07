#!/usr/bin/env bash
# Decide whether the off-host database backups are fresh, from object METADATA only.
#
# Usage: check-backup-freshness.sh PREFIX=MIN_DUMP_BYTES...
# Reads an S3-API `list-objects-v2` JSON document on STDIN (one listing, or
# several merged into one Contents array) and writes a verdict to stdout.
#
#   exit 0  fresh   -- last night's backup is present, complete and plausible
#   exit 1  STALE   -- missing, too old, incomplete, or implausibly small
#   exit 2  could not run -- malformed input, missing tools
#
# ⚠ STDIN RATHER THAN CALLING AWS ITSELF, so the fences can drive every branch
# with fixture listings and no AWS account. A probe that can only be exercised
# against a healthy live bucket proves nothing about its unhealthy paths, which
# are the only paths that matter.
#
# ⚠ WHY THIS RUNS OFF THE DROPLET. An alert emitted by the machine being backed
# up cannot fire when that machine is gone, or when cron never ran -- which is
# exactly the disaster this backup exists for. Silence is the signal, and only
# an external observer can read it.
#
# ⚠ "SOME OBJECT EXISTS" IS A FAIL-OPEN CHECK. A bucket full of week-old dumps
# satisfies it forever. Hence: the MANIFEST must be present (it is uploaded
# last, so its presence is the only proof the night completed end to end), it
# must be recent, and the dump must clear a size floor.
set -euo pipefail

MAX_AGE_HOURS="${MAX_AGE_HOURS:-25}"
NOW_EPOCH="${NOW_EPOCH:-$(date -u +%s)}"

command -v python3 >/dev/null 2>&1 || { echo "could not run: python3 missing" >&2; exit 2; }

# ⚠ The evaluator is written to a temp file and run as `python3 FILE`, NOT as
# `python3 - <<PY`. With `-` the heredoc BECOMES stdin, so the piped S3 listing
# never reaches the program and every input -- healthy or not -- is judged
# "listing has no Contents key". That was a real defect here, caught only
# because these branches are exercised behaviourally; a structural check that
# the script mentions "Contents" would have passed it.
PROG="$(mktemp)"
trap 'rm -f "$PROG"' EXIT

cat > "$PROG" <<'PY'
import datetime as dt
import json
import sys

max_age_hours = float(sys.argv[1])
now = int(sys.argv[2])

# prefix=min_dump_bytes, one argument per prefix. A missing or unparsable floor
# is a caller bug and must not read as fresh.
floors = []
for spec in sys.argv[3:]:
    name, _, floor = spec.partition("=")
    if not name or not floor.isdigit():
        print(f"could not run: bad prefix=floor argument {spec!r}")
        raise SystemExit(2)
    floors.append((name, int(floor)))
if not floors:
    print("could not run: no prefix=floor arguments")
    raise SystemExit(2)

try:
    raw = sys.stdin.read()
    doc = json.loads(raw) if raw.strip() else {}
except (ValueError, OSError) as exc:
    print(f"could not run: listing is not valid JSON ({exc})")
    raise SystemExit(2)

if not isinstance(doc, dict):
    print("could not run: listing is not a JSON object")
    raise SystemExit(2)

# ⚠ Pagination. `IsTruncated` is only absent-or-false on a complete listing.
# The AWS CLI auto-paginates today, so this never fires -- but the moment
# anyone adds --max-items to the workflow, a truncated listing would silently
# hide the newest page and this check would be reading a partial bucket.
# Refuse to answer rather than answer from half the data.
if doc.get("IsTruncated"):
    print("could not run: the listing is truncated, so the newest objects may "
          "be missing. Remove any --max-items/--page-size from the caller.")
    raise SystemExit(2)

contents = doc.get("Contents")
if contents is None:
    # ⚠ `aws s3api list-objects-v2` OMITS Contents entirely for an empty
    # result -- it does not emit `"Contents": []`. So an absent Contents with a
    # KeyCount of 0 is a genuinely EMPTY BUCKET, which is STALE. Only an absent
    # Contents with no KeyCount at all means the document is not a listing, i.e.
    # we could not answer. Conflating the two labelled a real empty bucket as
    # "could not run"; both alarm, but the operator was told the wrong thing.
    if doc.get("KeyCount") == 0:
        print("STALE: the bucket is empty. No backup has ever been uploaded, "
              "or every object has expired.")
        raise SystemExit(1)
    print("could not run: listing has no Contents key")
    raise SystemExit(2)

def age_hours(obj):
    stamp = obj.get("LastModified")
    if not stamp:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - int(parsed.timestamp())) / 3600.0

def base(o):
    # The dump and grants arrive gpg-encrypted as <name>.gpg (db-backup.yaml): judged by the name inside.
    return str(o.get("Key", "")).rpartition("/")[2].removesuffix(".gpg")

def judge(contents, min_dump_bytes):
    manifests = [o for o in contents
                 if base(o).startswith("manifest_") and base(o).endswith(".json")]
    if not manifests:
        return 1, ("STALE: no manifest object found. The manifest is uploaded last, so "
                   "its absence means no night has completed end to end.")

    # ⚠ Sort by the PARSED INSTANT, never by the timestamp string. AWS CLI v2
    # emits `+00:00` offsets while these fixtures emit `Z`, and once two formats
    # coexist a lexicographic max picks the wrong object -- a false STALE alarm
    # from a healthy bucket.
    dated = [(age_hours(o), o) for o in manifests]
    usable = [(a, o) for a, o in dated if a is not None]
    if not usable:
        return 2, "could not run: no manifest has a usable LastModified"
    age, newest = min(usable, key=lambda pair: pair[0])

    # ⚠ A negative age means the object is stamped in the FUTURE -- a clock skew or
    # a doctored timestamp -- and would otherwise sail through the freshness test
    # forever. Refuse to answer rather than report healthy.
    if age < 0:
        return 2, (f"could not run: manifest {newest.get('Key')} is dated in the future "
                   f"({-age:.1f}h ahead). Refusing to call that fresh.")

    if age > max_age_hours:
        return 1, (f"STALE: newest manifest {newest.get('Key')} is {age:.1f}h old "
                   f"(threshold {max_age_hours}h). At least one nightly run has been missed.")

    # The manifest is fresh; the artifacts it implies must be present in the same
    # prefix, carry the same stamp, and be plausibly sized.
    # ⚠ MATCH THE STAMP, NOT JUST THE DAY DIRECTORY. A retry or a manual Job
    # writes a second set into the same day; matching by directory let the
    # earlier set's big dump vouch for a broken newer one (INFRA-72).
    # Layout: <prefix>/YYYY/MM/DD/{<db>,grants,manifest}_<stamp>. The dump is .sql.gz, or .dump for a
    # custom-format Postgres archive (INFRA-235); the grants are always .sql.gz.
    prefix = str(newest.get("Key", "")).rpartition("/")[0]
    stamp = base(newest).removeprefix("manifest_").removesuffix(".json")
    siblings = [o for o in contents if str(o.get("Key", "")).rpartition("/")[0] == prefix]

    grants = [o for o in siblings if base(o) == f"grants_{stamp}.sql.gz"]
    dumps = [o for o in siblings if base(o).endswith((f"_{stamp}.sql.gz", f"_{stamp}.dump"))
             and base(o) != f"grants_{stamp}.sql.gz"]

    if not dumps:
        return 1, (f"STALE: manifest {newest.get('Key')} is fresh but no dump object "
                   "sits beside it.")
    if not grants:
        return 1, (f"STALE: manifest {newest.get('Key')} is fresh but no grants object "
                   "sits beside it. A restore would yield tables and zero logins.")

    dump = max(dumps, key=lambda o: int(o.get("Size", 0)))
    biggest = int(dump.get("Size", 0))
    if biggest < min_dump_bytes:
        return 1, (f"STALE: newest dump {dump.get('Key')} is {biggest} bytes, below the {min_dump_bytes} "
                   "byte floor. A plausible-looking but tiny dump is the failure mode a "
                   "presence check cannot see.")

    return 0, (f"fresh: manifest {newest.get('Key')} is {age:.1f}h old, dump {biggest} bytes")


# Each prefix is judged alone, on its own floor, and the run reports the worst
# verdict (could not run > STALE > fresh) naming every prefix that is not fresh.
# Kills: one stale database hiding behind a fresh one in the same bucket.
results = [(p, *judge([o for o in contents if str(o.get("Key", "")).startswith(p + "/")], f))
           for p, f in floors]
worst = max(rc for _, rc, _ in results)
bad = [p for p, rc, _ in results if rc]
print({0: "fresh: " + ", ".join(p for p, _, _ in results),
       1: "STALE: " + ", ".join(bad),
       2: "could not run: " + ", ".join(bad)}[worst])
for p, _, msg in results:
    print(f"  {p}: {msg}")
raise SystemExit(worst)
PY

python3 "$PROG" "$MAX_AGE_HOURS" "$NOW_EPOCH" "$@"
