"""Fences on the Terraform configuration: every committed .tf file, under terraform/ or anywhere else.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`.

HCP Terraform runs a speculative plan of every PR with the workspace's own credentials, and a plan configures the
providers, reads every data source and evaluates every function call, so HCL in a PR runs before any review. These
tests keep the stacks to the providers, provider arguments, block types and local files they use today.

Scope: a PR can change this file as easily as the HCL it checks, so these fences catch mistakes and drift, not a
determined author. Who and what may trigger a plan, and with which credentials, is set on the workspaces
(configuration-map.md, HCP Terraform).

The HCL is read as text. `lex` blanks comments and leaves strings, templates and heredocs alone, so a `#` or `/*`
inside a string cannot hide the lines after it. Anything the scan cannot read as a plain value fails.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TERRAFORM = ROOT / "terraform"
FENCE = ROOT / ".github/scripts/check-tbd-backups-fences.py"

# Every provider source the stacks use today. Adding one is a reviewed change to this list.
PROVIDER_ALLOWLIST = {
    "hashicorp/aws",
    "cloudflare/cloudflare",
    "hashicorp/tfe",
    "hetznercloud/hcloud",
    "netbirdio/netbird",
}
# What a provider block may set, from today's blocks (plus `alias`). Anything else a provider block can carry (an
# endpoint, proxy, CA bundle, credentials file, profile, assume_role, a `dynamic` block) changes where the provider
# sends its credentials or which credentials it uses.
PROVIDER_ARGS = {
    "hashicorp/aws": {"region", "default_tags"},
    "hashicorp/tfe": {"organization"},
    "cloudflare/cloudflare": set(),
    "hetznercloud/hcloud": set(),
    "netbirdio/netbird": set(),
}

HEREDOC = re.compile(r"<<-?([A-Za-z_][A-Za-z0-9_-]*)[ \t]*\r?\n")
LABEL = r'(?:"[^"\n]*"|[A-Za-z_][A-Za-z0-9_-]*)'
# HCL accepts a block label quoted or bare (`data external x {`), so both count.
BLOCK = re.compile(rf'(?<![\w.\-"])(resource|data|ephemeral|action|provider|module|provisioner)'
                   rf'((?:[ \t]+{LABEL})+)[ \t]*\{{')
PLAIN = r'"[^"\\$%\n]*"'  # a string literal with no escape, interpolation or directive
# Functions that read a local file at plan time; each takes a literal "${path.module}/..." path inside the repo.
FILE_FUNCS = re.compile(r"(?<![\w.:-])(templatefile|file[a-z0-9]*)[ \t]*\(")


def lex(src):
    """(src with every comment blanked, newlines kept; mask, True where a character is code, not string or comment).

    Understands nested template interpolation (`"${f("x")}"`) and heredocs."""
    out, code = [], []
    modes, depth = ["code"], [0]
    i, n = 0, len(src)

    def emit(s, is_code):
        out.append(s)
        code.extend([is_code] * len(s))

    while i < n:
        c = src[i]
        if modes[-1] == "str":
            if c == "\\":
                emit(src[i:i + 2], False)
                i += 2
            elif src.startswith(("$${", "%%{"), i):
                emit(src[i:i + 3], False)
                i += 3
            elif src.startswith(("${", "%{"), i):
                modes.append("code")
                depth.append(0)
                emit(src[i:i + 2], False)
                i += 2
            else:
                if c == '"':
                    modes.pop()
                emit(c, False)
                i += 1
            continue
        heredoc = HEREDOC.match(src, i)
        if c == "#" or src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            emit(" " * (j - i), False)
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            emit(re.sub(r"[^\n]", " ", src[i:j]), False)
            i = j
        elif c == '"':
            modes.append("str")
            emit(c, False)
            i += 1
        elif heredoc:
            end = re.compile(rf"(?m)^[ \t]*{re.escape(heredoc.group(1))}[ \t]*$").search(src, heredoc.end())
            j = n if end is None else end.end()
            emit(src[i:j], False)
            i = j
        elif c == "}" and len(modes) > 1 and depth[-1] == 0:
            # Closes a template interpolation: back into the string around it.
            modes.pop()
            depth.pop()
            emit(c, False)
            i += 1
        else:
            depth[-1] += {"{": 1, "}": -1}.get(c, 0)
            emit(c, True)
            i += 1
    return "".join(out), code


def close(text, code, start):
    """Index of the brace that closes the one just before `start`, counting code braces only."""
    depth = 1
    for i in range(start, len(text)):
        if code[i] and text[i] in "{}":
            depth += 1 if text[i] == "{" else -1
            if depth == 0:
                return i
    return len(text)


def items(text, code, start, end):
    """(name, rest) of each top-level attribute or nested block in the body text[start:end]; name is None when an
    item does not start with an identifier. Items end at a code newline outside any bracket."""
    out, depth, begin = [], 0, start
    for i in range(start, end + 1):
        last = i == end
        if not last and code[i] and text[i] in "{[(":
            depth += 1
        elif not last and code[i] and text[i] in "}])":
            depth -= 1
        if (last or (text[i] == "\n" and code[i])) and depth == 0:
            item = text[begin:i].strip()
            if item:
                m = re.match(r"([A-Za-z_][A-Za-z0-9_-]*)(.*)", item, re.DOTALL)
                out.append((m.group(1), m.group(2).strip()) if m else (None, item))
            begin = i + 1
    return out


def blocks(text, code):
    """(kind, labels, body start, body end) of every block header in the comment-stripped text, nested included."""
    for m in BLOCK.finditer(text):
        labels = [lb.strip('"') for lb in re.findall(LABEL, m.group(2))]
        yield m.group(1), labels, m.end(), close(text, code, m.end())


def normalize(source):
    s = source.lower()
    s = s[len("registry.terraform.io/"):] if s.startswith("registry.terraform.io/") else s
    return s if "/" in s else f"hashicorp/{s}"


def required_providers(path, text, code, problems):
    """[(local name, normalized source)] declared in one file. Each entry must be a plain version string or a plain
    { source = "...", version = "..." } object literal; anything else is a problem."""
    out = []
    for m in re.finditer(r"(?<![\w.-])required_providers[ \t]*\{", text):
        for name, rest in items(text, code, m.end(), close(text, code, m.end())):
            value = rest[1:].strip() if name and rest.startswith("=") else ""
            fields = re.fullmatch(rf"\{{\s*(?:(?:source|version)\s*=\s*{PLAIN}\s*,?\s*)*\}}", value)
            sources = re.findall(r'source\s*=\s*"([^"]*)"', value) if fields else []
            if re.fullmatch(PLAIN, value) or (fields and not sources):
                out.append((name, f"hashicorp/{name}"))  # no source: Terraform installs hashicorp/<name>
            elif fields and len(sources) == 1 and re.fullmatch(r"[A-Za-z0-9./_-]+", sources[0]):
                out.append((name, normalize(sources[0])))
            else:
                problems.append(f"{path}: required_providers {name or rest!r} is not a plain version string or "
                                '{ source = "...", version = "..." } object literal')
    return out


def file_reads(path, text, root, problems):
    """Every file function reads a literal "${path.module}/..." path that resolves inside the repo. Matched in strings
    too (a heredoc is a template, and a call in it runs), so prose that reads like a call fails safe."""
    if re.search(r"(?<![\w.:-])templatestring[ \t]*\(", text):
        problems.append(f"{path}: templatestring(): render templates from files with templatefile()")
    for m in FILE_FUNCS.finditer(text):
        func, arg = m.group(1), re.match(r'\s*"\$\{path\.module\}/([^"\\$%\n]+)"', text[m.end():])
        target = arg and (root / pathlib.PurePosixPath(path).parent / arg.group(1)).resolve()
        rel = target and target.is_relative_to(root.resolve()) and target.relative_to(root.resolve())
        if not rel or {".git", ".terraform"} & set(rel.parts):
            problems.append(f'{path}: {func}() must read a literal "${{path.module}}/..." path inside the repository')
        elif func == "templatefile" and target.is_file():
            if FILE_FUNCS.search(target.read_text()):
                problems.append(f"{path}: templatefile() target {rel.as_posix()} calls a file function itself")


def scan(files, root=ROOT):
    """Every plan-time fence violation in {path relative to the repo: text} for .tf files anywhere in the repo."""
    problems, by_dir = [], {}
    for path, src in sorted(files.items()):
        name = pathlib.PurePosixPath(path).name
        if name.endswith(("override.tf", "override.tf.json")):
            problems.append(f"{path}: override file; overrides are local only (gitignored), never committed")
        if path.endswith(".tf.json"):
            problems.append(f"{path}: JSON-syntax configuration; write it as .tf so these fences can read it")
            continue
        text, code = lex(src)
        d = by_dir.setdefault(str(pathlib.PurePosixPath(path).parent), {"providers": [], "uses": [], "blocks": []})
        d["providers"] += required_providers(path, text, code, problems)
        file_reads(path, text, root, problems)
        for kind, labels, start, end in blocks(text, code):
            first = labels[0] if labels else ""
            if kind == "data" and first in ("external", "http"):
                problems.append(f'{path}: data "{first}": no external or http data sources (they run at plan time)')
            if kind == "data" and first == "terraform_remote_state":
                problems.append(f'{path}: data "terraform_remote_state": no remote state reads; pass values in '
                                "through variables")
            if kind == "provisioner":
                problems.append(f'{path}: provisioner "{first}": no provisioners (local-exec and remote-exec run '
                                "commands with the run's environment)")
            if kind == "module":
                src_ = [re.fullmatch(rf"=\s*({PLAIN})", rest) for n, rest in items(text, code, start, end)
                        if n == "source"]
                source = src_[0].group(1)[1:-1] if len(src_) == 1 and src_[0] else ""
                target = (root / pathlib.PurePosixPath(path).parent / source).resolve()
                if (not source.startswith(("./", "../")) or ".terraform" in pathlib.PurePosixPath(source).parts
                        or not target.is_relative_to((root / "terraform").resolve())):
                    problems.append(f'{path}: module "{first}": modules come from a plain local path under '
                                    "terraform/ (a remote module brings code these fences never read)")
            if kind == "provider":
                d["uses"].append((first, f'provider "{first}"'))
                d["blocks"].append((path, first, [n for n, _ in items(text, code, start, end)]))
            if kind in ("resource", "data", "ephemeral", "action") and first:
                prefix = first.split("_", 1)[0]
                if prefix == "terraform":
                    if first not in ("terraform_data", "terraform_remote_state"):
                        problems.append(f'{path}: {kind} "{first}": unknown built-in type')
                else:
                    d["uses"].append((prefix, f'{kind} "{first}"'))
        for m in re.finditer(r"(?<![\w.-])provider[ \t]*=[ \t]*([A-Za-z_][A-Za-z0-9_-]*)", text):
            d["uses"].append((m.group(1), f"provider = {m.group(1)}"))
        for m in re.finditer(r"provider::([A-Za-z_][A-Za-z0-9_-]*)::", text):
            d["uses"].append((m.group(1), f"provider::{m.group(1)}::"))
    for directory, d in sorted(by_dir.items()):
        declared = {}
        for name, source in d["providers"]:
            declared.setdefault(name, set()).add(source)
            if source not in PROVIDER_ALLOWLIST:
                problems.append(f"{directory}: required_providers {name} = {source!r} is not in the allowlist")
        for name, sources in sorted(declared.items()):
            if len(sources) > 1:
                problems.append(f"{directory}: provider {name!r} is declared more than once with different sources "
                                f"{sorted(sources)}")
        resolve = {name: min(sources) for name, sources in declared.items()}
        for name, what in d["uses"]:
            source = resolve.get(name, f"hashicorp/{name}")
            if source not in PROVIDER_ALLOWLIST:
                problems.append(f"{directory}: {what} uses provider {name!r} ({source}), not in the allowlist")
        for path, name, args in d["blocks"]:
            allowed = PROVIDER_ARGS.get(resolve.get(name, f"hashicorp/{name}"), set()) | {"alias"}
            for arg in args:
                if arg not in allowed:
                    problems.append(f'{path}: provider "{name}" sets {arg}; a provider block sets only '
                                    f"{sorted(allowed)} (PROVIDER_ARGS)")
    return problems


def tree():
    """{path: text} of every committed .tf and .tf.json file in the repository."""
    try:
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "*.tf", "*.tf.json"],
                                capture_output=True, text=True, check=True).stdout.split("\0")
    except (OSError, subprocess.CalledProcessError):  # not a git checkout: every such file on disk
        listed = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.tf*")
                  if not {".git", ".terraform"} & set(p.parts) and p.name.endswith((".tf", ".tf.json"))]
    return {p: (ROOT / p).read_text() for p in sorted(listed) if p and (ROOT / p).is_file()}


def stack(main, versions=None, path="terraform/x"):
    """A fixture stack: the netbird stack's versions.tf plus the given main.tf."""
    if versions is None:
        versions = (TERRAFORM / "netbird/versions.tf").read_text()
    return {f"{path}/versions.tf": versions, f"{path}/main.tf": main}


