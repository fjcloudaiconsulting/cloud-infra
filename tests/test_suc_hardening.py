"""Fences on the system-upgrade-controller pod as Flux applies it (the render of clusters/hetzner/system-upgrade/).

Stdlib only; renders with `kubectl kustomize` through the helpers of test_platform_hardening.py.
"""
import re
import unittest

from test_platform_hardening import name_of, of_kind, parse_docs, render
from test_tbd_manifests import CLUSTERS, containers, pod_spec

DIR = "system-upgrade"
CONTROLLER = "system-upgrade-controller"
# The image is the static binary on scratch, run as 65534 with no capabilities (upstream package/Dockerfile and
# manifests at the vendored tag): the emptyDir upstream mounts here is the only path it can write.
WRITES = {"/tmp": "emptyDir"}
UNITS = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30}


def mebibytes(quantity):
    m = re.fullmatch(r"(\d+)(Ki|Mi|Gi)", quantity or "")
    if not m:
        raise ValueError(f"unexpected memory quantity: {quantity!r}")
    return int(m.group(1)) * UNITS[m.group(2)] / 2 ** 20


def controller():
    (d,) = [d for d in of_kind(render(DIR), "Deployment") if name_of(d) == CONTROLLER]
    return d


class ControllerPod(unittest.TestCase):
    """K8S-08: a read-only root with a writable mount for every path it writes, and a memory request and limit."""

    def test_read_only_root(self):
        for c in containers(controller()):
            self.assertIs((c.get("securityContext") or {}).get("readOnlyRootFilesystem"), True, c["name"])

    def test_every_written_path_is_a_writable_mount(self):
        d = controller()
        volumes = {v["name"]: v for v in pod_spec(d).get("volumes") or []}
        for c in containers(d):
            mounts = {m["mountPath"]: m for m in c.get("volumeMounts") or [] if not m.get("readOnly")}
            for path, kind in WRITES.items():
                self.assertIn(path, mounts, c["name"])
                self.assertIn(kind, volumes[mounts[path]["name"]], f"{c['name']}: {path}")

    def test_memory_request_and_limit(self):
        for c in containers(controller()):
            res = c.get("resources") or {}
            request = mebibytes((res.get("requests") or {}).get("memory"))
            limit = mebibytes((res.get("limits") or {}).get("memory"))
            self.assertLessEqual(request, limit, c["name"])
            # Headroom for a Go controller whose working set is tens of MiB (see the kustomization's comment).
            self.assertGreaterEqual(limit, 128, c["name"])

    def test_mebibytes(self):
        self.assertEqual(mebibytes("32Mi"), 32)
        self.assertEqual(mebibytes("1Gi"), 1024)
        with self.assertRaises(ValueError):
            mebibytes(None)


class UpgradeJobsUntouched(unittest.TestCase):
    """The controller patch reaches neither the Plans nor the settings the controller gives its upgrade Jobs."""

    def test_plans_render_as_written(self):
        written = of_kind(parse_docs((CLUSTERS / "hetzner" / DIR / "plans.yaml").read_text(), "plans.yaml"), "Plan")
        self.assertTrue(written)
        self.assertEqual(of_kind(render(DIR), "Plan"), written)

    def test_job_settings_render_as_vendored_except_the_kubectl_image(self):
        vendored = parse_docs((CLUSTERS / "hetzner" / DIR / f"{CONTROLLER}.yaml").read_text(), f"{CONTROLLER}.yaml")
        (want,) = [d for d in of_kind(vendored, "ConfigMap") if name_of(d) == "default-controller-env"]
        (got,) = [d for d in of_kind(render(DIR), "ConfigMap") if name_of(d) == "default-controller-env"]
        drop = {"SYSTEM_UPGRADE_JOB_KUBECTL_IMAGE"}
        self.assertEqual({k: v for k, v in got["data"].items() if k not in drop},
                         {k: v for k, v in want["data"].items() if k not in drop})

    def test_no_workload_but_the_controller(self):
        workloads = [(d["kind"], name_of(d)) for d in of_kind(render(DIR), "Deployment", "DaemonSet", "StatefulSet",
                                                               "Job", "CronJob")]
        self.assertEqual(workloads, [("Deployment", CONTROLLER)])


if __name__ == "__main__":
    unittest.main()
