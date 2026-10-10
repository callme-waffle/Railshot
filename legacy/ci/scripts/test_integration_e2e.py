"""Hosted smoke ownership/finally checks; no runtime, Kubernetes or cloud commands."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import integration_e2e as smoke


class HostedRuntimeTest(unittest.TestCase):
    def test_failed_deploy_cleans_only_its_owned_disposable_cluster(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            owner, cluster = root / 'host-owner.json', root / 'k3s'
            env = {'GITHUB_ACTIONS': 'true', 'RUNNER_ENVIRONMENT': 'github-hosted',
                   'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_JOB': 'runtime-smoke',
                   'GITHUB_SHA': 'a' * 40, 'GITHUB_WORKSPACE': str(smoke.ROOT), 'RUNNER_TEMP': str(root)}
            calls = []

            def swapoff(argv, **kwargs):
                self.assertEqual(argv, ['/sbin/swapoff', '-a'])
                self.assertTrue(owner.exists())
                self.assertFalse(cluster.exists())
                self.assertTrue(kwargs['check'])
                self.assertEqual(kwargs['timeout'], 30)
                calls.append('swapoff')

            def native(action, output, request, *, deadline=None):
                calls.append(action)
                self.assertEqual(request['runtime']['timeout_seconds'], 600)
                self.assertEqual(request['environment_id'], request['workload']['namespace'])
                if action == 'deploy':
                    self.assertIsNotNone(deadline)
                    cluster.write_text('partial owned installation')
                    raise RuntimeError('synthetic installation failure')
                self.assertEqual(action, 'cleanup')
                self.assertIsNone(deadline)
                cluster.unlink()
                return {'status': 'cleaned'}

            with patch.dict(os.environ, env, clear=True), patch.object(smoke.sys, 'platform', 'linux'), \
                    patch.object(smoke.os, 'geteuid', return_value=0), \
                    patch.object(smoke.platform, 'machine', return_value='x86_64'), \
                    patch.object(smoke, 'OWNER', owner), patch.object(smoke, 'CLUSTER', (cluster,)), \
                    patch.object(smoke, 'invoke', side_effect=native), \
                    patch.object(smoke.subprocess, 'run', side_effect=swapoff):
                # Root ownership is mandatory in production; emulate that stat in this non-root unit test.
                original_stat = Path.stat

                def stat(path, *args, **kwargs):
                    result = original_stat(path, *args, **kwargs)
                    if path == owner:
                        fields = list(result); fields[4] = 0
                        return os.stat_result(fields)
                    return result

                with patch.object(Path, 'stat', stat):
                    output = root / 'first'
                    with self.assertRaisesRegex(RuntimeError, 'synthetic installation failure'):
                        smoke.run(output)
                    self.assertEqual(calls, ['swapoff', 'deploy', 'cleanup'])
                    self.assertFalse(owner.exists()); self.assertFalse(cluster.exists())
                    receipt = json.loads((output / 'ownership.json').read_text())
                    self.assertEqual((receipt['smoke'], receipt['cleanup']), ('failed', 'succeeded'))
                    self.assertEqual(smoke.cleanup(output), {'cleanup': 'already_succeeded'})
                    with patch.dict(os.environ, {'GITHUB_RUN_ID': '456'}):
                        with self.assertRaisesRegex(ValueError, 'ownership differs'):
                            smoke.cleanup(output)
                    cluster.write_text('preexisting cluster')
                    with self.assertRaisesRegex(ValueError, 'existing cluster'):
                        smoke.run(root / 'second')
                    self.assertEqual(cluster.read_text(), 'preexisting cluster')
                    self.assertEqual(calls, ['swapoff', 'deploy', 'cleanup'])


if __name__ == '__main__':
    unittest.main()