AWS = 'terraform {\n  required_providers {\n    aws = {\n      source = "hashicorp/aws"\n    }\n  }\n}\n'


class PlanTimeCode(unittest.TestCase):
    def assertFlags(self, files, needle, root=ROOT):
        problems = scan(files, root)
        self.assertTrue(any(needle in p for p in problems), f"expected {needle!r} in {problems}")

    def test_repo_stacks_pass(self):
        # guard: today's tree is clean, and the scan does see it.
        files = tree()
        self.assertGreaterEqual(len({pathlib.PurePosixPath(p).parts[1] for p in files if p.startswith("terraform/")}),
                                5)
        headers = sum(len(re.findall(r'(?m)^(?:resource|data|provider) "', s)) for s in files.values())
        seen = sum(k in ("resource", "data", "provider") for s in files.values() for k, *_ in blocks(*lex(s)))
        self.assertEqual(seen, headers)
        reads = sum(len(FILE_FUNCS.findall(s)) for s in files.values())
        self.assertGreaterEqual(reads, 8)  # tbd-backups' policy documents and templates, cloudflare-tokens' JSON
        self.assertEqual(scan(files), [])

    def test_allowlist_is_what_the_stacks_declare(self):
        # guard: the allowlist names no provider the tree has stopped using.
        declared = set()
        for path, src in tree().items():
            text, code = lex(src)
            declared |= {source for _, source in required_providers(path, text, code, [])}
        self.assertEqual(declared, PROVIDER_ALLOWLIST)

    def test_external_and_http_data_sources_fail(self):
        for body_ in ('data "external" "env" {\n  program = ["sh", "-c", "env"]\n}\n',
                      'data "http" "x" {\n  url = "https://example.invalid"\n}\n',
                      "data external env {\n  program = [\"env\"]\n}\n",
                      'check "c" {\n  data "http" "x" {\n    url = "https://example.invalid"\n  }\n}\n'):
            with self.subTest(body_):
                self.assertFlags(stack(body_), "no external or http data sources")

    def test_files_outside_terraform_are_scanned(self):
        self.assertFlags(stack('data "external" "env" {\n  program = ["env"]\n}\n', path="clusters/x"),
                         "no external or http data sources")

    def test_remote_state_fails(self):
        self.assertFlags(stack('data "terraform_remote_state" "other" {\n  backend = "remote"\n}\n'),
                         "no remote state reads")

    def test_exec_provisioners_fail(self):
        for kind in ("local-exec", "remote-exec"):
            with self.subTest(kind):
                self.assertFlags(stack('resource "terraform_data" "x" {\n  provisioner "%s" {\n'
                                       '    command = "env"\n  }\n}\n' % kind),
                                 f'provisioner "{kind}"')

    def test_provider_source_outside_the_allowlist_fails(self):
        for source in ("example/evil", "registry.example.com/hashicorp/aws", "hashicorp/external"):
            with self.subTest(source):
                versions = ('terraform {\n  required_providers {\n    evil = {\n      source  = "%s"\n'
                            '      version = "1.0.0"\n    }\n  }\n}\n' % source)
                self.assertFlags(stack("", versions), "is not in the allowlist")

    def test_hostname_spelled_registry_source_passes(self):
        # guard: the long form of an allowlisted source is the same provider.
        versions = AWS.replace("hashicorp/aws", "registry.terraform.io/hashicorp/aws")
        self.assertEqual(scan(stack('resource "aws_s3_bucket" "b" {}\n', versions)), [])

    def test_required_providers_entry_that_is_not_plain_fails(self):
        for entry in ('aws = ({ source = "example/evil" })',
                      'aws = merge({ source = "hashicorp/aws" }, { source = "example/evil" })',
                      'aws = { source = <<EOT\nexample/evil\nEOT\n }',
                      'aws = { source = "${"example"}/evil" }',
                      'aws = { source = "hashicorp/aws", configuration_aliases = [aws.x] }',
                      'aws = { source = "hashicorp/aws", source = "example/evil" }',
                      'aws = local.providers',
                      '"aws" = { source = "example/evil" }'):
            with self.subTest(entry):
                versions = 'terraform {\n  required_providers {\n    %s\n  }\n}\n' % entry
                self.assertFlags(stack("", versions), "is not a plain version string")

    def test_provider_declared_twice_with_different_sources_fails(self):
        # fence: kills a dict.update() where the last file read wins.
        files = stack("", AWS)
        files["terraform/x/more.tf"] = AWS.replace("hashicorp/aws", "hashicorp/tfe")
        self.assertFlags(files, "declared more than once with different sources")

    def test_implied_provider_outside_the_allowlist_fails(self):
        # No required_providers entry: Terraform installs hashicorp/<prefix>.
        for main in ('resource "null_resource" "x" {}\n', 'data "dns_a_record_set" "x" {\n  host = "a"\n}\n',
                     'provider "google" {}\n', 'resource "netbird_group" "g" {\n  provider = local\n}\n',
                     'locals {\n  x = provider::time::rfc3339_parse("2026-01-01T00:00:00Z")\n}\n'):
            with self.subTest(main):
                self.assertFlags(stack(main), "not in the allowlist")

    def test_provider_arguments_beyond_todays_fail(self):
        aws = [("endpoints", 'endpoints {\n    sts = "https://example.invalid"\n  }'),
               ("dynamic", 'dynamic "endpoints" {\n    for_each = [1]\n    content {\n      sts = "x"\n    }\n  }'),
               ("assume_role", 'assume_role {\n    role_arn = "arn:aws:iam::123456789012:role/x"\n  }'),
               ("assume_role_with_web_identity", 'assume_role_with_web_identity {\n    role_arn = "x"\n  }')]
        aws += [(arg, f'{arg} = "x"') for arg in (
            "shared_config_files", "shared_credentials_files", "profile", "ec2_metadata_service_endpoint",
            "ec2_metadata_service_endpoint_mode", "sts_region", "custom_ca_bundle", "use_fips_endpoint",
            "skip_credentials_validation", "http_proxy", "https_proxy", "insecure")]
        for arg, setting in aws:
            with self.subTest(arg):
                self.assertFlags(stack('provider "aws" {\n  region = "eu-central-1"\n  %s\n}\n' % setting, AWS),
                                 f"sets {arg}")
        self.assertFlags(stack('provider "netbird" {\n  management_url = "https://example.invalid"\n}\n'),
                         "sets management_url")
        self.assertFlags(stack('provider "netbird" {\n  dynamic "x" {\n    for_each = []\n    content {}\n  }\n}\n'),
                         "sets dynamic")
        # guard: what today's blocks set, and an alias, pass.
        main = ('provider "aws" {\n  alias  = "x"\n  region = "eu-central-1"\n  default_tags {\n    tags = {\n'
                '      a = "b"\n    }\n  }\n}\n')
        self.assertEqual(scan(stack(main, AWS)), [])

    def test_modules_come_from_a_plain_local_path(self):
        for source in ("git::https://example.invalid/m.git", "../../clusters", "./.terraform/modules/m",
                       "../x/.terraform/modules/m", "${path.module}/../netbird"):
            with self.subTest(source):
                self.assertFlags(stack('module "m" {\n  source = "%s"\n}\n' % source), "modules come from a plain")
        self.assertEqual(scan(stack('module "m" {\n  source = "../netbird"\n}\n')), [])

    def test_override_and_json_files_fail(self):
        for name, needle in (("main_override.tf", "override file"), ("override.tf", "override file"),
                             ("x_override.tf.json", "override file"), ("extra.tf.json", "JSON-syntax")):
            with self.subTest(name):
                files = stack("")
                files[f"terraform/x/{name}"] = '{"data": {"external": {"x": {"program": ["env"]}}}}' \
                    if name.endswith(".json") else 'provider "netbird" {}\n'
                self.assertFlags(files, needle)

    def test_file_functions_read_inside_the_repo_only(self):
        for call in ('file("/proc/self/environ")', 'file("${path.module}/../../../outside")',
                     'file("${path.module}/${var.p}")', "file(var.p)", 'filebase64("~/.terraform.d/x")',
                     'file("${path.module}/../../.git/config")', 'file("${path.module}/.terraform/x")',
                     'fileset("/", "**")', 'templatefile("${path.root}/x", {})'):
            with self.subTest(call):
                self.assertFlags(stack("locals {\n  x = %s\n}\n" % call), "must read a literal")
        self.assertFlags(stack('locals {\n  x = <<EOT\n${file("/proc/self/environ")}\nEOT\n}\n'), "must read a literal")
        self.assertFlags(stack('locals {\n  x = templatestring(local.t, {})\n}\n'), "templatestring()")
        # guard: a file in the repo, by a module-relative path, passes (tbd-backups reads aws/bootstrap/).
        ok = 'locals {\n  x = file("${path.module}/../../aws/bootstrap/tfc-backups-trust.json")\n}\n'
        self.assertEqual(scan(stack(ok)), [])

    def test_template_that_reads_a_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "terraform/x").mkdir(parents=True)
            (root / "terraform/x/t.json").write_text('{"a": "${file("/proc/self/environ")}"}')
            (root / "terraform/x/ok.json").write_text('{"a": "${b}"}')
            self.assertFlags(stack('locals {\n  x = templatefile("${path.module}/t.json", {})\n}\n'),
                             "calls a file function itself", root)
            self.assertEqual(scan(stack('locals {\n  x = templatefile("${path.module}/ok.json", {b = 1})\n}\n'),
                                  root), [])

    def test_comments_do_not_count_and_strings_do_not_hide_code(self):
        # guard: a commented-out block is not a block.
        self.assertEqual(scan(stack('# data "external" "x" {}\n/* provisioner "local-exec" {} */\n')), [])
        # fence: kills a lexer that opens a comment at "/*" inside a string or template.
        main = ('locals {\n  a = "${"/*"}"\n}\ndata "external" "x" {\n  program = ["env"]\n}\n'
                'locals {\n  b = "*/"\n}\n')
        self.assertFlags(stack(main), "no external or http data sources")
        self.assertFlags(stack('locals {\n  a = "#"\n}\ndata "http" "x" {\n  url = "u"\n}\n'),
                         "no external or http data sources")


