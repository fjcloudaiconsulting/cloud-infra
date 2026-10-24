"""Fences on the Hetzner hosts: the Ansible playbook in ansible/ and the hetzner stack's variable checks.

Stdlib only so CI needs no installs: `python3 -m unittest discover -s tests`. The Ansible YAML is read with the subset
parser in test_tbd_manifests.py, which fails loudly on anything it does not understand.
"""
import pathlib
import unittest

from test_tbd_manifests import load_all

ROOT = pathlib.Path(__file__).resolve().parent.parent
ANSIBLE = ROOT / "ansible"
SITE = ANSIBLE / "site.yml"


def all_vars():
    return load_all(ANSIBLE / "group_vars/all.yml")[0]


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


if __name__ == "__main__":
    unittest.main()
