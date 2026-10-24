#!/usr/bin/env python3
"""Fail if any Secret under the given directory is not SOPS-encrypted (INFRA-23).

Rules: a Secret lives in its own *.secret.yaml file, that file carries SOPS
metadata with a MAC, and every data/stringData value is an ENC[...] string
(or empty, which sops leaves as is). Kustomize secretGenerator is not allowed.
A *.sops.yaml file (Ansible vars) carries SOPS metadata and every value is ENC[...].
Stdlib only, line-based: it understands the layout `sops encrypt` writes. manifest_fence.py first refuses any file
outside that layout (GHA-03): every file under a manifest root, and the *.sops.yaml files under a root named ansible.
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import manifest_fence  # noqa: E402

# YAML (quoted, commented, or nested in a List) or JSON.
KIND_SECRET = re.compile(r"""^\s*kind:\s*["']?Secret["']?\s*(#.*)?$|"kind"\s*:\s*"Secret\"""", re.M)
TOP_KEY = re.compile(r"^([^\s#][^:]*):\s*(.*)$")
ENTRY = re.compile(r"^\s+([^:#\s][^:]*):\s*(.*)$")
# Any `key: value`, `- key: value` or `- value` line; group 2 is the value.
VALUE = re.compile(r"^\s*(?:-\s+)?(?:([^:#\s][^:]*):(?:\s+|$))?(.*?)\s*$")


def sops_file_problems(text):
    """A *.sops.yaml file (Ansible vars, INFRA-64): metadata, and every value outside sops: is ENC[...]."""
    out = []
    if not re.search(r"^sops:", text, re.M) or not re.search(r"^\s+mac: ENC\[", text, re.M):
        out.append("no SOPS metadata; run `sops encrypt -i`")
    for line in text.splitlines():
        if line.startswith("sops:"):
            break
        m = VALUE.match(line)
        if line.strip() in ("---", "..."):
            continue
        if m and m.group(2) and not m.group(2).startswith(("ENC[", "#")) and m.group(2) not in ('""', "''"):
            out.append(f"{(m.group(1) or '-').strip()} is not encrypted")
    return out


def problems(path):
    text = path.read_bytes().decode("utf-8", "replace")  # manifest_fence reports a file that is not UTF-8
    if path.name.lower().endswith((".sops.yaml", ".sops.yml")):
        return sops_file_problems(text)
    out = []
    _, docs = manifest_fence.scan(text)
    if re.search(r"^\s*secretGenerator:", text, re.M):
        out.append("secretGenerator writes plaintext; use a *.secret.yaml file")
    if not path.name.lower().endswith(".secret.yaml"):
        # The scanner also sees `- kind: Secret` and a kind written in another case.
        if KIND_SECRET.search(text) or any(
            e.path[-1:] == ("kind",) and e.style in manifest_fence.SCALAR and e.text.lower() == "secret"
            for d in docs for e in d.entries
        ):
            out.append("Secret outside a *.secret.yaml file; move it and run `sops encrypt -i`")
        return out
    if any(d.root_col for d in docs):
        # The value check below reads data/stringData at column 0.
        out.append("a document that does not start at column 0")
    if not re.search(r"^sops:", text, re.M) or not re.search(r"^\s+mac: ENC\[", text, re.M):
        out.append("no SOPS metadata; run `sops encrypt -i`")
    block = None
    for line in text.splitlines():
        top = TOP_KEY.match(line)
        if top:
            block = top.group(1) if top.group(1) in ("data", "stringData") else None
            if block and top.group(2) and not top.group(2).startswith("#"):
                out.append(f"{block} is not encrypted (flow style)")
            continue
        m = ENTRY.match(line) if block else None
        if m and not m.group(2).startswith("ENC[") and m.group(2) not in ('""', "''"):
            out.append(f"{block}.{m.group(1)} is not encrypted")
    return out


def main(roots):
    failed = False
    for root in roots:
        for path, p in manifest_fence.problems(root, manifests=pathlib.Path(root).name != "ansible"):
            print(f"::error file={path}::{path}: {p}")
            failed = True
    files = sorted(
        p
        for root in roots
        for p in pathlib.Path(root).rglob("*")
        if p.suffix.lower() in (".yaml", ".yml", ".json")
        # Third-party collections installed (git-ignored) by ansible-galaxy and ansible-lint at the root only.
        and p.relative_to(root).parts[0] not in ("collections", ".ansible")
    )
    for path in files:
        for p in problems(path):
            print(f"::error file={path}::{path}: {p}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["clusters", "ansible"]))
