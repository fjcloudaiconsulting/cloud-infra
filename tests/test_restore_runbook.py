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

    def _scan_invocations(self):
        # Each `awk -v scan=<mode> -f /tmp/scan.awk` that runs the scanner on a backup file.
        return list(re.finditer(r"awk -v scan=(grants|dump)\s+-f /tmp/scan\.awk", self.text))

    def test_psql_backup_replays_are_preceded_by_the_scan(self):
        # A replay feeds a gunzipped backup file into psql.
        replays = [
            m.start() for m in re.finditer(
                r"gzip -dc[^\n|]*?/tmp/(?:dump|grants)\.sql\.gz[^\n]*?\|[^\n]*?psql", self.text)
        ]
        self.assertGreaterEqual(len(replays), 2, "expected the two Postgres replays (grants and dump)")
        scans = self._scan_invocations()
        self.assertTrue(scans, "RESTORE.md does not run the Postgres scanner (awk -v scan=… -f /tmp/scan.awk)")
        first_scan = min(m.start() for m in scans)
        for pos in replays:
            self.assertLess(
                first_scan, pos,
                msg="a psql replay of a backup file is not preceded by the scan step",
            )

    def test_scan_runs_both_modes_on_both_files(self):
        modes = {m.group(1) for m in self._scan_invocations()}
        self.assertEqual({"grants", "dump"}, modes, "both scan=grants and scan=dump must run")
        # Each backup file is scanned before it is piped anywhere else.
        self.assertRegex(self.text, r"gzip -dc /tmp/grants\.sql\.gz \| awk -v scan=grants")
        self.assertRegex(self.text, r"gzip -dc /tmp/dump\.sql\.gz\s+\| awk -v scan=dump")

    def test_grants_replay_uses_on_error_stop_and_drops_postgres_role(self):
        # The grants replay must fail-closed and not trip over the expected pre-existing postgres role.
        m = re.search(r"gzip -dc /tmp/grants\.sql\.gz[^\n]*psql[^\n]*", self.text)
        self.assertIsNotNone(m, "grants replay not found")
        line = m.group(0)
        self.assertIn("ON_ERROR_STOP=1", line)
        self.assertRegex(self.text, r'grep -v "\^CREATE ROLE postgres;\$"')

    # ---- DATA-04: the runbook states the checksum proves transport, not authorship ---------------

    def test_runbook_notes_checksum_proves_copy_not_author(self):
        self.assertRegex(self.text, r"(?i)not who wrote it")

    def test_runbook_calls_the_scan_a_guard_not_a_guarantee(self):
        self.assertRegex(self.text, r"(?i)guard against")
        self.assertRegex(self.text, r"(?i)not a guarantee")
        self.assertNotRegex(self.text, r"(?i)a restore cannot run commands")

    # ---- The scanner itself: extract it and run it against fixtures -------------------------------

    def _extract_scanner(self):
        m = re.search(r"# BEGIN pg-restore-scan.*?\n# END pg-restore-scan", self.text, re.S)
        self.assertIsNotNone(m, "pg-restore-scan markers not found in RESTORE.md")
        return m.group(0)

    def _run_scan(self, awk_bin, program_path, mode, fixture):
        return subprocess.run(
            [awk_bin, "-v", "scan=" + mode, "-f", str(program_path)],
            input=fixture, capture_output=True, text=True,
        ).returncode

    # A realistic pg_dump --create excerpt: header, the three meta-commands 18 emits, an allowlisted
    # extension, dollar-quoted function bodies (one whose body contains "$$" and a single-quoted string
    # with a backslash, so pg_dump tags it $_$), string literals and a comment carrying backslashes, and
    # a COPY data block whose rows start with a backslash or merely mention the dangerous tokens.
    CLEAN_DUMP = (
        "--\n-- PostgreSQL database dump\n--\n\n"
        "\\restrict a1b2c3\n\n"
        "SET statement_timeout = 0;\n"
        "SET standard_conforming_strings = on;\n\n"
        "\\connect ziftbook\n\n"
        "CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;\n"
        "COMMENT ON EXTENSION btree_gist IS 'support for indexing common datatypes in GiST';\n\n"
        "CREATE FUNCTION public.slugify(base text) RETURNS text\n"
        "    LANGUAGE plpgsql IMMUTABLE\n"
        "    AS $$\n"
        "BEGIN\n"
        "  -- a comment with a backslash \\ inside the body\n"
        "  RETURN regexp_replace(lower(base), '[^a-z0-9\\-]+', '-', 'g');\n"
        "END;\n"
        "$$;\n\n"
        "CREATE FUNCTION public.tagged() RETURNS text\n"
        "    LANGUAGE sql\n"
        "    AS $_$ SELECT 'it''s \\d and $$ inside' $_$;\n\n"
        "CREATE TABLE public.clients (\n"
        "    id bigint NOT NULL,\n"
        "    note text DEFAULT 'line\\none'::text,\n"
        "    CONSTRAINT ck_phone CHECK ((phone ~ '^\\+[1-9][0-9]{1,14}$'::text))\n"
        ");\n\n"
        "COMMENT ON TABLE public.clients IS 'paths as typed, e.g. C:\\temp\\new';\n\n"
        "COPY public.\"Raw Lines\" (line, n) FROM stdin;\n"
        "\\N\t1\n"
        "\\\\x5c2e0a\t2\n"
        "\\\\. inside a value\t3\n"
        "from program, superuser, alter role postgres: data only\t4\n"
        "\\.\n\n"
        "CREATE INDEX clients_name_re ON public.clients USING btree (((name ~ '\\w+'::text)));\n"
    )
    # pg_dumpall --globals-only excerpt: comments, SET, \restrict/\unrestrict, CREATE ROLE, the WITH
    # attribute line (postgres carries SUPERUSER, every other role NOSUPERUSER), ALTER ROLE ... SET, and
    # GRANT role membership with GRANTED BY.
    CLEAN_GLOBALS = (
        "--\n-- PostgreSQL database cluster dump\n--\n\n"
        "\\restrict k9\n\n"
        "SET default_transaction_read_only = off;\n\n"
        "-- Roles\n\n"
        "CREATE ROLE postgres;\n"
        "ALTER ROLE postgres WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN REPLICATION BYPASSRLS PASSWORD 'SCRAM-SHA-256$4096:aa==$bb:cc';\n"
        "CREATE ROLE ziftbook_app;\n"
        "ALTER ROLE ziftbook_app WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB LOGIN NOREPLICATION NOBYPASSRLS PASSWORD 'SCRAM-SHA-256$4096:dd==$ee:ff';\n"
        "CREATE ROLE ziftbook_prod_app;\n"
        "ALTER ROLE ziftbook_prod_app WITH NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB LOGIN NOREPLICATION NOBYPASSRLS PASSWORD 'SCRAM-SHA-256$4096:gg==$hh:ii';\n"
        "ALTER ROLE ziftbook_app SET search_path TO 'public', 'pg_temp';\n"
        "ALTER ROLE ziftbook_prod_app SET role TO 'ziftbook_app';\n"
        "GRANT ziftbook_app TO ziftbook_prod_app WITH INHERIT FALSE GRANTED BY postgres;\n"
        "GRANT ziftbook_app TO ziftbook_worker WITH INHERIT TRUE GRANTED BY postgres;\n"
        "\\unrestrict k9\n"
    )

    # The classic bypass: a dollar-quoted body that contains a COPY header, then a meta-command after the
    # body closes. The lexer must not enter COPY-data mode inside the dollar quote, so the trailing \! is
    # seen as code and refused.
    DOLLAR_BYPASS = (
        "CREATE FUNCTION public.evil() RETURNS void LANGUAGE sql AS $$\n"
        "COPY x FROM stdin;\n"
        "$$;\n"
        "\\! id\n"
    )

    def _crafted_dump(self):
        return {
            "shell meta-command after a dollar-quoted body (classic bypass)": self.DOLLAR_BYPASS,
            "mid-line meta-command": self.CLEAN_DUMP + "SELECT 1; \\! id\n",
            "output to a pipe (\\o)": self.CLEAN_DUMP + "\\o |cmd\n",
            "query to a pipe (\\g)": self.CLEAN_DUMP + "SELECT 1 \\g |cmd\n",
            "COPY TO a server file": self.CLEAN_DUMP + "COPY public.clients TO '/tmp/x.csv';\n",
            "COPY FROM a server file": self.CLEAN_DUMP + "COPY public.clients FROM '/etc/passwd';\n",
            "COPY FROM PROGRAM": self.CLEAN_DUMP + "COPY public.clients (id) FROM PROGRAM 'sh -c id';\n",
            "COPY from program, lowercase and tab": self.CLEAN_DUMP + "COPY x (c)  from\tprogram  'id';\n",
            "lo_import": self.CLEAN_DUMP + "SELECT lo_import('/etc/passwd');\n",
            "pg_read_file": self.CLEAN_DUMP + "SELECT pg_read_file('/etc/passwd');\n",
            "ALTER SYSTEM": self.CLEAN_DUMP + "ALTER SYSTEM SET archive_command = 'id';\n",
            "CREATE EXTENSION other than btree_gist": self.CLEAN_DUMP + "CREATE EXTENSION plpython3u;\n",
            "untrusted language function": self.CLEAN_DUMP + "CREATE FUNCTION f() RETURNS void LANGUAGE plpython3u AS 'x';\n",
            "dblink": self.CLEAN_DUMP + "SELECT dblink('host=evil', 'select 1');\n",
            "new SUPERUSER role": self.CLEAN_DUMP + "ALTER ROLE evil SUPERUSER;\n",
            "re-password postgres": self.CLEAN_DUMP + "ALTER ROLE postgres PASSWORD 'attacker';\n",
        }

    def _crafted_grants(self):
        return {
            "COPY in the globals": self.CLEAN_GLOBALS + "COPY pg_authid FROM stdin;\n",
            "shell meta-command": self.CLEAN_GLOBALS + "\\! id\n",
            "new SUPERUSER role": self.CLEAN_GLOBALS + "ALTER ROLE evil WITH SUPERUSER LOGIN PASSWORD 'x';\n",
            "arbitrary function": self.CLEAN_GLOBALS + "CREATE FUNCTION f() RETURNS void AS 'x';\n",
            "arbitrary SELECT": self.CLEAN_GLOBALS + "SELECT lo_import('/etc/passwd');\n",
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
            self.assertEqual(0, self._run_scan(awk_bin, prog, "dump", self.CLEAN_DUMP),
                             msg="dump scan refused a clean pg_dump")
            self.assertEqual(0, self._run_scan(awk_bin, prog, "grants", self.CLEAN_GLOBALS),
                             msg="grants scan refused a clean pg_dumpall --globals-only")
            for name, fixture in self._crafted_dump().items():
                self.assertNotEqual(0, self._run_scan(awk_bin, prog, "dump", fixture),
                                    msg=f"dump scan accepted a crafted file: {name}")
            for name, fixture in self._crafted_grants().items():
                self.assertNotEqual(0, self._run_scan(awk_bin, prog, "grants", fixture),
                                    msg=f"grants scan accepted a crafted file: {name}")
        finally:
            prog.unlink()


if __name__ == "__main__":
    unittest.main()
