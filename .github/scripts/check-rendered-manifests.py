#!/usr/bin/env python3
"""Check the objects Flux applies, as rendered, not the files as written (GHA-03).

manifest_fence.py holds clusters/ to a text form the line-based checks can read. This check does not depend on that
form: it renders what kustomize-controller renders and checks the objects that come out, where a YAML tag, flow
style, an escape, a List, a generator, an image override or a patch has already been resolved.

Render (offline; nothing is decrypted, so SOPS Secrets stay encrypted, and that is what is checked):
- what: from each <root>/<cluster>/flux-system/ (the bootstrap entry), every Flux Kustomization's spec.path, then the
  paths of the Kustomizations each render holds in turn; the source must be the repo's own GitRepository;
- each render from a fresh copy of the root with regular files only (a source artifact holds no symlinks);
- a directory without a kustomization file gets the one kustomize-controller generates (fluxcd/pkg/kustomize,
  scanManifests): every *.yaml and *.yml file below it, and each subdirectory that holds a kustomization file as one
  entry that is not descended into;
- `kustomize build --load-restrictor LoadRestrictionsNone`, plugins and helm off, as the controller builds; a wrapper
  kustomization only adds origin annotations, which name each object's file;
- no network: proxies point nowhere and git may use no transport, so a remote base or resource fails the render;
  kustomization files are read for remote entries first, for a clear message;
- a Kustomization spec field that changes the render (images, patches, postBuild, targetNamespace, ...) fails: the
  check does not emulate it.

On every object Flux applies: each rendered document, or, for a document with an `items` list, those items in its
place (ssautil.ReadObjects, whatever the kind; a kind other than *List that carries one fails):
- a Secret carries sops metadata with a MAC, and every data and stringData value is an ENC[...] value or empty;
- every image of a container, init container or ephemeral container (wherever a pod spec sits) and of a Plan's
  prepare and upgrade steps is a valid reference from REGISTRIES, pinned by @sha256; in an app namespace it comes
  from that app's GHCR path;
- no container env value holds a URL with a literal password.
Tag-only exceptions, as (file under the root, ref): TAG_ONLY in tests/test_ci_fences.py (read, not run) and
AWAITING_DIGEST below.

kustomize: $KUSTOMIZE (a path to the binary), else `kustomize` on PATH, else `kubectl kustomize`. CI pins the release
Flux's kustomize-controller embeds (ci.yml). Otherwise stdlib: kustomize's output is read by the reader below, which
takes only the form kustomize writes and fails on anything else. As a script: check-rendered-manifests.py [root...]
(default clusters) prints one line per problem and exits 1 if there is any.
"""
import ast
import itertools
import json
import os
import pathlib
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import manifest_fence  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
# The registries this cluster pulls from; Docker Hub also serves the short names (postgres, rancher/...). A new one is
# a reviewed change to this code-owned file.
REGISTRIES = ("ghcr.io", "docker.io", "mirror.gcr.io")
# K8S-02: refs that the open digest pins for the Flux controllers and the k3s upgrade Plans replace. Delete each entry
# as they land (tests/test_render_check.py fails while one is no longer rendered); never add one.
AWAITING_DIGEST = {
    ("hetzner/flux-system/gotk-components.yaml", "ghcr.io/fluxcd/source-controller:v1.9.6"),
    ("hetzner/flux-system/gotk-components.yaml", "ghcr.io/fluxcd/kustomize-controller:v1.9.6"),
    ("hetzner/system-upgrade/plans.yaml", "rancher/k3s-upgrade"),
}
# Flux Kustomization spec fields that leave the objects as kustomize builds them.
FLUX_SPEC = {"interval", "retryInterval", "timeout", "path", "prune", "sourceRef", "decryption", "dependsOn", "wait",
             "healthChecks", "healthCheckExprs", "force", "suspend", "serviceAccountName", "deletionPolicy",
             "kubeConfig", "commonMetadata"}
