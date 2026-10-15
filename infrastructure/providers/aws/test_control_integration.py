"""Actual common dispatcher and AWS registry adapter; only native boundaries are replaced."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infrastructure.providers.aws.native import AwsError, save
from infrastructure.providers.environments.aws import Adapter
from infrastructure.providers.environments.control import ControlPlane, read_private
from infrastructure.providers.environments.contract import ProviderError

SPEC = {'version': 1, 'name': 'integration', 'provider': 'aws', 'account_id': 'aws-user', 'region': 'ap-northeast-2', 'scope': 'shared', 'runtime': {'kind': 'kubeadm', 'control_planes': 1, 'workers': 2}, 'database': {'mode': 'none'}}

class CommonAwsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        save(self.home / 'control.json', {'version': 1, 'policy_revision': 'v1', 'state_dir': str(self.home / 'state'), 'accounts': [{'id': 'aws-user', 'provider': 'aws', 'ownership': 'owned', 'owner_id': 'account:alice', 'credential_ref': 'role-alice', 'regions': ['ap-northeast-2'], 'allow_apply': True, 'adapter': {'account_id': '123456789012'}}]})
        self.control = ControlPlane(self.home / 'control.json')
    def call(self, action, **kwargs):
        return self.control.dispatch({'action': action, 'owner_id': 'account:alice', **kwargs})
    def plan(self):
        with patch.object(Adapter, '_spec', return_value=['ap-northeast-2a', 'ap-northeast-2c']), patch.object(Adapter, 'connection', return_value={'authenticated': True}), patch('infrastructure.providers.aws.adapter.budget', return_value={'approval_id': 'local-fixture'}):
            return self.call('plan', spec=SPEC, idempotency_key='plan-1')
    def test_actual_plan_and_private_projection(self):
        result = self.plan()
        self.assertEqual(len(result['id']), 28)
        self.assertEqual(next(x['count'] for x in result['plan']['changes'] if x['kind'] == 'compute'), 3)
        self.assertNotIn('local-fixture', str(result))
        self.assertFalse(result['deployable'])
    def test_native_failure_translates_and_is_never_replayed(self):
        result = self.plan()
        with patch.object(Adapter, '_spec', side_effect=AwsError('AWS_NATIVE_TIMEOUT', unknown=True)) as native:
            row = self.call('apply', environment_id=result['id'], idempotency_key='apply-1')
            self.assertEqual(row['status'], 'unknown')
            self.assertEqual(row['operations'][0]['error'], 'AWS_NATIVE_TIMEOUT')
            self.call('apply', environment_id=result['id'], idempotency_key='apply-1')
            self.assertEqual(native.call_count, 1)
    def test_real_aws_delete_receipt_satisfies_common_residual_contract(self):
        row = self.plan()
        home = self.home / 'state' / row['id'] / 'aws'; home.mkdir(mode=0o700)
        save(home / 'deleted.json', {'status': 'succeeded', 'deleted': True, 'residuals': [], 'retained': [{'kind': 'efs', 'id': 'fs-retained', 'status': 'retained'}]})
        with patch.object(Adapter, '_session', return_value=object()):
            result = self.call('delete', environment_id=row['id'], idempotency_key='delete-1')
        self.assertEqual(result['status'], 'deleted')
        self.assertEqual(result['retained'][0]['id'], 'fs-retained')
    def test_native_error_translation_preserves_unknown(self):
        with patch.object(Adapter, '_session', side_effect=AwsError('AWS_SESSION_TIMEOUT', unknown=True)):
            with self.assertRaises(ProviderError) as caught:
                self.call('connection', account_id='aws-user')
        self.assertTrue(caught.exception.outcome_unknown)

if __name__ == '__main__': unittest.main()
