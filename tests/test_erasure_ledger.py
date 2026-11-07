"""Fence (INFRA-267): Ziftbook's erasure-ledger lock outlasts every restorable backup.

A restore replays the ledger's erasures, so an entry must stay locked at least as long as the oldest backup it could
apply to: the backup bucket's retention_days plus the noncurrent days its old versions are kept. If backup retention
grows, raise the ledger lock first. Stdlib only: `python3 -m unittest discover -s tests`.
"""
import re
import unittest
from pathlib import Path

TF = Path(__file__).resolve().parents[1] / "terraform"
R2 = TF / "cloudflare/r2.tf"
BACKUP_VARS = TF / "tbd-backups/variables.tf"
BACKUP_MAIN = TF / "tbd-backups/main.tf"


def number(pattern, text):
    m = re.search(pattern, text)
    if not m:
        raise AssertionError(f"not found: {pattern}")
    return int(m.group(1))


def lock_margin(r2, backup_vars, backup_main):
    """Ledger lock days minus the backups' retention (current days plus noncurrent days); negative is a gap."""
    lock = number(r"(?m)^\s*ziftbook_ledger_lock_days\s*=\s*(\d+)\s*$", r2)
    retention = number(r'variable "retention_days"\s*\{[^}]*?default\s*=\s*(\d+)', backup_vars)
    noncurrent = number(r"noncurrent_days\s*=\s*(\d+)", backup_main)
    return lock - (retention + noncurrent)


class ErasureLedgerLock(unittest.TestCase):
    def setUp(self):
        self.r2, self.vars, self.main = (p.read_text() for p in (R2, BACKUP_VARS, BACKUP_MAIN))

    def test_lock_outlasts_backup_retention(self):
        self.assertIn('"ziftbook-prod-postgres"', self.main)  # Ziftbook's backups are in this bucket
        self.assertGreaterEqual(lock_margin(self.r2, self.vars, self.main), 0)

    def test_lock_rule_uses_the_checked_days(self):
        self.assertRegex(self.r2, r'prefix\s*=\s*"erasures/"\s*\n\s*condition\s*=\s*\{ type = "Age", '
                                  r"max_age_seconds = local\.ziftbook_ledger_lock_days \* 86400 \}")

    def test_a_lowered_lock_fails(self):
        lowered = re.sub(r"(ziftbook_ledger_lock_days\s*=\s*)\d+", r"\g<1>8", self.r2)
        self.assertLess(lock_margin(lowered, self.vars, self.main), 0)


if __name__ == "__main__":
    unittest.main()