POD_LISTS = ("containers", "initContainers", "ephemeralContainers")
COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
DOMAIN = r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?::[0-9]+)?|localhost(?::[0-9]+)?"
IMAGE = re.compile(rf"(?:(?P<registry>{DOMAIN})/)?(?P<path>{COMPONENT}(?:/{COMPONENT})*)"
                   r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?")
# Kustomization entries that name something to fetch: kustomize's remote forms (as Flux lists them) or any URL.
REMOTE = ("git::", "gh:", "ssh://", "https://", "http://", "git@", "github.com:", "github.com/", "file://")
PATH_KEYS = {"resources", "bases", "components", "crds", "configurations", "generators", "transformers", "validators",
             "openapi", "patches", "patchesStrategicMerge", "patchesJson6902", "configMapGenerator", "secretGenerator",
             "helmCharts", "helmChartInflationGenerator"}
NOWHERE = "http://127.0.0.1:9"


class RenderError(Exception):
    """The render failed, or its output is not in the form this reader takes: fail closed."""


# ---- Reader for kustomize's output ----------------------------------------------------------------------------------
# kustomize writes every object through JSON and go-yaml: block mappings with sorted keys, indentless sequences, `{}`
# and `[]` only when empty, plain, quoted and literal scalars (long ones folded over lines), and no tags, anchors or
# comments. That is all this reader takes.

ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
           "e": "\x1b", " ": " ", '"': '"', "\\": "\\", "N": "\x85", "_": "\xa0", "L": " ", "P": " "}
HEX = {"x": 2, "u": 4, "U": 8}


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _plain_ok(s, first=True):
    if not s or s != s.strip(" ") or "\t" in s or ": " in s or " #" in s or s.endswith(":") or s.startswith("#"):
        return False
    return not first or (s[0] not in ",[]{}&*!|>'\"%@`" and not re.match(r"[-?:](?: |$)", s))


