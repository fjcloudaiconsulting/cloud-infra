"""Runs upload.sh from clusters/hetzner/data/db-backup.yaml with a stub `aws` (INFRA-203): R2 gets the dump and
grants only gpg-encrypted to the backup key, never in plaintext, and nothing at all without the key."""

import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "clusters/hetzner/data/db-backup.yaml"
SET = "tbd-mysql"
KEYS = [f"{SET}/2026/11/07/tbd_20261107-020000.sql.gz", f"{SET}/2026/11/07/grants_20261107-020000.sql.gz",
        f"{SET}/2026/11/07/manifest_20261107-020000.json"]
# Copies --body to $OUT/<--key> and logs the arguments, like a put-object that always succeeds.
STUB_AWS = """#!/bin/bash
while [ $# -gt 0 ]; do case $1 in --key) k=$2; shift;; --body) b=$2; shift;; esac; shift; done
mkdir -p "$OUT/$(dirname "$k")" && cp "$b" "$OUT/$k"
"""


@unittest.skipUnless(shutil.which("gpg"), "gpg not installed")
class BackupUpload(unittest.TestCase):
    def setUp(self):
        # Short path: gpg-agent's socket must fit in sun_path.
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bu", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.backup, self.scripts, self.out, self.bin = (self.tmp / d for d in ("b", "s", "o", "x"))
        for d in (self.backup, self.scripts, self.out, self.bin):
            d.mkdir()
        script = re.search(r"^  upload\.sh: \|\n((?:    .*\n|\n)+)", MANIFEST.read_text(), re.M).group(1)
        script = re.sub(r"(?m)^    ", "", script).replace("/scripts/", f"{self.scripts}/").replace("/backup/", f"{self.backup}/")
        (self.scripts / "upload.sh").write_text(script)
        (self.bin / "aws").write_text(STUB_AWS)
        (self.bin / "aws").chmod(0o755)
        for k in KEYS:
            (self.backup / k).parent.mkdir(parents=True, exist_ok=True)
            (self.backup / k).write_bytes(os.urandom(64) + k.encode())
        (self.backup / f"{SET}.ok").write_text("\n".join(KEYS) + "\n")
        self.home = self.tmp / "g"
        self.home.mkdir(mode=0o700)
        self.gpg("--quick-gen-key", "backup-test", "future-default", "default", "1d")
        pub = self.gpg("--armor", "--export", "backup-test").stdout
        self.assertIn(b"BEGIN PGP PUBLIC KEY BLOCK", pub)
        self.pub = pub

    def gpg(self, *args, stdin=None):
        return subprocess.run(["gpg", "--homedir", str(self.home), "--batch", "--pinentry-mode", "loopback",
                               "--passphrase", "", *args], input=stdin, capture_output=True, check=True)

    def run_upload(self, **env):
        e = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.tmp), "OUT": str(self.out),
             "DUMP_SETS": SET, "BUCKET": "b", **env}
        return subprocess.run(["bash", str(self.scripts / "upload.sh")], env=e, capture_output=True, text=True)

    def uploaded(self):
        return sorted(str(p.relative_to(self.out)) for p in self.out.rglob("*") if p.is_file())

    def test_r2_gets_only_ciphertext_and_a_plaintext_manifest(self):
        (self.scripts / "backups.pub.asc").write_bytes(self.pub)
        # In the pod the set directories belong to the dump containers' user: the uploader cannot write there.
        dirs = [d for d in (self.backup / SET).rglob("*") if d.is_dir()] + [self.backup / SET]
        for d in dirs:
            d.chmod(0o555)
        self.addCleanup(lambda: [d.chmod(0o755) for d in dirs])
        r = self.run_upload(R2_ENDPOINT="https://r2.invalid")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.uploaded(), sorted([KEYS[0] + ".gpg", KEYS[1] + ".gpg", KEYS[2]]))
        for k in KEYS[:2]:
            body = (self.out / (k + ".gpg")).read_bytes()
            self.assertNotIn(k.encode(), body)
            self.assertEqual(self.gpg("--decrypt", stdin=body).stdout, (self.backup / k).read_bytes())
        self.assertEqual((self.out / KEYS[2]).read_bytes(), (self.backup / KEYS[2]).read_bytes())

    def test_r2_without_the_key_uploads_nothing(self):
        r = self.run_upload(R2_ENDPOINT="https://r2.invalid")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no backup encryption key", r.stderr)
        self.assertEqual(self.uploaded(), [])

    def test_s3_still_gets_the_plain_keys(self):
        r = self.run_upload(KMS_KEY_ARN="arn:aws:kms:eu-central-1:0:key/x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.uploaded(), sorted(KEYS))


if __name__ == "__main__":
    unittest.main()
