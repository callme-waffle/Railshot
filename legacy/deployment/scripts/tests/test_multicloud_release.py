"""Exercise release fanout and promotion with no cloud credentials or mutations."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deployment/scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


release = load('multicloud_release')
admission = load('release_admission')
worker_helpers = load('platform_workers')


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.manifest = {'version': 1, 'source_sha': 'a' * 40, 'platform_revision': 'b' * 40,
            'images': {name: 'ghcr.io/jasmin-softbank/railshot-' + name + '@sha256:' + 'c' * 64 for name in release.COMPONENTS},
            'runtime_policy': json.loads((ROOT / 'deployment/airgap/versions.json').read_text()),
            'edge_kinds': {p: 'native' for p in release.PROVIDERS},
            'edge_modules': {provider: 'd' * 64 for provider in release.PROVIDERS},
            'provider_targets': {provider: 'k3s-' + provider for provider in release.PROVIDERS}}
        self.config = {'version': 1, 'state_dir': str(Path(self.directory.name).resolve()),
            'workers': {'runner_url': 'https://github.com/Jasmin-Softbank/railshot-apps', 'build_node': 'build-01',
                        'object_uids': {key: key + '-uid' for key in worker_helpers.KEYS}}, 'apps': {}, 'operator_kubeconfig': release.OPERATOR_STATE + 'control-kubeconfig',
            'gcp_credentials_file': release.OPERATOR_STATE + 'gcp-wif.json', 'targets': [
            {'provider': provider, 'target_id': 'k3s-' + provider,
             **{name: release.OPERATOR_STATE + 'config/' + provider + '/' + name for name in
                ('registry_file', 'config_file', 'registration_state', 'from_policy_file', 'edge_config_file')}} for provider in sorted(release.PROVIDERS)]}
        self.promotions = []
        self.workers = SimpleNamespace(scoped_config=worker_helpers.scoped_config, apply_workers=lambda *args: {'status': 'verified', 'executable_verification': True}, verify_workers=lambda *args: {'status':'verified', 'executable_verification': True}, verify_apps=lambda *args: {'status':'verified'})

    def run_release(self, target):
        def promote(*args):
            self.promotions.append(args[2]); return {'status': 'verified'}
        return release.execute(self.config, self.manifest, workers=self.workers, target_runner=target,
                               platform_verify=lambda *args: {'status': 'cluster_verified'}, promote=promote,
                               target_verifier=lambda row, manifest: self.proof(row))

    def proof(self, target, status='verified'):
        return {'provider': target['provider'], 'target_id': target['target_id'], 'source_sha': self.manifest['source_sha'], 'release_sha': self.manifest['source_sha'], 'status': status,
                **({'scope': target['scope']} if 'scope' in target else {})}

    def node_target(self):
        target = self.config['targets'][0]
        target['scope'] = 'node-only'; target.pop('edge_config_file')
        self.manifest['edge_kinds'][target['provider']] = 'none'
        self.manifest['edge_modules'][target['provider']] = None
        return target

    def test_parallel_three_target_success_promotes_once_and_replay_is_read_only(self):
        barrier = threading.Barrier(3)
        def target(row, manifest):
            barrier.wait(timeout=5); return self.proof(row)
        result = self.run_release(target)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual({t['provider'] for t in result['targets']}, release.PROVIDERS)
        self.assertEqual(len(self.promotions), 1)
        again = self.run_release(lambda *args: self.fail('mutation replayed'))
        self.assertEqual(again['status'], 'verified'); self.assertIn('reverified_at', again)

    def test_one_failure_keeps_all_results_and_never_promotes_or_retries(self):
        result = self.run_release(lambda target, manifest: self.proof(target, 'failed' if target['provider'] == 'gcp' else 'verified'))
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(len(result['targets']), 3)
        self.assertFalse(self.promotions)
        with self.assertRaisesRegex(ValueError, 'RECONCILIATION_REQUIRED'):
            self.run_release(lambda *args: self.fail('failed mutation replayed'))

    def test_ci_promotion_needs_no_provider_config_and_provider_failure_cannot_revert_it(self):
        core = {key: self.manifest[key] for key in ('version', 'source_sha', 'platform_revision', 'images')}
        config = {key: self.config[key] for key in ('version', 'state_dir', 'workers', 'apps')}
        def apply(*args, scope, before_resume=None):
            if scope == 'ci':
                before_resume({'status': 'declarations_verified', 'source_sha': core['source_sha'],
                               'controller_suspended': True, 'runner_jobs': 'preserved'})
            else:
                self.assertEqual(scope, 'credentials')
                self.assertIsNone(before_resume)
            return {'status': 'verified', 'source_sha': core['source_sha'], 'executable_verification': True}
        self.workers.apply_workers = mock.Mock(side_effect=apply)
        self.workers.promote_apps = mock.Mock(return_value={'status': 'verified'})
        def run_ci():
            return release.execute(config, core, scope='ci-runtime', workers=self.workers,
                platform_verify=lambda *args: {'status': 'cluster_verified'},
                target_runner=lambda *args: self.fail('CI rollout accessed a provider'))
        result = run_ci()
        self.assertEqual((result['status'], result['scope'], result['targets']), ('verified', 'ci-runtime', []))
        self.assertEqual(self.workers.promote_apps.call_args.args[2]['status'], 'prepared')
        self.assertEqual(run_ci()['status'], 'verified')
        self.assertEqual(self.workers.apply_workers.call_count, 1)
        # Credential-owner changes do not invalidate the CI proof or enter its reads.
        self.config['workers']['object_uids']['credentials_cron'] = 'changed-by-credential-owner'
        failed = self.run_release(lambda row, _: self.proof(row, 'failed'))
        self.assertEqual(failed['status'], 'incomplete')
        self.assertEqual(run_ci()['status'], 'verified')
        self.assertEqual(self.workers.apply_workers.call_count, 2)
        self.assertEqual(self.workers.apply_workers.call_args.kwargs, {'scope': 'credentials'})
        self.assertEqual(self.workers.promote_apps.call_count, 1)
        self.assertFalse(self.promotions, 'provider release must only read back the prior CI promotion')

    def test_uncertain_ci_promotion_does_not_resume_or_automatically_retry_workers(self):
        core = {key: self.manifest[key] for key in ('version', 'source_sha', 'platform_revision', 'images')}
        def apply(*args, before_resume, scope):
            self.assertEqual(scope, 'ci')
            before_resume({'status': 'declarations_verified'})
            self.fail('worker resumed after uncertain promotion')
        self.workers.apply_workers = mock.Mock(side_effect=apply)
        self.workers.promote_apps = mock.Mock(side_effect=ValueError('PLATFORM_REF_NOT_VERIFIED'))
        def run():
            return release.execute(self.config, core, scope='ci-runtime', workers=self.workers, platform_verify=lambda *args: {})
        result = run()
        self.assertEqual(result['code'], 'PLATFORM_REF_NOT_VERIFIED')
        with self.assertRaisesRegex(ValueError, 'RECONCILIATION_REQUIRED'): run()
        self.assertEqual(self.workers.apply_workers.call_count, 1)

    def test_misbound_receipt_cannot_satisfy_three_provider_gate(self):
        result = self.run_release(lambda target, manifest: {**self.proof(target), 'source_sha': 'f' * 40})
        self.assertEqual(result['status'], 'incomplete'); self.assertFalse(self.promotions)

    def test_target_program_rejects_misbound_runtime_receipt_without_promoting_baseline(self):
        temporary = Path(self.directory.name).resolve()
        target = copy.deepcopy(self.config['targets'][0])
        target['from_policy_file'] = str(temporary / 'from-policy.json')
        target['edge_config_file'] = str(temporary / 'edge-config.json')
        target['config_file'] = str(temporary / 'runtime-config.json')
        before = {'previous_policy': True}
        release.save(Path(target['from_policy_file']), before)
        release.save(Path(target['edge_config_file']), {'edge_kind': 'native'})
        release.save(Path(target['config_file']), {'version': 1})
        edge_proof = {'phase': 'succeeded', 'provider': target['provider'], 'release_sha': self.manifest['source_sha']}
        runtime_proof = {**self.proof(target), 'provider': 'gcp'}
        replies = [SimpleNamespace(returncode=0, stdout=json.dumps(edge_proof)),
                   SimpleNamespace(returncode=0, stdout=json.dumps(runtime_proof))]
        real_path = Path
        fixed_state = '/home/railshot-operator/.local/share/railshot/multicloud-releases'
        def redirected_path(value, *parts):
            return real_path(temporary / 'target-state' if str(value) == fixed_state else value, *parts)
        output = io.StringIO()
        payload = {'target': target, 'release': self.manifest, 'source_root': str(ROOT)}
        # Execute the unchanged production program, replacing only its external processes
        # and fixed operator state root. durable_write still writes real private temp files.
        with mock.patch.dict(sys.modules, {'pathlib': SimpleNamespace(Path=redirected_path)}), \
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, 'stdout', output), \
                mock.patch.object(sys, 'path', list(sys.path)), \
                mock.patch('subprocess.run', side_effect=replies) as run, \
                self.assertRaises(SystemExit) as stopped:
            exec(compile(release.TARGET_PROGRAM, '<target-program>', 'exec'), {})
        self.assertEqual(stopped.exception.code, 1)
        proof = json.loads(output.getvalue())
        self.assertEqual(proof['status'], 'failed')
        self.assertEqual(proof['code'], 'RUNTIME_RECEIPT_BINDING_MISMATCH')
        self.assertEqual(json.loads(real_path(target['from_policy_file']).read_text()), before)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(real_path(run.call_args_list[0].args[0][1]).name, 'edge_update.py')
        self.assertEqual(real_path(run.call_args_list[1].args[0][1]).name, 'runtime-update.py')

    def test_node_only_scope_and_edge_omission_require_explicit_matching_bindings(self):
        target = self.node_target()
        release.validate(self.manifest, self.config)
        for change in ('implicit', 'edge-file', 'binding-file', 'wrong-scope', 'native-kind', 'module-pin'):
            with self.subTest(change=change):
                config, manifest = copy.deepcopy(self.config), copy.deepcopy(self.manifest)
                row = config['targets'][0]
                if change == 'implicit': row.pop('scope')
                elif change == 'edge-file': row['edge_config_file'] = release.OPERATOR_STATE + 'edge.json'
                elif change == 'binding-file': row['binding_file'] = release.OPERATOR_STATE + 'binding.json'
                elif change == 'wrong-scope': row['scope'] = 'node'
                elif change == 'native-kind': manifest['edge_kinds'][target['provider']] = 'native'
                else: manifest['edge_modules'][target['provider']] = 'd' * 64
                with self.assertRaises(ValueError): release.validate(manifest, config)
        result = self.run_release(lambda row, _: self.proof(row))
        self.assertEqual(result['status'], 'verified')
        with self.assertRaisesRegex(ValueError, 'LIVE_READBACK_FAILED'):
            release.execute(self.config, self.manifest, workers=self.workers, platform_verify=lambda *args: {},
                target_verifier=lambda row, _: {k: v for k, v in self.proof(row).items() if k != 'scope'})

    def test_node_only_receipt_scope_mismatch_blocks_promotion(self):
        self.node_target()
        result = self.run_release(lambda row, _: {k: v for k, v in self.proof(row).items() if k != 'scope'})
        self.assertEqual(result['status'], 'incomplete')
        self.assertFalse(self.promotions)

    def test_actual_node_only_program_runs_runtime_without_edge_and_rejects_scope_mismatch(self):
        target = copy.deepcopy(self.node_target())
        temporary = Path(self.directory.name).resolve()
        target['from_policy_file'] = str(temporary / 'from-policy.json')
        target['config_file'] = str(temporary / 'runtime-config.json')
        baseline = {'previous_policy': True}
        runtime_config = {'version': 2, 'scope': 'node-only'}
        real_path = Path
        def redirected_path(value, *parts):
            fixed = '/home/railshot-operator/.local/share/railshot/multicloud-releases'
            return real_path(temporary / 'target-state' if str(value) == fixed else value, *parts)
        def execute(config, proof, verify_only=False, reconcile=False):
            release.save(real_path(target['config_file']), config)
            output = io.StringIO()
            payload = {'target': target, 'release': self.manifest, 'source_root': str(ROOT), 'verify_only': verify_only,
                       'reconcile': reconcile, 'api_image': 'current-approved-api-image'}
            with mock.patch.dict(sys.modules, {'pathlib': SimpleNamespace(Path=redirected_path)}), \
                    mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                    mock.patch.object(sys, 'stdout', output), mock.patch.object(sys, 'path', list(sys.path)), \
                    mock.patch('subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(proof))) as run:
                try:
                    exec(compile(release.TARGET_PROGRAM, '<target-program>', 'exec'), {})
                except AssertionError:
                    run.assert_not_called()
                    raise
                except SystemExit as stopped:
                    return stopped.code, json.loads(output.getvalue()), run
        release.save(real_path(target['from_policy_file']), baseline)
        for invalid in ({'version': 1}, {'version': 1, 'scope': 'node-only'}, {'version': 2, 'scope': 'application'}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(AssertionError, 'runtime scope differs'):
                execute(invalid, self.proof(target))
        bad_proof = self.proof(target); bad_proof.pop('scope')
        code, proof, run = execute(runtime_config, bad_proof)
        self.assertEqual(code, 1)
        self.assertEqual(proof['code'], 'RUNTIME_RECEIPT_BINDING_MISMATCH')
        self.assertEqual(json.loads(real_path(target['from_policy_file']).read_text()), baseline)
        code, proof, run = execute(runtime_config, self.proof(target))
        self.assertEqual(code, 0)
        self.assertEqual(proof['edge'], {'status': 'not_applicable', 'reason': 'node-only'})
        self.assertEqual(run.call_count, 1)
        self.assertEqual(real_path(run.call_args.args[0][1]).name, 'runtime-update.py')
        self.assertEqual(json.loads(real_path(target['from_policy_file']).read_text()), self.manifest['runtime_policy'])
        code, proof, run = execute(runtime_config, self.proof(target), verify_only=True)
        self.assertEqual(code, 0); self.assertEqual(run.call_count, 1)
        self.assertIn('--verify-only', run.call_args.args[0])
        snapshot = {str(p): p.read_bytes() for p in temporary.rglob('*.json')}
        code, proof, run = execute(runtime_config, self.proof(target, 'reconciled'), reconcile=True)
        self.assertEqual((code, proof['status'], run.call_count), (0, 'reconciled', 1))
        self.assertIn('--reconcile', run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['env']['RAILSHOT_RELEASE_API_IMAGE'], 'current-approved-api-image')
        self.assertEqual(snapshot, {str(p): p.read_bytes() for p in temporary.rglob('*.json')})

    def failed_noop_with_current_ci(self):
        for target in self.config['targets']:
            target['scope'] = 'node-only'; target.pop('edge_config_file')
            self.manifest['edge_kinds'][target['provider']] = 'none'
            self.manifest['edge_modules'][target['provider']] = None
        def failed(target, _):
            return {**self.proof(target, 'failed'), 'input_sha256': release.digest(target),
                    'from_policy_sha256': release.digest(self.manifest['runtime_policy']),
                    'to_policy_sha256': release.digest(self.manifest['runtime_policy']),
                    'runtime': {'status': 'verified', 'changed': False, 'after': {'node_uid': target['provider'] + '-node'}}}
        previous = self.run_release(failed)
        home = Path(self.config['state_dir']); state = home / self.manifest['source_sha']
        release.save(state / 'workers.json', {'config': worker_helpers.scoped_config(self.config['workers'], 'credentials')})
        ci_manifest = {key: copy.deepcopy(self.manifest[key]) for key in ('version', 'source_sha', 'platform_revision', 'images')}
        ci_manifest.update(source_sha='e' * 40, platform_revision='f' * 40)
        ci_manifest['images'] = {k: v.replace('c' * 64, 'd' * 64) for k, v in ci_manifest['images'].items()}
        config = {key: self.config[key] for key in ('version', 'state_dir', 'workers', 'apps')}
        config['workers'] = worker_helpers.scoped_config(config['workers'], 'ci')
        current = {'status': 'verified', 'source_sha': ci_manifest['source_sha'],
                   'input_sha256': release.digest({'manifest': ci_manifest, 'config': config})}
        ci_state = home / 'ci-runtime' / ci_manifest['source_sha']; ci_state.mkdir(parents=True, mode=0o700)
        release.save(ci_state / 'manifest.json', ci_manifest); release.save(ci_state / 'receipt.json', current)
        release.save(ci_state / 'workers.json', {'config': config['workers']})
        release.save(home / 'ci-runtime' / 'current.json', current)
        def verified_workers(path):
            expected = ci_manifest if path.parent == ci_state else self.manifest
            return {'status': 'verified', 'executable_verification': True,
                    'source_sha': expected['source_sha'], 'images': expected['images']}
        self.workers.verify_workers = mock.Mock(side_effect=verified_workers)
        self.workers.verify_apps = mock.Mock(return_value={'status': 'verified', 'source_sha': 'e' * 40, 'platform_ref': 'e' * 40})
        by_id = {row['target_id']: row for row in previous['targets']}
        def reconciled(target, _):
            old = by_id[target['target_id']]
            return {**self.proof(target, 'reconciled'), 'input_sha256': old['input_sha256'],
                    'to_policy_sha256': old['to_policy_sha256'], 'after': old['runtime']['after']}
        return state, ci_manifest, reconciled

    def test_explicit_reconciliation_preserves_failure_and_allows_only_next_source(self):
        state, current, target = self.failed_noop_with_current_ci()
        before = {p: p.read_bytes() for p in (state / 'receipt.json', state.parent / 'last-attempt.json')}
        verify = mock.Mock(return_value={'status': 'cluster_verified'})
        result = release.reconcile(self.config, self.manifest, workers=self.workers,
                                   target_reconciler=target, platform_verify=verify)
        self.assertEqual((result['status'], result['current_ci_source_sha']), ('reconciled', 'e' * 40))
        verify.assert_called_with(current['platform_revision'], {k: current['images'][k] for k in ('dashboard', 'api')})
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse((state.parent / 'current.json').exists(), 'old deployment must not become verified')
        ack = (state / 'reconciliation.json').read_bytes()
        release.reconcile(self.config, self.manifest, workers=self.workers, target_reconciler=target, platform_verify=verify)
        self.assertEqual(ack, (state / 'reconciliation.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'RELEASE_RECONCILIATION_REQUIRED'):
            self.run_release(lambda *args: self.fail('failed source replayed'))
        self.manifest['source_sha'] = 'f' * 40
        self.assertEqual(self.run_release(lambda row, _: self.proof(row))['status'], 'verified')
        self.assertEqual(before[state / 'receipt.json'], (state / 'receipt.json').read_bytes())

    def test_partial_reconciliation_and_changed_failure_cannot_clear_barrier(self):
        state, _, target = self.failed_noop_with_current_ci()
        original = (state / 'receipt.json').read_bytes()
        check = self.workers.verify_workers.side_effect
        for field, value in (('source_sha', '0' * 40), ('images', {})):
            self.workers.verify_workers.side_effect = lambda path: {**check(path), field: value}
            with self.subTest(worker_field=field), self.assertRaisesRegex(ValueError, 'WORKER_EXECUTION_NOT_VERIFIED'):
                release.reconcile(self.config, self.manifest, workers=self.workers,
                                  target_reconciler=target, platform_verify=lambda *args: {})
            self.assertFalse((state / 'reconciliation.json').exists())
        self.workers.verify_workers.side_effect = check
        for field, value in (('status', 'unknown'), ('source_sha', 'b' * 40), ('input_sha256', '0' * 64),
                             ('after', {'node_uid': 'other-node'})):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'THREE_NODE_RECONCILIATION_NOT_VERIFIED'):
                release.reconcile(self.config, self.manifest, workers=self.workers, platform_verify=lambda *args: {},
                    target_reconciler=lambda row, m: {**target(row, m), **({field: value} if row['provider'] == 'gcp' else {})})
            self.assertFalse((state / 'reconciliation.json').exists())
            self.assertEqual(original, (state / 'receipt.json').read_bytes())
        release.reconcile(self.config, self.manifest, workers=self.workers, target_reconciler=target, platform_verify=lambda *args: {})
        saved = release.private(state / 'receipt.json'); saved['error_type'] = 'new-observation'
        release.save(state / 'receipt.json', saved)
        self.manifest['source_sha'] = 'f' * 40
        with self.assertRaisesRegex(ValueError, 'PRIOR_RELEASE_RECONCILIATION_REQUIRED'):
            self.run_release(lambda *args: self.fail('changed failure acknowledged'))

    def test_new_source_cannot_bypass_prior_uncertain_release(self):
        self.assertEqual(self.run_release(lambda row, _: self.proof(row, 'unknown'))['status'], 'incomplete')
        self.manifest['source_sha'] = 'e' * 40
        with self.assertRaisesRegex(ValueError, 'PRIOR_RELEASE_RECONCILIATION_REQUIRED'):
            self.run_release(lambda *args: self.fail('new source bypassed unresolved release'))
        self.assertFalse(self.promotions)

    def test_missing_provider_or_mutated_pin_rejected_before_workers(self):
        for field in ('targets', 'policy'):
            with self.subTest(field=field):
                config, manifest = copy.deepcopy(self.config), copy.deepcopy(self.manifest)
                if field == 'targets': config['targets'].pop()
                else: manifest['runtime_policy']['runtime']['k3s_version'] = 'latest'
                with self.assertRaises(ValueError): release.validate(manifest, config)

    def test_verified_release_requires_live_health_and_cannot_replay_after_new_attempt(self):
        self.run_release(lambda row, _: self.proof(row))
        with self.assertRaisesRegex(ValueError, 'LIVE_READBACK_FAILED'):
            release.execute(self.config, self.manifest, workers=self.workers,
                platform_verify=lambda *args: {}, target_verifier=lambda row, _: self.proof(row, 'failed'))
        release.save(Path(self.config['state_dir']) / 'last-attempt.json', {'input_sha256': 'later-incomplete'})
        with self.assertRaisesRegex(ValueError, 'SUPERSEDED_RECONCILIATION_REQUIRED'):
            self.run_release(lambda *args: self.fail('mutation replayed'))

    def test_host_program_compiles_and_tool_download_rejects_wrong_checksum(self):
        compile(release.TARGET_PROGRAM, '<target>', 'exec')
        tools = load('bootstrap-release-tools')
        body = b'tested tool bytes'
        self.assertEqual(tools.checked(body, tools.hashlib.sha256(body).hexdigest()), body)
        with self.assertRaisesRegex(ValueError, 'CHECKSUM_MISMATCH'):
            tools.checked(body, '0' * 64)


class AdmissionTests(unittest.TestCase):
    def test_actions_skip_only_superseded_and_keep_other_failures_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            summary = Path(directory) / 'summary'
            args = ['--source-sha', 'a' * 40, '--ref', 'refs/heads/main']
            for error, skip, expected_status, expected_exit in (
                (None, True, 'admitted', 0),
                (ValueError('SUPERSEDED_RELEASE_SOURCE'), True, 'superseded', 0),
                (ValueError('SUPERSEDED_RELEASE_SOURCE'), False, 'blocked', 1),
                (ValueError('EXACT_SOURCE_CI_GATE_REQUIRED'), True, 'blocked', 1),
                (ValueError('TRUSTED_RELEASE_SOURCE_REQUIRED'), True, 'blocked', 1),
                (OSError('network unavailable'), True, 'blocked', 1),
            ):
                with self.subTest(error=error, skip=skip):
                    output.write_text(''); summary.write_text('')
                    with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary)}), \
                         mock.patch.object(admission, 'admit', side_effect=error, return_value={'status': 'admitted'}), \
                         mock.patch('sys.stdout', new_callable=io.StringIO) as stdout:
                        code = admission.main(args + (['--skip-superseded'] if skip else []))
                    self.assertEqual(code, expected_exit)
                    self.assertEqual(json.loads(stdout.getvalue())['status'], expected_status)
                    self.assertEqual(output.read_text(), ('admitted=' + str(expected_status == 'admitted').lower() + '\n') if skip else '')
                    self.assertEqual(bool(summary.read_text()), expected_status == 'superseded')
            with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': ''}), mock.patch.object(admission, 'admit') as checked, \
                 mock.patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                admission.main(args + ['--skip-superseded'])
            checked.assert_not_called()

    def test_requires_current_sha_trusted_push_latest_attempt_gate(self):
        sha = 'a' * 40
        run = {'id': 12, 'run_attempt': 2, 'head_sha': sha, 'head_branch': 'main', 'head_repository': {'full_name': admission.REPOSITORY},
               'path': '.github/workflows/railshot-ci.yml', 'event': 'push', 'conclusion': None}
        jobs = {'total_count': 1, 'jobs': [{'name': 'Railshot CI gate', 'status': 'completed', 'conclusion': 'success'}]}
        def read(path):
            if path == 'git/ref/heads/main': return {'object': {'sha': sha}}
            if path == 'actions/runs/12': return run
            self.assertEqual(path, 'actions/runs/12/attempts/2/jobs?per_page=100'); return jobs
        self.assertEqual(admission.admit(sha, 'refs/heads/main', 12, read=read)['ci_run_attempt'], 2)
        for change in ({'event': 'pull_request'}, {'head_sha': 'b' * 40}, {'conclusion': 'failure'}):
            old = dict(run); run.update(change)
            with self.assertRaises(ValueError): admission.admit(sha, 'refs/heads/main', 12, read=read)
            run.clear(); run.update(old)
        jobs['jobs'][0]['conclusion'] = 'failure'
        with self.assertRaises(ValueError): admission.admit(sha, 'refs/heads/main', 12, read=read)


if __name__ == '__main__':
    unittest.main()