def run_fence(mutate=None, main=None):
    """Run check-tbd-backups-fences.py on a copy of the files it reads; mutate(docs) edits the parsed
    {"provisioner": ..., "plan": ...} policies in aws/bootstrap/ first, main(text) the stack's main.tf."""
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        (d / ".github/scripts").mkdir(parents=True)
        shutil.copy(FENCE, d / ".github/scripts")
        shutil.copytree(ROOT / "terraform/tbd-backups", d / "terraform/tbd-backups",
                        ignore=shutil.ignore_patterns(".terraform"))
        shutil.copytree(ROOT / "aws/bootstrap", d / "aws/bootstrap")
        if mutate:
            paths = {n: d / f"aws/bootstrap/tfc-backups-{n}.json" for n in ("provisioner", "plan")}
            docs = {n: json.loads(p.read_text()) for n, p in paths.items()}
            mutate(docs)
            for n, p in paths.items():
                p.write_text(json.dumps(docs[n], indent=2))
        if main:
            tf = d / "terraform/tbd-backups/main.tf"
            text = tf.read_text()
            tf.write_text(main(text))
            assert tf.read_text() != text, "the main.tf mutation matched nothing"
        return subprocess.run([sys.executable, "-I", str(d / ".github/scripts" / FENCE.name)],
                              capture_output=True, text=True)


