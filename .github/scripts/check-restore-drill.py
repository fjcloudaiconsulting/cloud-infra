#!/usr/bin/env python3
"""Restore drill freshness (DATA-05, INFRA-214): is the last successful restore drill recent enough?

The backup probe proves each night's set exists and is big enough; only a drill proves a set restores. The record is
one line in docs/restore-drill.md, `last-successful-drill: YYYY-MM-DD` (UTC) or `never`, set in a PR after each drill
(docs/runbooks.md "Restore drill"). Overdue: never drilled, or more than MAX_AGE_DAYS ago (a quarterly drill plus
slack).

  check-restore-drill.py RECORD           verdict only, touches nothing: exit 0 fresh, 1 overdue, 2 unreadable
  check-restore-drill.py --issue RECORD   also open or update ONE issue when not fresh, close it when fresh: exit 0,
                                          2 unreadable record (the issue is still raised), 3 issue lookup/write failed

--issue needs `gh` with GH_TOKEN, GH_REPO and RUN_URL (the workflow sets them). TODAY=YYYY-MM-DD overrides the date
(tests). Fails CLOSED: an unreadable or future-dated record raises the issue and fails the run, and a failed issue
lookup touches nothing, so a broken probe never closes the issue. Stdlib only.
"""
import datetime as dt
import os
import re
import subprocess
import sys

MAX_AGE_DAYS = 100
KEY = "last-successful-drill:"
LINE = re.compile(r"last-successful-drill: (never|\d{4}-\d{2}-\d{2})")
TITLE = "[restore-drill] database restore drill is overdue"
BOT = "app/github-actions"
HOWTO = ('Run the drill (docs/runbooks.md "Restore drill": RESTORE.md steps 1-5 on a scratch namespace), then set '
         "its date in `docs/restore-drill.md` in a PR. The probe closes this issue when that PR merges.")


class RecordError(ValueError):
    pass


def parse_record(text):
    """The last drill's date, or None for `never`. RecordError unless exactly one record line, well formed."""
    # Any line that starts like the record counts, so a typo or a second copy fails instead of being skipped.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().lower().startswith(KEY)]
    if len(lines) != 1:
        raise RecordError(f"expected one '{KEY}' line, found {len(lines)}")
    m = LINE.fullmatch(lines[0])
    if not m:
        raise RecordError(f"the record line is not '{KEY} YYYY-MM-DD' or '{KEY} never'")
    if m.group(1) == "never":
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        raise RecordError(f"the record date {m.group(1)} is not a calendar date") from None


def verdict(last, today, max_age=MAX_AGE_DAYS):
    """('fresh' or 'overdue', detail). RecordError for a date after today: a typo must not silence the probe."""
    if last is None:
        return "overdue", "no successful restore drill has been recorded"
    age = (today - last).days
    if age < 0:
        raise RecordError(f"the record date {last.isoformat()} is after today")
    detail = f"the last successful restore drill was on {last.isoformat()}, {age} days ago (limit {max_age})"
    return ("overdue" if age > max_age else "fresh"), detail


def today():
    forced = os.environ.get("TODAY")
    return dt.date.fromisoformat(forced) if forced else dt.datetime.now(dt.timezone.utc).date()


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def own_open_issue():
    # Our own issue only: the repo is public, so match the exact title AND the Actions bot as author. The author filter
    # works only through search (same lookup as the release drift and backup probes, INFRA-119).
    out = gh("issue", "list", "--state", "open", "--author", BOT, "--search", f'"{TITLE}" in:title',
             "--json", "number,title", "--jq", ".[] | [.number, .title] | @tsv")
    for line in out.splitlines():
        number, _, title = line.partition("\t")
        if title == TITLE:
            return number
    return None


def main(argv):
    args = argv[1:]
    issue = args[:1] == ["--issue"]
    if issue:
        args = args[1:]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        with open(args[0], encoding="utf-8") as fh:
            state, detail = verdict(parse_record(fh.read()), today())
    except (OSError, RecordError) as e:
        state, detail = "could-not-run", f"the drill record could not be read: {e.strerror if isinstance(e, OSError) else e}"
    print(f"{state}: {detail}")
    if not issue:
        return {"fresh": 0, "overdue": 1}.get(state, 2)

    run_url = os.environ["RUN_URL"]
    try:
        existing = own_open_issue()
        if state == "fresh":
            if existing:
                gh("issue", "close", existing, "--comment", f"Drill recorded: {detail}. Run: {run_url}")
                print(f"closed #{existing}")
        else:
            head = ("The restore drill probe could not read the drill record" if state == "could-not-run"
                    else "The restore drill is overdue")
            body = (f"{head}: {detail}.\n\nThe backup probe shows the nightly sets exist; only a drill shows they "
                    f"restore. {HOWTO}\n\nRun: {run_url}")
            if existing:
                gh("issue", "edit", existing, "--body", body)
                print(f"updated #{existing}")
            else:
                gh("issue", "create", "--title", TITLE, "--body", body)
                print("opened the issue")
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"issue lookup or update failed: {e}", file=sys.stderr)
        return 3
    return 2 if state == "could-not-run" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
