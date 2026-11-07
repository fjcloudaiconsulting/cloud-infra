"""Fence (INFRA-267): Ziftbook's erasure-ledger lock outlasts every restorable backup.

A restore replays the ledger's erasures, so an entry must stay locked at least as long as the oldest backup it could
apply to: the backup bucket deletes each object a day after its lock ends (no versions are kept). If backup retention
grows, raise the ledger lock first. Stdlib only: `python3 -m unittest discover -s tests`.
"""
import re
import unittest
from pathlib import Path

TF = Path(__file__).resolve().parents[1] / "terraform"
R2 = TF / "cloudflare/r2.tf"


def number(pattern, text):
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f"not found: {pattern}")
    return int(m.group(1))


def lock_margin(r2):
    """Ledger lock days minus the backups' retention (lock days plus the one day before deletion); negative is a gap."""
    lock = number(r"(?m)^\s*ziftbook_ledger_lock_days\s*=\s*(\d+)\s*$", r2)
    retention = number(r"(?m)^\s*backup_lock_days\s*=\s*(\d+)\s*$", r2) + 1
    return lock - retention


class ErasureLedgerLock(unittest.TestCase):
    def setUp(self):
        self.r2 = R2.read_text()

    def test_lock_outlasts_backup_retention(self):
        self.assertGreaterEqual(lock_margin(self.r2), 0)

    def test_backup_deletion_uses_the_checked_days(self):
        # guard: the retention lock_margin reads is the one the backup bucket's lifecycle applies.
        self.assertRegex(self.r2, r"condition = \{ type = \"Age\", max_age = \(local\.backup_lock_days \+ 1\) \* 86400 \}")

    def test_lock_rule_uses_the_checked_days(self):
        self.assertRegex(self.r2, r'prefix\s*=\s*"erasures/"\s*\n\s*condition\s*=\s*\{ type = "Age", '
                                  r"max_age_seconds = local\.ziftbook_ledger_lock_days \* 86400 \}")

    def test_a_lowered_lock_fails(self):
        lowered = re.sub(r"(ziftbook_ledger_lock_days\s*=\s*)\d+", r"\g<1>7", self.r2)
        self.assertLess(lock_margin(lowered), 0)


if __name__ == "__main__":
    unittest.main()