def statement(doc, sid):
    return next(s for s in doc["Statement"] if s.get("Sid") == sid)


def added(name, **stmt):
    return lambda docs: docs[name]["Statement"].append({"Sid": "Added", "Effect": "Allow", **stmt})


def edited(name, sid, fn):
    return lambda docs: fn(statement(docs[name], sid))


def dropped(name, sid):
    return lambda docs: docs[name]["Statement"].remove(statement(docs[name], sid))


OTHER_ROLE = "arn:aws:iam::123456789012:role/other"


class BackupRolePolicies(unittest.TestCase):
    """The provisioner (apply) and plan role policies stay inside the backup chain."""

    def assertFenceFails(self, cases):
        for label, mutate, needle in cases:
            with self.subTest(label):
                r = run_fence(mutate)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(needle, r.stdout)

    def test_repo_policies_pass(self):
        # guard: the committed documents pass, also after a parse and re-serialize round trip.
        for mutate in (None, lambda docs: None):
            r = run_fence(mutate)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_provisioner_widenings_fail(self):
        iam = "ManageOnlyTheBackupIdentities"
        self.assertFenceFails([
            ("every action", added("provisioner", Action="*", Resource="*"), "allows every action"),
            ("iam on *", added("provisioner", Action="iam:*", Resource="*"), "IAM stays on the six backup"),
            ("iam write on *", added("provisioner", Action=["iam:PutRolePolicy"], Resource="*"),
             "IAM stays on the six backup"),
            ("a seventh identity", edited("provisioner", iam, lambda s: s["Resource"].append(OTHER_ROLE)),
             "IAM stays on the six backup"),
            ("a role wildcard", edited("provisioner", iam, lambda s: s["Resource"].append(
                OTHER_ROLE.replace("123456789012:role/other", "123456789012:role/*"))), "IAM stays on the six backup"),
            ("NotAction", added("provisioner", NotAction="iam:*", Resource="*"), "no NotAction or NotResource"),
            ("NotResource", added("provisioner", Action="s3:*", NotResource="arn:aws:s3:::x"),
             "no NotAction or NotResource"),
            ("s3 on every bucket", added("provisioner", Action="s3:*", Resource="*"), "S3 stays on the backup bucket"),
            ("kms:* without the tag", edited("provisioner", "ManageTheBackupKey", lambda s: s.pop("Condition")),
             "keys not tagged"),
            ("a key action on any key", edited("provisioner", "CreateTheBackupKey",
                                               lambda s: s["Action"].append("kms:ScheduleKeyDeletion")),
             "keys not tagged"),
            ("another service", added("provisioner", Action="secretsmanager:GetSecretValue", Resource="*"),
             "only ['iam', 'kms', 's3', 'sts'] actions"),
            ("sts beyond identity", added("provisioner", Action="sts:AssumeRole", Resource="*"), "STS beyond"),
        ])

    def test_terraform_applies_the_checked_documents(self):
        doc = 'file("${path.module}/../../aws/bootstrap/%s")'
        inline = ('jsonencode({ Version = "2012-10-17", '
                  'Statement = [{ Effect = "Allow", Action = "*", Resource = "*" }] })')
        attach = ('\nresource "aws_iam_role_policy_attachment" "extra" {\n'
                  '  role       = aws_iam_role.tfc_backups_provisioner.name\n'
                  '  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"\n}\n')
        cases = [
            ("inline provisioner policy", lambda t: t.replace(doc % "tfc-backups-provisioner.json", inline),
             "must be exactly file() of aws/bootstrap/tfc-backups-provisioner.json"),
            ("inline plan policy", lambda t: t.replace(doc % "tfc-backups-plan.json", inline),
             "must be exactly file() of aws/bootstrap/tfc-backups-plan.json"),
            ("plan role trusted like the apply role", lambda t: t.replace(doc % "tfc-backups-plan-trust.json",
                                                                          doc % "tfc-backups-trust.json"),
             "must be exactly file() of aws/bootstrap/tfc-backups-plan-trust.json"),
            ("plan policy on the apply role", lambda t: t.replace("role   = aws_iam_role.tfc_backups_plan.id",
                                                                  "role   = aws_iam_role.tfc_backups_provisioner.id"),
             "must attach to aws_iam_role.tfc_backups_plan"),
            ("a second policy on a TFC role", lambda t: t + attach, "refers to a TFC role"),
            ("a second inline policy by role name", lambda t: t + attach.replace(
                "aws_iam_role.tfc_backups_provisioner.name", '"tfc-backups-plan"'), "refers to a TFC role"),
        ]
        for label, main, needle in cases:
            with self.subTest(label):
                r = run_fence(main=main)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(needle, r.stdout)

    def test_provisioner_denies_stay(self):
        content, mint = "NeverReadBackupContent", "NeverMintTheK3sUploaderCredentials"
        self.assertFenceFails([
            ("content deny dropped", dropped("provisioner", content), f"Deny {content}"),
            ("content deny loses kms:Decrypt",
             edited("provisioner", content, lambda s: s["Action"].remove("kms:Decrypt")), f"Deny {content}"),
            ("content deny narrowed", edited("provisioner", content, lambda s: s.update(Resource="arn:aws:s3:::x/*")),
             f"Deny {content}"),
            ("content deny conditioned", edited("provisioner", content, lambda s: s.update(
                Condition={"Bool": {"aws:ViaAWSService": "true"}})), f"Deny {content}"),
            ("mint deny dropped", dropped("provisioner", mint), f"Deny {mint}"),
            ("mint deny turned Allow", edited("provisioner", mint, lambda s: s.update(Effect="Allow")), f"Deny {mint}"),
            ("mint deny loses CreateLoginProfile", edited("provisioner", mint,
                                                          lambda s: s["Action"].remove("iam:CreateLoginProfile")),
             f"Deny {mint}"),
        ])

    def test_plan_role_writes_fail(self):
        ids = "ReadBackupIdentities"
        self.assertFenceFails([
            ("iam:PassRole", edited("plan", ids, lambda s: s["Action"].append("iam:PassRole")), "stays read-only"),
            ("a bucket write", added("plan", Action="s3:PutBucketPolicy", Resource="*"), "stays read-only"),
            ("every action", added("plan", Action="*", Resource="*"), "stays read-only"),
            ("a service wildcard", added("plan", Action="kms:*", Resource="*"), "stays read-only"),
            ("a read on another service", added("plan", Action="secretsmanager:GetSecretValue", Resource="*"),
             "stays read-only"),
            ("sts beyond identity", added("plan", Action="sts:AssumeRole", Resource="*"), "stays read-only"),
            ("NotAction", added("plan", NotAction="s3:GetObject", Resource="*"), "no NotAction or NotResource"),
            ("content deny dropped", dropped("plan", "NeverReadBackupContent"), "Deny NeverReadBackupContent"),
            ("content deny turned Allow", edited("plan", "NeverReadBackupContent", lambda s: s.update(Effect="Allow")),
             "Deny NeverReadBackupContent"),
        ])

    def test_plan_role_reads_stay_on_the_backup_chain(self):
        ids, key = "ReadBackupIdentities", "ReadKeyMetadata"
        tag = {"StringEquals": {"aws:ResourceTag/component": "mysql-offhost-backup"}}
        self.assertFenceFails([
            ("iam reads on every identity", edited("plan", ids, lambda s: s.update(Resource="*")),
             "IAM stays on the six backup identities"),
            ("a seventh identity", edited("plan", ids, lambda s: s["Resource"].append(OTHER_ROLE)),
             "IAM stays on the six backup identities"),
            ("an account-wide iam read", added("plan", Action="iam:GetAccountAuthorizationDetails", Resource="*"),
             "IAM stays on the six backup identities"),
            ("key reads on every key", edited("plan", key, lambda s: s.pop("Condition")), "keys not tagged"),
            ("another tag value", edited("plan", key, lambda s: s.update(
                Condition={"StringEquals": {"aws:ResourceTag/component": "other"}})), "keys not tagged"),
            ("the tag only if it exists", edited("plan", key, lambda s: s.update(
                Condition={"StringEqualsIfExists": tag["StringEquals"]})), "keys not tagged"),
            ("a key read beside ListAliases",
             edited("plan", "ListKeyAliasesTakesNoResource",
                    lambda s: s.update(Action=["kms:ListAliases", "kms:GetKeyPolicy"])), "keys not tagged"),
            ("another bucket", added("plan", Action="s3:GetBucketPolicy", Resource="arn:aws:s3:::other"),
             "S3 stays on the backup bucket"),
        ])


if __name__ == "__main__":
    unittest.main()
