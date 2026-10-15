"""Offline control contract tests; no cloud credentials or endpoints are used."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from infrastructure.providers.gcp.adapter import Adapter
from infrastructure.providers.gcp.auth import Session, credential_identity
from infrastructure.providers.gcp.config import config_template, validate_config
from infrastructure.providers.gcp.native import GcpError, digest, save
from infrastructure.providers.gcp.terraform import Terraform


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(); self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.context = {'environment_id': 'env-' + 'a' * 24, 'owner_id': 'account:owner', 'account_id': 'gcp-customer',
            'credential_ref': 'customer-role', 'policy_revision': 'v1', 'state_dir': str(self.root), 'operation_id': 'operation-1'}
        self.project = 'railshot-project'
        self.email = 'fixture@' + self.project + '.iam.gserviceaccount.com'
        self.credential = self.root / 'adc.json'
        save(self.credential, {'type': 'service_account', 'project_id': self.project, 'client_email': self.email,
            'private_key': '-----BEGIN PRIVATE KEY-----fixture-----END PRIVATE KEY-----', 'token_uri': 'https://oauth2.googleapis.com/token'})
        self.config = {'project_id': self.project, 'project_number': '123456789012',
            'auth': {'mode': 'managed_service_account', 'credentials_file': str(self.credential), 'service_account': self.email}}
        self.adapter = Adapter(self.config)

    def wif(self):
        token = self.root / 'token.txt'; token.write_text('subject-v1'); token.chmod(0o600)
        value = {'type': 'external_account', 'audience': '//iam.googleapis.com/projects/123456789012/locations/global/workloadIdentityPools/customers/providers/company',
            'subject_token_type': 'urn:ietf:params:oauth:token-type:jwt', 'token_url': 'https://sts.googleapis.com/v1/token',
            'service_account_impersonation_url': 'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/' + self.email + ':generateAccessToken', 'credential_source': {'file': str(token)}}
        save(self.credential, value); self.config['auth']['mode'] = 'workload_identity'
        return token, value

    def test_wif_rotation_keeps_authority_and_drops_ambient(self):
        token, value = self.wif(); session = Session(self.config['auth'], self.project, self.root / 'auth')
        with patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY': 'ambient', 'GOOGLE_OAUTH_ACCESS_TOKEN': 'ambient', 'CLOUDSDK_AUTH_ACCESS_TOKEN': 'ambient'}):
            env = session.env()
        token.write_text('subject-v2'); self.assertEqual(session.env(), env)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env); self.assertNotIn('GOOGLE_OAUTH_ACCESS_TOKEN', env)
        value['audience'] += '-changed'; save(self.credential, value)
        with self.assertRaisesRegex(GcpError, 'AUTHORITY_CHANGED'): session.env()

    def test_wif_rejects_executable_and_foreign_impersonation(self):
        _, value = self.wif(); value['credential_source'] = {'executable': {'command': 'untrusted'}}; save(self.credential, value)
        with self.assertRaisesRegex(GcpError, 'FILE_SOURCE_REQUIRED'): credential_identity(self.credential, self.project)
        _, value = self.wif(); value['service_account_impersonation_url'] = value['service_account_impersonation_url'].replace(self.project, 'foreign-project'); save(self.credential, value)
        with self.assertRaisesRegex(GcpError, 'SCOPE_MISMATCH'): credential_identity(self.credential, self.project)

    def test_plan_requires_budget_before_live_connection(self):
        with patch('infrastructure.providers.gcp.adapter.require_config'), patch.object(self.adapter, '_spec'), patch.object(self.adapter, 'connection') as connection:
            with self.assertRaises(KeyError): self.adapter.plan({'database': {'mode': 'none'}}, self.context)
            connection.assert_not_called()

    def test_empty_template_is_not_valid_authorization(self):
        template = config_template(self.root)
        self.assertFalse(validate_config(template['adapter'])['valid'])
        self.assertEqual(template['files']['gcp-budget.json']['approval_id'], '')
        self.assertIsNone(template['files']['gcp-budget.json']['max_cost_usd'])

    def test_project_number_readback_is_required(self):
        session = Mock(); session.call.return_value = {'projectId': self.project, 'projectNumber': '999999999999', 'lifecycleState': 'ACTIVE'}
        with patch.object(self.adapter, '_session', return_value=session):
            with self.assertRaisesRegex(GcpError, 'IDENTITY_MISMATCH'): self.adapter.connection(self.context)

    def node(self, role='control'):
        name = self.adapter._name(self.context) + ('-db' if role == 'database' else '-cp')
        return {'target_id': name, 'instance_id': '10001', 'location': {'zone': 'asia-northeast3-a'},
            'resource_id': 'projects/' + self.project + '/zones/asia-northeast3-a/instances/' + name,
            'addresses': {'private': '10.76.0.14' if role == 'database' else '10.76.0.2', 'public': '8.8.4.4'}}

    def observation(self, node, state='RUNNING'):
        return {'id': node['instance_id'], 'status': state, 'labels': {'environment': self.context['environment_id'], 'owner': digest(self.context['owner_id'])[:32]},
            'networkInterfaces': [{'networkIP': node['addresses']['private'], 'accessConfigs': [{'natIP': node['addresses']['public']}]}]}

    def test_native_identity_and_static_addresses_are_pinned(self):
        node = self.node(); session = Mock(); row = self.observation(node); session.call.return_value = row
        self.assertEqual(self.adapter._observe(self.context, session, {'control': node})[0]['state'], 'running')
        for change in ('owner', 'public', 'id'):
            changed = copy.deepcopy(row)
            if change == 'owner': changed['labels']['owner'] = 'foreign'
            elif change == 'public': changed['networkInterfaces'][0]['accessConfigs'][0]['natIP'] = '8.8.8.8'
            else: changed['id'] = '10002'
            session.call.return_value = changed
            with self.assertRaises(GcpError): self.adapter._observe(self.context, session, {'control': node})

    def test_stopped_describe_never_reuses_ready_receipt(self):
        home = self.adapter._home(self.context); node = self.node(); save(home / 'nodes.json', {'control': node})
        save(home / 'runtime-result.json', {'runtime_ready': True, 'storage_ready': True, 'edge_ready': True})
        session = Mock(); session.call.return_value = self.observation(node, 'TERMINATED')
        with patch.object(self.adapter, '_session', return_value=session): result = self.adapter.describe(self.context)
        self.assertEqual(result['state'], 'stopped'); self.assertTrue(result['stopped_verified'])
        self.assertFalse(any(result['readiness'].values()))

    def test_database_static_ip_and_dedicated_identity(self):
        self.assertEqual(self.adapter._database_ip(), '10.76.0.14')
        self.config.update(boot_image='image', ssh_public_key='key', management_source_cidrs=['8.8.8.8/32'], database_config_file=str(self.root / 'pg.json'))
        save(self.root / 'pg.json', {'backup': {'type': 'gcs', 'identity': 'instance', 'bucket': 'dedicated-pg-backups'}})
        modules = []
        def tf(context, name, module, session, extra):
            obj = Mock(); obj.home = self.root / name
            def apply(variables):
                overrides = json.loads(extra['environment_override.tf.json']); modules.append((name, variables, overrides))
                node = self.node('database' if name.endswith('database') else 'control'); return {'node_descriptor': node}
            obj.apply.side_effect = apply; return obj
        with patch.object(self.adapter, '_tf', side_effect=tf): self.adapter._nodes({'database': {'mode': 'standalone'}}, self.context, Mock(), {'name': 'vpc', 'subnet_name': 'subnet'})
        self.assertEqual(len(modules), 4)
        for name, variables, override in modules:
            instance = override['resource']['google_compute_instance']['node']
            self.assertEqual(bool(variables['k3s_control_source_cidrs']), name == 'nodes/control')
            if name == 'nodes/database':
                self.assertEqual(instance['network_interface'][0]['network_ip'], '10.76.0.14')
                self.assertIn('google_storage_bucket_iam_member', override['resource'])
            else:
                self.assertNotIn('service_account', instance); self.assertNotIn('google_storage_bucket_iam_member', override['resource'])

    def test_unknown_terraform_apply_is_not_repeated(self):
        session = Mock(); session.env.return_value = {}; runner = Mock()
        tf = Terraform(self.root / 'tf', 'gcp-network', session, {'owner': 'self'}, runner=runner)
        save(tf.home / 'journal.json', {'variables_sha256': digest({'name': 'vpc'}), 'status': 'unknown'})
        with self.assertRaisesRegex(GcpError, 'RECONCILIATION_REQUIRED'): tf.apply({'name': 'vpc'})
        runner.assert_not_called()

    def test_common_port_uses_dict_action_and_no_native_call_on_invalid(self):
        with patch.object(self.adapter, 'describe', return_value={'status': 'blocked'}) as describe:
            self.assertEqual(self.adapter.operate({'action': 'verify'}, self.context), {'status': 'blocked'})
            describe.assert_called_once_with(self.context)
        with self.assertRaisesRegex(GcpError, 'OPERATION_INVALID'): self.adapter.operate('stop', self.context)

    def test_real_common_control_plan_and_blocked_readiness_projection(self):
        from infrastructure.providers.environments.control import ControlPlane
        path = self.root / 'control.json'
        save(path, {'version': 1, 'policy_revision': 'v1', 'state_dir': str(self.root / 'control-state'), 'accounts': [{
            'id': 'gcp-customer', 'owner_id': 'account:owner', 'provider': 'gcp', 'ownership': 'owned',
            'credential_ref': 'customer-role', 'regions': ['asia-northeast3'], 'allow_apply': True, 'adapter': self.config}]})
        plane = ControlPlane(path, lambda *_: self.adapter)
        spec = {'version': 1, 'name': 'gcp-test', 'provider': 'gcp', 'account_id': 'gcp-customer', 'region': 'asia-northeast3',
            'scope': 'shared', 'runtime': {'kind': 'kubeadm', 'control_planes': 1, 'workers': 2}, 'database': {'mode': 'none'}}
        with patch.object(self.adapter, '_spec'), patch.object(self.adapter, 'connection'), patch('infrastructure.providers.gcp.adapter.budget', return_value={'max_cost_usd': 100}):
            planned = plane.dispatch({'owner_id': 'account:owner', 'action': 'plan', 'spec': spec, 'idempotency_key': 'plan-test'})
        self.assertEqual(planned['plan']['changes'][1], {'kind': 'compute', 'action': 'create', 'count': 3})
        with patch.object(self.adapter, 'apply', return_value={'status': 'succeeded', 'readiness': {'infrastructure': True}, 'resources': []}):
            result = plane.dispatch({'owner_id': 'account:owner', 'action': 'apply', 'environment_id': planned['id'], 'idempotency_key': 'apply-test'})
        self.assertFalse(result['deployable']); self.assertEqual(result['status'], 'blocked')
        self.assertNotIn('credentials_file', json.dumps(result)); self.assertNotIn(str(self.root), json.dumps(result))

    def test_registry_port_translates_native_unknown(self):
        from infrastructure.providers.environments.gcp import Adapter as Port
        from infrastructure.providers.environments.contract import ProviderError
        adapter = Port(self.config)
        with patch.object(adapter, '_session', side_effect=GcpError('GCP_NATIVE_TIMEOUT', unknown=True)):
            with self.assertRaises(ProviderError) as caught: adapter.connection(self.context)
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(caught.exception.code, 'GCP_NATIVE_TIMEOUT')


if __name__ == '__main__': unittest.main()
