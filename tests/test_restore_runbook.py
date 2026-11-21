"""Fences on the database restore runbook (clusters/hetzner/data/RESTORE.md).

The runbook replays backup files through `mysql` and `psql`, which run client meta-commands (`\\!`,
`system`) and, for Postgres, `COPY ... FROM/TO PROGRAM` as superuser. A set is written by the same
identity as its manifest, so the checksum proves the copy, not the author (DATA-04): a restore must not
let backup content run commands (DATA-01). These fences assert the runbook keeps those guards: MySQL replays
with --binary-mode, Postgres restores only through pg_restore as a role that is not a superuser.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`.
"""
import pathlib
import re
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


    # ---- DATA-01: no backup file reaches psql ---------------------------------------------------------

    def test_no_backup_file_is_replayed_through_psql(self):
        # Kills: the plain-SQL superuser replay coming back. psql acts on `\!` lines and, as postgres, on COPY
        # ... PROGRAM; the scan that guarded it is gone with it.
        self.assertNotRegex(self.text, r"/tmp/(?:dump|grants)\.[^\n]*\|[^\n]*psql")
        self.assertNotRegex(self.text, r"psql[^\n]*/tmp/(?:dump|grants)\.")

    # ---- Postgres dumps restore through pg_restore, never as a superuser ----

    def test_custom_format_dump_restores_with_pg_restore_as_the_owner_role(self):
        restores = re.findall(r"pg_restore -(?:[^\n\\]|\\\n)*", self.text)  # with its continuation lines
        self.assertTrue(restores, "RESTORE.md has no pg_restore step")
        for cmd in restores:
            for flag in ('-U "$ZLOGIN"', "--role=ziftbook_migrate", "--no-owner", "--single-transaction",
                         "--exit-on-error", "/tmp/dump.dump"):
                self.assertIn(flag, cmd, msg=f"pg_restore step without {flag}: {cmd!r}")
        # The login is a Ziftbook role, never the superuser, in every place it is set.
        logins = re.findall(r"\bZLOGIN=(\S+)", self.text)
        self.assertGreaterEqual(len(logins), 3, "expected ZLOGIN for both sets and the drill")
        self.assertTrue(all(v.startswith("ziftbook_") for v in logins), logins)

    def test_custom_format_dump_never_reaches_psql(self):
        self.assertNotRegex(self.text, r"/tmp/dump\.dump[^\n]*\|[^\n]*psql")
        self.assertNotRegex(self.text, r"psql[^\n]*/tmp/dump\.dump")

    # ---- DATA-04: the runbook states the checksum proves transport, not authorship ---------------

    def test_real_restore_revokes_every_tbd_api_token(self):
        # fence: a token revoked after the dump night must not come back live after a real restore.
        self.assertIn("echo 'UPDATE api_tokens SET revoked_at = UTC_TIMESTAMP() WHERE revoked_at IS NULL;' | my tbd",
                      self.text)
        self.assertIn("echo 'UPDATE invitations SET revoked_at = UTC_TIMESTAMP() WHERE accepted_at IS NULL AND "
                      "revoked_at IS NULL;' | my tbd", self.text)

    def test_runbook_notes_checksum_proves_copy_not_author(self):
        self.assertRegex(self.text, r"(?i)not who wrote it")


if __name__ == "__main__":
    unittest.main()