class _Reader:
    def __init__(self, lines, first):
        self.lines, self.i, self.first = lines, 0, first

    def fail(self, msg):
        raise RenderError(f"rendered line {self.first + self.i}: {msg}")

    def peek(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def node(self, col):
        text = self.lines[self.i][col:]
        return self.seq(col) if text == "-" or text.startswith("- ") else self.map(col)

    def _line(self, col):
        """The text of the next line at exactly col; None at a line further left or the end."""
        line = self.peek()
        if line is None:
            return None
        if not line.strip(" "):
            self.fail("blank line outside a scalar")
        if line[_indent(line)] == "\t":
            self.fail("tab in indentation")
        if _indent(line) < col:
            return None
        if _indent(line) > col:
            self.fail("unexpected indentation")
        return line[col:]

    def map(self, col):
        out = {}
        while True:
            text = self._line(col)
            if text is None:
                return out
            if text == "-" or text.startswith("- "):
                self.fail("sequence entry where a key belongs")
            key, rest = self.key(text)
            if key in out:
                self.fail(f"duplicate key {key!r}")
            out[key] = self.value(col, rest, True)

    def seq(self, col):
        out = []
        while True:
            text = self._line(col)
            if text is None or not (text == "-" or text.startswith("- ")):
                return out  # an indentless sequence ends at its parent's next key
            rest = text[2:]
            if rest.startswith(" "):
                self.fail("more than one space after `-`")
            if rest and (rest == "-" or rest.startswith("- ") or self.is_key(rest)):
                self.lines[self.i] = " " * (col + 2) + rest  # `- k: v` is the mapping `k: v` at col + 2
                out.append(self.node(col + 2))
            else:
                out.append(self.value(col, rest, False))

    def is_key(self, text):
        if text[0] in "\"'":
            try:
                _, tail = self.quoted(text, None)
            except RenderError:
                return False  # a quoted scalar that goes on to the next line
            return tail == ":" or tail.startswith(": ")
        return ": " in text or text.endswith(":")

    def key(self, text):
        if text[0] == "?" and text[1:2] in ("", " "):
            self.fail("complex key")
        if text[0] in "\"'":
            key, tail = self.quoted(text, None)
        else:
            end = text.find(": ")
            end = end if end >= 0 else (len(text) - 1 if text.endswith(":") else -1)
            if end < 0 or not _plain_ok(text[:end]):
                self.fail(f"not a key: {text!r}")
            key, tail = text[:end], text[end:]
        if tail != ":" and not tail.startswith(": "):
            self.fail(f"not a key: {text!r}")
        return key, tail[2:]

    def value(self, col, rest, is_key):
        """The node after `key: ` (is_key) or `- ` on the current line; col is the key's or the dash's column."""
        if not rest:
            self.i += 1
            line = self.peek()
            if line is not None and line.strip(" ") and _indent(line) > col:
                return self.node(_indent(line))
            dash = line is not None and _indent(line) == col and (line[col:] == "-" or line[col:col + 2] == "- ")
            if is_key and dash:
                return self.seq(col)
            self.fail("no value")
        if rest in ("{}", "[]"):
            self.i += 1
            return {} if rest == "{}" else []
        c = rest[0]
        if c in "|>":
            return self.literal(rest, col)
        if c in "\"'":
            text, tail = self.quoted(rest, col)
            if tail:
                self.fail("text after a quoted scalar")
            self.i += 1
            return text
        if c in "[{&*!%@`":
            self.fail(f"unexpected {c!r}")
        return self.plain(rest, col)

    def _next(self, col):
        """Move to the next line of a scalar that goes on: (empty lines skipped, its text without indentation)."""
        empties = 0
        self.i += 1
        while self.peek() == "":
            empties += 1
            self.i += 1
        line = self.peek()
        if line is None or not line.strip(" ") or _indent(line) <= col:
            self.fail("scalar runs past its block")
        return empties, line.strip(" ")

    def plain(self, first, col):
        if not _plain_ok(first):
            self.fail(f"not a plain scalar: {first!r}")
        parts = [first]
        self.i += 1
        while True:
            j = self.i
            while j < len(self.lines) and self.lines[j] == "":
                j += 1
            if j == len(self.lines) or not self.lines[j].strip(" ") or _indent(self.lines[j]) <= col:
                break
            text = self.lines[j].strip(" ")
            if not _plain_ok(text, first=False):
                self.i = j
                self.fail(f"not a plain continuation: {text!r}")
            parts += ["\n" * (j - self.i) or " ", text]  # a line break folds to a space, an empty line to \n
            self.i = j + 1
        if len(parts) > 1:
            return "".join(parts)
        return None if first in ("null", "~") else {"true": True, "false": False}.get(first, first)

    def quoted(self, text, col):
        """(content, text after the closing quote) for the quoted scalar opening text, which starts on the current
        line; it may go on over the next lines unless col is None. Leaves the reader on its last line."""
        q, k, out, ws = text[0], 1, [], []
        while True:
            escaped_break = False
            while k < len(text):
                c = text[k]
                if c == q:
                    if q == "'" and text[k + 1:k + 2] == "'":
                        out += ws + ["'"]
                        ws, k = [], k + 2
                        continue
                    return "".join(out + ws), text[k + 1:]
                if q == '"' and c == "\\":
                    out += ws
                    ws, e = [], text[k + 1:k + 2]
                    if not e:
                        escaped_break = True
                        break
                    if e in HEX:
                        h = text[k + 2:k + 2 + HEX[e]]
                        if not re.fullmatch(r"[0-9A-Fa-f]{%d}" % HEX[e], h):
                            self.fail("bad escape")
                        out.append(chr(int(h, 16)))
                        k += 2 + HEX[e]
                        continue
                    if e not in ESCAPES:
                        self.fail(f"unknown escape \\{e}")
                    out.append(ESCAPES[e])
                    k += 2
                    continue
                if c in " \t":
                    ws.append(c)
                else:
                    out += ws + [c]
                    ws = []
                k += 1
            if col is None:
                self.fail("quoted scalar runs past its line")
            # Trailing blanks drop; a line break folds to a space, an empty line to \n, an escaped break to nothing.
            empties, text = self._next(col)
            out.append("\n" * empties if escaped_break else "\n" * empties or " ")
            ws, k = [], 0

    def literal(self, header, col):
        m = re.fullmatch(r"\|(?:([1-9])?([-+])?|([-+])([1-9]))", header)
        if not m:
            self.fail(f"block scalar header {header!r}")
        indicator, chomp = m.group(1) or m.group(4), m.group(2) or m.group(3) or ""
        content = col + int(indicator) if indicator else None
        lines = []
        self.i += 1
        while self.peek() is not None:
            line = self.peek()
            if not line.strip(" "):
                if line:
                    self.fail("whitespace-only line in a block scalar")
                lines.append("")
            else:
                if content is None and _indent(line) > col:
                    content = _indent(line)
                if content is None or _indent(line) < content:
                    break
                lines.append(line[content:])
            self.i += 1
        end = max((n + 1 for n, s in enumerate(lines) if s), default=0)
        body, trailing = lines[:end], len(lines) - end
        if not body:
            return "\n" * trailing if chomp == "+" else ""
        return "\n".join(body) + {"-": "", "": "\n", "+": "\n" * (trailing + 1)}[chomp]


def parse(text):
    """The documents of kustomize's output, as Python values."""
    m = re.search("[\r\x85  ]", text)
    if m:  # go-yaml reads these as line breaks; the lines below are split at \n only
        raise RenderError(f"rendered line {text.count(chr(10), 0, m.start()) + 1}: line break U+{ord(m.group()):04X}")
    docs, start, lines = [], 1, text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    chunk = []
    for n, line in enumerate(lines + ["---"], 1):
        if line != "---":
            chunk.append(line)
            continue
        if chunk:
            r = _Reader(chunk, start)
            if not chunk[0].strip(" ") or _indent(chunk[0]):
                r.fail("a document starts at column 0")
            doc = r.node(0)
            if r.i != len(chunk):
                r.fail("text after the document")
            docs.append(doc)
        chunk, start = [], n + 1
    return docs


def has_items(obj):
    """Unstructured.IsList: a top-level `items` list, whatever the kind."""
    return isinstance(obj, dict) and isinstance(obj.get("items"), list)


def objects(doc):
    """What Flux applies for one rendered document: its items, one level down and as written, or the document."""
    return list(doc["items"]) if has_items(doc) else [doc]


# ---- Render ----------------------------------------------------------------------------------------------------------

def kustomize_command():
    if os.environ.get("KUSTOMIZE"):
        return [os.environ["KUSTOMIZE"], "build"]
    for cmd in (["kustomize", "build"], ["kubectl", "kustomize"]):
        if shutil.which(cmd[0]):
            return [shutil.which(cmd[0])] + cmd[1:]
    return None


def _copy(root, dest):
    """root's directories and regular files under dest; symlinks are left out."""
    for d, dirs, files in os.walk(root):
        rel = pathlib.Path(d).relative_to(root)
        dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            src = pathlib.Path(d, f)
            if not src.is_symlink() and src.is_file():
                shutil.copyfile(src, dest / rel / f)


def has_kustomization(d):
    return any((d / k).is_file() for k in manifest_fence.KUSTOMIZATIONS)


def _json(value):
    return json.dumps(value, indent=1) + "\n"  # JSON is YAML, with no quoting rules to get wrong


def generate(d):
    """Write the kustomization.yaml that kustomize-controller generates for a directory without one."""
    found = []

    def walk(p):
        for c in sorted(p.iterdir(), key=lambda x: x.name):
            if c.is_dir():
                if has_kustomization(c):
                    found.append(c)
                else:
                    walk(c)
            elif c.suffix in (".yaml", ".yml"):  # Go's filepath.Ext: case-sensitive, and not .json
                found.append(c)

    walk(d)
    kus = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization"}
    if found:
        kus["resources"] = ["./" + c.relative_to(d).as_posix() for c in found]
    else:
        kus["namespace"] = "_placeholder"
    (d / "kustomization.yaml").write_text(_json(kus))


def _remote(v):
    return not re.search(r"\s", v) and ("://" in v or v.lower().startswith(REMOTE))


def remote_entries(root):
    """Problems for kustomization entries under root that name something to fetch."""
    out = []
    for p in sorted(root.rglob("*")):
        if p.name not in manifest_fence.KUSTOMIZATIONS or p.is_symlink() or not p.is_file():
            continue
        _, docs = manifest_fence.scan(p.read_bytes().decode("utf-8", "replace"))
        out += [f"{p.relative_to(root.parent).as_posix()}: line {e.line}: remote {e.path[0]} entry {e.text}: commit "
                "what it names instead" for d in docs for e in d.entries
                if e.path and e.path[0] in PATH_KEYS and e.style in manifest_fence.SCALAR and _remote(e.text)]
    return out


def render(root, rel, base):
    """[(origin file under root, object)] that kustomize builds from root/rel the way Flux builds it, in base."""
    copy = base / root.name
    _copy(root, copy)
    d = copy / rel
    if not has_kustomization(d):
        generate(d)
    wrapper = base / "render"
    wrapper.mkdir()
    (wrapper / "kustomization.yaml").write_text(_json({
        "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
        "resources": [os.path.relpath(d, wrapper)], "buildMetadata": ["originAnnotations"]}))
    cmd = kustomize_command()
    if not cmd:
        raise RenderError("no kustomize: set KUSTOMIZE, or put kustomize or kubectl on PATH")
    home = base / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TMPDIR": str(home),
           "KUBECONFIG": os.devnull, "GIT_ALLOW_PROTOCOL": "none", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "NO_PROXY": "", "no_proxy": ""}
    env.update(dict.fromkeys(("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"), NOWHERE))
    where = posixpath.join(root.name, rel.as_posix()).rstrip("/.")
    try:
        r = subprocess.run(cmd + ["--load-restrictor", "LoadRestrictionsNone", str(wrapper)], env=env,
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RenderError(f"{where}: kustomize build did not run: {e}") from e
    if r.returncode:
        err = r.stderr.strip().replace(os.path.realpath(base) + os.sep, "").replace(str(base) + os.sep, "")
        raise RenderError(f"{where}: kustomize build failed: {err}")
    out = []
    for doc in parse(r.stdout):
        meta = doc.get("metadata") if isinstance(doc, dict) and isinstance(doc.get("metadata"), dict) else {}
        annotations = meta.get("annotations") if isinstance(meta.get("annotations"), dict) else {}
        origin = annotations.get("config.kubernetes.io/origin")
        m = re.search(r"^(?:path|configuredIn): (.+)$", origin if isinstance(origin, str) else "", re.M)
        f = posixpath.normpath(posixpath.join("render", m.group(1))) if m else ""
        f = f[len(root.name) + 1:] if f.startswith(root.name + "/") else None
        if has_items(doc) and not str(doc.get("kind")).endswith("List"):
            out.append((f, doc))  # refused in object_problems
        out += [(f, obj) for obj in objects(doc)]  # items take the origin kustomize gave their document
    return out


def flux_kustomizations(rendered):
    return [o for _, o in rendered if isinstance(o, dict) and o.get("kind") == "Kustomization"
            and str(o.get("apiVersion", "")).startswith("kustomize.toolkit.fluxcd.io/")]


def flux_path(ks, root):
    """(directory relative to root, problem) that a Flux Kustomization applies."""
    meta = ks.get("metadata") if isinstance(ks.get("metadata"), dict) else {}
    spec = ks.get("spec") if isinstance(ks.get("spec"), dict) else {}
    name = f"Kustomization {meta.get('namespace')}/{meta.get('name')}"
    extra = sorted(set(spec) - FLUX_SPEC)
    if extra:
        return None, f"{name}: spec {', '.join(extra)} changes what Flux renders, and this check does not emulate it"
    ref = spec.get("sourceRef") if isinstance(spec.get("sourceRef"), dict) else {}
    if (ref.get("kind"), ref.get("name"), ref.get("namespace", meta.get("namespace"))) != (
            "GitRepository", "flux-system", "flux-system"):
        return None, f"{name}: its source is not this repository (GitRepository flux-system/flux-system)"
    parts = pathlib.PurePosixPath(posixpath.normpath(str(spec.get("path") or "."))).parts
    rel = pathlib.PurePosixPath(*parts[1:]) if parts[:1] == (root.name,) else None
    if rel is None or not (root / rel).is_dir():
        return None, f"{name}: path {spec.get('path')} is not a directory under {root.name}/"
    return rel, None


def tag_only(path=REPO / "tests" / "test_ci_fences.py"):
    """TAG_ONLY in tests/test_ci_fences.py (K8S-03), read without running the file; empty once it is deleted."""
    try:
        tree = ast.parse(path.read_text())
    except OSError:
        return set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "TAG_ONLY" for t in node.targets):
            return set(ast.literal_eval(node.value))
    return set()


