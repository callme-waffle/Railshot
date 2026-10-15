"""No AWS calls: exercise native boundaries, replay fences and authority checks."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from infrastructure.providers.aws.native import AwsError, Session, save, private_json
from infrastructure.providers.aws.terraform import Terraform
from infrastructure.providers.aws.adapter import Adapter
from infrastructure.providers.aws.config import budget

ACCOUNT = '123456789012'
ROLE = 'arn:aws:iam::123456789012:role/railshot'


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name); self.home.chmod(0o700)
        self.source = self.home / 'source.json'
        save(self.source, {'AWS_ACCESS_KEY_ID': 'source-key', 'AWS_SECRET_ACCESS_KEY': 'source-secret'})
        self.config = {'mode': 'customer_role', 'role_arn': ROLE, 'external_id': 'customer-unique', 'source_credentials_file': str(self.source)}
        self.time = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
        self.calls = []

    def runner(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if 'assume-role' in argv:
            return json.dumps({'Credentials': {'AccessKeyId': 'session-key', 'SecretAccessKey': 'session-secret', 'SessionToken': 'token', 'Expiration': (self.time + dt.timedelta(hours=1)).isoformat()}})
        return json.dumps({'Account': ACCOUNT})

    def test_customer_role_external_id_and_explicit_source(self):
        with patch.dict(os.environ, {'AWS_PROFILE': 'ambient-admin', 'AWS_SESSION_TOKEN': 'wrong', 'TF_VAR_bad': 'ambient'}):
            session = Session(self.config, ACCOUNT, runner=self.runner, now=lambda: self.time)
            env = session.env()
        self.assertIn('--external-id', self.calls[0][0])
        self.assertNotIn('AWS_PROFILE', env)
        self.assertNotIn('TF_VAR_bad', env)
        self.assertEqual(env['AWS_SESSION_TOKEN'], 'token')
        self.assertEqual(env['AWS_EC2_METADATA_DISABLED'], 'true')

    def test_renewal_before_long_child_and_secret_not_binding(self):
        session = Session(self.config, ACCOUNT, runner=self.runner, now=lambda: self.time)
        session.env(); self.time += dt.timedelta(minutes=26); session.env()
        self.assertEqual(sum('assume-role' in c[0] for c in self.calls), 2)
        self.assertEqual(session.config['role_arn'], ROLE)

    def test_wrong_account_never_usable(self):
        def runner(argv, **kw):
            return self.runner(argv, **kw) if 'assume-role' in argv else '{"Account":"999999999999"}'
        with self.assertRaisesRegex(AwsError, 'SCOPE'):
            Session(self.config, ACCOUNT, runner=runner, now=lambda: self.time).env()

    def test_other_account_role_and_missing_external_id_rejected(self):
        with self.assertRaises(AwsError): Session({**self.config, 'role_arn': ROLE.replace(ACCOUNT, '999999999999')}, ACCOUNT)
        with self.assertRaises(AwsError): Session({**self.config, 'external_id': ''}, ACCOUNT)

    def test_managed_mode_still_assumes_exact_role(self):
        session = Session({**self.config, 'mode': 'managed_role'}, ACCOUNT, runner=self.runner, now=lambda: self.time)
        session.env()
        self.assertNotIn('--external-id', self.calls[0][0])
        self.assertEqual(self.calls[0][0][self.calls[0][0].index('--role-arn') + 1], ROLE)

    def test_budget_expiry_and_scope(self):
        path = self.home / 'budget.json'
        receipt = {'version': 1, 'approval_id': 'review-1', 'account_id': ACCOUNT, 'region': 'ap-northeast-2',
            'expires_at': '2099-01-01T00:00:00Z', 'max_cost_usd': 20, 'estimated_cost_usd': 10, 'duration_hours': 2,
            'node_count': 3, 'elastic_ip_count': 3, 'instance_type': 't3.large', 'data_disk_gib': 100, 'pricing_basis': 'operator quote including EC2/EBS/EFS/ALB/DNS/publicIPv4', 'data_policy': 'retain', 'ledger': {'ledger_path': '/private/ledger.sqlite', 'scope': 'aws-sandbox', 'currency': 'USD', 'incremental_cost': '10', 'limit': '20', 'unreported_cost': '0', 'quoted_at': '2026-10-15T00:00:00Z', 'expires_at': '2099-01-01T00:00:00Z'}}
        config = {'account_id': ACCOUNT, 'budget_file': str(path)}
        save(path, receipt)
        self.assertEqual(budget(config, {'database': {'mode': 'none'}})['max_cost_usd'], 20)
        with self.assertRaises(AwsError): budget(config, {'database': {'mode': 'standalone'}})
        save(path, {**receipt, 'expires_at': '2000-01-01T00:00:00Z'})
        with self.assertRaises(AwsError): budget(config, {'database': {'mode': 'none'}})


class TerraformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'stage'; self.calls = []
        self.session = type('Session', (), {'env': lambda _: {}})()
        self.actions, self.fail_apply = ['create'], False
        self.tf = Terraform(self.home, 'aws-network', self.session, {'environment_id': 'demo'}, runner=self.runner)

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == 'plan': (self.home / 'reviewed.plan').write_bytes(b'bound-native-plan')
        if argv[1] == 'show': return json.dumps({'resource_changes': [{'address': 'aws_vpc.environment', 'change': {'actions': self.actions}}]})
        if argv[1] == 'apply' and self.fail_apply: raise AwsError('TIMEOUT', unknown=True)
        if argv[1] == 'output': return '{"network":{"value":{"vpc_id":"vpc-12345678"}}}'
        return ''

    def test_destructive_plan_never_applied(self):
        self.actions = ['delete', 'create']
        with self.assertRaisesRegex(AwsError, 'DESTRUCTIVE'): self.tf.apply({'x': 1})
        self.assertFalse(any(c[1] == 'apply' for c in self.calls))

    def test_lost_apply_is_fenced_before_another_plan(self):
        self.fail_apply = True
        with self.assertRaises(AwsError): self.tf.apply({'x': 1})
        count = len(self.calls)
        with self.assertRaisesRegex(AwsError, 'OUTCOME_UNKNOWN'): self.tf.apply({'x': 1})
        self.assertEqual(len(self.calls), count)

    def test_saved_plan_tamper_prevents_mutation(self):
        receipt = self.tf.plan({'x': 1})
        (self.home / 'reviewed.plan').write_bytes(b'changed')
        with self.assertRaisesRegex(AwsError, 'PLAN_CHANGED'): self.tf.apply_saved(receipt)
        self.assertFalse(any(c[1] == 'apply' for c in self.calls))

    def test_binding_change_does_not_adopt_state(self):
        with self.assertRaisesRegex(AwsError, 'BINDING_CHANGED'):
            Terraform(self.home, 'aws-network', self.session, {'environment_id': 'other'}, runner=self.runner)


class OwnershipTests(unittest.TestCase):
    def test_registry_account_alias_is_not_cloud_account(self):
        self.assertEqual(Adapter({'account_id': ACCOUNT})._scope({'account_id': 'customer-a'}), ACCOUNT)

    def test_foreign_owned_instance_not_mutated(self):
        adapter = Adapter({'account_id': ACCOUNT})
        desc = {'control': {'instance_id': 'i-12345678901234567', 'resource_id': 'arn', 'addresses': {'private': '10.72.1.2'}}}
        class Cloud:
            def call(self, *args):
                return {'Reservations': [{'Instances': [{'InstanceId': 'i-12345678901234567', 'PrivateIpAddress': '10.72.1.2', 'Tags': [{'Key': 'Environment', 'Value': 'other'}]}]}]}
        with self.assertRaisesRegex(AwsError, 'OWNERSHIP'):
            adapter._observe({'environment_id': 'demo', 'owner_id': 'owner', 'account_id': 'account-alias'}, Cloud(), desc)

    def test_stopped_instance_still_requires_its_reserved_management_ip(self):
        adapter = Adapter({'account_id': ACCOUNT}); cloud = Mock()
        descriptor = {'instance_id': 'i-12345678901234567', 'resource_id': 'arn:aws:ec2:ap-northeast-2:' + ACCOUNT + ':instance/i-12345678901234567', 'addresses': {'private': '10.72.0.5', 'public': '8.8.8.8'}}
        instance = {'InstanceId': descriptor['instance_id'], 'PrivateIpAddress': '10.72.0.5', 'State': {'Name': 'stopped'}, 'Placement': {'AvailabilityZone': 'ap-northeast-2a'}, 'Tags': [{'Key': 'Environment', 'Value': 'env'}, {'Key': 'Owner', 'Value': 'owner'}]}
        cloud.call.side_effect = [{'Reservations': [{'Instances': [instance]}]}, {'Addresses': [{'InstanceId': descriptor['instance_id'], 'PrivateIpAddress': '10.72.0.5'}]}]
        context = {'environment_id': 'env', 'owner_id': 'owner', 'account_id': 'alias'}
        self.assertEqual(adapter._observe(context, cloud, {'control': descriptor})[0]['state'], 'stopped')
        cloud.call.side_effect = [{'Reservations': [{'Instances': [instance]}]}, {'Addresses': [{'InstanceId': 'i-other', 'PrivateIpAddress': '10.72.0.5'}]}]
        with self.assertRaisesRegex(AwsError, 'MANAGEMENT_ADDRESS'): adapter._observe(context, cloud, {'control': descriptor})


class PortContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.context = {'state_dir': self.tmp.name, 'environment_id': 'env-' + 'a' * 28,
            'owner_id': 'account:owner', 'account_id': 'customer-connection', 'credential_ref': 'customer-role',
            'policy_revision': 'v1', 'operation_id': 'op-stop'}
        self.adapter = Adapter({'account_id': ACCOUNT})

    def test_common_operation_object_returns_verified_stop_contract(self):
        home = self.adapter._home(self.context)
        save(home / 'nodes.json', {'control': {'instance_id': 'i-12345678901234567'}})
        calls = []
        class Cloud:
            def call(self, *args, **kwargs): calls.append(args); return {}
            def env(self): return {}
        with patch.object(self.adapter, '_session', return_value=Cloud()), patch.object(self.adapter, '_observe', return_value=[]), patch.object(self.adapter, 'describe', return_value={'status': 'succeeded', 'resources': [{'state': 'stopped'}]}):
            self.adapter.runner = lambda *args, **kwargs: ''
            first = self.adapter.operate({'action': 'stop'}, self.context)
            second = self.adapter.operate({'action': 'stop'}, self.context)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first['state'], 'stopped')
        self.assertTrue(first['stopped_verified'])

    def test_deleted_receipt_contains_residual_contract_on_replay(self):
        home = self.adapter._home(self.context)
        receipt = {'status': 'succeeded', 'deleted': True, 'residuals': [], 'retained_resources': {'efs': 'fs-retained'}}
        save(home / 'deleted.json', receipt)
        with patch.object(self.adapter, '_session', return_value=object()):
            self.assertEqual(self.adapter.delete(self.context), receipt)

    def test_database_exec_uses_rotatable_session_and_enrolled_hostkey(self):
        home = self.adapter._home(self.context)
        source = Path(self.tmp.name) / 'postgres-config.json'
        save(source, {'version': 1, 'backup': {'identity': 'instance'}})
        self.adapter.config.update(database_config_file=str(source), ssh_identity_file='/private/operator-key')
        class Session:
            def env(self): return {'AWS_SESSION_TOKEN': 'fresh-synthetic-session'}
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            request = private_json(home / 'database-request.json')
            self.assertEqual(request['action'], {'action': 'install'})
            generated = private_json(request['config_file'])
            self.assertEqual(generated['ssh']['known_hosts_file'], str(home / 'database.known_hosts'))
            return json.dumps({'status': 'succeeded', 'ready': True, 'application_bundle_file': '/private/owned-bundle'})
        self.adapter.runner = runner
        result = self.adapter._database({'action': 'install'}, self.context, Session())
        self.assertTrue(result['ready'])
        self.assertEqual(calls[0][1]['env']['AWS_SESSION_TOKEN'], 'fresh-synthetic-session')
        self.assertTrue(calls[0][1]['mutation'])

    def test_schema_template_contains_no_self_approval(self):
        from infrastructure.providers.aws.config import config_template, validate_config
        template = config_template(Path(self.tmp.name))
        self.assertEqual(template['files']['aws-budget.json']['approval_id'], '')
        self.assertIsNone(template['files']['aws-budget.json']['max_cost_usd'])
        self.assertFalse(validate_config(template['adapter'])['valid'])

    def test_compute_alias_stays_within_existing_target_contract(self):
        from infrastructure.providers.aws.native import digest
        for role in ('control', 'compute-a', 'compute-b', 'database'):
            alias = 'rs-' + digest(self.context['environment_id'])[:20] + '-' + role
            self.assertLessEqual(len(alias), 40)

    def test_runtime_eips_and_database_static_private_address(self):
        self.adapter.config.update(ami_id='ami-' + 'a' * 17, ssh_public_key='fixture')
        network = {'vpc_id': 'vpc-test', 'subnets': {'ap-northeast-2a': 'subnet-a', 'ap-northeast-2c': 'subnet-c'},
            'profiles': {'control': 'cp', 'worker': 'worker', 'database': 'db'}, 'database_sg': 'db-sg', 'cluster_sg': 'cluster-sg', 'target_sg': 'edge-sg', 'metrics_sg': 'metrics-sg', 'control_api_sg': 'api-sg'}
        calls = []
        def tf(context, name, module, session, extra):
            obj = Mock(); obj.home = Path(self.tmp.name) / name
            def apply(variables):
                calls.append((name, variables, json.loads(extra['environment_override.tf.json'])))
                return {'node_descriptor': {'addresses': {'private': '10.72.0.14'}}}
            obj.apply.side_effect = apply; return obj
        with patch.object(self.adapter, '_tf', side_effect=tf): self.adapter._nodes({'database': {'mode': 'standalone'}}, self.context, Mock(), network)
        self.assertEqual(len(calls), 4)
        for name, values, override in calls:
            self.assertEqual(values['allocate_eip'], name != 'nodes/database')
            self.assertEqual('api-sg' in values['additional_security_group_ids'], name == 'nodes/control')
            if name == 'nodes/database': self.assertEqual(override['resource']['aws_instance']['node']['private_ip'], '10.72.0.14')

    def test_nested_empty_ci_is_rejected_offline(self):
        from infrastructure.providers.aws.config import config_template, validate_config
        root = Path(self.tmp.name); template = config_template(root); config = template['adapter']
        for name, value in template['files'].items(): save(root / name, value)
        key = root / 'key'; key.write_text('private-fixture'); key.chmod(0o600)
        config.update(account_id=ACCOUNT, ami_id='ami-' + 'a' * 17, ssh_identity_file=str(key), ssh_public_key='ssh-ed25519 Zml4dHVyZQ==', management_source_cidrs=['8.8.8.8/32'], observer_source_cidrs=['8.8.4.4/32'])
        config['auth'].update(role_arn=ROLE, external_id='fixture-owner')
        save(root / 'aws-credentials.json', {'AWS_ACCESS_KEY_ID': 'fixture', 'AWS_SECRET_ACCESS_KEY': 'fixture'})
        save(root / 'aws-edge.json', {'zone_id': 'ZABCDEF', 'base_domain': 'railshot.io', 'routes': {'bootstrap': {'host': 'runtime.railshot.io', 'node_port': 30080, 'health_path': '/health', 'priority': 1}}})
        app = template['files']['application-template.json']; app['cd'].update(state_dir=str(root / 'cd'), repository=str(root / 'repo'), branch='main', context='argocd')
        app['profile'].update(tenant='tenant', source_repository='owner/repo', pull_secret_file=str(root / 'pull.json'))
        app['profile']['target'].update(repo_url='https://github.com/owner/repo.git', ingress_cidrs=['10.0.0.0/8'])
        app['profile']['ingress'].update(base_domain='railshot.io', ci_credentials_file=str(root / 'ci.json'))
        save(root / 'application-template.json', app); save(root / 'pull.json', {'auths': {'ghcr.io': {'auth': 'dXNlcjpwYXNz'}}}); save(root / 'ci.json', {'token': ''})
        result = validate_config(config)
        self.assertFalse(result['valid']); self.assertEqual(result['errors'], ['AWS_CI_CREDENTIAL_REQUIRED'])

    def test_registered_environment_revokes_common_binding_before_vm_delete(self):
        home = self.adapter._home(self.context); calls = []
        save(home / 'nodes.json', {'control': {'data_disk': {'resource_id': 'vol-owned'}}})
        save(home / 'application-binding-result.json', {'application_binding_receipt_path': '/private/binding'})
        session = Mock(); session.env.return_value = {'AWS_SESSION_TOKEN': 'fresh-fixture'}
        def child(argv, **kwargs):
            self.assertEqual(argv[-1], 'teardown'); self.assertEqual(kwargs['env']['AWS_SESSION_TOKEN'], 'fresh-fixture')
            calls.append('common-teardown'); return '{"status":"succeeded"}'
        self.adapter.runner = child
        tf = Mock(); tf.destroy.side_effect = lambda **kw: calls.append('native-delete') or {}
        with patch.object(self.adapter, '_session', return_value=session), patch.object(self.adapter, '_observe', return_value=[]), patch('infrastructure.providers.aws.adapter.open_owned', return_value=tf):
            result = self.adapter.delete(self.context)
        self.assertEqual(calls, ['common-teardown', 'native-delete', 'native-delete'])
        self.assertEqual(result['residuals'], []); session.materialize.assert_called_once()


if __name__ == '__main__': unittest.main()
