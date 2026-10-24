#!/usr/bin/env python3
"""Canonical form of the manifests under clusters/ (GHA-03).

check-sops-secrets.py, check-prod-tags.py and check-image-provenance.sh read the manifests as text, line by line.
Text tells them what kustomize renders only while every file keeps the plain shape this repo writes, so each of them
runs this fence first and fails closed on anything else instead of guessing:

- bytes: UTF-8, `\\n` line breaks only, no control character but a tab, no tab in indentation;
- YAML: no tags, anchors, aliases, merge keys, directives, explicit `?` keys or quoted keys, nothing after `---`;
  quoted scalars and flow collections (`[...]`, `{...}`) close on their own line, and a flow collection holds no
  WATCHED key, escape, tag, anchor or alias;
- WATCHED keys: no backslash escape in a double-quoted value; except `value`, the value sits on the key's own line
  (no block scalar, no flow collection, no value on the next line);
- no `kind: List` or other `*List` kind (kustomize unwraps their items);
- kustomization files: only resources and configMapGenerator, plus patches and images in the vendored tier-0
  directories (EXEMPT), where every images entry carries a sha256 digest; each resource is a path inside the tree;
- Flux objects (`*.toolkit.fluxcd.io`) only under flux-system;
- app namespaces (<app>-prod, <app>-staging): every image is ghcr.io/fjcloudaiconsulting/<app>/..., and a document
  that runs an image in one lives in that namespace's directory;
- no `scheme://user:password@host` with a literal password outside a *.secret.yaml file;
- no symlinks.

Stdlib only: CI runs the runner's python3 with no installs. As a script: manifest_fence.py [root...] (default
clusters) prints one line per problem and exits 1 if there is any.
"""
import collections
import os
import pathlib
import re
import sys

ORG = "ghcr.io/fjcloudaiconsulting/"
APP_NS = re.compile(r"[a-z0-9][a-z0-9-]*-(?:prod|staging)")
# Vendored from upstream and patched in their kustomization.yaml (code-owned, see .github/CODEOWNERS).
EXEMPT = ("flux-system", "system-upgrade")
KUSTOMIZATIONS = ("kustomization.yaml", "kustomization.yml", "Kustomization")
KUSTOMIZATION_KEYS = {"apiVersion", "kind", "resources", "configMapGenerator"}
EXEMPT_KUSTOMIZATION_KEYS = KUSTOMIZATION_KEYS | {"patches", "images"}
# Keys whose values the checks read: the kind of an object, image refs and kustomize's image fields, the namespace,
# env values.
WATCHED = {"kind", "image", "name", "namespace", "newName", "newTag", "digest", "value"}
SAME_LINE = WATCHED - {"value"}
SCALAR = ("plain", "single", "double")
BAD_CHAR = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ufeff]")
CREDENTIAL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@'\"]+:([^\s/@'\"]+)@")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
KEY = re.compile(r"(.*?)[ \t]*:(?:[ \t]|$)")
BLOCK_HEADER = re.compile(r"[|>][1-9+-]{0,2}(?:[ \t]+#.*)?")
FLOW_KEY = re.compile(r"(?:^|[\s,\[{])(" + "|".join(sorted(WATCHED)) + r")[ \t]*:")
FLOW_NODE_PROPERTY = re.compile(r"(?:^|[\s,\[{:])[!&*]")

Entry = collections.namedtuple("Entry", "line path style text")


class Doc:
    """One YAML document: entries for every key and scalar node, path from the root (keys, item line numbers)."""

    def __init__(self):
        self.entries = []
        self.root_col = None


def _close(s, q, i=0):
    """(end, escaped) for a scalar quoted with q: end is the index after the closing quote, None if s ends first."""
    escaped = False
    while i < len(s):
        c = s[i]
        if q == '"' and c == "\\":
            escaped, i = True, i + 2
            continue
        if c == q:
            if q == "'" and s[i + 1:i + 2] == "'":
                i += 2
                continue
            return i + 1, escaped
        i += 1
    return None, escaped


