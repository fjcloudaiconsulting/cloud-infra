"""Fences on the cluster's Pod Security admission defaults (ansible/files/k3s-pod-security.yaml, ANS-08).

Stdlib only; reuses the helpers of test_ansible_hosts.py and the YAML subset parser of test_tbd_manifests.py.
"""
import re
import unittest

from test_ansible_hosts import ANSIBLE, all_vars, flat_tasks, md_section, module_args, play, server_config
from test_tbd_manifests import CLUSTERS, ROOT, _Parser, load_all

PSA_FILE = ANSIBLE / "files/k3s-pod-security.yaml"
PSA = "pod-security.kubernetes.io/"
DEFAULTS = {"enforce": "baseline", "enforce-version": "latest", "audit": "restricted", "audit-version": "latest",
            "warn": "restricted", "warn-version": "latest"}
# k3s's add-ons, and the local-path provisioner's helper pods with their hostPath volume.
EXEMPT_NAMESPACES = ["kube-system"]
# Namespaces k3s makes itself; none has a Namespace object in git.
K3S_NAMESPACES = ("default", "kube-public", "kube-node-lease", "kube-system")
POD_KINDS = ("Pod", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob")
# Pod Security Standards, baseline (latest).
BASELINE_CAPS = {"AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD", "NET_BIND_SERVICE",
                 "SETFCAP", "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT"}
SAFE_SYSCTLS = {"kernel.shm_rmid_forced", "net.ipv4.ip_local_port_range", "net.ipv4.ip_unprivileged_port_start",
                "net.ipv4.tcp_syncookies", "net.ipv4.ping_group_range", "net.ipv4.ip_local_reserved_ports",
                "net.ipv4.tcp_keepalive_time", "net.ipv4.tcp_fin_timeout", "net.ipv4.tcp_keepalive_intvl",
                "net.ipv4.tcp_keepalive_probes"}
SELINUX_TYPES = {None, "", "container_t", "container_init_t", "container_kvm_t", "container_engine_t"}


def psa_config():
    return load_all(PSA_FILE)[0]


def pod_security_plugin():
    (plugin,) = [p for p in psa_config().get("plugins") or [] if p.get("name") == "PodSecurity"]
    return plugin["configuration"]


def cluster_docs():
    """(file, doc) for every non-Secret document under clusters/, Flux's generated file included (CRDs skipped)."""
    for f in sorted(CLUSTERS.rglob("*.yaml")):
        if f.name.endswith(".secret.yaml"):
            continue
        for part in re.split(r"(?m)^---[ \t]*$", f.read_text()):
            if re.search(r"(?m)^kind: CustomResourceDefinition$", part):
                continue
            p = _Parser(part.split("\n"), f)
            doc = p.block(0)
            if isinstance(doc, dict) and doc.get("kind"):
                yield f, doc


def enforce_labels():
    """Namespace name -> its own enforce label (None when it sets none), for every Namespace in git."""
    out = {}
    for _, d in cluster_docs():
        if d["kind"] == "Namespace":
            out[d["metadata"]["name"]] = ((d["metadata"].get("labels") or {}).get(PSA + "enforce"))
    return out


def template(doc):
    """(pod metadata, pod spec) of a Pod or a workload's pod template."""
    if doc["kind"] == "Pod":
        return doc.get("metadata") or {}, doc.get("spec") or {}
    spec = doc.get("spec") or {}
    if doc["kind"] == "CronJob":
        spec = (spec.get("jobTemplate") or {}).get("spec") or {}
    t = spec.get("template") or {}
    return t.get("metadata") or {}, t.get("spec") or {}


def baseline_violations(meta, spec):
    out = []
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(key):
            out.append(key)
    out += [f"hostPath volume {v['name']}" for v in spec.get("volumes") or [] if "hostPath" in v]
    psc = spec.get("securityContext") or {}
    out += [f"sysctl {s.get('name')}" for s in psc.get("sysctls") or [] if s.get("name") not in SAFE_SYSCTLS]
    for k, v in (meta.get("annotations") or {}).items():
        if k.startswith("container.apparmor.security.beta.kubernetes.io/") and v == "unconfined":
            out.append(f"apparmor annotation {k}")
    scopes = [("pod", psc)]
    for key in ("initContainers", "containers", "ephemeralContainers"):
        for c in spec.get(key) or []:
            scopes.append((c["name"], c.get("securityContext") or {}))
            out += [f"{c['name']}: hostPort" for p in c.get("ports") or [] if p.get("hostPort")]
    for who, sc in scopes:
        if sc.get("privileged"):
            out.append(f"{who}: privileged")
        added = set(((sc.get("capabilities") or {}).get("add")) or [])
        if added - BASELINE_CAPS:
            out.append(f"{who}: capabilities {sorted(added - BASELINE_CAPS)}")
        if sc.get("procMount") not in (None, "Default"):
            out.append(f"{who}: procMount")
        for key in ("seccompProfile", "appArmorProfile"):
            if (sc.get(key) or {}).get("type") == "Unconfined":
                out.append(f"{who}: {key} Unconfined")
        se = sc.get("seLinuxOptions") or {}
        if se.get("type") not in SELINUX_TYPES or se.get("user") or se.get("role"):
            out.append(f"{who}: seLinuxOptions")
        if (sc.get("windowsOptions") or {}).get("hostProcess"):
            out.append(f"{who}: hostProcess")
    return out


class AdmissionConfig(unittest.TestCase):
    """ANS-08: enforce baseline, warn and audit restricted for every namespace without its own labels; only
    kube-system is exempt."""

    def test_file_is_a_pod_security_admission_configuration(self):
        cfg = psa_config()
        self.assertEqual((cfg.get("apiVersion"), cfg.get("kind")), ("apiserver.config.k8s.io/v1", "AdmissionConfiguration"))
        self.assertEqual([p.get("name") for p in cfg["plugins"]], ["PodSecurity"])
        ps = pod_security_plugin()
        self.assertEqual((ps.get("apiVersion"), ps.get("kind")),
                         ("pod-security.admission.config.k8s.io/v1", "PodSecurityConfiguration"))

    def test_defaults(self):
        self.assertEqual(pod_security_plugin().get("defaults"), DEFAULTS)

    def test_only_kube_system_is_exempt(self):
        self.assertEqual(pod_security_plugin().get("exemptions"),
                         {"usernames": [], "runtimeClasses": [], "namespaces": EXEMPT_NAMESPACES})


class ApiServerFlag(unittest.TestCase):
    """Every server's API server reads the file: one setting in group_vars/all.yml, overridden nowhere."""

    def test_flag_names_the_installed_file(self):
        path = all_vars()["k3s_psa_config_file"]
        self.assertTrue(path.startswith("/etc/rancher/k3s/"), path)
        args = server_config().get("kube-apiserver-arg") or []
        self.assertIn(f"admission-control-config-file={path}", args)
        self.assertEqual(sum(a.startswith("admission-control-config-file=") for a in args), 1)

    def test_no_group_or_host_overrides_it(self):
        keys = {"server_config_yaml", "k3s_psa_config_file"}
        for f in sorted((ANSIBLE / "group_vars").glob("*.yml")):
            if f.name != "all.yml":
                self.assertFalse(keys & set(load_all(f)[0] or {}), f.name)
        self.assertFalse((ANSIBLE / "host_vars").exists())
        groups = load_all(ANSIBLE / "inventory.yaml")[0]["all"]
        self.assertFalse(keys & set(groups.get("vars") or {}))
        for name, group in (groups.get("children") or {}).items():
            self.assertFalse(keys & set(group.get("vars") or {}), name)
            for host, hv in (group.get("hosts") or {}).items():
                self.assertFalse(keys & set(hv or {}), f"{name}/{host}")


class InstalledBeforeK3s(unittest.TestCase):
    """The API server does not start without the file, so it is on every server before k3s is installed or
    restarted: a pre_task of the K3s servers play, on every run, before the k3s_server role."""

    def test_copied_in_pre_tasks_on_every_run(self):
        p = play("K3s servers")
        pre = list(flat_tasks(p["pre_tasks"]))
        want = {"src": "k3s-pod-security.yaml", "dest": "{{ k3s_psa_config_file }}", "owner": "root",
                "group": "root", "mode": "0600"}
        (task,) = [t for t in pre if module_args(t, "ansible.builtin.copy") == want]
        self.assertNotIn("k3s_apply", str(task.get("when", "")))
        names = [t.get("name") for t in pre]
        self.assertLess(names.index("The k3s config directory exists"), names.index(task["name"]))
        self.assertTrue(PSA_FILE.is_file())
        for key in ("tasks", "post_tasks"):
            self.assertFalse([t for t in flat_tasks(p.get(key)) if module_args(t, "ansible.builtin.copy") == want])
        self.assertIn("k3s.orchestration.k3s_server", [r.get("role") for r in p.get("roles") or []])


class NamespacesUnderTheDefault(unittest.TestCase):
    """A namespace label wins over a default, so the namespaces in git keep their level; a pod template in a
    namespace that sets no enforce level (and is not exempt) must meet baseline, or the default would refuse it."""

    def test_every_pod_template_meets_its_namespace_level(self):
        labels = enforce_labels()
        seen = 0
        for f, d in cluster_docs():
            if d["kind"] not in POD_KINDS:
                continue
            seen += 1
            ns = (d.get("metadata") or {}).get("namespace") or "default"
            level = "privileged" if ns in EXEMPT_NAMESPACES else (labels.get(ns) or DEFAULTS["enforce"])
            if level == "privileged":
                continue
            self.assertEqual(baseline_violations(*template(d)), [],
                             f"{f.relative_to(ROOT)} {d['kind']}/{d['metadata']['name']} in {ns} ({level})")
        self.assertGreater(seen, 10)

    def test_k3s_namespaces_are_not_in_git(self):
        # Their only Pod Security setting is the default (or the exemption), as the README describes.
        self.assertFalse(set(enforce_labels()) & set(K3S_NAMESPACES))

    def test_baseline_checker(self):
        ok = {"containers": [{"name": "a", "securityContext": {"capabilities": {"add": ["NET_BIND_SERVICE"]}}}]}
        self.assertEqual(baseline_violations({}, ok), [])
        bad = {"hostNetwork": True, "volumes": [{"name": "h", "hostPath": {"path": "/"}}],
               "containers": [{"name": "a", "securityContext": {"privileged": True, "capabilities": {"add": ["NET_ADMIN"]}},
                               "ports": [{"containerPort": 80, "hostPort": 80}]}]}
        self.assertEqual(baseline_violations({}, bad), ["hostNetwork", "hostPath volume h", "a: hostPort",
                                                        "a: privileged", "a: capabilities ['NET_ADMIN']"])

    def test_readme_gives_the_dry_run_check(self):
        text = md_section(ANSIBLE / "README.md", "Pod Security defaults")
        self.assertIn("kubectl label --dry-run=server ns", text)
        for ns in K3S_NAMESPACES:
            self.assertIn(f"`{ns}`", text)


if __name__ == "__main__":
    unittest.main()
