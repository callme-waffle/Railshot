import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from infrastructure.providers.environments.contract import ProviderError, validate_spec
from infrastructure.providers.environments.control import ControlPlane, read_private, save

SPEC = {'version': 1, 'name': 'test-env', 'provider': 'aws', 'account_id': 'aws-test', 'region': 'ap-northeast-2',
        'scope': 'shared', 'runtime': {'kind': 'kubeadm', 'control_planes': 1, 'workers': 2}, 'database': {'mode': 'none'}}


class Adapter:
    def __init__(self):
        self.calls = 0
        self.failure = False
        self.result = {'status': 'succeeded', 'readiness': dict.fromkeys(('infrastructure', 'cluster', 'storage', 'public_access', 'deployment_binding'), True),
                       'resources': [{'id': 'instance-id', 'kind': 'vm', 'secret': 'never-public'}], 'application_config_path': '/private/app.json'}
    def plan(self, spec, context):
        return {'changes': [{'kind': 'vm', 'action': 'create', 'count': 3}], 'secret': 'not-public'}
    def apply(self, spec, context):
        self.calls += 1
        if self.failure:
            raise RuntimeError('sensitive native error')
        return self.result
    def describe(self, context):
        return self.result
    def delete(self, context):
        return {'status': 'succeeded', 'deleted': True, 'residuals': []}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.config = self.home / 'config.json'
        self.adapter = Adapter()
        save(self.config, {'version': 1, 'policy_revision': 'v1', 'state_dir': str(self.home / 'state'), 'accounts': [
            {'id': 'aws-test', 'owner_id': 'account:alice', 'provider': 'aws', 'ownership': 'owned',
             'credential_ref': 'role-alice', 'regions': ['ap-northeast-2'], 'allow_apply': True,
             'adapter': {'role_file': '/private/rotatable-role.json'}}]})
        self.control = ControlPlane(self.config, lambda *_: self.adapter)
    def tearDown(self):
        self.temp.cleanup()
    def call(self, action, **kwargs):
        return self.control.dispatch({'owner_id': 'account:alice', 'action': action, **kwargs})
    def plan(self):
        return self.call('plan', spec=SPEC, idempotency_key='plan-1')['id']
    def test_admission_is_durable_before_execution_and_replay_does_not_reapply(self):
        environment = self.plan()
        row = self.call('admit', environment_id=environment, operation_action='apply', idempotency_key='apply-1')
        self.assertEqual(row['status'], 'queued'); self.assertEqual(self.adapter.calls, 0)
        row = self.call('apply', environment_id=environment, idempotency_key='apply-1')
        self.assertEqual(row['status'], 'ready'); self.assertFalse(row['readiness']['application_http'])
        self.call('apply', environment_id=environment, idempotency_key='apply-1')
        self.assertEqual(self.adapter.calls, 1)
        with self.assertRaisesRegex(ProviderError, 'IDEMPOTENCY_CONFLICT'):
            self.call('admit', environment_id=environment, operation_action='delete', idempotency_key='apply-1')
    def test_cross_owner_account_and_environment_are_not_disclosed(self):
        environment = self.plan()
        for request in ({'action': 'get', 'environment_id': environment}, {'action': 'plan', 'spec': SPEC, 'idempotency_key': 'plan-2'}):
            with self.assertRaises(ProviderError) as caught:
                self.control.dispatch({'owner_id': 'account:bob', **request})
            self.assertEqual(caught.exception.status, 404)
    def test_unknown_blocks_new_dispatch_until_actual_describe_proves_readiness(self):
        environment = self.plan(); self.adapter.failure = True
        row = self.call('apply', environment_id=environment, idempotency_key='apply-1')
        self.assertEqual(row['status'], 'unknown')
        self.assertNotIn('sensitive', json.dumps(row))
        with self.assertRaisesRegex(ProviderError, 'RECONCILIATION_REQUIRED'):
            self.call('apply', environment_id=environment, idempotency_key='apply-2')
        self.adapter.failure = False
        self.assertEqual(self.call('reconcile', environment_id=environment)['status'], 'ready')
        self.assertEqual(self.adapter.calls, 1)
    def test_lost_process_marker_is_unknown_and_never_replayed(self):
        environment = self.plan()
        self.call('admit', environment_id=environment, operation_action='apply', idempotency_key='apply-1')
        path = self.home / 'state' / (environment + '.json')
        row = read_private(path); row['status'] = 'running'
        next(iter(row['operations'].values()))['status'] = 'running'; save(path, row)
        self.assertEqual(self.call('apply', environment_id=environment, idempotency_key='apply-1')['status'], 'unknown')
        self.assertEqual(self.adapter.calls, 0)
    def test_policy_binding_revocation_and_secret_rotation_are_distinct(self):
        environment = self.plan()
        # Rotating bytes behind role_file does not change the stable policy binding.
        self.assertEqual(self.call('get', environment_id=environment)['status'], 'planned')
        config = read_private(self.config); config['policy_revision'] = 'v2'; save(self.config, config)
        control = ControlPlane(self.config, lambda *_: self.adapter)
        with self.assertRaisesRegex(ProviderError, 'ACCOUNT_POLICY_CHANGED'):
            control.dispatch({'owner_id': 'account:alice', 'action': 'apply', 'environment_id': environment, 'idempotency_key': 'apply-1'})
    def test_public_projection_does_not_expose_private_paths_or_native_fields(self):
        environment = self.plan()
        row = self.call('apply', environment_id=environment, idempotency_key='apply-1')
        self.assertNotIn('secret', json.dumps(row)); self.assertNotIn('/private', json.dumps(row))
        self.assertEqual(row['plan']['changes'], [{'kind': 'vm', 'action': 'create', 'count': 3}])
    def test_missing_readiness_cannot_become_deployable(self):
        self.adapter.result['readiness']['cluster'] = False
        row = self.call('apply', environment_id=self.plan(), idempotency_key='apply-1')
        self.assertFalse(row['deployable']); self.assertEqual(row['status'], 'blocked')
    def test_deleted_requires_residual_observation(self):
        self.adapter.delete = lambda _: {'status': 'succeeded'}
        row = self.call('delete', environment_id=self.plan(), idempotency_key='delete-1')
        self.assertEqual(row['status'], 'unknown')
    def test_strict_spec_shapes(self):
        for field, value in [('provider', []), ('scope', {}), ('version', True)]:
            spec = copy.deepcopy(SPEC); spec[field] = value
            with self.assertRaisesRegex(ProviderError, 'INVALID_ENVIRONMENT_SPEC'):
                validate_spec(spec)
        spec = copy.deepcopy(SPEC); spec['runtime']['control_planes'] = True
        with self.assertRaisesRegex(ProviderError, 'INVALID_ENVIRONMENT_SPEC'):
            validate_spec(spec)

    def test_queued_admission_recovers_without_replaying_started_mutation(self):
        environment = self.plan()
        self.call('admit', environment_id=environment, operation_action='apply', idempotency_key='recover-1')
        pending = self.call('pending')['items']
        self.assertEqual(len(pending), 1)
        self.call(pending[0]['action'], environment_id=environment, idempotency_key=pending[0]['idempotency_key'])
        self.assertEqual(self.adapter.calls, 1)
        self.assertEqual(self.call('pending')['items'], [])

    def test_reconciled_partial_environment_can_be_cleaned_up(self):
        environment = self.plan(); self.adapter.failure = True
        self.call('apply', environment_id=environment, idempotency_key='apply-1')
        self.adapter.result = {'status': 'blocked', 'ownership_verified': True, 'mutation_in_progress': False,
                               'readiness': {}, 'resources': [{'kind': 'vm', 'id': 'owned-vm'}]}
        self.assertEqual(self.call('reconcile', environment_id=environment)['status'], 'blocked')
        self.assertEqual(self.call('delete', environment_id=environment, idempotency_key='cleanup-1')['status'], 'deleted')

    def test_missing_frontend_readback_prevents_deployment(self):
        self.adapter.result['readiness']['public_access'] = False
        row = self.call('apply', environment_id=self.plan(), idempotency_key='apply-1')
        self.assertFalse(row['deployable'])


if __name__ == '__main__':
    unittest.main()