# ---- Checks ----------------------------------------------------------------------------------------------------------

def _name(obj):
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    return f"{obj.get('kind')} {meta['namespace'] + '/' if meta.get('namespace') else ''}{meta.get('name')}"


def secret_problems(obj):
    sops = obj.get("sops")
    mac = sops.get("mac") if isinstance(sops, dict) else None
    if not (isinstance(mac, str) and manifest_fence.SOPS_VALUE.fullmatch(mac)):
        return ["no sops metadata with a MAC: keep it in a *.secret.yaml file and run `sops encrypt -i`"]
    out = []
    for field in ("data", "stringData"):
        values = obj.get(field)
        if values is None:
            continue
        if not isinstance(values, dict):
            out.append(f"{field} is not a mapping")
            continue
        out += [f"{field}.{k} is not encrypted" for k, v in values.items()
                if v not in (None, "") and not (isinstance(v, str) and manifest_fence.SOPS_VALUE.fullmatch(v))]
    return out


def images(obj):
    """[(where, image, container or None)]: every pod-spec container in obj, and a Plan's prepare and upgrade."""
    out = []

    def walk(node, where):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in POD_LISTS and isinstance(v, list):
                    out.extend((f"{where}{k}[{i}]", c.get("image"), c) for i, c in enumerate(v) if isinstance(c, dict))
                walk(v, f"{where}{k}.")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where[:-1]}[{i}].")

    walk(obj, "")
    spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
    if obj.get("kind") == "Plan" and str(obj.get("apiVersion", "")).startswith("upgrade.cattle.io/"):
        out += [(f"spec.{s}", spec[s].get("image"), None) for s in ("prepare", "upgrade")
                if isinstance(spec.get(s), dict) and "image" in spec[s]]
    return out


