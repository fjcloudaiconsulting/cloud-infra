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

    def test_finished_jobs_are_removed(self):
        """Kills keeping finished pods indefinitely: their volume would outlive the run."""
        self.assertRegex(self.text, r"\n      ttlSecondsAfterFinished: \d+\n")


if __name__ == "__main__":
    unittest.main()
