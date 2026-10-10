"""Offline executor contract tests; all Terraform invocations are mocked."""
import json
import io
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from contextlib import closing
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import provision


class ProvisionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.modules = {}
        for name in ('aws', 'gcp', 'azure'):
            module = self.root / ('source-' + name)
            module.mkdir()
            (module / 'main.tf').write_text('# trusted ' + name)
            (module / '.terraform.lock.hcl').write_text('# locked provider')
            self.modules[name] = module
        self.patch = patch.dict(provision.MODULES, self.modules)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.target = self.root / 'target.json'
        self.state = self.root / 'state'
        self.calls = []
        self.fail_apply = False
        def terraform(args, work, log):
            self.calls.append((args, work))
            if args[0] == 'version':
                return '{"terraform_version":"1.5.7"}'
            if args[0] == 'plan':
                Path(next(arg[5:] for arg in args if arg.startswith('-out='))).write_bytes(b'saved-binary-plan')
            if args[0] == 'show':
                return json.dumps({'resource_changes': [
                    {'address': 'example.replace', 'change': {'actions': ['delete', 'create']}},
                    {'address': 'example.delete', 'change': {'actions': ['delete']}},
                    {'address': 'example.unchanged', 'change': {'actions': ['no-op']}}]})
            if args[0] == 'output':
                target = json.loads(self.target.read_text())
                return json.dumps({'schema_version': 'v1', 'provider_kind': target['provider_kind'],
                                   'target_id': target['target_id'], 'execution_driver': 'terraform',
                                   'owner_ref': 'terraform:local:' + str(work.parent / 'terraform.tfstate'),
                                   'purpose': target['variables'].get('purpose', 'runtime'),
                                   'data_disk': {'mount_path': '/var/lib/postgresql' if target['variables'].get('purpose') == 'database' else '/var/lib/rancher'},
                                   'runtime': {'readiness': 'not_configured'}})
            if args[0] == 'apply' and self.fail_apply:
                raise ValueError('Terraform failed')
            return ''
        self.mock = patch.object(provision, 'command', side_effect=terraform)
        self.command_mock = self.mock.start()
        self.addCleanup(self.mock.stop)
        self.budget_mock = patch.object(provision, 'reserve_budget', return_value={'state': 'held'})
        self.budget_mock.start()
        self.addCleanup(self.budget_mock.stop)

    def target_for(self, provider):
        target = {'schema_version': 'v1', 'provider_kind': provider, 'target_id': provider + '-test',
                  'execution_driver': 'terraform', 'variables': {'target_id': provider + '-test',
                  {'aws': 'instance_type', 'gcp': 'machine_type', 'azure': 'vm_size'}[provider]: 'reviewed-size'},
                  'profile': {'kind': 'app_cluster', 'allowed_sizes': ['reviewed-size']}}
        self.target.write_text(json.dumps(target))
        return target

    def maintenance(self, result):
        return {**{key: result[key] for key in ('target_id', 'owner_ref', 'plan_sha256')},
                'observed_at': datetime.now(timezone.utc).isoformat(), 'dispatch_blocked': True,
                'active_jobs': 0, 'active_leases': 0, 'dependencies_stopped': True, 'backup_verified': True}

    def plan(self, provider='gcp'):
        self.target_for(provider)
        return provision.execute('plan', self.target, self.state)

    def test_all_providers_share_saved_plan_flow_and_private_state(self):
        for provider in ('aws', 'gcp', 'azure'):
            with self.subTest(provider=provider):
                self.calls.clear()
                result = self.plan(provider)
                self.assertEqual([args[0] for args, _ in self.calls], ['init', 'version', 'plan', 'show'])
                self.assertTrue(result['destructive'])
                self.assertEqual([c['risk'] for c in result['changes']], ['replace', 'delete'])
                self.assertEqual((self.state / (provider + '-test')).stat().st_mode & 0o777, 0o700)
                applied = provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
                args, work = next(x for x in reversed(self.calls) if x[0][0] == 'apply')
                self.assertEqual(args[0], 'apply')
                self.assertEqual(Path(args[-1]), self.state / (provider + '-test') / 'reviewed.tfplan')
                self.assertNotIn('-auto-approve', args)
                self.assertEqual(applied['readiness'], 'unverified')
                self.assertEqual(work.parent, self.state / (provider + '-test'))

    def test_wireguard_inputs_are_rejected_before_state_or_commands(self):
        for provider in ('aws', 'gcp', 'azure'):
            for value in ([], ['192.0.2.1/32']):
                target = self.target_for(provider)
                target['variables']['wireguard_peer_public_cidrs'] = value
                self.target.write_text(json.dumps(target))
                with self.assertRaisesRegex(ValueError, 'WireGuard provisioning is retired'):
                    provision.execute('plan', self.target, self.state)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.state.exists())

    def test_state_detachment_requires_maintenance_without_claiming_deletion(self):
        result = provision.review({'resource_changes': [
            {'address': 'google_compute_firewall.wireguard_ingress[0]',
             'change': {'actions': ['forget']}}]})
        self.assertEqual(result['changes'][0]['risk'], 'forget')
        self.assertTrue(result['maintenance_required'])
        self.assertFalse(result['destructive'])

    def test_changed_plan_target_sources_and_workspace_are_rejected(self):
        mutations = ('plan', 'target', 'source', 'workspace', 'variables')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                result = self.plan()
                home = self.state / 'gcp-test'
                if mutation == 'plan':
                    (home / 'reviewed.tfplan').write_bytes(b'tampered')
                elif mutation == 'target':
                    target = json.loads(self.target.read_text()); target['variables']['project_id'] = 'another-project'
                    self.target.write_text(json.dumps(target))
                elif mutation == 'source':
                    (self.modules['gcp'] / 'main.tf').write_text('# changed source')
                elif mutation == 'workspace':
                    (home / 'module' / 'injected.tf').write_text('# injected')
                else:
                    (home / 'module' / 'inputs.tfvars.json').write_text('{}')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
                self.assertNotEqual(self.calls[-1][0][0], 'apply')

    def test_missing_wrong_approval_and_repeated_apply_are_rejected(self):
        result = self.plan()
        for checksum in (None, '0' * 64):
            with self.assertRaises(ValueError):
                provision.execute('apply', self.target, self.state, checksum)
        self.fail_apply = True
        with self.assertRaisesRegex(ValueError, 'Terraform failed'):
            provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
        self.fail_apply = False
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))

    def test_repo_selection_and_in_checkout_state_are_rejected(self):
        target = self.target_for('gcp')
        target['module_path'] = '/arbitrary/repo'
        self.target.write_text(json.dumps(target))
        with self.assertRaisesRegex(ValueError, 'trusted target'):
            provision.execute('plan', self.target, self.state)
        self.target_for('gcp')
        with self.assertRaisesRegex(ValueError, 'outside'):
            provision.execute('plan', self.target, provision.REPO / 'bad-state')
        self.assertEqual(self.calls, [])

    def test_existing_state_binding_cannot_switch_providers(self):
        result = self.plan()
        target = json.loads(self.target.read_text())
        target['provider_kind'] = 'aws'
        target['variables']['instance_type'] = 'reviewed-size'
        self.target.write_text(json.dumps(target))
        with self.assertRaisesRegex(ValueError, 'different binding'):
            provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
        self.assertNotEqual(self.calls[-1][0][0], 'apply')

    def test_real_command_uses_argv_and_removes_hidden_terraform_overrides(self):
        self.mock.stop()
        work = self.root / 'command-work'
        work.mkdir()
        with patch.dict(provision.os.environ, {'TF_CLI_ARGS_apply': '-auto-approve', 'TF_VAR_secret': 'hidden', 'TF_WORKSPACE': 'wrong'}):
            with patch.object(provision.subprocess, 'run') as run:
                run.return_value.returncode = 0
                run.return_value.stdout = 'private output'
                run.return_value.stderr = ''
                provision.command(['version', '-json'], work, self.root / 'terraform.log')
                args, kwargs = run.call_args
                self.assertEqual(args[0], ['terraform', 'version', '-json'])
                self.assertNotIn('shell', kwargs)
                self.assertNotIn('TF_CLI_ARGS_apply', kwargs['env'])
                self.assertNotIn('TF_VAR_secret', kwargs['env'])
                self.assertNotIn('TF_WORKSPACE', kwargs['env'])
                self.assertEqual(kwargs['env']['TF_DATA_DIR'], str(work.parent / '.terraform'))

    def test_failed_apply_preserves_exit_cause_without_secret_in_public_error(self):
        self.mock.stop()
        work = self.root / 'failure-work'
        work.mkdir()
        with patch.object(provision.subprocess, 'run') as run:
            run.return_value.returncode = 7
            run.return_value.stdout = 'secret-token-output'
            run.return_value.stderr = 'secret-token-error'
            with self.assertRaises(provision.OperationError) as caught:
                provision.command(['apply', 'plan'], work, self.root / 'terraform.log')
        error = caught.exception.as_dict()
        self.assertEqual(error['code'], 'INFRA_EXECUTION_FAILED')
        self.assertEqual(error['side_effect'], 'possible')
        self.assertEqual(error['retry_policy'], 'after_reconcile')
        self.assertEqual(error['causes'][0]['returncode'], 7)
        self.assertNotIn('secret-token', json.dumps(error))

    def test_missing_apply_input_is_blocked_without_fabricated_side_effect(self):
        output = io.StringIO()
        with patch.object(provision.sys, 'argv', ['provision', 'apply', '--target', str(self.root / 'missing'),
                                                 '--state-root', str(self.state)]), redirect_stderr(output):
            self.assertEqual(provision.main(), 2)
        record = json.loads(output.getvalue())
        self.assertEqual(record['outcome'], 'BLOCKED')
        self.assertEqual(record['error']['side_effect'], 'none')
        self.assertEqual(record['error']['code'], 'INFRA_CONFIG_INVALID')
        self.assertEqual(self.calls, [])

    def test_atomic_private_writer_fsyncs_file_before_replace_then_directory(self):
        events = []
        sync, replace = provision.os.fsync, provision.os.replace
        def fsync(fd):
            events.append('fsync'); return sync(fd)
        def swap(a, b, **kwargs):
            events.append('replace'); return replace(a, b, **kwargs)
        path = self.root / 'receipt.json'
        with patch.object(provision.os, 'fsync', side_effect=fsync), patch.object(provision.os, 'replace', side_effect=swap):
            provision.write_private(path, b'completed')
        self.assertEqual(events, ['fsync', 'replace', 'fsync'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_marker_write_failure_prevents_dispatch_and_keeps_budget_hold(self):
        result = self.plan()
        writer = provision.write_private
        def fail_marker(path, value):
            if path.name == 'plan-manifest.json' and json.loads(value).get('apply_attempted'):
                raise OSError(28, 'private path must not enter public error')
            return writer(path, value)
        with patch.object(provision, 'write_private', side_effect=fail_marker):
            with self.assertRaises(provision.OperationError) as caught:
                provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
        self.assertEqual(caught.exception.code, 'STATE_STORAGE_FAILED')
        self.assertFalse(any(args[0] == 'apply' for args, _ in self.calls))

    def test_real_process_exit_after_dispatch_marker_blocks_replay_and_new_plan(self):
        result = self.plan()
        script = """import sys,json,os
from pathlib import Path
sys.path.insert(0, sys.argv[1]); import provision
provision.MODULES['gcp']=Path(sys.argv[2])
provision.reserve_budget=lambda *args: {'state':'held'}
def command(args,work,log):
 if args[0]=='version': return '{"terraform_version":"1.5.7"}'
 if args[0]=='apply': os._exit(9)
 raise AssertionError(args[0])
provision.command=command
provision.execute('apply',Path(sys.argv[3]),Path(sys.argv[4]),sys.argv[5],maintenance=json.loads(sys.argv[6]))
"""
        child = subprocess.run([sys.executable, '-c', script, str(Path(provision.__file__).parent),
                                str(self.modules['gcp']), str(self.target), str(self.state), result['plan_sha256'],
                                json.dumps(self.maintenance(result))])
        self.assertEqual(child.returncode, 9)
        marker = json.loads((self.state / 'gcp-test/plan-manifest.json').read_text())
        self.assertTrue(marker['apply_attempted'])
        self.assertEqual(marker['apply_status'], 'outcome_unknown')
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            provision.execute('apply', self.target, self.state, result['plan_sha256'])
        with self.assertRaises(provision.OperationError) as caught:
            provision.execute('plan', self.target, self.state)
        self.assertEqual(caught.exception.code, 'STATE_INFLIGHT_UNCERTAIN')

    def test_resize_review_requires_current_bound_drain_observation(self):
        review = provision.review({'resource_changes': [{'address': 'node', 'change': {
            'actions': ['update'], 'before': {'machine_type': 'small'}, 'after': {'machine_type': 'large'}}}]})
        self.assertTrue(review['maintenance_required'])
        result = self.plan()
        for data in (None, {**self.maintenance(result), 'active_jobs': 1},
                     {**self.maintenance(result), 'owner_ref': 'wrong'},
                     {**self.maintenance(result), 'observed_at': '2000-01-01T00:00:00Z'}):
            with self.assertRaises(provision.OperationError) as caught:
                provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=data)
            self.assertEqual(caught.exception.code, 'INFRA_MAINTENANCE_REQUIRED')
        self.assertFalse(any(args[0] == 'apply' for args, _ in self.calls))

    def test_database_profile_uses_same_saved_plan_and_requires_matching_purpose(self):
        for provider in ('aws', 'gcp'):
            target = self.target_for(provider)
            target['profile']['kind'] = 'database_cluster'
            self.target.write_text(json.dumps(target))
            with self.assertRaises(provision.OperationError):
                provision.execute('plan', self.target, self.state)
            target['variables']['purpose'] = 'database'
            self.target.write_text(json.dumps(target))
            plan = provision.execute('plan', self.target, self.state)
            self.assertEqual(plan['capabilities']['kind'], 'database_cluster')
            applied = provision.execute('apply', self.target, self.state, plan['plan_sha256'], maintenance=self.maintenance(plan))
            self.assertEqual(applied['readiness'], 'unverified')
        target = self.target_for('azure')
        target['profile']['kind'] = 'database_cluster'
        target['variables']['purpose'] = 'database'
        self.target.write_text(json.dumps(target))
        with self.assertRaises(provision.OperationError):
            provision.execute('plan', self.target, self.state)
        target = self.target_for('gcp')
        target['variables']['purpose'] = 'database'
        self.target.write_text(json.dumps(target))
        with self.assertRaises(provision.OperationError):
            provision.execute('plan', self.target, self.state)

    def test_database_apply_rejects_runtime_storage_output_as_unknown(self):
        target = self.target_for('gcp')
        target['profile']['kind'] = 'database_cluster'
        target['variables']['purpose'] = 'database'
        self.target.write_text(json.dumps(target))
        plan = provision.execute('plan', self.target, self.state)
        original = self.command_mock.side_effect
        def wrong_storage(args, work, log):
            result = original(args, work, log)
            if args[0] == 'output':
                descriptor = json.loads(result)
                descriptor['data_disk']['mount_path'] = '/var/lib/rancher'
                return json.dumps(descriptor)
            return result
        self.command_mock.side_effect = wrong_storage
        with self.assertRaises(provision.OperationError) as caught:
            provision.execute('apply', self.target, self.state, plan['plan_sha256'], maintenance=self.maintenance(plan))
        self.assertEqual(caught.exception.outcome, 'UNKNOWN')
        self.assertEqual(caught.exception.side_effect, 'completed')

    def test_registered_profile_is_required_before_terraform(self):
        target = self.target_for('gcp')
        target['profile']['allowed_sizes'] = ['different-size']
        self.target.write_text(json.dumps(target))
        with self.assertRaises(provision.OperationError) as caught:
            provision.execute('plan', self.target, self.state)
        self.assertEqual(caught.exception.code, 'INFRA_CAPABILITY_UNSUPPORTED')
        self.assertEqual(self.calls, [])

    def test_owner_is_injected_and_wrong_output_is_unknown(self):
        result = self.plan()
        variables = json.loads((self.state / 'gcp-test/module/inputs.tfvars.json').read_text())
        self.assertEqual(variables['owner_ref'], result['owner_ref'])
        original = self.command_mock.side_effect
        def bad_output(args, work, log):
            return '{"owner_ref":"other-state"}' if args[0] == 'output' else original(args, work, log)
        self.command_mock.side_effect = bad_output
        with self.assertRaises(provision.OperationError) as caught:
            provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
        self.assertEqual(caught.exception.outcome, 'UNKNOWN')
        self.assertEqual(caught.exception.side_effect, 'completed')

    def budget_target(self):
        self.budget_mock.stop()
        target = self.target_for('gcp')
        target['variables']['project_id'] = 'test-project'
        now = datetime.now(timezone.utc)
        path = self.root / 'billing.sqlite3'
        row = {'usage_start_time': now.isoformat(), 'currency': 'USD', 'cost': '1', 'project': {'id': 'test-project'}}
        with closing(provision.costs.ledger(path)) as db:
            provision.costs.import_snapshot(db, 'test-scope', 'gcp', now.strftime('%Y-%m'), now.isoformat(),
                                           [row], now=now, source_scope='test-project')
        target['budget'] = {'ledger_path': str(path), 'scope': 'test-scope', 'currency': 'USD',
                            'incremental_cost': '2', 'limit': '10', 'unreported_cost': '1',
                            'quoted_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=10)).isoformat()}
        return target

    def test_real_ledger_reservation_precedes_apply_and_remains_held(self):
        target = self.budget_target(); self.target.write_text(json.dumps(target))
        result = provision.execute('plan', self.target, self.state)
        original = self.command_mock.side_effect
        def asserted(args, work, log):
            if args[0] == 'apply':
                with closing(provision.costs.ledger(target['budget']['ledger_path'])) as db:
                    self.assertEqual(db.execute('SELECT state FROM reservations WHERE operation_id=?',
                                                (result['operation_id'],)).fetchone(), ('held',))
            return original(args, work, log)
        self.command_mock.side_effect = asserted
        applied = provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
        self.assertEqual(applied['budget_reservation']['state'], 'held')

    def test_wrong_account_missing_and_expired_billing_prevent_apply(self):
        target = self.budget_target()
        variants = [{**target, 'budget': None},
                    {**target, 'variables': {**target['variables'], 'project_id': 'other'}},
                    {**target, 'budget': {**target['budget'], 'expires_at': '2000-01-01T00:00:00Z'}},
                    {**target, 'budget': {**target['budget'], 'limit': '1'}}]
        for value in variants:
            self.target.write_text(json.dumps(value))
            result = provision.execute('plan', self.target, self.state)
            with self.assertRaises(provision.OperationError) as caught:
                provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
            self.assertEqual(caught.exception.code, 'INFRA_BUDGET_BLOCKED')
        self.assertFalse(any(args[0] == 'apply' for args, _ in self.calls))

    def test_aws_requires_its_own_account_snapshot_before_dispatch(self):
        target = self.budget_target()
        target['provider_kind'] = 'aws'; target['target_id'] = 'aws-test'
        target['variables'].update(target_id='aws-test', instance_type='reviewed-size', account_id='123456789012')
        self.target.write_text(json.dumps(target))
        result = provision.execute('plan', self.target, self.state)
        with self.assertRaises(provision.OperationError) as caught:
            provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
        self.assertEqual(caught.exception.code, 'INFRA_BUDGET_BLOCKED')
        self.assertTrue(result['capabilities']['billing_reservation_supported'])
        self.assertFalse(any(args[0] == 'apply' for args, _ in self.calls))

    def test_aws_account_bound_reservation_precedes_apply(self):
        target = self.budget_target()
        target.update(provider_kind='aws', target_id='aws-test')
        target['variables'].update(target_id='aws-test', instance_type='reviewed-size', account_id='123456789012')
        now = datetime.now(timezone.utc)
        interval = {'Start': now.date().replace(day=1).isoformat(), 'End': (now.date() + timedelta(days=1)).isoformat()}
        snapshot = {'account_id': '123456789012', 'request': {'TimePeriod': interval, 'Granularity': 'MONTHLY',
            'Metrics': ['UnblendedCost'], 'Filter': {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': ['123456789012']}}},
            'response': {'ResultsByTime': [{'TimePeriod': interval, 'Total': {'UnblendedCost': {'Amount': '0', 'Unit': 'USD'}},
                                          'Estimated': True, 'Groups': []}]}}
        target['budget']['scope'] = 'aws-scope'
        with closing(provision.costs.ledger(target['budget']['ledger_path'])) as db:
            provision.costs.import_snapshot(db, 'aws-scope', 'aws', now.strftime('%Y-%m'), now.isoformat(),
                                           snapshot, now=now, source_scope='123456789012')
        wrong = {**target, 'variables': {**target['variables'], 'account_id': '999999999999'}}
        self.target.write_text(json.dumps(wrong))
        result = provision.execute('plan', self.target, self.state)
        with self.assertRaises(provision.OperationError) as caught:
            provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
        self.assertEqual(caught.exception.code, 'INFRA_BUDGET_BLOCKED')
        self.assertFalse(any(args[0] == 'apply' for args, _ in self.calls))
        self.target.write_text(json.dumps(target))
        result = provision.execute('plan', self.target, self.state)
        original = self.command_mock.side_effect
        def asserted(args, work, log):
            if args[0] == 'apply':
                with closing(provision.costs.ledger(target['budget']['ledger_path'])) as db:
                    self.assertEqual(db.execute('SELECT state FROM reservations WHERE operation_id=?',
                                               (result['operation_id'],)).fetchone(), ('held',))
            return original(args, work, log)
        self.command_mock.side_effect = asserted
        applied = provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
        self.assertEqual(applied['budget_reservation']['state'], 'held')

    def test_executor_policy_drift_invalidates_saved_plan(self):
        result = self.plan()
        with patch.object(provision, 'executor_hash', return_value='0' * 64):
            with self.assertRaisesRegex(ValueError, 'changed'):
                provision.execute('apply', self.target, self.state, result['plan_sha256'], maintenance=self.maintenance(result))
        self.assertFalse(any(args[0] == 'apply' for args, _ in self.calls))


if __name__ == '__main__':
    unittest.main()
