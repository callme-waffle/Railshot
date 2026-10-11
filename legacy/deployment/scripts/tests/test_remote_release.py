"""SSM send-once/readback behavior, without network or credentials."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('remote_release', Path(__file__).parents[1] / 'remote_release.py')
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)
SHA = 'a' * 40
COMMAND = '12345678-1234-1234-1234-123456789abc'
IMAGES = {name: 'ghcr.io/jasmin-softbank/railshot-' + name + '@sha256:' + 'c' * 64 for name in ('api', 'dashboard', 'ci-runner')}


class RemoteReleaseTests(unittest.TestCase):
    def proof(self):
        return {'status': 'verified', 'source_sha': SHA,
                'targets': [{'provider': name, 'target_id': 'k3s-' + name, 'source_sha': SHA, 'status': 'verified'}
                            for name in ('aws', 'gcp', 'openstack')]}

    def run_remote(self, proof=None, pending=0, poll_error=False, send_error=False, ticks=None, scope='multicloud'):
        self.calls = []
        self.pauses = []
        pending_states = iter(['Pending', 'InProgress', 'Delayed'][:pending])

        def call(*args):
            self.calls.append(args)
            if args[0] == 'send-command':
                if send_error:
                    raise RuntimeError('uncertain send')
                return {'Command': {'CommandId': COMMAND}}
            if poll_error:
                raise RuntimeError('readback unavailable')
            status = next(pending_states, None)
            return {'Status': status} if status else {'Status': 'Success', 'ResponseCode': 0,
                    'StandardOutputContent': json.dumps(proof if proof is not None else self.proof())}

        return remote.release(SHA, 'b' * 40, IMAGES, '1', 'd' * 64, '12', '1', call=call,
                              scope=scope, sleep=self.pauses.append, clock=(lambda: next(ticks)) if ticks else lambda: 0)

    def test_ci_scope_requires_same_source_images_execution_and_platform_ref(self):
        proof = {'scope': 'ci-runtime', 'status': 'verified', 'source_sha': SHA,
                 'workers': {'status': 'verified', 'source_sha': SHA, 'images': IMAGES,
                             'executable_verification': True, 'runner_jobs': 'preserved'},
                 'apps': {'status': 'verified', 'source_sha': SHA, 'platform_ref': SHA}}
        self.assertEqual(self.run_remote(proof, scope='ci-runtime')['status'], 'verified')
        parameters = json.loads(self.calls[0][-1])
        self.assertEqual(parameters['Scope'], ['ci-runtime'])
        for section, key, value in (('apps', 'platform_ref', 'f' * 40), ('workers', 'images', {}),
                                    ('workers', 'executable_verification', False), ('workers', 'status', 'declarations_verified')):
            changed = copy.deepcopy(proof); changed[section][key] = value
            self.assertEqual(self.run_remote(changed, scope='ci-runtime')['status'], 'incomplete')
        self.assertEqual(self.run_remote(proof)['status'], 'incomplete', 'CI proof must not satisfy provider rollout')

    def test_send_once_pending_then_bound_success(self):
        result = self.run_remote(pending=3)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['command_id'], COMMAND)
        self.assertEqual(self.pauses, [10, 10, 10])
        sends = [call for call in self.calls if call[0] == 'send-command']
        self.assertEqual(len(sends), 1)
        self.assertIn(remote.DOCUMENT, sends[0])
        self.assertIn('--document-hash-type', sends[0])

    def test_wrong_source_missing_provider_failed_and_malformed_proofs_fail_closed(self):
        original = self.proof()
        wrong = copy.deepcopy(original); wrong['targets'][0]['source_sha'] = 'f' * 40
        failed = copy.deepcopy(original); failed['targets'][0]['status'] = 'unknown'
        for proof in ([1], {}, {'status': 'verified', 'targets': None}, {**original, 'targets': original['targets'][:2]}, wrong, failed):
            with self.subTest(proof=proof):
                result = self.run_remote(proof)
                self.assertEqual(result['status'], 'incomplete')
                self.assertEqual(result['command_id'], COMMAND)

    def test_uncertain_send_is_not_retried(self):
        with self.assertRaises(RuntimeError):
            self.run_remote(send_error=True)
        self.assertEqual(len(self.calls), 1)

    def test_read_error_and_timeout_preserve_sent_command_id(self):
        result = self.run_remote(poll_error=True)
        self.assertEqual((result['status'], result['command_id']), ('unknown', COMMAND))
        result = self.run_remote(ticks=iter([0, 4801]))
        self.assertEqual(result['code'], 'RELEASE_TIMEOUT_READBACK_REQUIRED')
        self.assertEqual(result['command_id'], COMMAND)
        self.assertEqual(len(self.calls), 1)

    def test_invocation_not_yet_visible_maps_to_pending(self):
        missing = SimpleNamespace(returncode=255, stderr='InvocationDoesNotExist', stdout='')
        with patch.object(remote.verifier.subprocess, 'run', return_value=missing) as native:
            self.assertEqual(remote.verifier.aws('get-command-invocation', '--command-id', COMMAND), {'Status': 'Pending'})
        self.assertEqual(native.call_count, 1)


if __name__ == '__main__':
    unittest.main()