def _quoted(s):
    """(content, end, escaped) for the quoted scalar starting at s[0]; end is None when it does not close here."""
    end, escaped = _close(s, s[0], 1)
    if end is None:
        return None, None, escaped
    text = s[1:end - 1]
    return (text.replace("''", "'") if s[0] == "'" else text), end, escaped


def _flow(s):
    """(end, problem) for the flow collection starting at s[0]."""
    depth, i, norm, blank = 0, 0, [], []
    while i < len(s):
        c = s[i]
        if c in "\"'":
            text, end, escaped = _quoted(s[i:])
            if end is None:
                return None, "quoted scalar runs past its line"
            if escaped:
                return None, "escape in a flow collection"
            norm.append(text)
            blank.append(c * 2)
            i += end
            continue
        if c == "#" and i and s[i - 1] in " \t":
            break
        norm.append(c)
        blank.append(c)
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                m = FLOW_KEY.search("".join(norm))
                if m:
                    return None, f"flow collection holds `{m.group(1)}`: write it in block style"
                if FLOW_NODE_PROPERTY.search("".join(blank)):
                    return None, "tag, anchor or alias in a flow collection"
                return i + 1, None
        i += 1
    return None, "flow collection runs past its line"


def _node(raw):
    """(style, text, problem) for the node that follows `key:` or `- ` on a line."""
    if not raw or raw.startswith("#"):
        return "empty", None, None
    c = raw[0]
    if c in "\"'":
        text, end, escaped = _quoted(raw)
        if end is None:  # it continues on the next lines: text is (quote, escaped so far)
            return "open", (c, escaped), None
        tail = raw[end:].lstrip(" \t")
        if tail and not tail.startswith("#"):
            return "bad", None, "text after a quoted scalar"
        return ("double-escaped" if escaped else "double") if c == '"' else "single", text, None
    if c in "[{":
        end, problem = _flow(raw)
        if problem is None:
            tail = raw[end:].lstrip(" \t")
            if tail and not tail.startswith("#"):
                problem = "text after a flow collection"
        return "flow", None, problem
    if c in "!&*":
        return "bad", None, {"!": "YAML tag", "&": "YAML anchor", "*": "YAML alias"}[c]
    if c in "|>":
        if not BLOCK_HEADER.fullmatch(raw):
            return "bad", None, "malformed block scalar header"
        return "block", None, None
    if c in "@`%":
        return "bad", None, f"reserved indicator {c}"
    return "plain", re.split(r"[ \t]#", raw, maxsplit=1)[0].rstrip(), None


