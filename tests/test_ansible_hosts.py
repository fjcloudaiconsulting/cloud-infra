"""Fences on the Hetzner hosts: the Ansible playbook in ansible/ and the hetzner stack's variable checks.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`. The Ansible YAML is read with the subset
parser in test_tbd_manifests.py, which fails loudly on anything it does not understand.
"""
import base64
import ipaddress
import pathlib
import re
import struct
import types
import unittest

from test_tbd_edge import resource_body
from test_tbd_manifests import _Parser, load_all

ROOT = pathlib.Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / "ansible"
SITE = ANSIBLE / "site.yml"
HETZNER_TF = ROOT / "terraform/hetzner"


def all_vars():
    return load_all(ANSIBLE / "group_vars/all.yml")[0]


def server_config():
    """server_config_yaml, parsed. `{{ name }}` of a plain string in group_vars/all.yml is filled in; any other
    expression becomes JINJA and `{% %}` tags are dropped (only the static keys are fenced here)."""
    v = all_vars()

    def fill(m):
        val = v.get(m.group(1).strip())
        return val if isinstance(val, str) and "{" not in val else "JINJA"

    text = re.sub(r"\{%.*?%\}", "", re.sub(r"\{\{(.*?)\}\}", fill, v["server_config_yaml"]))
    return _Parser(text.split("\n"), "server_config_yaml").block(0)


def server_private_ips():
    hosts = load_all(ANSIBLE / "inventory.yaml")[0]["all"]["children"]["server"]["hosts"]
    return {name: h["private_ip"] for name, h in hosts.items()}


def plays(path):
    """The plays of a playbook (its one YAML document)."""
    return load_all(path)[0]


def play(name):
    for p in plays(SITE):
        if p.get("name") == name:
            return p
    raise AssertionError(f"no play {name!r} in site.yml")


def flat_tasks(tasks):
    """Tasks in run order, with each block's own tasks after it (blocks keep their keywords for the caller)."""
    for t in tasks or []:
        yield t
        for key in ("block", "rescue", "always"):
            yield from flat_tasks(t.get(key))


def module_args(task, module):
    """The arguments of `module` (FQCN or short name) in the task, or None."""
    short = module.rsplit(".", 1)[-1]
    for key in (module, short):
        if key in task:
            return task[key]
    return None


# ---- Terraform variable validations --------------------------------------------------------------------------------

def _balanced_end(text, start, opener, closer):
    """Index just past the `closer` that matches the `opener` at text[start] (HCL strings skipped)."""
    depth, i, in_str = 0, start, False
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError(f"unbalanced {opener}{closer} from offset {start}")


