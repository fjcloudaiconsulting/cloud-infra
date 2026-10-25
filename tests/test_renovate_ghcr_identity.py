"""Renovate reads GHCR as a dedicated machine user, not an owner account (GHA-09, INFRA-253). Stdlib only."""
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
READER = "fjc-renovate-reader"
SECRET = "GHCR_RENOVATE_TOKEN"
RETIRED_SECRET = "GHCR_READ_TOKEN"
# Every file that names the Mend secret.
NAMES_SECRET = ["renovate.json", "docs/configuration-map.md", ".github/scripts/check-release-drift.sh"]


def ghcr_rules():
    config = json.loads((ROOT / "renovate.json").read_text())
    return [r for r in config.get("hostRules", []) if "ghcr.io" in r.get("matchHost", "")]


class RenovateGhcrIdentity(unittest.TestCase):
    def test_one_ghcr_rule(self):
        self.assertEqual(len(ghcr_rules()), 1)

    def test_rule_uses_the_machine_user(self):
        rule = ghcr_rules()[0]
        self.assertEqual(rule["matchHost"], "ghcr.io")
        self.assertEqual(rule["hostType"], "docker")
        self.assertEqual(rule["username"], READER)

    def test_password_is_the_machine_users_mend_secret(self):
        self.assertEqual(ghcr_rules()[0]["password"], "{{ secrets.%s }}" % SECRET)

    def test_retired_secret_is_not_named_anywhere(self):
        for rel in NAMES_SECRET:
            with self.subTest(rel):
                self.assertNotIn(RETIRED_SECRET, (ROOT / rel).read_text())

    def test_configuration_map_records_the_identity(self):
        text = (ROOT / "docs/configuration-map.md").read_text()
        mend = text.split("## Mend Renovate", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"`{SECRET}`", mend)
        self.assertIn(f"`{READER}`", mend)
        expiries = text.split("## Expiries and rotation", 1)[1]
        self.assertIn(f"`{SECRET}` (Mend)", expiries)


if __name__ == "__main__":
    unittest.main()
