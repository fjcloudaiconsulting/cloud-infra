"""Fence: no HCP Terraform workspace id in a tracked file. Scripts look a workspace up by name (public repo)."""
import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WS_ID = re.compile(r"\bws-[A-Za-z0-9]{16}\b")


class NoWorkspaceIds(unittest.TestCase):
    def test_tracked_files_hold_no_workspace_id(self):
        files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True,
                               check=True).stdout.split("\0")
        hits = [f for f in files if f and (ROOT / f).is_file()
                and WS_ID.search((ROOT / f).read_text(errors="ignore"))]
        self.assertEqual(hits, [])

    def test_pattern_matches_a_workspace_id(self):
        # guard: the regex finds the shape HCP Terraform uses.
        self.assertTrue(WS_ID.search('WS = "ws-' + 'AbCdEfGh12345678"'))  # split, or this file would match


if __name__ == "__main__":
    unittest.main()
