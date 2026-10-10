"""Run the real CI helper against temporary paths and inert host-command stubs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class CNISmokePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'etc/cni/net.d/87-podman-bridge.conflist'
        self.config.parent.mkdir(parents=True)
        self.original = json.dumps({'name': 'podman', 'plugins': [{'type': 'bridge', 'bridge': 'cni-podman0'}, {'type': 'portmap'}]}).encode()
        self.config.write_bytes(self.original)
        self.config.chmod(0o640)
        self.runner_temp = self.root / 'runner-temp'
        self.runner_temp.mkdir()
        self.backup = self.runner_temp / 'railshot-cni-smoke-podman/87-podman-bridge.conflist'
        source = Path(__file__).with_name('prepare-cni-smoke.sh').read_text()
        for prefix in ('/etc/', '/var/lib/', '/usr/local/bin/'):
            source = source.replace(prefix, str(self.root) + prefix)
        self.script = self.root / 'helper.sh'
        self.script.write_text(source)
        binaries = self.root / 'bin'
        binaries.mkdir()
        for name, body in {
            'id': 'echo 0', 'uname': 'echo Linux',
            'podman': 'test "$*" = "ps --all --quiet" || exit 90; printf "%s" "${PODMAN_CONTAINERS:-}"; exit "${PODMAN_EXIT:-0}"',
            'ip': 'test "$*" = "-j link show" || exit 91; printf "%s" "$LINKS"',
        }.items():
            path = binaries / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(binaries) + ':' + os.environ['PATH'],
                    'RUNNER_TEMP': str(self.runner_temp), 'GITHUB_ACTIONS': 'true',
                    'RUNNER_ENVIRONMENT': 'github-hosted', 'LINKS': '[]',
                    'PODMAN_CONTAINERS': '', 'PODMAN_EXIT': '0'}

    def run_helper(self, action, **env):
        return subprocess.run(['bash', str(self.script), action], env={**self.env, **env}, capture_output=True, text=True)

    def test_roundtrip_preserves_only_known_file_and_its_mode(self):
        other = self.config.with_name('unrelated.conflist')
        other.write_text('untouched')
        result = self.run_helper('prepare')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.backup.read_bytes(), self.original)
        result = self.run_helper('restore')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(other.read_text(), 'untouched')
        self.assertEqual(self.run_helper('restore').returncode, 0)

    def test_refuses_non_hosted_active_or_unverifiable_podman(self):
        for env in ({'GITHUB_ACTIONS': 'false'}, {'RUNNER_ENVIRONMENT': 'self-hosted'},
                    {'PODMAN_CONTAINERS': 'container-id'}, {'PODMAN_EXIT': '42'},
                    {'LINKS': '[{"ifname":"cni-podman0"}]'}, {'LINKS': 'invalid-json'}):
            with self.subTest(env=env):
                self.assertNotEqual(self.run_helper('prepare', **env).returncode, 0)
                self.assertEqual(self.config.read_bytes(), self.original)
                self.assertFalse(self.backup.exists())
        self.config.write_text('{"name":"other-network"}')
        self.assertNotEqual(self.run_helper('prepare').returncode, 0)
        self.assertFalse(self.backup.exists())

    def test_restore_preserves_backup_if_teardown_failed_or_destination_changed(self):
        self.assertEqual(self.run_helper('prepare').returncode, 0)
        cluster = self.root / 'var/lib/rancher/k3s'
        cluster.mkdir(parents=True)
        self.assertNotEqual(self.run_helper('restore').returncode, 0)
        self.assertEqual(self.backup.read_bytes(), self.original)
        cluster.rmdir()
        self.config.write_text('new-owner')
        self.assertNotEqual(self.run_helper('restore').returncode, 0)
        self.assertEqual(self.config.read_text(), 'new-owner')
        self.assertEqual(self.backup.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