def scan(text):
    """(problems, docs) for one YAML file: problems as (line, message), docs as Doc."""
    problems, docs = [], [Doc()]
    stack = []    # (column, label, is_item): label is a key, or the line number of a sequence entry
    block = None  # deeper lines belong to a block scalar
    plain = None  # deeper lines continue a plain scalar
    quote = None  # (quote, key) of a quoted scalar that continues on the next line (vendored CRDs wrap them)

    def node(n, path, raw, key, next_line):
        nonlocal quote
        style, value, problem = _node(raw)
        if problem:
            problems.append((n, problem))
        if style == "open":
            quote, style = (value[0], key), "multi-line"
            if value[1] and key in WATCHED:
                problems.append((n, f"escape in a quoted `{key}` value"))
            value = None
        if style == "double-escaped":
            if key in WATCHED:
                problems.append((n, f"escape in a quoted `{key}` value"))
            style = "double"
        if key in SAME_LINE and (next_line or style in ("block", "flow", "multi-line")):
            problems.append((n, f"`{key}` value must be a scalar on the key's line"))
        if key == "kind" and style in SCALAR and value.endswith("List"):
            problems.append((n, f"kind {value}: write each object as its own document"))
        if style != "bad":
            docs[-1].entries.append(Entry(n, path, style, value))
        return style

    for n, line in enumerate(text.split("\n"), 1):
        body = line.lstrip(" ")
        col = len(line) - len(body)
        content = body.strip(" \t")
        if quote is not None:
            if col == 0 and content[:3] in ("---", "..."):
                problems.append((n, "document marker inside a quoted scalar"))
            end, escaped = _close(line, quote[0])
            if escaped and quote[1] in WATCHED:
                problems.append((n, f"escape in a quoted `{quote[1]}` value"))
            if end is None:
                continue
            tail = line[end:].strip(" \t")
            if tail.startswith(":"):
                problems.append((n, "quoted key: write the key plain"))
            elif tail and not tail.startswith("#"):
                problems.append((n, "text after a quoted scalar"))
            quote = None
            continue
        if block is not None:
            if not content or col > block:
                continue
            block = None
        if not content or content.startswith("#"):
            continue
        if plain is not None and col > plain:
            continue
        plain = None
        if col == 0 and content[:3] in ("---", "...") and content[3:4] in ("", " ", "\t"):
            rest = content[3:].strip(" \t")
            if rest and not rest.startswith("#"):
                problems.append((n, "content after a document marker"))
            docs.append(Doc())
            stack = []
            continue
        if col == 0 and content.startswith("%"):
            problems.append((n, "YAML directive"))
            continue
        if body.startswith("\t"):
            problems.append((n, "tab in indentation"))
            continue
        rest, c, dashes = content, col, []
        while rest == "-" or rest.startswith(("- ", "-\t")):
            dashes.append(c)
            after = rest[1:].lstrip(" \t")
            c += len(rest) - len(after)
            rest = after
        first = dashes[0] if dashes else c
        # A sequence may sit at its key's column; any other line at a column closes what is open there.
        while stack and (stack[-1][0] > first or (stack[-1][0] == first and (stack[-1][2] or not dashes))):
            stack.pop()
        if docs[-1].root_col is None:
            docs[-1].root_col = first
        parent = stack[-1] if stack and not dashes else None
        stack.extend((d, n, True) for d in dashes)
        if not rest or rest.startswith("#"):
            continue
        if rest[0] in "?:" and rest[1:2] in ("", " ", "\t"):
            problems.append((n, "explicit `?`/`:` key"))
            continue
        key, raw = None, rest
        if rest[0] in "\"'":
            text_, end, _ = _quoted(rest)
            tail = rest[end:].lstrip(" \t") if end else ""
            if end is not None and tail[:1] == ":" and tail[1:2] in ("", " ", "\t"):
                problems.append((n, "quoted key: write the key plain"))
                key, raw = text_, tail[1:].lstrip(" \t")
        elif rest[0] not in "[{!&*|>@`%":
            # A plain key ends at the first `:` followed by a blank, before any comment.
            m = KEY.match(re.split(r"[ \t]#", rest, maxsplit=1)[0])
            if m and m.group(1):
                key, raw = m.group(1), rest[m.end():].lstrip(" \t")
        if key is not None:
            if key == "<<":
                problems.append((n, "YAML merge key"))
            stack.append((c, key, False))
            style = node(n, tuple(s[1] for s in stack), raw, key, False)
            if style == "block":
                block = c
            elif style == "plain":
                plain = c
            continue
        # A scalar or flow node on its own: a sequence entry's, or the value of the key above (next line).
        owner = dashes[-1] if dashes else (parent[0] if parent else -1)
        okey = None if dashes or not parent or parent[2] else parent[1]
        style = node(n, tuple(s[1] for s in stack), raw, okey, not dashes)
        if style == "block":
            block = owner
        elif style == "plain":
            plain = owner
    if quote is not None:
        problems.append((n, "quoted scalar runs past the end of the file"))
    return problems, docs


def read(path):
    """(text, problem) for a file: UTF-8 with `\\n` line breaks and no control character but a tab."""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None, "not UTF-8"
    m = BAD_CHAR.search(text)
    if m:
        return None, f"line {text.count(chr(10), 0, m.start()) + 1}: character U+{ord(m.group()):04X}"
    return text, None


