"""Offline guards for the direct-inventory Cilium entrypoint; no remote execution."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

HERE = Path(__file__).resolve().parent


class CiliumEntryTests(unittest.TestCase):
    def setUp(self):
        self.play = yaml.safe_load((HERE / 'site.yml').read_text())[0]
        self.defaults = yaml.safe_load((HERE / 'group_vars/all.yml').read_text())

    def test_cilium_runs_between_api_and_node_ready(self):
        names = [task['name'] for task in self.play['tasks']]
        api = names.index('Wait for local API readiness')
        cni = names.index('Install Cilium before waiting for Node Ready or CoreDNS')
        ready = names.index('Wait for node networking to become ready')
        self.assertLess(api, cni)
        self.assertLess(cni, ready)

    def test_legacy_identity_rejected_before_mutation(self):
        identity = self.play['vars']['k3s_identity']
        self.assertEqual(identity['cni'], 'cilium')
        self.assertIn('cilium_version', identity)
        self.assertIn('cilium_cli_version', identity)
        guard = next(t for t in self.play['pre_tasks'] if t['name'] == 'Reject implicit upgrades or destructive network changes')
        self.assertIn('(k3s_previous.content | b64decode | from_json) == k3s_identity',
                      guard['ansible.builtin.assert']['that'])
        self.assertEqual(guard['when'], 'k3s_existing.results[0].stat.exists')

    def test_reuses_team_installer_with_lock(self):
        tasks = self.play['tasks']
        copy = next(t for t in tasks if 'Reuse the team Cilium' in t['name'])
        self.assertEqual(copy['loop'], ['scripts/common.sh', 'cilium/install.sh', 'cilium/preflight.py', 'airgap/versions.json'])
        install = next(t for t in tasks if t['name'].startswith('Install Cilium before'))
        self.assertEqual(install['ansible.builtin.command']['argv'][:3],
                         ['/usr/bin/flock', '--nonblock', '/run/railshot-deployment.lock'])
        self.assertEqual(install['environment']['CILIUM_VERSION'], '{{ cilium_version }}')
        self.assertEqual(install['environment']['RAILSHOT_OFFLINE'], 'false')
        self.assertEqual(install['environment']['RAILSHOT_BUNDLE'], '')
        self.assertEqual(install['environment']['RAILSHOT_BUNDLE_SHA256'], '')

    def test_copied_online_installer_dependency_closure(self):
        copy = next(t for t in self.play['tasks'] if 'Reuse the team Cilium' in t['name'])
        install = next(t for t in self.play['tasks'] if t['name'].startswith('Install Cilium before'))
        # Stage the files addressed by the actual Ansible copy task, not a fixture.
        with tempfile.TemporaryDirectory() as directory:
            staged = Path(directory)
            for item in copy['loop']:
                source = copy['ansible.builtin.copy']['src'].replace('{{ playbook_dir }}', str(HERE)).replace('{{ item }}', item)
                target = copy['ansible.builtin.copy']['dest'].replace('{{ item }}', item)
                destination = staged / Path(target).relative_to('/var/lib/railshot/standalone-cilium')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            entry = install['ansible.builtin.command']['argv'][-1]
            entry = staged / Path(entry).relative_to('/var/lib/railshot/standalone-cilium')
            # Loading the real common script resolves ROOT_DIR from the staged tree.
            subprocess.run(['bash', '-ec', 'source "$(dirname -- "$1")/../scripts/common.sh"; '
                            'test -s "$ROOT_DIR/airgap/versions.json"; bash -n "$1"', 'check', str(entry)], check=True)
            policy = json.loads((staged / 'airgap/versions.json').read_text())
            self.assertEqual(policy['runtime'], {key: self.defaults[key] for key in ('k3s_version', 'cilium_version', 'cilium_cli_version')})
            self.assertEqual(set(policy['cilium_images']), {'agent', 'operator', 'envoy'})
            for reference in policy['cilium_images'].values():
                self.assertRegex(reference, r'^quay\.io/cilium/[^@]+@sha256:[0-9a-f]{64}$')

    def test_pins_match_integrated_runtime(self):
        runtime = yaml.safe_load((HERE / 'runtime.yml').read_text())[0]
        identity = runtime['vars']['railshot_runtime_identity']
        self.assertIn("/airgap/versions.json", runtime['vars']['railshot_approved_runtime'])
        for key in ('k3s_version', 'cilium_version'):
            self.assertEqual(identity[key], '{{ railshot_approved_runtime.' + key + ' }}')
            approved = json.loads((HERE.parents[1] / 'deployment/airgap/versions.json').read_text())['runtime']
            self.assertEqual(self.defaults[key], approved[key])
        profile = next(t for t in self.play['pre_tasks'] if t['name'] == 'Validate the shared single-node Cilium profile')
        checks = profile['ansible.builtin.assert']['that']
        self.assertIn("k3s_cluster_cidr == '10.42.0.0/16'", checks)
        self.assertIn("ansible_facts.architecture == 'x86_64'", checks)


if __name__ == '__main__':
    unittest.main()
