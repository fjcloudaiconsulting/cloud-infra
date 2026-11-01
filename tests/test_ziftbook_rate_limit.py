"""Fence: the Ziftbook edge rate limit lists every credential endpoint and keeps the encoded-slash guard (ZIF-219).

Stdlib only: `python3 -m unittest discover -s tests`. The Terraform is read as text.
"""
import pathlib
import re
import unittest

TF = pathlib.Path(__file__).resolve().parent.parent / "terraform" / "cloudflare"
REQUIRED = {
    "/api/session", "/api/admin/session", "/api/admin/session/second-factor", "/api/sign-up",
    "/api/sign-up/complete", "/api/password-reset", "/api/password-reset/complete", "/api/invites/accept",
    "/api/health/dependencies",
}


class ZiftbookRateLimit(unittest.TestCase):
    def test_paths_and_guard(self):
        text = (TF / "rate_limit.tf").read_text()
        m = re.search(r"ziftbook = \[(.*?)\]", text, re.S)
        self.assertTrue(m, "ziftbook path list not found")
        paths = set(re.findall(r'"(/[^"]*)"', m.group(1)))
        self.assertFalse(REQUIRED - paths, f"missing from the rate limit: {sorted(REQUIRED - paths)}")
        self.assertIn('contains \\"%2F\\"', text)
        self.assertIn('contains \\"%2f\\"', text)

    def test_pseudo_ipv4_is_off(self):
        text = (TF / "main.tf").read_text()
        m = re.search(r'resource "cloudflare_zone_setting" "ziftbook_pseudo_ipv4" \{(.*?)\n\}', text, re.S)
        self.assertTrue(m)
        self.assertRegex(m.group(1), r'setting_id\s*=\s*"pseudo_ipv4"')
        self.assertRegex(m.group(1), r'value\s*=\s*"off"')


if __name__ == "__main__":
    unittest.main()