def _inside(root, p):
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _kustomization(root, path, docs, exempt):
    out, allowed = [], EXEMPT_KUSTOMIZATION_KEYS if exempt else KUSTOMIZATION_KEYS
    for doc in docs:
        images = {}
        for e in doc.entries:
            if len(e.path) == 1 and e.path[0] not in allowed:
                where = "" if exempt else f" outside {' and '.join(EXEMPT)}"
                out.append(f"line {e.line}: kustomization key `{e.path[0]}`{where}: it can rewrite what the checks read")
            if len(e.path) == 2 and e.path[0] == "resources" and e.style in SCALAR:
                target = path.parent / e.text
                if "://" in e.text or not _inside(root, target) or not target.exists():
                    out.append(f"line {e.line}: resource {e.text} is not a path inside {root}")
            if len(e.path) >= 2 and e.path[0] == "images":
                fields = images.setdefault(e.path[1], {})
                if len(e.path) == 3 and e.style in SCALAR:
                    fields[e.path[2]] = e.text
        for item, fields in images.items():
            if not DIGEST.fullmatch(fields.get("digest") or ""):
                out.append(f"line {item}: images entry without `digest: sha256:<64 hex>`")
    return out


def _manifest_rules(root, path, text, docs):
    rel = path.relative_to(root)
    dirs = rel.parts[:-1]
    dir_ns = {d for d in dirs if APP_NS.fullmatch(d)}
    out = []
    if not path.name.lower().endswith(".secret.yaml"):
        for n, line in enumerate(text.split("\n"), 1):
            if any(not m.group(1).startswith("$") for m in CREDENTIAL.finditer(line)):
                out.append(f"line {n}: password in a URL: keep it in a SOPS Secret")
    if path.name in KUSTOMIZATIONS:
        out += _kustomization(root, path, docs, not dir_ns and any(d in EXEMPT for d in dirs))
    for doc in docs:
        top = {e.path[0]: e for e in doc.entries if len(e.path) == 1 and e.style in SCALAR}
        api = top.get("apiVersion")
        if api and ".toolkit.fluxcd.io/" in api.text and (dir_ns or "flux-system" not in dirs):
            out.append(f"line {api.line}: Flux objects live under flux-system")
        images = [e for e in doc.entries if e.path[-1:] == ("image",) and e.style in SCALAR]
        doc_ns = {e.text for e in doc.entries
                  if e.path[-1:] == ("namespace",) and e.style in SCALAR and APP_NS.fullmatch(e.text)}
        if images and doc_ns - dir_ns:
            out.append(f"line {images[0].line}: a workload in {', '.join(sorted(doc_ns - dir_ns))} "
                       "lives in that namespace's directory")
        for ns in sorted(dir_ns | doc_ns):
            app = ns.rsplit("-", 1)[0]
            for e in images:
                if not e.text.startswith(f"{ORG}{app}/"):
                    out.append(f"line {e.line}: image in {ns} must come from {ORG}{app}/: {e.text}")
    return out


def problems(root, manifests=True):
    """[(path, message)] for every file under root that the checks read.

    manifests=False is an Ansible tree: only its *.sops.yaml files are read, for the YAML rules alone, and the
    collections ansible-galaxy and ansible-lint install at its root are skipped.
    """
    root, out = pathlib.Path(root), []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not manifests and rel.parts[0] in ("collections", ".ansible"):
            continue
        if p.is_symlink():
            if manifests:
                out.append((p, "symlink: commit the file itself"))
            continue
        if not p.is_file():
            continue
        if manifests:
            if p.suffix.lower() not in (".yaml", ".yml", ".json") and p.name not in KUSTOMIZATIONS:
                continue
        elif not p.name.lower().endswith((".sops.yaml", ".sops.yml")):
            continue
        text, why = read(p)
        if why:
            out.append((p, why))
            continue
        errs, docs = scan(text)
        out += [(p, f"line {n}: {msg}") for n, msg in errs]
        if manifests:
            out += [(p, msg) for msg in _manifest_rules(root, p, text, docs)]
    return out


def main(roots):
    bad = []
    for root in roots:
        if not pathlib.Path(root).is_dir():
            bad.append((root, "no such directory"))
        bad += problems(root)
    for p, msg in bad:
        print(f"::error file={p}::{p}: {msg}" if os.environ.get("GITHUB_ACTIONS") else f"{p}: {msg}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["clusters"]))
