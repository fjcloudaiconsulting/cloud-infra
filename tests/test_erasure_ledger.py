"""Fence (INFRA-267): Ziftbook's erasure-ledger lock outlasts every restorable backup.

A restore replays the ledger's erasures, so an entry must stay locked at least as long as the oldest backup it could
apply to: the backup bucket deletes each object a day after its lock ends (no versions are kept). If backup retention
grows, raise the ledger lock first. The worker's token reaches exactly that environment's ledger bucket, at object
level. Stdlib only: `python3 -m unittest discover -s tests`.
"""
import re
import unittest
from pathlib import Path

TF = Path(__file__).resolve().parents[1] / "terraform"
R2 = TF / "cloudflare/r2.tf"
TOKENS = TF / "cloudflare-tokens/main.tf"


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


def block(text, header):
    """The body of the first block that starts with header, up to the next top-level closing brace."""
    start = text.index(header)
    return text[start:text.index("\n}\n", start)]


def worker_token_problems(r2, tokens):
    """Why the ledger worker tokens are not scoped to exactly the ledger buckets, at object level."""
    envs = set(re.findall(r"(?m)^    (\w+)\s*=\s*\{ origin", block(r2, "  ziftbook_r2 = {")))
    bucket, token = block(r2, 'resource "cloudflare_r2_bucket" "ziftbook_ledger"'), \
        block(tokens, 'resource "cloudflare_account_token" "ziftbook_ledger_worker"')
    problems = []
    if 'name         = "ziftbook-ledger-${each.key}"' not in bucket or 'jurisdiction = "eu"' not in bucket:
        problems.append("the ledger bucket name or jurisdiction moved")
    m = re.search(r"for_each\s*=\s*toset\(\[([^\]]*)\]\)", token)
    if not m or set(re.findall(r'"(\w+)"', m.group(1))) != envs:
        problems.append(f"token environments differ from {sorted(envs)}")
    if token.count("effect ") != 1 or '_eu_ziftbook-ledger-${each.key}" = "*"' not in token:
        problems.append("not one policy on that environment's ledger bucket")
    if "com.cloudflare.api.account" in token or re.findall(r"one\(local\.(\w+)\)", token) != ["r2_item_write_ids"]:
        problems.append("broader than object access on the bucket")
    return problems


class LedgerWorkerToken(unittest.TestCase):
    def setUp(self):
        self.r2, self.tokens = R2.read_text(), TOKENS.read_text()

    def test_scoped_to_exactly_the_ledger_buckets(self):
        self.assertEqual(worker_token_problems(self.r2, self.tokens), [])

    def test_widened_tokens_fail(self):
        account = '"com.cloudflare.api.account.${local.account_id}" = "*"'
        for name, edit in (
                ("account-wide", lambda s: s.replace('"com.cloudflare.edge.r2.bucket.${local.account_id}_eu_ziftbook-ledger-${each.key}" = "*"', account)),
                ("bucket-admin group", lambda s: s.replace("one(local.r2_item_write_ids)", "one(local.r2_write_ids)")),
                ("an extra environment", lambda s: s.replace('toset(["staging", "prod"])', 'toset(["staging", "prod", "dev"])')),
                ("default jurisdiction", lambda s: s.replace("_eu_ziftbook-ledger-", "_default_ziftbook-ledger-"))):
            with self.subTest(name):
                widened = edit(self.tokens)
                self.assertNotEqual(widened, self.tokens)
                self.assertNotEqual(worker_token_problems(self.r2, widened), [])


if __name__ == "__main__":
    unittest.main()