def image_problems(origin, obj, exempt):
    out = []
    meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    ns = meta.get("namespace") if isinstance(meta.get("namespace"), str) else ""
    app = f"{manifest_fence.ORG}{ns.rsplit('-', 1)[0]}/" if manifest_fence.APP_NS.fullmatch(ns) else None
    for where, ref, container in images(obj):
        m = IMAGE.fullmatch(ref) if isinstance(ref, str) else None
        if not m:
            out.append(f"{where}: {ref!r} is not an image reference")
            continue
        if (m.group("registry") or "docker.io") not in REGISTRIES:
            out.append(f"{where}: {ref} is not from {', '.join(REGISTRIES)}")
        if not m.group("digest") and (origin, ref) not in exempt:
            out.append(f"{where}: {ref} is not pinned by @sha256")
        if app and not ref.startswith(app):
            out.append(f"{where}: {ref} in {ns} is not from {app}")
        env = (container or {}).get("env")
        for e in env if isinstance(env, list) else []:
            v = e.get("value") if isinstance(e, dict) else None
            if isinstance(v, str) and any(not manifest_fence.PLACEHOLDER.fullmatch(c.group(1))
                                          for c in manifest_fence.CREDENTIAL.finditer(v)):
                out.append(f"{where}: env {e.get('name')} holds a password in a URL: keep it in a SOPS Secret")
    return out


