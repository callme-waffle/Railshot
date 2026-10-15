"""No AWS calls: exercise native boundaries, replay fences and authority checks."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
            'node_count': 3, 'instance_type': 't3.large', 'data_disk_gib': 100, 'pricing_basis': 'operator quote including EC2/EBS/EFS/ALB/DNS/publicIPv4', 'data_policy': 'retain', 'ledger': {'ledger_path': '/private/ledger.sqlite', 'scope': 'aws-sandbox', 'currency': 'USD', 'incremental_cost': '10', 'limit': '20', 'unreported_cost': '0', 'quoted_at': '2026-10-15T00:00:00Z', 'expires_at': '2099-01-01T00:00:00Z'}}
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


if __name__ == '__main__': unittest.main()
