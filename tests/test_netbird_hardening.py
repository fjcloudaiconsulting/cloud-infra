"""Fences on the NetBird peer in clusters/hetzner/netbird/ (the owner's kubectl path, INFRA-66).

Stdlib only; reuses the YAML subset parser of test_tbd_manifests.py.
"""
import posixpath
import unittest

from test_tbd_manifests import containers, manifests, pod_spec, where

NS = "netbird"
PSA = "pod-security.kubernetes.io/"
# Where the rootless client writes, per upstream client/Dockerfile-rootless and the client source at the pinned tag:
# the state directory holds the config, the state, the daemon socket and (the image user's home) the CLI's ~/.config;
# os.TempDir() takes debug bundles and packet captures. Each must be a writable mount once the root is read-only.
WRITES = {"/var/lib/netbird": "persistentVolumeClaim", "/tmp": "emptyDir"}
# Env vars that move one of those paths. NB_LOG_FILE also takes the keywords console and syslog; NB_DAEMON_ADDR is a
# unix:// URL (tcp:// writes nothing).
PATH_ENV = ("NB_CONFIG", "NB_STATE_DIR", "NB_DAEMON_ADDR", "NB_LOG_FILE", "TMPDIR", "HOME", "XDG_CONFIG_HOME")
LOG_KEYWORDS = {"console", "syslog"}


def namespace():
    (doc,) = [d for _, d in manifests(NS) if d.get("kind") == "Namespace" and d["metadata"]["name"] == NS]
    return doc


def peer():
    (hit,) = [(f, d) for f, d in manifests(NS) if d.get("kind") == "Deployment" and d["metadata"]["name"] == NS]
    return hit


def env_paths(c):
    """(env name, path) for every path the container's env sets."""
    for e in c.get("env") or []:
        name, value = e.get("name"), e.get("value")
        if name not in PATH_ENV or value is None:
            continue
        if name == "NB_DAEMON_ADDR":
            if value.startswith("unix://"):
                yield name, value[len("unix://"):]
            continue
        for part in value.split(",") if name == "NB_LOG_FILE" else [value]:
            if part.strip() not in LOG_KEYWORDS:
                yield name, part.strip()


def under(path, mount):
    path = posixpath.normpath(path)
    return path == mount or path.startswith(mount.rstrip("/") + "/")


class PodSecurityLabels(unittest.TestCase):
    """K8S-09: the peer runs hostNetwork, so enforce stays privileged; warn and audit report anything past baseline."""

    def test_enforce_stays_privileged(self):
        self.assertEqual(namespace()["metadata"]["labels"].get(PSA + "enforce"), "privileged")

    def test_warn_and_audit_at_baseline(self):
        labels = namespace()["metadata"]["labels"]
        for mode in ("warn", "audit"):
            self.assertEqual(labels.get(PSA + mode), "baseline", mode)


class PeerSecurityContext(unittest.TestCase):
    """K8S-08: the runtime's seccomp profile and a read-only root, with a writable mount for every path it writes."""

    def test_seccomp_runtime_default(self):
        f, d = peer()
        self.assertEqual((pod_spec(d).get("securityContext") or {}).get("seccompProfile"), {"type": "RuntimeDefault"})
        for c in containers(d):
            # A container-level profile would override the pod's.
            self.assertNotIn("seccompProfile", c.get("securityContext") or {}, where(f, d, c))

    def test_no_capabilities_and_no_escalation(self):
        f, d = peer()
        for c in containers(d):
            sc = c.get("securityContext") or {}
            self.assertIs(sc.get("allowPrivilegeEscalation"), False, where(f, d, c))
            self.assertEqual(sc.get("capabilities"), {"drop": ["ALL"]}, where(f, d, c))

    def test_read_only_root(self):
        f, d = peer()
        for c in containers(d):
            self.assertIs((c.get("securityContext") or {}).get("readOnlyRootFilesystem"), True, where(f, d, c))

    def test_every_written_path_is_a_writable_mount(self):
        f, d = peer()
        volumes = {v["name"]: v for v in pod_spec(d).get("volumes") or []}
        self.assertEqual(volumes["state"].get("persistentVolumeClaim"), {"claimName": "netbird-state"})
        for c in containers(d):
            mounts = {m["mountPath"]: m for m in c.get("volumeMounts") or [] if not m.get("readOnly")}
            for path, kind in WRITES.items():
                self.assertIn(path, mounts, where(f, d, c))
                self.assertIn(kind, volumes[mounts[path]["name"]], f"{where(f, d, c)}: {path}")
            for name, path in env_paths(c):
                self.assertTrue(any(under(path, m) for m in mounts), f"{where(f, d, c)}: {name}={path}")

    def test_env_path_helper(self):
        c = {"env": [{"name": "NB_LOG_FILE", "value": "console,/var/log/netbird/client.log"},
                     {"name": "NB_DAEMON_ADDR", "value": "unix:///var/run/netbird.sock"},
                     {"name": "NB_HOSTNAME", "value": "x"}]}
        self.assertEqual(list(env_paths(c)), [("NB_LOG_FILE", "/var/log/netbird/client.log"),
                                              ("NB_DAEMON_ADDR", "/var/run/netbird.sock")])
        self.assertTrue(under("/var/lib/netbird/config.json", "/var/lib/netbird"))
        self.assertFalse(under("/var/lib/netbird-other", "/var/lib/netbird"))


if __name__ == "__main__":
    unittest.main()
