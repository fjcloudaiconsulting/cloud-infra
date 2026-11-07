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


if __name__ == "__main__":
    unittest.main()
