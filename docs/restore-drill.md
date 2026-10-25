# Restore drill record

The date (UTC) the last successful restore drill finished: [RESTORE.md](../clusters/hetzner/data/RESTORE.md) steps
1-5 for a recent production set of each engine, both passing step 5. The weekly
[restore drill probe](../.github/workflows/restore-drill-probe.yml) opens one `[restore-drill]` issue while the line
below says `never` or a date more than 100 days old, and closes it when a newer date merges. Change only that line,
in a PR, after a drill: [runbooks.md](runbooks.md#restore-drill).

The INFRA-31 drill (2026-10-02) ran before the production sets held data and before the current procedure, so it is
not recorded here.

last-successful-drill: never
