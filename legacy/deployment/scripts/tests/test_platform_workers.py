"""Exercise worker CAS/readback and apps promotion locally; no cloud credentials."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('platform_workers', ROOT / 'deployment/scripts/platform_workers.py')
workers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workers)
SHA = 'd' * 40
IMAGES = {name: 'ghcr.io/jasmin-softbank/railshot-' + name + '@sha256:' + 'b' * 64 for name in ('api', 'ci-runner')}
OLD = {name: value.replace('b' * 64, 'a' * 64) for name, value in IMAGES.items()}
POLICY = {'version': 1, 'targets': [{'secret': 'railshot-k3s-gcp', 'target_id': 'k3s-gcp', 'server': 'https://10.66.0.2:6443',
          'project': 'tenant-demo', 'namespaces': ['tenant-demo'], 'service_account': {'name': 'argo', 'namespace': 'tenant-demo',
          'uid': '12345678-1234-1234-1234-123456789012'}, 'ca_sha256': 'a' * 64, 'audiences': ['https://kubernetes.default.svc']}]}


class Cluster:
    def __init__(self):
        rendered = workers.renderer.render_build_controller(OLD, 'https://github.com/Jasmin-Softbank/railshot-apps', 'build-01')['items']
        rendered += workers.credentials.render(POLICY, OLD['api'])['items']
        self.objects = {}
        for key, identity in workers.KEYS.items():
            item = copy.deepcopy(next(obj for obj in rendered if (obj['kind'], obj['metadata'].get('namespace'), obj['metadata']['name']) == identity))
            item['metadata'].update(uid=key + '-uid', resourceVersion='1', annotations={'operator': 'preserved'})
            self.objects[key] = item
        self.original = copy.deepcopy(self.objects)
        self.calls = []
        self.failure = None
        self.uncertain = False
        self.jobs = {'build_cron': {}, 'credentials_cron': {}}
        self.auto_build = True
        self.auto_renew = False
        self.wrong_digest = False
        self.renewal_failed = False
        self.create_rejected = False
        self.create_uncertain = False

    def job(self, key, name):
        cron = self.objects[key]
        return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'namespace': cron['metadata']['namespace'],
                'uid': name + '-uid', 'creationTimestamp': '2026-10-03T00:00:00Z',
                'ownerReferences': [{'kind': 'CronJob', 'uid': cron['metadata']['uid'], 'controller': True}]},
                'spec': copy.deepcopy(cron['spec']['jobTemplate']['spec']),
                'status': {'conditions': [{'type': 'Complete', 'status': 'True'}]}}

    def __call__(self, action, key, document=None):
        self.calls.append((action, key))
        if action == 'jobs':
            cron = self.objects[key]
            if (self.auto_build if key == 'build_cron' else self.auto_renew) and not cron['spec'].get('suspend') and workers.container(cron['spec'])['image'] == IMAGES['api']:
                name = key + '-scheduled'
                self.jobs[key].setdefault(name, self.job(key, name))
            return copy.deepcopy(list(self.jobs[key].values()))
        if action == 'job':
            return copy.deepcopy(self.jobs[key].get(document))
        if action == 'create-job':
            if self.create_rejected:
                raise RuntimeError('create response unknown')
            job = copy.deepcopy(document)
            job['metadata']['uid'] = job['metadata']['name'] + '-uid'
            job['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
            self.jobs[key][job['metadata']['name']] = job
            if self.create_uncertain:
                raise RuntimeError('lost create acknowledgement')
            return copy.deepcopy(job)
        if action == 'pods':
            job = self.jobs[key][document]
            containers = copy.deepcopy(job['spec']['template']['spec']['containers'])
            return [{'metadata': {'name': document + '-pod', 'uid': document + '-pod-uid',
                     'ownerReferences': [{'kind': 'Job', 'uid': job['metadata']['uid'], 'controller': True}]},
                     'spec': {'containers': containers}, 'status': {'phase': 'Succeeded', 'containerStatuses': [
                         {'name': containers[0]['name'], 'state': {'terminated': {'exitCode': 0}},
                          'imageID': OLD['api'] if self.wrong_digest else containers[0]['image']} ]}}]
        if action == 'logs':
            return json.dumps({'results': [{'secret': target['secret'], 'status': 'unchanged' if self.renewal_failed else 'renewed'}
                                         for target in POLICY['targets']]})
        if action == 'patch':
            if self.failure == key:
                raise RuntimeError('mutation rejected')
            obj = self.objects[key]
            for operation in document[:-1]:
                value = obj
                for part in operation['path'].strip('/').split('/'):
                    value = value[part]
                if value != operation['value']:
                    raise RuntimeError('CAS rejected')
            operation = document[-1]
            obj[operation['path'].strip('/')] = copy.deepcopy(operation['value'])
            obj['metadata']['resourceVersion'] = str(int(obj['metadata']['resourceVersion']) + 1)
            if self.uncertain:
                raise RuntimeError('response lost after write')
        return copy.deepcopy(self.objects[key])


class Apps:
    def __init__(self):
        self.content = b'old workflow with ${{ vars.PLATFORM_REF }}\n'
        self.original = self.content
        self.variable = {'name': 'PLATFORM_REF', 'value': 'a' * 40, 'updated_at': 'old'}
        self.calls = []
        self.variable_reads = 0
        self.concurrent_variable = False
        self.uncertain_workflow = False

    def workflow(self):
        return {'sha': hashlib.sha1(b'blob ' + str(len(self.content)).encode() + b'\0' + self.content).hexdigest(),
                'content': base64.b64encode(self.content).decode()}

    def __call__(self, path, method='GET', body=None):
        self.calls.append((method, path, body))
        if '/contents/' in path:
            if method == 'PUT':
                if body['sha'] != self.workflow()['sha']:
                    raise RuntimeError('content CAS failed')
                self.content = base64.b64decode(body['content'])
                if self.uncertain_workflow:
                    raise RuntimeError('lost acknowledgement')
            return self.workflow()
        if method == 'GET':
            self.variable_reads += 1
            if self.concurrent_variable and self.variable_reads == 2:
                self.variable['value'] = 'c' * 40
                self.variable['updated_at'] = 'concurrent'
        else:
            self.variable.update(value=body['value'], updated_at=self.variable['updated_at'] + '-next')
        return copy.deepcopy(self.variable)


class WorkerTests(unittest.TestCase):
    def test_github_token_padding_is_normalized_and_invalid_headers_do_not_escape(self):
        with patch.dict(workers.os.environ, {'GITHUB_TOKEN': '  gho_test_token\n'}), patch.object(workers, 'build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{}'
            self.assertEqual(workers.github('repos/Jasmin-Softbank/railshot-apps/'), {})
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('Authorization'), 'Bearer gho_test_token')
        for invalid in ('gho_test\ntoken', 'gho_test\rtoken', 'gho_test\ttoken', '  ', None):
            with self.subTest(invalid=invalid), patch.dict(workers.os.environ, {'GITHUB_TOKEN': invalid or ''}), patch.object(workers, 'build_opener') as opener:
                with self.assertRaisesRegex(workers.bootstrap.Blocked, '^GITHUB_TOKEN_INVALID$'):
                    workers.github('repos/Jasmin-Softbank/railshot-apps/')
                opener.assert_not_called()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'state.json'
        self.cluster = Cluster()
        self.config = workers.discover_workers(kube=self.cluster)

    def apply(self):
        return workers.apply_workers(self.config, IMAGES, SHA, self.state, kube=self.cluster)

    def assert_restored(self):
        for key, original in self.cluster.original.items():
            field = 'data' if original['kind'] == 'ConfigMap' else 'spec'
            self.assertEqual(self.cluster.objects[key][field], original[field])
            self.assertEqual(self.cluster.objects[key]['metadata']['annotations'], original['metadata']['annotations'])

    def test_promote_exact_digests_preserves_policy_and_explicit_rollback(self):
        result = self.apply()
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['scope'], 'worker_execution')
        self.assertTrue(result['executable_verification'])
        for key in ('build_cron', 'credentials_cron'):
            self.assertEqual(workers.container(self.cluster.objects[key]['spec'])['image'], IMAGES['api'])
        data = self.cluster.objects['build_config']['data']
        policy = json.loads(data['policy.json'])
        self.assertEqual(policy['image'], IMAGES['ci-runner'])
        self.assertEqual(policy['template_sha256'], hashlib.sha256(data['job.json'].encode()).hexdigest())
        self.assertEqual(self.cluster.objects['credentials_config'], self.cluster.original['credentials_config'])
        self.assertNotIn('suspend', self.cluster.objects['build_cron']['spec'])
        self.assertEqual({key for _, key in self.cluster.calls}, set(workers.KEYS))
        workers.rollback_workers(self.state, kube=self.cluster)
        self.assert_restored()

    def test_ci_pins_workflow_while_suspended_and_preserves_running_runner_job(self):
        self.cluster.calls.clear()  # Discovery is an operator preflight, outside this rollout.
        self.cluster.objects['credentials_config']['data']['policy.json'] = 'provider policy not ready'
        running = {'metadata': {'name': 'existing-runner', 'uid': 'existing-runner-uid'},
                   'spec': {'template': {'spec': {'containers': [{'image': OLD['ci-runner']}]}}},
                   'status': {'active': 1}}
        self.cluster.jobs['build_cron']['existing-runner'] = copy.deepcopy(running)
        apps = Apps()
        template = (ROOT / 'ci/workflows/railshot-deploy.yml').read_bytes()
        def promote(prepared):
            self.assertIs(self.cluster.objects['build_cron']['spec']['suspend'], True)
            self.assertEqual(workers.container(self.cluster.objects['build_cron']['spec'])['image'], IMAGES['api'])
            self.assertEqual(json.loads(self.cluster.objects['build_config']['data']['policy.json'])['image'], IMAGES['ci-runner'])
            proof = workers.promote_apps({'repository': 'Jasmin-Softbank/railshot-apps', 'branch': 'main'}, SHA,
                {'scope': 'ci-runtime', 'status': 'prepared', 'source_sha': SHA, 'workers': prepared},
                Path(self.temp.name) / 'apps.json', gh=apps)
            self.assertEqual(proof['platform_ref'], SHA)
        with patch.object(workers.bootstrap, 'native', side_effect=lambda command, **_: SHA.encode() if command[1] == 'rev-parse' else template):
            result = workers.apply_workers(self.config, IMAGES, SHA, self.state, kube=self.cluster, before_resume=promote, scope='ci')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.cluster.jobs['build_cron']['existing-runner'], running)
        self.assertEqual(apps.content.count(SHA.encode()), 5)
        self.assertEqual(apps.variable['value'], SHA)
        self.assertNotIn('suspend', self.cluster.objects['build_cron']['spec'])
        self.assertFalse(any(key.startswith('credentials_') for _, key in self.cluster.calls))

    def test_ci_promotion_failure_keeps_replenishment_suspended(self):
        def fail(_): raise ValueError('PLATFORM_REF_NOT_VERIFIED')
        with self.assertRaisesRegex(ValueError, 'PLATFORM_REF_NOT_VERIFIED'):
            workers.apply_workers(self.config, IMAGES, SHA, self.state, kube=self.cluster, before_resume=fail, scope='ci')
        self.assertIs(self.cluster.objects['build_cron']['spec']['suspend'], True)
        self.assertEqual(workers.bootstrap.private(self.state)['status'], 'incomplete')

    def test_provider_followup_updates_only_credentials_without_touching_ci(self):
        self.cluster.calls.clear()
        result = workers.apply_workers(self.config, IMAGES, SHA, self.state, kube=self.cluster, scope='credentials')
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(set(result['executions']), {'credentials_cron'})
        self.assertFalse(any(key.startswith('build_') for _, key in self.cluster.calls))
        self.assertEqual(self.cluster.objects['build_cron'], self.cluster.original['build_cron'])

    def test_original_suspended_state_is_retained(self):
        self.cluster.objects['build_cron']['spec']['suspend'] = True
        self.cluster.objects['credentials_cron']['spec']['suspend'] = True
        result = self.apply()
        self.assertIs(self.cluster.objects['build_cron']['spec']['suspend'], True)
        self.assertEqual(result['status'], 'declarations_verified')
        self.assertFalse(result['executable_verification'])
        self.assertEqual(result['scope'], 'declarations/suspended')
        self.assertFalse(any(action == 'create-job' for action, _ in self.cluster.calls))

    def test_replaced_resource_fails_before_any_patch(self):
        self.config['object_uids']['build_cron'] = 'different'
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'WORKER_UID_DIFFERS'):
            self.apply()
        self.assertFalse(any(action == 'patch' for action, _ in self.cluster.calls))

    def test_uncertain_patch_is_read_back_without_replay(self):
        self.cluster.uncertain = True
        self.apply()
        self.assertEqual(sum(action == 'patch' for action, _ in self.cluster.calls), 5)

    def test_partial_failure_keeps_receipt_and_rolls_back_prior_changes(self):
        self.cluster.failure = 'credentials_cron'
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'WORKER_PATCH_NOT_VERIFIED'):
            self.apply()
        saved = workers.bootstrap.private(self.state)
        self.assertEqual(saved['status'], 'incomplete')
        self.assertIs(self.cluster.objects['build_cron']['spec']['suspend'], True)
        self.cluster.failure = None
        workers.rollback_workers(self.state, kube=self.cluster)
        self.assert_restored()

    def test_rollback_refuses_concurrent_operator_change(self):
        self.apply()
        self.cluster.objects['build_cron']['spec']['schedule'] = '*/5 * * * *'
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'WORKER_ROLLBACK_CONCURRENT_CHANGE'):
            workers.rollback_workers(self.state, kube=self.cluster)
        self.assertEqual(self.cluster.objects['build_cron']['spec']['schedule'], '*/5 * * * *')

    def test_active_controller_blocks_before_template_change(self):
        self.cluster.objects['build_cron']['status'] = {'active': [{'uid': 'active-controller'}]}
        with patch.object(workers.time, 'monotonic', side_effect=[0, 66]):
            with self.assertRaisesRegex(workers.bootstrap.Blocked, 'BUILD_CONTROLLER_STILL_ACTIVE'):
                self.apply()
        self.assertEqual(self.cluster.objects['build_config'], self.cluster.original['build_config'])
        workers.rollback_workers(self.state, kube=self.cluster)
        self.assert_restored()

    def test_verify_rejects_policy_drift(self):
        self.apply()
        self.cluster.objects['credentials_config']['data']['policy.json'] = '{}'
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'WORKER_READBACK_DIFFERS'):
            workers.verify_workers(self.state, kube=self.cluster)

    def test_new_scheduled_renewal_is_used_without_creating_sample(self):
        self.cluster.auto_renew = True
        result = self.apply()
        self.assertEqual(result['executions']['credentials_cron']['renewed'], ['railshot-k3s-gcp'])
        self.assertFalse(any(action == 'create-job' for action, _ in self.cluster.calls))

    def test_sample_creation_response_loss_is_observed_without_second_create(self):
        self.cluster.create_uncertain = True
        self.apply()
        workers.verify_workers(self.state, kube=self.cluster)
        self.assertEqual(sum(action == 'create-job' for action, _ in self.cluster.calls), 1)

    def test_unobserved_sample_create_keeps_intent_and_never_retries(self):
        self.cluster.create_rejected = True
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'WORKER_SAMPLE_CREATE_UNKNOWN_NO_REPLAY'):
            self.apply()
        state = workers.bootstrap.private(self.state)
        self.assertEqual(state['executions']['credentials_cron']['status'], 'create_unknown')
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'WORKER_SAMPLE_CREATE_UNKNOWN_NO_REPLAY'):
            workers.verify_workers(self.state, kube=self.cluster)
        self.assertEqual(sum(action == 'create-job' for action, _ in self.cluster.calls), 1)

    def test_wrong_running_digest_cannot_verify_worker(self):
        self.cluster.wrong_digest = True
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'WORKER_JOB_IMAGE_DIGEST_DIFFERS'):
            self.apply()
        self.assertEqual(workers.bootstrap.private(self.state)['status'], 'incomplete')

    def test_partial_renewal_cannot_verify_worker(self):
        self.cluster.renewal_failed = True
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'WORKER_RENEWAL_TARGETS_NOT_VERIFIED'):
            self.apply()

    def test_preexisting_success_is_not_new_execution_proof(self):
        self.cluster.auto_build = False
        self.cluster.objects['build_cron']['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['image'] = IMAGES['api']
        self.cluster.jobs['build_cron']['old-success'] = self.cluster.job('build_cron', 'old-success')
        with patch.object(workers.time, 'monotonic', side_effect=[0, 1, 182]):
            with self.assertRaisesRegex(workers.bootstrap.Blocked, 'WORKER_EXECUTION_TIMEOUT'):
                self.apply()


class AppsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name).resolve() / 'promotion.json'
        self.apps = Apps()
        self.config = {'repository': 'Jasmin-Softbank/railshot-apps', 'branch': 'main'}
        self.receipt = {'status': 'verified', 'source_sha': SHA,
                        'targets': [{'provider': p, 'target_id': 'k3s-' + p, 'status': 'verified', 'release_sha': SHA}
                                    for p in ('aws', 'gcp', 'openstack')]}
        template = (ROOT / 'ci/workflows/railshot-deploy.yml').read_bytes()
        self.native = patch.object(workers.bootstrap, 'native', side_effect=lambda command, **_: SHA.encode() if command[1] == 'rev-parse' else template)
        self.native.start()
        self.addCleanup(self.native.stop)

    def promote(self):
        return workers.promote_apps(self.config, SHA, self.receipt, self.state, gh=self.apps)

    def test_three_verified_targets_promote_all_pins_and_rollback(self):
        result = self.promote()
        self.assertEqual(result['platform_ref'], SHA)
        self.assertNotIn(b'vars.PLATFORM_REF', self.apps.content)
        self.assertEqual(self.apps.content.count(SHA.encode()), 5)
        self.assertEqual(self.apps.variable['value'], SHA)
        workers.rollback_apps(self.state, gh=self.apps)
        self.assertEqual(self.apps.content, self.apps.original)
        self.assertEqual(self.apps.variable['value'], 'a' * 40)

    def test_missing_failed_duplicate_and_wrong_revision_targets_block_writes(self):
        for targets in (self.receipt['targets'][:2], [self.receipt['targets'][0]] * 3,
                        [{**r, 'status': 'failed'} for r in self.receipt['targets']],
                        [{**r, 'release_sha': 'e' * 40} for r in self.receipt['targets']]):
            with self.subTest(targets=targets):
                with self.assertRaisesRegex(workers.bootstrap.Blocked, 'THREE_TARGET_RELEASE_NOT_VERIFIED'):
                    workers.promote_apps(self.config, SHA, {**self.receipt, 'targets': targets}, self.state, gh=self.apps)
        self.assertEqual(self.apps.calls, [])

    def test_variable_concurrent_change_is_preserved_and_partial_state_saved(self):
        self.apps.concurrent_variable = True
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'PLATFORM_REF_CONCURRENT_CHANGE'):
            self.promote()
        self.assertEqual(workers.bootstrap.private(self.state)['status'], 'incomplete')
        self.assertEqual(self.apps.variable['value'], 'c' * 40)
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.apps.calls))
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'PLATFORM_REF_CONCURRENT_CHANGE'):
            workers.rollback_apps(self.state, gh=self.apps)

    def test_lost_workflow_acknowledgement_never_repeats_write(self):
        self.apps.uncertain_workflow = True
        self.promote()
        self.assertEqual(sum(method == 'PUT' for method, _, _ in self.apps.calls), 1)

    def test_promotion_readback_detects_later_drift_without_writes(self):
        self.promote()
        self.assertEqual(workers.verify_apps(self.state, gh=self.apps)['status'], 'verified')
        self.apps.variable['value'] = 'f' * 40
        writes = sum(method != 'GET' for method, _, _ in self.apps.calls)
        with self.assertRaisesRegex(workers.bootstrap.Blocked, 'APPS_PROMOTION_READBACK_DIFFERS'):
            workers.verify_apps(self.state, gh=self.apps)
        self.assertEqual(writes, sum(method != 'GET' for method, _, _ in self.apps.calls))


if __name__ == '__main__':
    unittest.main()