def object_problems(origin, obj, exempt):
    if not isinstance(obj, dict):
        return [f"{origin}: a rendered document is not a mapping"]
    if has_items(obj):
        return [f"{origin or '(no origin)'}: {_name(obj)}: a top-level items list, which Flux applies in place of the "
                "object: write each item as its own document"]
    out = []
    if isinstance(obj.get("kind"), str) and obj["kind"].lower() == "secret":
        out += secret_problems(obj)
    out += image_problems(origin, obj, exempt)
    return [f"{origin or '(no origin)'}: {_name(obj)}: {p}" for p in out]


def check(root, exempt=None):
    """[problem] for the objects Flux applies from root, a manifest root such as clusters/."""
    root = pathlib.Path(root).resolve()
    exempt = (tag_only() | AWAITING_DIGEST) if exempt is None else exempt
    if not root.is_dir():
        return [f"{root}: no such directory"]
    out = remote_entries(root)
    if out:
        return out
    entries = sorted(d.relative_to(root) / "flux-system" for d in root.iterdir() if (d / "flux-system").is_dir())
    if not entries:
        return [f"{root.name}/: no <cluster>/flux-system/ directory, so nothing says what Flux applies"]
    with tempfile.TemporaryDirectory() as t:
        bases = (pathlib.Path(t, str(n)) for n in itertools.count())
        queue, seen = [], set()
        try:
            for e in entries:
                found = flux_kustomizations(render(root, e, next(bases)))
                if not found:
                    out.append(f"{root.name}/{e.as_posix()}: no Flux Kustomization")
                queue += found
            while queue:
                rel, why = flux_path(queue.pop(0), root)
                if why and why not in out:
                    out.append(why)
                if rel is None or rel in seen:
                    continue
                seen.add(rel)
                rendered = render(root, rel, next(bases))
                queue += flux_kustomizations(rendered)
                for origin, obj in rendered:
                    out += [p for p in object_problems(origin, obj, exempt) if p not in out]
        except RenderError as e:
            out.append(str(e))
    return out


def main(roots):
    bad = [p for root in roots for p in check(root)]
    for p in bad:
        print(f"::error::{p}" if os.environ.get("GITHUB_ACTIONS") else p, file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["clusters"]))