def tf_validation(name):
    """The condition expression of `variable "<name>"`'s validation block in terraform/hetzner/variables.tf."""
    text = (HETZNER_TF / "variables.tf").read_text()
    m = re.search(rf'(?m)^variable "{re.escape(name)}" \{{', text)
    if not m:
        raise AssertionError(f"variable {name} not found")
    body = text[m.end() - 1:_balanced_end(text, m.end() - 1, "{", "}")]
    m = re.search(r"(?m)^\s*condition\s*=\s*", body)
    if not m:
        raise AssertionError(f"variable {name} has no validation condition")
    # The expression runs to the end of the line once every bracket is closed.
    i, depth, in_str = m.end(), 0, False
    while i < len(body) and (body[i] != "\n" or depth or in_str):
        c = body[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        i += 1
    return body[m.end():i].strip()


def _cidrnetmask(s):
    if "/" not in s:
        raise ValueError(f"{s!r} is not in CIDR notation")
    net = ipaddress.ip_network(s, strict=False)
    if net.version != 4:
        raise ValueError("only IPv4 has a netmask")
    return str(net.netmask)


def _regex(pattern, s):
    m = re.search(pattern, s)
    if not m:
        raise ValueError("pattern did not match")
    return m.group(0)


def _can(thunk):
    try:
        thunk()
        return True
    except (ValueError, TypeError, IndexError):
        return False


HCL_FUNCTIONS = {
    "length": len, "alltrue": all, "can": _can, "cidrnetmask": _cidrnetmask, "regex": _regex,
    "startswith": lambda s, p: s.startswith(p), "endswith": lambda s, p: s.endswith(p),
    "split": lambda sep, s: s.split(sep), "tonumber": lambda s: float(s) if "." in s else int(s),
    "true": True, "false": False,
}


def hcl_eval(expr, **variables):
    """Evaluates a validation condition with `var.<name>` bound. It knows only what these conditions use (string
    literals, `[for x in xs : ...]`, && || !, comparisons and the functions above) and fails on anything else, so a
    new construct updates this fence instead of slipping past it."""
    strings = []

    def keep(m):
        if "${" in m.group(1):
            raise AssertionError("string templates are not supported")
        strings.append(re.sub(r"\\(.)", r"\1", m.group(1)))
        return f"__s{len(strings) - 1}"

    py = re.sub(r'"((?:[^"\\]|\\.)*)"', keep, expr)
    if re.search(r"[?%$]|\bfor\b(?![^\[]*\bin\b)", py):
        raise AssertionError(f"unsupported HCL in {expr!r}")
    while "[for " in py:
        start = py.index("[for ")
        end = _balanced_end(py, start, "[", "]")
        m = re.fullmatch(r"\[for (\w+) in (.+?) : (.*)\]", py[start:end], re.S)
        py = f"{py[:start]}[({m.group(3)}) for {m.group(1)} in ({m.group(2)})]{py[end:]}"
    py = py.replace("can(", "can(lambda: ").replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    # One namespace as globals: lambdas (can) and comprehensions (for) resolve names there, not in eval's locals.
    env = dict(HCL_FUNCTIONS, __builtins__={}, var=types.SimpleNamespace(**variables))
    env.update({f"__s{i}": v for i, v in enumerate(strings)})
    return bool(eval(py, env))  # noqa: S307 - the input is this repo's own variables.tf


def ssh_key(key_type, comment="owner@laptop"):
    """An OpenSSH public key line of `key_type` with a made-up 32-byte key (sk keys carry the usual "ssh:" app)."""
    def s(b):
        return struct.pack(">I", len(b)) + b
    blob = s(key_type.encode()) + s(bytes(range(32)))
    if key_type.startswith("sk-"):
        blob += s(b"ssh:")
    return f"{key_type} {base64.b64encode(blob).decode()}" + (f" {comment}" if comment else "")


# ---- k3s API audit policy ------------------------------------------------------------------------------------------

AUDIT_RULE_KEYS = {"level", "users", "userGroups", "verbs", "resources", "nonResourceURLs"}


def _audit_rule_matches(rule, req):
    unknown = set(rule) - AUDIT_RULE_KEYS
    if unknown:
        raise AssertionError(f"audit rule keys this fence does not model: {sorted(unknown)}")
    if "users" in rule and req["user"] not in rule["users"]:
        return False
    if "userGroups" in rule and not set(req["groups"]) & set(rule["userGroups"]):
        return False
    if "verbs" in rule and req["verb"] not in rule["verbs"]:
        return False
    if "path" in req:  # a non-resource request
        if "resources" in rule:
            return False
        urls = rule.get("nonResourceURLs")
        return urls is None or any(req["path"] == u or (u.endswith("*") and req["path"].startswith(u[:-1]))
                                   for u in urls)
    if "nonResourceURLs" in rule:
        return False
    if "resources" not in rule:
        return True
    # "nodes" matches the resource only, not nodes/status: subresources are named explicitly (or with a *).
    return any(g.get("group", "") == req.get("group", "") and (not g.get("resources") or req["resource"] in g["resources"])
               for g in rule["resources"])


def audit_level(policy, user, verb, resource=None, group="", groups=("system:authenticated",), path=None):
    """The level the API server logs a request at: the first matching rule's, None when no rule matches."""
    req = {"user": user, "groups": list(groups), "verb": verb}
    req.update({"path": path} if path else {"resource": resource, "group": group})
    for rule in policy["rules"]:
        if _audit_rule_matches(rule, req):
            return rule["level"]
    return "None"


def kubeconfig_block(site_plays):
    post = next(p for p in site_plays if p.get("name") == "K3s servers")["post_tasks"]
    blocks = [t for t in post if "block" in t and "kubeconfig" in str(t.get("when"))]
    if len(blocks) != 1:
        raise AssertionError(f"expected one post_tasks block on the fetched kubeconfig, found {len(blocks)}")
    return blocks[0]


class AdminKubeconfig(unittest.TestCase):
    """ANS-04: the fetched admin kubeconfig (a system:masters client certificate) never stays on the operator machine;
    only its cluster CA does."""

    def test_site_keeps_the_ca_and_deletes_the_kubeconfig(self):
        block = kubeconfig_block(plays(SITE))
        self.assertEqual(block.get("delegate_to"), "localhost")
        self.assertIs(block.get("run_once"), True)
        steps = block["block"]
        writes = [i for i, t in enumerate(steps) if (module_args(t, "ansible.builtin.copy") or {}).get("dest")
                  == "{{ kubeconfig_ca | expanduser }}"]
        deletes = [i for i, t in enumerate(steps) if (module_args(t, "ansible.builtin.file") or {}).get("state")
                   == "absent" and module_args(t, "ansible.builtin.file").get("path") == "{{ kubeconfig | expanduser }}"]
        self.assertEqual(len(writes), 1, "a copy task writes kubeconfig_ca")
        self.assertEqual(len(deletes), 1, "a file task deletes kubeconfig")
        self.assertLess(writes[0], deletes[0], "the CA is written before the kubeconfig is deleted")
        content = module_args(steps[writes[0]], "ansible.builtin.copy")["content"]
        self.assertIn("certificate-authority-data", content)
        self.assertNotIn("client-", content)

    def test_ca_file_sits_next_to_the_kubeconfig(self):
        v = all_vars()
        self.assertEqual(pathlib.PurePosixPath(v["kubeconfig_ca"]).parent,
                         pathlib.PurePosixPath(v["kubeconfig"]).parent)
        self.assertTrue(v["kubeconfig_ca"].endswith(".crt"))

    def test_no_task_or_runbook_reads_the_fetched_kubeconfig(self):
        # Only the CA block may touch it; the runbooks use the CA file (ansible/README.md may say it is deleted).
        site = plays(SITE)
        allowed = list(flat_tasks([kubeconfig_block(site)]))
        for p in site + plays(ANSIBLE / "reboot.yml"):
            for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                for t in flat_tasks(p.get(section)):
                    if any(t is a for a in allowed):
                        continue
                    self.assertNotIn("kubeconfig", str({k: v for k, v in t.items() if k != "name"}),
                                     f"{p['name']}: task {t.get('name')!r} uses the fetched kubeconfig")
        for doc in ("docs/runbooks.md", "docs/configuration-map.md", "clusters/hetzner/data/RESTORE.md"):
            self.assertNotIn("fjc-hetzner.yaml", (ROOT / doc).read_text(), f"{doc} still uses the fetched kubeconfig")


class RootSshKeys(unittest.TestCase):
    """ANS-03: root's authorized_keys is exactly the listed deploy keys, written only after the keys are checked and
    the connecting key is known to work; FIDO2 ed25519 keys are accepted."""

    def tasks(self):
        return load_all(ANSIBLE / "roles/node_base/tasks/main.yml")[0]

    def test_exclusive_authorized_key_task_is_guarded(self):
        tasks = self.tasks()
        writes = [i for i, t in enumerate(tasks) if module_args(t, "ansible.posix.authorized_key")]
        self.assertEqual(len(writes), 1, "one authorized_key task")
        args = module_args(tasks[writes[0]], "ansible.posix.authorized_key")
        self.assertEqual(args.get("user"), "root")
        self.assertIs(args.get("exclusive"), True)
        self.assertEqual(args.get("key"), "{{ node_base_root_keys | join('\n') }}")
        self.assertNotIn("when", tasks[writes[0]])
        before = tasks[:writes[0]]
        collect = [module_args(t, "ansible.builtin.set_fact") for t in before
                   if "node_base_root_keys" in (module_args(t, "ansible.builtin.set_fact") or {})]
        self.assertEqual(len(collect), 1)
        self.assertRegex(collect[0]["node_base_root_keys"],
                         r"lookup\('ansible\.builtin\.file', \(ansible_ssh_private_key_file \| expanduser\) ~ '\.pub'\)\]"
                         r" \+ deploy_ssh_extra_public_keys")
        checks = [str(module_args(t, "ansible.builtin.assert")["that"]) for t in before
                  if module_args(t, "ansible.builtin.assert")]
        self.assertTrue(any("node_base_root_keys | length > 0" in c and
                            "reject('match', node_base_ssh_key_pattern)" in c for c in checks),
                        "an assert before the write refuses an empty list and malformed keys")
        self.assertTrue(any("node_base_root_keys[0]" in c and "node_base_authorized_keys.content" in c for c in checks),
                        "an assert before the write checks the connecting key is already authorized")
        self.assertEqual(all_vars().get("deploy_ssh_extra_public_keys"), [])

    def test_key_pattern_accepts_ed25519_and_fido2_ed25519_only(self):
        pattern = load_all(ANSIBLE / "roles/node_base/vars/main.yml")[0]["node_base_ssh_key_pattern"]

        def ok(key):  # Jinja's `match` test is re.match
            return re.match(pattern, key) is not None

        for key in (ssh_key("ssh-ed25519"), ssh_key("sk-ssh-ed25519@openssh.com"), ssh_key("ssh-ed25519", None),
                    ssh_key("sk-ssh-ed25519@openssh.com", "yubikey 5 nano")):
            self.assertTrue(ok(key), key)
        ed = ssh_key("ssh-ed25519")
        for key in ("", "ssh-ed25519", ssh_key("ssh-rsa"), ssh_key("ecdsa-sha2-nistp256"),
                    ssh_key("sk-ecdsa-sha2-nistp256@openssh.com"),
                    "sk-ssh-ed25519@openssh.com " + ed.split()[1],  # type and blob disagree
                    f'command="/bin/sh" {ed}', f"{ed}\n{ssh_key('ssh-rsa')}", "https://github.com/someone.keys"):
            self.assertFalse(ok(key), key)

    def test_terraform_accepts_both_key_types(self):
        cond = tf_validation("deploy_ssh_public_key")
        for key in (ssh_key("ssh-ed25519"), ssh_key("sk-ssh-ed25519@openssh.com")):
            self.assertTrue(hcl_eval(cond, deploy_ssh_public_key=key), key)
        for key in (ssh_key("ssh-rsa"), ssh_key("ecdsa-sha2-nistp256"), "", "ssh-ed25519"):
            self.assertFalse(hcl_eval(cond, deploy_ssh_public_key=key), key)


class PublicNicExposure(unittest.TestCase):
    """ANS-01 (part): the SSH allowlist takes only /24 or narrower entries, and NodePorts listen on the private
    network only."""

    def test_ssh_allowlist_needs_a_24_or_narrower_prefix(self):
        cond = tf_validation("ssh_allowed_cidrs")
        for ok in (["203.0.113.7/32"], ["198.51.100.0/24"], ["203.0.113.7/32", "198.51.100.0/24"]):
            self.assertTrue(hcl_eval(cond, ssh_allowed_cidrs=ok), ok)
        for bad in ([], ["0.0.0.0/0"], ["0.0.0.0/1"], ["0.0.0.0/1", "128.0.0.0/1"], ["198.51.100.0/23"],
                    ["203.0.113.7"], ["2001:db8::/48"], ["203.0.113.7/32", "0.0.0.0/1"], ["not-an-ip/32"]):
            self.assertFalse(hcl_eval(cond, ssh_allowed_cidrs=bad), bad)

    def test_nodeports_listen_on_the_private_network_only(self):
        cidr = all_vars()["private_cidr"]
        self.assertIn(f"nodeport-addresses={cidr}", server_config().get("kube-proxy-arg") or [])
        subnet = resource_body((HETZNER_TF / "main.tf").read_text(), "hcloud_network_subnet", "main")
        self.assertEqual(re.search(r'ip_range\s*=\s*"([^"]+)"', subnet).group(1), cidr,
                         "private_cidr is the Hetzner private subnet")
        for name, ip in server_private_ips().items():
            self.assertIn(ipaddress.ip_address(ip), ipaddress.ip_network(cidr), name)


class ApiAuditLog(unittest.TestCase):
    """ANS-02: the API servers write an audit log under a policy Ansible installs before k3s is configured."""

    ADMIN = {"user": "system:admin", "groups": ("system:masters", "system:authenticated")}
    OWNER = {"user": "system:serviceaccount:netbird:owner-admin",
             "groups": ("system:serviceaccounts", "system:serviceaccounts:netbird", "system:authenticated")}
    FLUX = {"user": "system:serviceaccount:flux-system:kustomize-controller",
            "groups": ("system:serviceaccounts", "system:serviceaccounts:flux-system", "system:authenticated")}
    KUBELET = {"user": "system:node:node-1", "groups": ("system:nodes", "system:authenticated")}

    def policy(self):
        return load_all(ANSIBLE / "files/k3s-audit-policy.yaml")[0]

    def test_api_server_args_point_at_the_policy_and_cap_the_log(self):
        args = dict(a.split("=", 1) for a in server_config().get("kube-apiserver-arg") or [])
        self.assertEqual(args.get("audit-policy-file"), all_vars()["k3s_audit_policy_file"])
        self.assertTrue(args.get("audit-log-path", "").startswith("/var/lib/rancher/k3s/server/logs/"))
        for key in ("audit-log-maxage", "audit-log-maxbackup", "audit-log-maxsize"):
            self.assertRegex(args.get(key, ""), r"^[1-9]\d*$", key)
        # The CAX21 root disk holds the OS, images and etcd: the log may take at most 1 GB per server.
        self.assertLessEqual((int(args["audit-log-maxbackup"]) + 1) * int(args["audit-log-maxsize"]), 1024)

    def test_site_installs_the_policy_before_k3s(self):
        pre = list(flat_tasks(play("K3s servers")["pre_tasks"]))
        copies = [module_args(t, "ansible.builtin.copy") for t in pre if module_args(t, "ansible.builtin.copy")]
        self.assertIn({"src": "k3s-audit-policy.yaml", "dest": "{{ k3s_audit_policy_file }}", "owner": "root",
                       "group": "root", "mode": "0600"}, copies)
        self.assertTrue((ANSIBLE / "files/k3s-audit-policy.yaml").is_file())

    def test_policy_levels(self):
        pol = self.policy()
        self.assertEqual((pol.get("apiVersion"), pol.get("kind")), ("audit.k8s.io/v1", "Policy"))
        self.assertIn("RequestReceived", pol.get("omitStages") or [])
        cases = [
            # (who, verb, resource, group, expected)
            (self.OWNER, "get", "secrets", "", "Metadata"),
            (self.FLUX, "patch", "secrets", "", "Metadata"),
            (self.OWNER, "list", "configmaps", "", "Metadata"),
            (self.ADMIN, "create", "serviceaccounts/token", "", "Metadata"),
            (self.KUBELET, "create", "serviceaccounts/token", "", "Metadata"),
            (self.KUBELET, "create", "tokenreviews", "authentication.k8s.io", "Metadata"),
            (self.ADMIN, "create", "pods/exec", "", "Request"),
            (self.OWNER, "get", "pods/exec", "", "Request"),
            (self.OWNER, "create", "pods/attach", "", "Request"),
            (self.OWNER, "create", "pods/portforward", "", "Request"),
            (self.ADMIN, "create", "clusterrolebindings", "rbac.authorization.k8s.io", "Request"),
            (self.FLUX, "patch", "roles", "rbac.authorization.k8s.io", "Request"),
            (self.OWNER, "delete", "rolebindings", "rbac.authorization.k8s.io", "Request"),
            (self.OWNER, "patch", "nodes", "", "Request"),
            (self.ADMIN, "delete", "nodes", "", "Request"),
            (self.KUBELET, "get", "secrets", "", "None"),
            (self.KUBELET, "watch", "configmaps", "", "None"),
            ({"user": "system:kube-controller-manager", "groups": ("system:authenticated",)}, "list", "secrets", "",
             "None"),
            (self.KUBELET, "patch", "nodes/status", "", "None"),
            (self.OWNER, "get", "clusterroles", "rbac.authorization.k8s.io", "None"),
        ]
        for who, verb, resource, group, want in cases:
            got = audit_level(pol, who["user"], verb, resource, group, who["groups"])
            self.assertEqual(got, want, f"{who['user']} {verb} {group}/{resource}")
        for path in ("/healthz", "/livez", "/readyz", "/readyz/etcd", "/version"):
            self.assertEqual(audit_level(pol, "system:anonymous", "get", path=path), "None", path)

    def test_secret_values_are_never_logged(self):
        pol = self.policy()
        for who in (self.ADMIN, self.OWNER, self.FLUX, self.KUBELET):
            for verb in ("get", "list", "watch", "create", "update", "patch", "delete"):
                self.assertIn(audit_level(pol, who["user"], verb, "secrets", "", who["groups"]), ("None", "Metadata"))


if __name__ == "__main__":
    unittest.main()
