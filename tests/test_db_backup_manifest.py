"""Guards for clusters/hetzner/data/db-backup.yaml (INFRA-205): plaintext dumps never touch node disk."""

import pathlib
import re
import unittest

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "clusters/hetzner/data/db-backup.yaml"


class DbBackupManifest(unittest.TestCase):
    def setUp(self):
        self.text = MANIFEST.read_text()

    def test_dump_volume_is_memory_backed(self):
        """Kills a disk-backed emptyDir: the dumps (password hashes included) would stay on the node's disk."""
        vol = re.search(r"- name: backup\n(?:\s+#.*\n)*\s+emptyDir:\n((?:\s+\w+: .+\n)+)", self.text)
        self.assertIsNotNone(vol, "backup emptyDir volume not found")
        self.assertRegex(vol.group(1), r"medium: Memory")

    def test_postgres_dump_is_custom_format(self):
        """INFRA-235: a plain SQL dump would be restored through psql as a superuser; the archive is restored by
        pg_restore as the owner role (RESTORE.md)."""
        script = re.search(r"^  pg-dump\.sh: \|\n((?:    .*\n|\n)+)", self.text, re.M).group(1)
        self.assertRegex(script, r"(?m)^\s*DUMP=\$\{DUMP%\.sql\.gz\}\.dump$")
        self.assertRegex(script, r'(?m)^\s*pg_dump -Fc -f "\$DUMP" "\$DB"$')
        self.assertNotRegex(script, r"pg_dump [^\n]*\|")

    def test_postgres_dumps_log_in_as_their_backup_login(self):
        """INFRA-206: the superuser is to refuse network logins, so each dump uses its database's backup login, and the
        globals skip the password hashes a non-superuser cannot read."""
        script = re.search(r"^  pg-dump\.sh: \|\n((?:    .*\n|\n)+)", self.text, re.M).group(1)
        self.assertRegex(script, r'(?m)^\s*pg_dumpall --globals-only --no-role-passwords -l "\$DB" \|')
        want = {"pg-dump": ("ziftbook_backup", "postgres-ziftbook-backup"),
                "pg-dump-prod": ("ziftbook_prod_backup", "postgres-ziftbook-prod-backup")}
        for name, (role, secret) in want.items():
            block = re.search(rf"(?m)^            - name: {name}\n((?:             .*\n|\s*#.*\n)+)", self.text).group(1)
            self.assertRegex(block, rf"- name: PGUSER\n\s+value: {role}\n", name)
            self.assertRegex(block, rf"- name: PGPASSWORD\n\s+valueFrom:\n\s+secretKeyRef:\n\s+name: {secret}\n"
                                    r"\s+key: password\n", name)
            self.assertNotIn("admin-password", block, name)


if __name__ == "__main__":
    unittest.main()
