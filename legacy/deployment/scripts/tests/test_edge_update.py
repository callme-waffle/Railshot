"""Saved-plan edge updates: no cloud calls and no automatic replay of uncertain writes."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import edge_update as edge


class EdgeUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.address = 'aws_lb.app'
        self.state = {'version': 4, 'serial': 3, 'lineage': 'operator-owned-lineage', 'resources': [
            {'mode': 'managed', 'type': 'aws_lb', 'name': 'app', 'instances': [{'attributes': {'id': 'lb-owned'}}]}]}
        self.config = {'version': 1, 'provider': 'aws', 'state_file': str(self.root / 'edge.tfstate'),
                       'variables_file': str(self.root / 'variables.json'), 'state_dir': str(self.root / 'updates'),
                       'state_lineage': self.state['lineage'], 'owned_resources': {self.address: 'lb-owned'},
                       'previous_source_sha': 'a' * 40}
        self.config_path = self.root / 'config.json'
        edge.durable_write(Path(self.config['state_file']), edge.encoded(self.state))
        edge.durable_write(Path(self.config['variables_file']), edge.encoded({'region': 'test'}))
        self.calls, self.applied, self.no_change = [], False, False
        self.failure = self.tamper = self.plan_override = None
        for provider in edge.MODULES:
            directory = self.source / edge.MODULES[provider]
            directory.mkdir(parents=True)
            (directory / 'main.tf').write_text('terraform { required_version = ">= 1.5" }\n')
            (directory / '.terraform.lock.hcl').write_text('# test fixture lock\n')
        relay = self.source / edge.module_path('openstack', 'aws-relay')
        relay.mkdir(parents=True)
        (relay / 'main.tf').write_text('# relay module\nterraform { required_version = ">= 1.5" }\n')
        (relay / '.terraform.lock.hcl').write_text('# test fixture lock\n')
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('add', '.')
        self.git('commit', '-m', 'approved source')
        self.release = self.git('rev-parse', 'HEAD').strip()
        self.config['previous_source_sha'] = self.release
        self.write_config()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.source), *args], check=True,
                              capture_output=True, text=True).stdout

    def write_config(self):
        edge.durable_write(self.config_path, edge.encoded(self.config))

    def fake_terraform(self, module, *args):
        self.calls.append(args)
        if args[0] == 'init':
            self.assertIn('-lockfile=readonly', args)
            self.assertIn('-backend-config=path=' + self.config['state_file'], args)
            (module / '.terraform').mkdir(mode=0o700)
            edge.durable_write(module / '.terraform/terraform.tfstate', edge.encoded({
                'backend': {'type': 'local', 'config': {'path': self.config['state_file']}}}), mode=0o644)
        if args[0] == 'plan':
            self.assertIn('-lock-timeout=10s', args)
            self.plan_path = Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('-out=')))
            self.plan_path.write_bytes(b'exact-saved-plan')
        if args[0] == 'show':
            if self.tamper:
                self.plan_path.write_bytes(b'tampered-plan')
            plan = {'resource_changes': [{'address': address, 'mode': 'managed', 'change': {
                'actions': ['no-op'] if self.applied or self.no_change else ['update'],
                'before': {'id': identity}, 'after': {'id': identity}, 'after_unknown': {}}}
                for address, identity in self.config['owned_resources'].items()]}
            return json.dumps(self.plan_override or plan).encode()
        if args[0] == 'apply':
            self.assertEqual(Path(args[-1]).read_bytes(), b'exact-saved-plan')
            self.applied = True
            self.state['serial'] += 1
            edge.durable_write(Path(self.config['state_file']), edge.encoded(self.state))
            if self.failure:
                raise RuntimeError('synthetic interrupted provider update')
        return b''

    def execute(self, image_mode=False, verify_only=False):
        digest = edge.module_digest(self.source, self.config['provider'], self.config.get('edge_kind', 'native')) if image_mode else None
        with patch.object(edge, 'terraform', side_effect=self.fake_terraform):
            return edge.run(self.source, self.release, self.config_path, digest, verify_only)

    def receipt(self):
        return edge.read_private(Path(self.config['state_dir']) / ('attempt-' + self.release + '.json'))

    def test_saved_plan_apply_then_fresh_plan_and_idempotent_receipt(self):
        before = Path(self.config['state_file']).read_bytes()
        result = self.execute()
        self.assertEqual(result['phase'], 'succeeded')
        self.assertEqual([call[0] for call in self.calls], ['init', 'validate', 'plan', 'show', 'apply', 'plan', 'show'])
        self.assertEqual(result['state_before_sha256'], edge.sha256(before))
        self.assertNotEqual(result['state_before_sha256'], result['state_after_sha256'])
        self.assertEqual(result['previous_source_sha'], self.release)
        self.assertTrue(result['refreshed_plan_no_changes'])
        self.assertEqual((Path(self.config['state_dir']) / self.release / 'state-before.json').read_bytes(), before)
        calls_before = len(self.calls)
        self.assertTrue(self.execute()['cached'])
        self.assertEqual(calls_before, len(self.calls))
        self.assertNotIn('lb-owned', json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_cached_release_rejects_changed_operator_variables(self):
        self.execute()
        edge.durable_write(Path(self.config['variables_file']), edge.encoded({'region': 'changed'}))
        with patch.object(edge, 'terraform') as tf, self.assertRaisesRegex(ValueError, 'requires observation'):
            edge.run(self.source, self.release, self.config_path)
        tf.assert_not_called()

    def test_output_only_update_is_not_misreported_as_no_change(self):
        plan = {'resource_changes': [{'address': self.address, 'mode': 'managed', 'change': {
            'actions': ['no-op'], 'before': {'id': 'lb-owned'}, 'after': {'id': 'lb-owned'}}}],
                'output_changes': {'public_address': {'actions': ['update']}}}
        self.assertEqual(edge.validate_plan(plan, self.config),
                         [{'address': 'output.public_address', 'actions': ['update']}])

    def test_verify_only_makes_fresh_plan_without_mutating_attempt_or_state(self):
        self.execute(image_mode=True)
        receipt_path = Path(self.config['state_dir']) / ('attempt-' + self.release + '.json')
        original_receipt = receipt_path.read_bytes()
        original_state = Path(self.config['state_file']).read_bytes()
        self.calls.clear()
        result = self.execute(image_mode=True, verify_only=True)
        self.assertTrue(result['reverified'])
        self.assertEqual(result['phase'], 'succeeded')
        self.assertEqual([call[0] for call in self.calls], ['plan', 'show'])
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        self.assertEqual(Path(self.config['state_file']).read_bytes(), original_state)
        self.assertFalse(list(Path(self.config['state_dir']).glob('.verify-*')))

    def test_verify_only_rejects_drift_pending_updates_and_source_mismatch(self):
        self.execute(image_mode=True)
        original = self.receipt()
        self.plan_override = {'resource_drift': [{'address': self.address}]}
        with self.assertRaisesRegex(ValueError, 'reconcile edge drift'):
            self.execute(image_mode=True, verify_only=True)
        self.plan_override = None
        self.applied = False
        with self.assertRaisesRegex(ValueError, 'still has changes'):
            self.execute(image_mode=True, verify_only=True)
        self.applied = True
        (self.source / edge.MODULES['aws'] / 'main.tf').write_text('# unexpected source change\n')
        with self.assertRaisesRegex(ValueError, 'existing edge attempt'):
            self.execute(image_mode=True, verify_only=True)
        self.assertEqual(self.receipt(), original)

    def test_verify_only_requires_succeeded_attempt_and_bound_backend(self):
        with patch.object(edge, 'terraform') as tf, self.assertRaisesRegex(ValueError, 'succeeded edge attempt'):
            edge.run(self.source, self.release, self.config_path, verify_only=True)
        tf.assert_not_called()
        self.execute()
        backend = Path(self.config['state_dir']) / self.release / 'module/.terraform/terraform.tfstate'
        edge.durable_write(backend, edge.encoded({'backend': {'type': 'local', 'config': {'path': '/unbound'}}}))
        with patch.object(edge, 'terraform') as tf, self.assertRaisesRegex(ValueError, 'bound state'):
            edge.run(self.source, self.release, self.config_path, verify_only=True)
        tf.assert_not_called()

    def test_no_change_still_records_verified_release_without_apply(self):
        self.no_change = True
        result = self.execute(image_mode=True)
        self.assertTrue(result['refreshed_plan_no_changes'])
        self.assertEqual(result['state_before_sha256'], result['state_after_sha256'])
        self.assertEqual(sum(call[0] == 'plan' for call in self.calls), 2)
        self.assertFalse(self.applied)

    def test_plan_tamper_never_reaches_apply(self):
        self.tamper = True
        with self.assertRaisesRegex(ValueError, 'saved plan changed'):
            self.execute()
        self.assertFalse(self.applied)
        self.assertEqual(self.receipt()['phase'], 'failed')

    def test_partial_failure_and_new_release_cannot_replay_mutation(self):
        self.failure = True
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            self.execute()
        self.assertEqual(self.receipt()['phase'], 'uncertain')
        self.assertEqual(self.state['serial'], 4)
        for release in (self.release, 'f' * 40):
            with patch.object(edge, 'terraform') as tf, self.assertRaisesRegex(ValueError, 'automatic replay forbidden'):
                edge.run(self.source, release, self.config_path)
            tf.assert_not_called()

    def test_destroy_replacement_create_move_import_and_drift_rejected(self):
        base = {'address': self.address, 'mode': 'managed', 'change': {
            'actions': ['update'], 'before': {'id': 'lb-owned'}, 'after': {'id': 'lb-owned'}}}
        bad = []
        for actions in (['delete'], ['create'], ['delete', 'create'], ['create', 'delete']):
            row = copy.deepcopy(base)
            row['change']['actions'] = actions
            bad.append({'resource_changes': [row]})
        row = copy.deepcopy(base)
        row['previous_address'] = 'aws_lb.other'
        bad.append({'resource_changes': [row]})
        row = copy.deepcopy(base)
        row['change']['importing'] = {'id': 'lb-owned'}
        bad.append({'resource_changes': [row]})
        bad.extend(({'resource_changes': [base], 'resource_drift': [{'address': self.address}]},
                    {'resource_changes': []}, {'resource_changes': [base], 'complete': False}))
        for plan in bad:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                edge.validate_plan(plan, self.config)

    def refresh_plan(self):
        drift = {'address': self.address, 'mode': 'managed', 'change': {
            'actions': ['update'], 'before': {'id': 'lb-owned', 'labels': None},
            'after': {'id': 'lb-owned', 'labels': {}}, 'after_unknown': {}}}
        following = copy.deepcopy(drift)
        following['change']['actions'] = ['no-op']
        following['change']['before'] = copy.deepcopy(following['change']['after'])
        return {'resource_drift': [drift], 'resource_changes': [following]}

    def test_computed_refresh_with_owned_noop_is_recorded_without_state_write(self):
        self.plan_override = self.refresh_plan()
        state_before = Path(self.config['state_file']).read_bytes()
        result = self.execute(image_mode=True)
        accepted = [{'address': self.address, 'observed_actions': ['update'], 'planned_actions': ['no-op'],
                     'identity_preserved': True, 'changed_fields': ['labels']}]
        self.assertEqual(result['accepted_noop_refresh'], accepted)
        self.assertEqual(result['verification_accepted_noop_refresh'], accepted)
        self.assertFalse(self.applied)
        self.assertEqual(state_before, Path(self.config['state_file']).read_bytes())
        receipt_before = self.receipt()
        verified = self.execute(image_mode=True, verify_only=True)
        self.assertTrue(verified['reverified'])
        self.assertEqual(verified['verification_accepted_noop_refresh'], accepted)
        self.assertEqual(receipt_before, self.receipt())

    def test_refresh_rejects_planned_mutation_unknown_identity_import_replace_and_deposed(self):
        cases = []
        for actions in (['update'], ['delete'], ['create'], ['forget'], ['unknown']):
            plan = self.refresh_plan()
            plan['resource_changes'][0]['change']['actions'] = actions
            cases.append(plan)
        plan = self.refresh_plan()
        plan['resource_drift'][0]['address'] = 'aws_lb.unbound'
        cases.append(plan)
        for section in ('resource_drift', 'resource_changes'):
            for modification in ('id', 'import', 'replace', 'deposed'):
                plan = self.refresh_plan()
                row = plan[section][0]
                if modification == 'id': row['change']['after']['id'] = 'different-owner'
                elif modification == 'import': row['change']['importing'] = {'id': 'lb-owned'}
                elif modification == 'replace': row['change']['replace_paths'] = [['id']]
                else: row['deposed'] = '00000001'
                cases.append(plan)
        for plan in cases:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                edge.validate_plan(plan, self.config)

    def test_identity_and_lineage_mismatch_fail_before_terraform(self):
        for mutation in ({'state_lineage': 'another-lineage'}, {'owned_resources': {self.address: 'another-id'}}):
            self.config.update(mutation)
            self.write_config()
            with patch.object(edge, 'terraform') as tf, self.assertRaisesRegex(ValueError, 'mismatch'):
                edge.run(self.source, self.release, self.config_path)
            tf.assert_not_called()

    def test_image_source_requires_exact_release_digest_and_regular_files(self):
        directory = self.root / 'materialized'
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            edge.source_module(self.source, self.release, self.config, directory, 'f' * 64)
        self.assertFalse(directory.exists())
        file = self.source / edge.MODULES['aws'] / 'main.tf'
        file.unlink()
        file.symlink_to(self.root / 'config.json')
        with self.assertRaisesRegex(ValueError, 'regular module file'):
            edge.module_digest(self.source, 'aws')

    def test_fixed_module_selection_for_three_providers(self):
        for provider in edge.MODULES:
            config = {**self.config, 'provider': provider}
            target = self.root / ('materialized-' + provider)
            proof = edge.source_module(self.source, self.release, config, target,
                                       edge.module_digest(self.source, provider))
            self.assertEqual(proof['module'], edge.MODULES[provider])
            self.assertIn('backend "local"', (target / 'railshot-backend.tf').read_text())

    def test_provider_resource_prefix_matches_real_terraform_state(self):
        for provider, kind in (('aws', 'aws_lb'), ('gcp', 'google_compute_backend_service'),
                               ('openstack', 'openstack_lb_loadbalancer_v2')):
            self.config['provider'] = provider
            self.config['owned_resources'] = {kind + '.app': 'lb-owned'}
            self.state['resources'][0]['type'] = kind
            edge.durable_write(Path(self.config['state_file']), edge.encoded(self.state))
            self.assertEqual(edge.bound_state(self.config)[0]['lineage'], self.state['lineage'])

    def relay(self):
        target = {'id': '172.31.0.10', 'port': 13200,
                  'target_group_arn': 'arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:targetgroup/relay/1234abcd'}
        self.config.update(provider='openstack', edge_kind='aws-relay', backend_target=target,
                           backend_ingress={'rule_id': 'sgr-' + 'a' * 17, 'security_group_id': 'sg-' + 'b' * 17,
                                            'source_security_group_id': 'sg-' + 'c' * 17},
                           owned_resources={'aws_lb_target_group.app': target['target_group_arn'],
                                            'aws_lb_listener_rule.app': 'rule-owned', 'aws_route53_record.app': 'dns-owned'})
        self.state['resources'] = [{'mode': 'managed', 'type': address.split('.')[0], 'name': 'app',
                                   'instances': [{'attributes': {'id': identity}}]}
                                  for address, identity in self.config['owned_resources'].items()]
        edge.durable_write(Path(self.config['state_file']), edge.encoded(self.state))
        self.write_config()
        self.aws_calls = []
        self.target_response = [{'Target': {'Id': target['id'], 'Port': target['port']}, 'TargetHealth': {'State': 'healthy'}}]
        ingress = self.config['backend_ingress']
        self.rule_response = [{'SecurityGroupRuleId': ingress['rule_id'], 'GroupId': ingress['security_group_id'],
                               'GroupOwnerId': '123456789012', 'IsEgress': False, 'IpProtocol': 'tcp',
                               'FromPort': target['port'], 'ToPort': target['port'],
                               'ReferencedGroupInfo': {'GroupId': ingress['source_security_group_id']}}]

    def fake_aws(self, args, timeout=120):
        self.aws_calls.append(args)
        self.assertEqual(args[0], 'aws')
        self.assertEqual(args[args.index('--region') + 1], 'ap-northeast-2')
        if args[1] == 'elbv2':
            return edge.encoded({'TargetHealthDescriptions': self.target_response})
        return edge.encoded({'SecurityGroupRules': self.rule_response})

    def test_relay_kind_has_fixed_module_and_pre_post_readonly_binding_checks(self):
        self.relay()
        with patch.object(edge, 'native', side_effect=self.fake_aws):
            result = self.execute(image_mode=True)
            self.assertEqual(len(self.aws_calls), 4)
            self.assertEqual(result['module'], edge.module_path('openstack', 'aws-relay'))
            self.assertTrue(result['external_target_verified'] and result['external_ingress_verified'])
            before = self.receipt()
            self.aws_calls.clear()
            verified = self.execute(image_mode=True, verify_only=True)
            self.assertTrue(verified['reverified'])
            self.assertEqual(len(self.aws_calls), 4)
            self.assertEqual(before, self.receipt())
            self.rule_response[0]['ToPort'] = 13201
            with self.assertRaisesRegex(ValueError, 'ingress rule mismatch'):
                self.execute(image_mode=True, verify_only=True)
            self.assertEqual(before, self.receipt())

    def test_relay_gate_rejects_extra_unhealthy_or_changed_targets_and_ingress(self):
        self.relay()
        target, rule = copy.deepcopy(self.target_response), copy.deepcopy(self.rule_response)
        cases = [('extra target', lambda: self.target_response.append(copy.deepcopy(target[0]))),
                 ('unhealthy', lambda: self.target_response[0]['TargetHealth'].update(State='unhealthy')),
                 ('wrong port', lambda: self.target_response[0]['Target'].update(Port=13201)),
                 ('wrong ingress', lambda: self.rule_response[0].update(IsEgress=True)),
                 ('wrong source', lambda: self.rule_response[0]['ReferencedGroupInfo'].update(GroupId='sg-unbound')),
                 ('wrong owner', lambda: self.rule_response[0].update(GroupOwnerId='999999999999'))]
        for name, mutate in cases:
            self.target_response, self.rule_response = copy.deepcopy(target), copy.deepcopy(rule)
            mutate()
            with self.subTest(name=name), patch.object(edge, 'native', side_effect=self.fake_aws), self.assertRaises(ValueError):
                edge.verify_relay(self.config)

    def test_relay_external_failure_stops_before_terraform(self):
        self.relay()
        self.target_response[0]['TargetHealth']['State'] = 'unhealthy'
        with patch.object(edge, 'native', side_effect=self.fake_aws), self.assertRaisesRegex(ValueError, 'health mismatch'):
            self.execute(image_mode=True)
        self.assertFalse(self.calls)
        self.assertEqual(self.receipt()['phase'], 'failed')

    def test_relay_post_apply_health_failure_records_uncertain_and_verify_failure_preserves_receipt(self):
        self.relay()
        def interrupted_health(args, timeout=120):
            if self.applied:
                self.target_response[0]['TargetHealth']['State'] = 'unhealthy'
            return self.fake_aws(args, timeout)
        with patch.object(edge, 'native', side_effect=interrupted_health), self.assertRaisesRegex(ValueError, 'health mismatch'):
            self.execute(image_mode=True)
        self.assertTrue(self.applied)
        self.assertEqual(self.receipt()['phase'], 'uncertain')

    def test_relay_binding_cannot_select_other_provider_module_or_manage_sg(self):
        self.relay()
        self.assertEqual(edge.configuration(self.config_path), self.config)
        self.assertNotEqual(edge.module_digest(self.source, 'openstack'), edge.module_digest(self.source, 'openstack', 'aws-relay'))
        for mutation in ({'provider': 'aws'}, {'edge_kind': '../aws-edge'},
                         {'owned_resources': {**self.config['owned_resources'], 'aws_security_group.control': 'unowned'}}):
            invalid = {**self.config, **mutation}
            edge.durable_write(self.config_path, edge.encoded(invalid))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                edge.configuration(self.config_path)

    def test_ambient_terraform_overrides_cannot_change_operator_binding(self):
        result = subprocess.CompletedProcess(['terraform'], 0, stdout=b'', stderr=b'')
        overrides = {'TF_CLI_ARGS_plan': '-destroy', 'TF_VAR_region': 'unbound',
                     'TF_WORKSPACE': 'other', 'TF_DATA_DIR': '/other', 'AWS_REGION': 'bound-credential-region'}
        with patch.dict(os.environ, overrides), patch.object(edge.subprocess, 'run', return_value=result) as command:
            edge.native(['terraform', 'version'])
        environment = command.call_args.kwargs['env']
        self.assertFalse(any(key in environment for key in overrides if key.startswith('TF_')))
        self.assertEqual(environment['AWS_REGION'], 'bound-credential-region')

    def test_backend_or_unlocked_source_cannot_be_materialized(self):
        file = self.source / edge.MODULES['aws'] / 'main.tf'
        file.write_text('terraform { backend "s3" {} }\n')
        with self.assertRaisesRegex(ValueError, 'backend migration'):
            edge.source_module(self.source, self.release, self.config, self.root / 'materialized',
                               edge.module_digest(self.source, 'aws'))
        (file.parent / '.terraform.lock.hcl').unlink()
        with self.assertRaisesRegex(ValueError, 'locked edge module'):
            edge.source_module(self.source, self.release, self.config, self.root / 'unlocked',
                               edge.module_digest(self.source, 'aws'))


if __name__ == '__main__':
    unittest.main()
