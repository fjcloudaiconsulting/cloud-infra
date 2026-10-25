#!/usr/bin/env python3
"""PR-time fences for terraform/tbd-backups, ported from tbd's test_backup_offhost.py (F3, F5)
when the stack moved here (INFRA-18), plus the TFC role policies (F6). Stdlib only; exits 1 with every failure
listed. tests/test_terraform_fences.py runs it on mutated copies."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STACK = ROOT / "terraform/tbd-backups"
BOOTSTRAP = ROOT / "aws/bootstrap"
TRUST = BOOTSTRAP / "tfc-backups-trust.json"
failures = []


def check(ok, msg):
    if not ok:
        failures.append(msg)


def actions(doc):
    out = set()
    for stmt in doc["Statement"]:
        a = stmt["Action"]
        out.update(a if isinstance(a, list) else [a])
    return out


# F3. The uploader is put-only and the probe list-only, both directions. Effect and Resource are
# as load-bearing as Action: a mutant that flipped Effect to Deny and widened Resource to
# arn:aws:s3:::*/* kept the action set identical.
# The uploader template takes its Resource list as raw JSON (like the probe's prefixes); render it the way
# main.tf does, from the real prefix list, so the exact-resource check below sees every ARN.
prefixes = json.loads(re.search(r"k3s_backup_prefixes\s*=\s*(\[[^\]]*\])", (STACK / "main.tf").read_text()).group(1))
check(prefixes, "could not find local.k3s_backup_prefixes in main.tf")
check(all(re.fullmatch(r"[a-z0-9-]+", p) for p in prefixes), f"odd backup prefix in {prefixes}")
check('"arn:aws:s3:::${var.bucket_name}/${p}/*"' in (STACK / "main.tf").read_text(),
      "main.tf no longer builds the uploader Resource list as exactly arn:aws:s3:::<bucket>/<prefix>/*")
uploader = json.loads((STACK / "policies/backup-uploader.json").read_text().replace(
    "${resources}", json.dumps([f"arn:aws:s3:::${{bucket}}/{p}/*" for p in prefixes])))
# The probe template injects its prefix list as raw JSON; stand in an empty list so it parses.
probe = json.loads((STACK / "policies/backup-probe.json").read_text().replace("${prefixes}", "[]"))
check(actions(uploader) == {"s3:PutObject", "kms:GenerateDataKey", "kms:Encrypt", "kms:DescribeKey"},
      f"uploader policy actions are {sorted(actions(uploader))}; it must stay exactly put + encrypt "
      "(read access would let a compromised uploader harvest every historical dump)")
check(actions(probe) == {"s3:ListBucket"}, f"probe policy actions are {sorted(actions(probe))}; must be exactly s3:ListBucket")
# Exact resources: a substring test would pass arn:aws:s3:::*${bucket}*.
SCOPED = {*(f"arn:aws:s3:::${{bucket}}/{p}/*" for p in prefixes), "${kms_key_arn}", "arn:aws:s3:::${bucket}"}
for name, doc in (("uploader", uploader), ("probe", probe)):
    for stmt in doc["Statement"]:
        check(stmt["Effect"] == "Allow", f"{name} policy has a {stmt['Effect']} statement; these are grant policies")
        res = stmt["Resource"]
        for r in res if isinstance(res, list) else [res]:
            check(r in SCOPED, f"{name} policy grants on {r!r}, not exactly the bucket, prefix or key")
# StringEquals against an absent header fails, so the uploader must send both SSE headers; the
# uploader-side half of this fence stays in tbd with the script. Every PutObject statement: a second
# unconditioned one would otherwise allow unencrypted writes.
puts = [s for s in uploader["Statement"] if "s3:PutObject" in str(s["Action"])]
check(puts, "uploader policy has no PutObject statement")
for put in puts:
    cond = put.get("Condition", {}).get("StringEquals", {})
    check(cond.get("s3:x-amz-server-side-encryption") == "aws:kms", "an uploader PutObject statement does not require SSE aws:kms")
    check(any("kms-key-id" in k for k in cond), "an uploader PutObject statement does not pin the KMS key id")

# F5. TBD-372: the role trust is managed BY the workspace it authorizes, so a rename applied with
# the trust unchanged locks the workspace out of its own fix. The declared workspace must be
# authorized by ANY statement (not ALL: the safe rename widens to both names first), and a
# statement must not glob the declared name.
m = re.search(r'workspaces\s*\{[^}]*name\s*=\s*"([^"]+)"', (STACK / "versions.tf").read_text(), re.DOTALL)
check(m, "could not find the workspace name in versions.tf")
if m:
    ws = m.group(1)
    # main.tf builds the trust sub from var.tfc_workspace_name, so the plan-time precondition there
    # cannot see a rename in versions.tf alone; pin the two together here.
    v = re.search(r'variable\s+"tfc_workspace_name"\s*\{.*?default\s*=\s*"([^"]+)"',
                  (STACK / "variables.tf").read_text(), re.DOTALL)
    check(v and v.group(1) == ws, f"var tfc_workspace_name default {v and v.group(1)!r} != versions.tf workspace {ws!r}")
    trust = json.loads(TRUST.read_text())
    subs = [v for st in trust["Statement"] for c in st.get("Condition", {}).values()
            for k, v in c.items() if k.endswith(":sub")]
    subs = [s for v in subs for s in (v if isinstance(v, list) else [v])]
    check(any(f"workspace:{ws}:" in s for s in subs),
          f"versions.tf declares workspace {ws!r} but no trust statement authorizes it (subs: {subs}). "
          "Widen the trust, apply, rename, then narrow.")
    for s in subs:
        seg = re.search(r"workspace:([^:]+):", s)
        if seg and "*" in seg.group(1):
            prefix = seg.group(1).split("*")[0]
            check(not (prefix and ws.startswith(prefix)),
                  f"trust workspace segment {seg.group(1)!r} globs the declared workspace {ws!r}; name it exactly")

# F6. The two TFC role policies in aws/bootstrap/, both applied by this workspace. The provisioner's policy is
# applied by the provisioner itself, so a widening merged here is one it grants itself on the next apply; the plan
# role runs every PR's speculative plan before review. Grants stay inside the backup chain and are made by name:
# NotAction or NotResource in an Allow grants everything else. A change is re-checked by the owner with
# simulate-principal-policy (terraform/tbd-backups/README.md).
SERVICES = {"s3", "kms", "iam", "sts"}
# The six identities this stack manages (any account id: IAM acts only in the caller's own account).
IDENTITIES = {"user/k3s-backup-uploader", "role/github-actions-backup-probe", "role/tfc-backups-provisioner",
              "role/tfc-backups-plan", "oidc-provider/app.terraform.io",
              "oidc-provider/token.actions.githubusercontent.com"}
CONTENT = {"s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectAttributes", "kms:Decrypt", "kms:ReEncryptFrom"}
# Key actions the provisioner holds without the tag condition: creating the key, before a tag can authorize it.
UNTAGGED_KEY_ACTIONS = {"kms:createkey", "kms:createalias", "kms:deletealias", "kms:listaliases", "kms:tagresource",
                        "kms:describekey"}
bucket = re.search(r'variable\s+"bucket_name"\s*\{.*?default\s*=\s*"([^"]+)"', (STACK / "variables.tf").read_text(),
                   re.DOTALL)
check(bucket, "could not find var.bucket_name's default in variables.tf")
BUCKET = {f"arn:aws:s3:::{bucket.group(1)}", f"arn:aws:s3:::{bucket.group(1)}/*"} if bucket else set()


def as_list(v):
    return v if isinstance(v, list) else [v]


def identity(arn):
    m = re.fullmatch(r"arn:aws:iam::\d{12}:(.+)", arn)
    return bool(m) and m.group(1) in IDENTITIES


def tagged(stmt):
    # Conditions are ANDed: with this one present, only keys carrying the backup tag match.
    return stmt.get("Condition", {}).get("StringEquals", {}).get("aws:ResourceTag/component") == "mysql-offhost-backup"


def allows(name, doc):
    """(Sid, lower-cased actions, resources, statement) of every statement that is not a Deny."""
    out = []
    for s in doc["Statement"]:
        if s.get("Effect") != "Deny":
            check(s.get("Effect") == "Allow" and "Action" in s and "Resource" in s,
                  f"{name} policy statement {s.get('Sid')!r} must Allow a named Action on a named Resource "
                  "(no NotAction or NotResource)")
            out.append((s.get("Sid"), {a.lower() for a in as_list(s.get("Action", []))},
                        as_list(s.get("Resource", [])), s))
    return out


def kept_deny(name, doc, sid, acts, resource):
    st = [s for s in doc["Statement"] if s.get("Sid") == sid]
    ok = (len(st) == 1 and st[0].get("Effect") == "Deny" and not {"Condition", "NotAction", "NotResource"} & set(st[0])
          and {a.lower() for a in acts} <= {a.lower() for a in as_list(st[0].get("Action", []))}
          and any(re.fullmatch(resource, r) for r in as_list(st[0].get("Resource", []))))
    check(ok, f"{name} policy must keep its unconditional Deny {sid} of {sorted(acts)}")


def scoped(name, sid, acts, res, stmt, untagged_kms):
    """IAM on the six identities, S3 on the bucket, key actions on the tagged key."""
    check(all(a.split(":")[0] in SERVICES for a in acts),
          f"{name} policy statement {sid!r} grants {sorted(acts)}; only {sorted(SERVICES)} actions, by service")
    check(not {a for a in acts if a.startswith("sts:")} - {"sts:getcalleridentity"},
          f"{name} policy statement {sid!r} grants STS beyond sts:GetCallerIdentity")
    # ListOpenIDConnectProviders takes no resource ARN and returns provider ARNs only.
    iam = {a for a in acts if a.startswith("iam:")} - {"iam:listopenidconnectproviders"}
    off = [r for r in res if not identity(r)]
    check(not (iam and off), f"{name} policy statement {sid!r} grants {sorted(iam)} on {off}; "
          "IAM stays on the six backup identities")
    s3 = {a for a in acts if a.startswith("s3:")}
    check(not (s3 and set(res) - BUCKET), f"{name} policy statement {sid!r} grants {sorted(s3)} on "
          f"{sorted(set(res) - BUCKET)}; S3 stays on the backup bucket")
    kms = {a for a in acts if a.startswith("kms:")}
    check(not kms or tagged(stmt) or kms <= untagged_kms,
          f"{name} policy statement {sid!r} grants {sorted(kms - untagged_kms)} on keys not tagged "
          "component=mysql-offhost-backup")


# What Terraform applies is what this fence reads: both TFC roles take their trust and their one inline policy from
# the aws/bootstrap/ documents by file(), and no other resource in the stack attaches anything to them.
BOUND = {("aws_iam_role", "tfc_backups_provisioner"): ("assume_role_policy", "tfc-backups-trust.json"),
         ("aws_iam_role", "tfc_backups_plan"): ("assume_role_policy", "tfc-backups-plan-trust.json"),
         ("aws_iam_role_policy", "tfc_backups_provisioner"): ("policy", "tfc-backups-provisioner.json"),
         ("aws_iam_role_policy", "tfc_backups_plan"): ("policy", "tfc-backups-plan.json")}
resources = {}
for tf in sorted(STACK.glob("*.tf")):
    for m in re.finditer(r'(?ms)^resource "([^"]+)" "([^"]+)" \{\n(.*?)^\}', tf.read_text()):
        resources.setdefault((m.group(1), m.group(2)), []).append(m.group(3))
        refs = re.findall(r'aws_iam_role\.tfc_backups_(?:provisioner|plan)\.(?!arn\b)\w+'
                          r'|"tfc-backups-(?:provisioner|plan)"', m.group(3))
        check((m.group(1), m.group(2)) in BOUND or not refs,
              f"{tf.name}: {m.group(1)}.{m.group(2)} refers to a TFC role ({refs}); "
              "only that role's one inline policy attaches to it")
for (kind, name), (attr, doc) in BOUND.items():
    bodies = resources.get((kind, name), [])
    vals = [v for b in bodies for v in re.findall(rf"(?m)^\s*{attr}\s*=\s*(.+?)\s*$", b)]
    check(len(bodies) == 1 and vals == [f'file("${{path.module}}/../../aws/bootstrap/{doc}")'],
          f"{kind}.{name} {attr} must be exactly file() of aws/bootstrap/{doc}, the document this fence checks")
    if kind == "aws_iam_role_policy":
        roles = [v for b in bodies for v in re.findall(r"(?m)^\s*role\s*=\s*(.+?)\s*$", b)]
        check(roles == [f"aws_iam_role.{name}.id"], f"{kind}.{name} must attach to aws_iam_role.{name}")

prov = json.loads((BOOTSTRAP / "tfc-backups-provisioner.json").read_text())
for sid, acts, res, stmt in allows("provisioner", prov):
    check("*" not in acts, f"provisioner policy statement {sid!r} allows every action")
    scoped("provisioner", sid, acts, res, stmt, UNTAGGED_KEY_ACTIONS)
kept_deny("provisioner", prov, "NeverReadBackupContent", CONTENT, r"\*")
kept_deny("provisioner", prov, "NeverMintTheK3sUploaderCredentials", {"iam:CreateAccessKey", "iam:CreateLoginProfile"},
          r"arn:aws:iam::\d{12}:user/k3s-backup-uploader")

# The plan role reads: Get, List and Describe verbs on the four services, and STS only to learn who it is. It reads
# only what this stack's refresh reads: the six identities, the bucket and the tagged key. kms:ListAliases takes no
# resource (aws_kms_alias is read by listing), so it alone stays on every key.
plan = json.loads((BOOTSTRAP / "tfc-backups-plan.json").read_text())
for sid, acts, res, stmt in allows("plan", plan):
    bad = sorted(a for a in acts
                 if not re.fullmatch(r"(s3|kms|iam):(get|list|describe)[a-z0-9]*\*?|sts:getcalleridentity", a))
    check(not bad, f"plan policy statement {sid!r} allows {bad}; the plan role stays read-only "
          "(Get*, List*, Describe*)")
    scoped("plan", sid, acts, res, stmt, {"kms:listaliases"})
kept_deny("plan", plan, "NeverReadBackupContent", CONTENT, r"\*")

for f in failures:
    print(f"::error::{f}")
print("tbd-backups fences:", "FAIL" if failures else "ok")
sys.exit(1 if failures else 0)
