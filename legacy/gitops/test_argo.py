import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import argo
import handoff
from execution import GATE_ORDER


class NativeTimeoutTest(unittest.TestCase):
    def test_default_and_long_operation_timeout_reach_subprocess(self):
        for options, expected in [({}, 30), ({'timeout': 300}, 300)]:
            with self.subTest(timeout=expected), patch('argo.subprocess.run',
                    return_value=subprocess.CompletedProcess(['fixture'], 0, stdout='ok')) as run:
                self.assertEqual(argo.native(['fixture'], document={'version': 1}, **options), 'ok')
                self.assertEqual(run.call_args.kwargs['timeout'], expected)
                self.assertEqual(json.loads(run.call_args.kwargs['input']), {'version': 1})
        with patch('argo.subprocess.run', side_effect=subprocess.TimeoutExpired(['fixture'], 300)):
            with self.assertRaisesRegex(RuntimeError, 'observe before retrying'):
                argo.native(['fixture'], timeout=300)


class ArgoTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.prepare()

    def prepare(self, database=False, image_digest='c' * 64, migration_command=None, storage=False):
        published = self.root / 'published'; published.mkdir(exist_ok=True)
        spec = {'apiVersion': 'railshot/v0', 'app': 'demo', 'services': [
            {'name': 'web', 'build': {'dockerfile': 'Dockerfile'}, 'port': 8080, 'route': '/health', 'health': '/health'}]}
        if database:
            spec['resources'] = {'postgres': {'size': 'small'}}
            spec['services'][0].update(migrate={'command': migration_command or ['python', 'migrate.py']}, secrets=['DATABASE_URL'])
        if storage:
            spec['services'][0]['storage'] = {'mountPath': '/var/opt/memos', 'sizeGi': 1}
        verdict = {'release_eligible': True, 'ok': True, 'status': 'PASS', 'source_sha256': 'a' * 64,
                   'layers': [{'layer': layer, 'ok': True} for layer in GATE_ORDER],
                   'images': {'web': 'local/web:test'}, 'image_ids': {'web': 'sha256:' + 'b' * 64}}
        values = {'railshot.yaml': spec, 'verdict.json': verdict,
                  'images.json': {'web': 'ghcr.io/example/web@sha256:' + image_digest}}
        data = {k: json.dumps(v).encode() for k, v in values.items()}
        manifest = {'version': 1, 'trust': handoff.TRUST, 'source_sha256': verdict['source_sha256'],
                    'images': {'web': {'id': verdict['image_ids']['web'], 'local_ref': 'local/web:test'}},
                    'files': {k: hashlib.sha256(v).hexdigest() for k, v in data.items() if k != 'images.json'}}
        data['manifest.json'] = json.dumps(manifest).encode()
        receipt = {'version': 2, 'status': 'published', 'run_id': 1, 'producer_attempt': 1, 'bundle_artifact_id': 2,
                   'source_commit': 'd' * 40, 'target_id': 'k3s-aws', 'tenant': 'team', 'app': 'demo',
                   'files': {k: hashlib.sha256(v).hexdigest() for k, v in data.items()}}
        receipt['registry'] = {'visibility': 'public', 'verification': 'anonymous_manifest_read',
                               'images_sha256': receipt['files']['images.json'], 'image_pull_secret': None}
        data['handoff.json'] = json.dumps(receipt).encode()
        for name, raw in data.items():
            (published / name).write_bytes(raw)
        target = {'id': 'k3s-aws', 'namespace': 'tenant-demo', 'argocd_namespace': 'argocd', 'project': 'railshot',
                  'architecture': 'amd64', 'repo_url': 'https://github.com/example/config.git',
                  'cluster_server': 'https://192.0.2.1:6443', 'path': 'targets/k3s-aws/demo',
                  'revision': 'e' * 40, 'node_port': 30080, 'ingress_cidrs': ['10.20.0.0/24'],
                  'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}}}
        if database:
            target['database'] = {'host': '10.20.0.10', 'port': 5432, 'runtime_secret': 'demo-runtime',
                                  'migration_secret': 'demo-migration', 'ca_secret': 'demo-ca'}
        rendered = handoff.render(published, target)
        self.review = {'application': rendered.pop('application'), 'workload': rendered.pop('workload'), 'receipt': rendered}
        self.directory = self.root / 'review'; self.save_review(self.review, self.directory)

    def save_review(self, review, directory):
        review['receipt']['documents'] = {name: handoff.document_hash(review[name]) for name in ('application', 'workload')}
        directory.mkdir(exist_ok=True)
        for name, value in review.items():
            (directory / (name + '.json')).write_text(json.dumps(value))

    def healthy(self, review=None):
        review = review or self.review
        app = copy.deepcopy(review['application']); spec = app['spec']
        app['status'] = {'sync': {'status': 'Synced', 'revision': spec['source']['targetRevision'],
                                 'comparedTo': {'source': copy.deepcopy(spec['source']), 'destination': copy.deepcopy(spec['destination'])}},
                         'health': {'status': 'Healthy'}, 'operationState': {'phase': 'Succeeded',
                         'syncResult': {'revision': spec['source']['targetRevision']}},
                         'resources': [{'group': item['apiVersion'].split('/')[0] if '/' in item['apiVersion'] else '',
                                        'kind': item['kind'], 'name': item['metadata']['name'],
                                        'namespace': spec['destination']['namespace'], 'status': 'Synced',
                                        **({'health': {'status': 'Healthy'}} if item['kind'] in ('Deployment', 'Job') else {})}
                                       for item in review['workload']['items']],
                         'summary': {'images': [item['spec']['template']['spec']['containers'][0]['image']
                                                for item in review['workload']['items'] if item['kind'] == 'Deployment']}}
        app['status']['operationState']['syncResult']['resources'] = copy.deepcopy(app['status']['resources'])
        app['status']['operationState']['syncResult']['resources'][0]['images'] = app['status']['summary']['images'][:]
        return app

    def test_database_rollout_orders_policy_migration_and_runtime_without_credentials(self):
        self.prepare(database=True)
        review = argo.load_review(self.directory)
        items = {item['kind']: item for item in review['workload']['items']}
        db = review['receipt']['database']
        self.assertEqual(items['NetworkPolicy']['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '-2')
        self.assertEqual(items['NetworkPolicy']['spec']['egress'], [{'to': [{'ipBlock': {'cidr': '10.20.0.10/32'}}],
                                                                  'ports': [{'protocol': 'TCP', 'port': 5432}]}])
        self.assertEqual(items['Job']['metadata']['annotations']['argocd.argoproj.io/sync-wave'], '-1')
        self.assertEqual(items['Job']['metadata']['annotations']['argocd.argoproj.io/compare-options'], 'IgnoreExtraneous')
        migration = items['Job']['spec']['template']['spec']['containers'][0]
        runtime = items['Deployment']['spec']['template']['spec']['containers'][0]
        self.assertEqual(migration['image'], runtime['image'])
        self.assertEqual(migration['command'], ['python', 'migrate.py'])
        self.assertNotIn(db['migration_secret'], json.dumps(runtime))
        self.assertNotIn(db['runtime_secret'], json.dumps(migration))
        self.assertNotIn('postgresql://', json.dumps(review))
        self.assertEqual(items['Job']['spec']['backoffLimit'], 0)
        self.assertEqual(items['Job']['spec']['template']['spec']['restartPolicy'], 'Never')
        self.assertNotEqual(items['Service']['spec']['selector'], items['Job']['spec']['template']['metadata']['labels'])
        project = argo.projects([review])['items'][0]
        self.assertIn(argo.JOB_KIND, project['spec']['namespaceResourceWhitelist'])
        argo.validate_project(project, review['application'], review['workload'])
        project['spec']['namespaceResourceWhitelist'].remove(argo.JOB_KIND)
        with self.assertRaises(ValueError): argo.validate_project(project, review['application'], review['workload'])
        live = self.healthy()
        self.assertEqual(argo.observe(review, live)['migration']['state'], 'succeeded')
        for field in ('missing_job', 'failed_job', 'old_job', 'still_running'):
            bad = copy.deepcopy(live)
            if field == 'missing_job': bad['status']['operationState']['syncResult']['resources'].pop()
            elif field == 'failed_job': bad['status']['resources'][-1]['health']['status'] = 'Degraded'
            elif field == 'old_job': bad['status']['operationState']['syncResult']['resources'][-1]['name'] += '-old'
            else: bad['status']['operationState']['phase'] = 'Running'
            with self.subTest(field=field): self.assertFalse(argo.observe(review, bad)['deployed'])
        for field in ('retry', 'image', 'secret', 'privileged', 'missing_compare', 'hook', 'ignore_health'):
            bad = copy.deepcopy(review)
            job = bad['workload']['items'][-1]
            if field == 'retry': job['spec']['backoffLimit'] = 1
            elif field == 'image': job['spec']['template']['spec']['containers'][0]['image'] = 'unreviewed:latest'
            elif field == 'secret': job['spec']['template']['spec']['containers'][0]['env'][-1]['valueFrom']['secretKeyRef']['name'] = db['runtime_secret']
            elif field == 'privileged': job['spec']['template']['spec']['containers'][0]['securityContext']['privileged'] = True
            elif field == 'missing_compare': job['metadata']['annotations'].pop('argocd.argoproj.io/compare-options')
            elif field == 'hook': job['metadata']['annotations']['argocd.argoproj.io/hook'] = 'PreSync'
            else: job['metadata']['annotations']['argocd.argoproj.io/ignore-healthcheck'] = 'true'
            self.save_review(bad, self.directory)
            with self.subTest(field=field), self.assertRaises(ValueError): argo.load_review(self.directory)

    def test_retained_migrations_require_owned_completed_jobs_and_current_deployment_images(self):
        self.prepare(database=True)
        live = self.healthy()
        old = {'group': 'batch', 'kind': 'Job', 'namespace': 'tenant-demo', 'name': 'demo-migrate-0123456789ab',
               'requiresPruning': True, 'status': 'OutOfSync', 'health': {'status': 'Healthy'}}
        live['status']['resources'].append(old)
        live['status']['summary']['images'].append('ghcr.io/example/old@sha256:' + 'a' * 64)
        self.assertTrue(argo.observe(self.review, live)['deployed'])
        for field in ('group', 'kind', 'namespace', 'name', 'ownership', 'hook', 'health_missing', 'health_running',
                      'health_failed', 'duplicate', 'current_job', 'images_missing', 'images_old', 'deployment_missing',
                      'deployment_duplicate', 'conflicting_deployment', 'conflicting_job', 'aggregate_health', 'aggregate_sync'):
            bad = copy.deepcopy(live); retired = bad['status']['resources'][-1]
            synced = bad['status']['operationState']['syncResult']['resources']
            if field in ('group', 'kind', 'namespace', 'name'): retired[field] = 'foreign'
            elif field == 'ownership': retired['requiresPruning'] = False
            elif field == 'hook': retired['hook'] = True
            elif field == 'health_missing': retired.pop('health')
            elif field == 'health_running': retired['health']['status'] = 'Progressing'
            elif field == 'health_failed': retired['health']['status'] = 'Degraded'
            elif field == 'duplicate': bad['status']['resources'].append(copy.deepcopy(retired))
            elif field == 'current_job': synced[-1]['name'] = retired['name']
            elif field == 'images_missing': synced[0].pop('images')
            elif field == 'images_old': synced[0]['images'] = [bad['status']['summary']['images'][-1]]
            elif field == 'deployment_missing': synced.pop(0)
            elif field == 'deployment_duplicate': synced.append(copy.deepcopy(synced[0]))
            elif field == 'conflicting_deployment': synced.append({**synced[0], 'status': 'SyncFailed'})
            elif field == 'conflicting_job': synced.append({**synced[-1], 'status': 'SyncFailed'})
            elif field == 'aggregate_health': bad['status']['health']['status'] = 'Degraded'
            else: bad['status']['sync']['status'] = 'OutOfSync'
            with self.subTest(field=field): self.assertFalse(argo.observe(self.review, bad)['deployed'])

    def test_storage_project_roundtrip_and_stateless_app_in_storage_capable_project(self):
        stateless = copy.deepcopy(self.review)
        self.prepare(storage=True)
        loaded = argo.load_review(self.directory)
        project = argo.projects([loaded])['items'][0]
        self.assertIn(argo.PVC_KIND, project['spec']['namespaceResourceWhitelist'])
        argo.validate_project(project, loaded['application'], loaded['workload'])
        argo.validate_project(project, stateless['application'], stateless['workload'])
        self.assertTrue(argo.observe(loaded, self.healthy())['deployed'])
        project['spec']['namespaceResourceWhitelist'].remove(argo.PVC_KIND)
        with self.assertRaisesRegex(ValueError, 'restrict resources'):
            argo.validate_project(project, loaded['application'], loaded['workload'])

    def test_actual_rendered_review_hash_and_project_contract(self):
        loaded = argo.load_review(self.directory)
        self.assertEqual(loaded['receipt']['http']['route'], '/health')
        project = argo.projects([loaded])['items'][0]
        argo.validate_project(project, loaded['application'])
        self.assertEqual(project['spec']['clusterResourceWhitelist'], [])
        for changes in ({'sourceRepos': ['*']}, {'destinations': [{'server': '*', 'namespace': '*'}]},
                        {'namespaceResourceWhitelist': [{'group': '*', 'kind': '*'}]},
                        {'clusterResourceWhitelist': [{'group': '', 'kind': 'Namespace'}]}):
            bad = copy.deepcopy(project); bad['spec'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                argo.validate_project(bad, loaded['application'])
        workload = copy.deepcopy(loaded['workload']); workload['items'][0]['spec']['replicas'] = 20
        (self.directory / 'workload.json').write_text(json.dumps(workload))
        with self.assertRaisesRegex(ValueError, 'hashes differ'):
            argo.load_review(self.directory)

    def test_deployed_requires_matching_live_binding_revision_resources_and_health(self):
        live = self.healthy()
        result = argo.observe(self.review, live)
        self.assertTrue(result['deployed']); self.assertFalse(result['public_verified']); self.assertIsNone(result['url'])
        without_resource_health = copy.deepcopy(live)
        for row in without_resource_health['status']['resources']: row.pop('health', None)
        self.assertTrue(argo.observe(self.review, without_resource_health)['deployed'])
        for field in ('sync_revision', 'operation_revision', 'operation_phase', 'health', 'resources', 'images', 'compared_source', 'pending'):
            bad = copy.deepcopy(live)
            if field == 'sync_revision': bad['status']['sync']['revision'] = 'f' * 40
            elif field == 'operation_revision': bad['status']['operationState']['syncResult']['revision'] = 'f' * 40
            elif field == 'operation_phase': bad['status']['operationState']['phase'] = 'Failed'
            elif field == 'health': bad['status']['health']['status'] = 'Progressing'
            elif field == 'resources': bad['status']['resources'][0]['namespace'] = 'another-app'
            elif field == 'images': bad['status']['summary']['images'] = []
            elif field == 'compared_source': bad['status']['sync']['comparedTo']['source']['path'] = 'other/app'
            else: bad['operation'] = {'sync': {'revision': 'e' * 40}}
            with self.subTest(field=field):
                self.assertFalse(argo.observe(self.review, bad)['deployed'])
        for field in ('server', 'namespace'):
            bad = copy.deepcopy(live); bad['spec']['destination'][field] = 'other'
            with self.subTest(field=field), self.assertRaises(ValueError): argo.observe(self.review, bad)
        bad = copy.deepcopy(live); bad['spec']['ignoreDifferences'] = [{'kind': '*'}]
        with self.assertRaises(ValueError): argo.observe(self.review, bad)

    def test_missing_summary_uses_only_exact_revision_deployment_sync_images(self):
        live = self.healthy()
        images = live['status'].pop('summary')['images']
        synced = {'group': 'apps', 'kind': 'Deployment', 'namespace': 'tenant-demo', 'name': 'demo',
                  'status': 'Synced', 'images': images}
        live['status']['operationState']['syncResult']['resources'] = [synced]
        self.assertTrue(argo.observe(self.review, live)['deployed'])
        for field in ('revision', 'group', 'kind', 'namespace', 'name', 'images', 'missing_images', 'duplicate', 'summary'):
            bad = copy.deepcopy(live)
            result = bad['status']['operationState']['syncResult']; row = result['resources'][0]
            if field == 'revision': result['revision'] = 'f' * 40
            elif field in ('group', 'kind', 'namespace', 'name'): row[field] = 'other'
            elif field == 'images': row['images'] = ['ghcr.io/example/other@sha256:' + 'a' * 64]
            elif field == 'missing_images': row.pop('images')
            elif field == 'duplicate': result['resources'].append(copy.deepcopy(row))
            else: bad['status']['summary'] = {'images': []}
            with self.subTest(field=field):
                self.assertFalse(argo.observe(self.review, bad)['deployed'])

    def test_sync_requests_exact_revision_and_resume_does_not_start_a_second_operation(self):
        calls = []
        def client(context, namespace, *args, document=None):
            calls.append((args, document))
            if len(calls) == 1: return None
            return self.healthy()
        with patch('argo.kubectl', side_effect=client):
            result = argo.deploy(self.review, 'control', sync=True, timeout=0)
        self.assertTrue(result['deployed'])
        operation = next(document for args, document in calls if args[0] == 'patch')['operation']
        self.assertEqual(operation['sync'], {'revision': 'e' * 40, 'prune': False, 'syncStrategy': {'apply': {}}})
        pending = self.healthy(); pending['operation'] = copy.deepcopy(operation)
        with patch('argo.kubectl', side_effect=[pending, self.healthy()]) as client:
            self.assertTrue(argo.deploy(self.review, 'control', sync=True, timeout=0)['deployed'])
            self.assertTrue(all(call.args[2] == 'get' for call in client.call_args_list))

    def test_completed_sync_is_read_only_but_new_revision_still_syncs(self):
        with patch('argo.kubectl', return_value=self.healthy()) as client:
            self.assertTrue(argo.deploy(self.review, 'control', sync=True, timeout=0)['deployed'])
            self.assertTrue(all(call.args[2] == 'get' for call in client.call_args_list))
        old = self.healthy(); old['spec']['source']['targetRevision'] = 'f' * 40
        with patch('argo.kubectl', side_effect=[old, self.healthy(), self.healthy(), self.healthy()]) as client:
            self.assertTrue(argo.deploy(self.review, 'control', sync=True, timeout=0)['deployed'])
            self.assertEqual([call.args[2] for call in client.call_args_list], ['get', 'apply', 'patch', 'get'])

    def test_pinned_git_tree_must_match_reviewed_workload_without_extra_resources(self):
        repository = self.root / 'config'; repository.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repository), '-c', 'core.hooksPath=/dev/null', *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git('init', '-q'); git('remote', 'add', 'origin', 'https://github.com/example/config.git')
        app_path = repository / self.review['application']['spec']['source']['path']; app_path.mkdir(parents=True)
        (app_path / 'workload.json').write_text(json.dumps(self.review['workload']))
        git('add', '.'); git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.review['application']['spec']['source']['targetRevision'] = git('rev-parse', 'HEAD')
        argo.verify_git(self.review, repository)
        (app_path / 'extra.json').write_text('{}'); git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'extra')
        self.review['application']['spec']['source']['targetRevision'] = git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(ValueError, 'only workload.json'): argo.verify_git(self.review, repository)

    def test_remote_cluster_registration_scopes_secret_and_never_exposes_credentials(self):
        credential = {'bearerToken': 'synthetic-sensitive-token', 'tlsClientConfig': {
            'insecure': False, 'caData': base64.b64encode(b'-----BEGIN CERTIFICATE-----\nsynthetic\n').decode()}}
        calls = []; stored = {}
        def run(args, **kwargs):
            nonlocal stored
            calls.append(args)
            self.assertNotIn(credential['bearerToken'], json.dumps(args))
            self.assertFalse(kwargs.get('shell', False))
            if 'apply' in args:
                stored = json.loads(kwargs['input'])
            return subprocess.CompletedProcess(args, 0, json.dumps(stored) if stored else '', '')
        with patch('argo.subprocess.run', side_effect=run):
            result = argo.register_cluster([self.review], 'control', credential)
        data = {k: base64.b64decode(v).decode() for k, v in stored['data'].items()}
        self.assertEqual(data['namespaces'], 'tenant-demo'); self.assertEqual(data['clusterResources'], 'false')
        self.assertEqual(data['project'], 'railshot'); self.assertNotIn(credential['bearerToken'], json.dumps(result))
        self.assertTrue(any('--server-side' in args for args in calls))
        with patch('argo.subprocess.run', return_value=subprocess.CompletedProcess([], 1, credential['bearerToken'], credential['bearerToken'])):
            with self.assertRaises(ValueError) as error:
                argo.register_cluster([self.review], 'control', credential)
            self.assertNotIn(credential['bearerToken'], str(error.exception))
        credential['tlsClientConfig']['insecure'] = True
        with patch('argo.kubectl') as client, self.assertRaises(ValueError):
            argo.register_cluster([self.review], 'control', credential)
        client.assert_not_called()

    def test_application_credential_relabels_owned_cluster_without_changing_data(self):
        app_id = 'app-' + 'a' * 24
        review = copy.deepcopy(self.review)
        review['receipt']['target_id'] = review['application']['spec']['project'] = app_id
        review['application']['spec']['destination']['namespace'] = app_id
        credential = {'bearerToken': 'synthetic-old-token', 'tlsClientConfig': {
            'insecure': False, 'caData': base64.b64encode(b'-----BEGIN CERTIFICATE-----\nsynthetic\n').decode()}}
        stored, writes = {}, []
        rotate_before_patch = False
        def kube(_context, _namespace, *args, document=None):
            if args[0] == 'apply':
                stored.clear(); stored.update(copy.deepcopy(document))
                stored['metadata']['uid'] = 'same-secret-uid'
                stored['metadata']['resourceVersion'] = '1'
                writes.append(copy.deepcopy(document))
            elif args[0] == 'patch':
                self.assertIn('--type=json', args)
                self.assertEqual(document, [
                    {'op': 'test', 'path': '/metadata/uid', 'value': 'same-secret-uid'},
                    {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '1'},
                    {'op': 'replace', 'path': '/metadata/labels/argocd.argoproj.io~1secret-type', 'value': 'railshot-application'}])
                if rotate_before_patch:
                    auth = json.loads(base64.b64decode(stored['data']['config']))
                    auth['bearerToken'] = 'synthetic-concurrently-renewed-token'
                    stored['data']['config'] = base64.b64encode(json.dumps(auth).encode()).decode()
                    stored['metadata']['resourceVersion'] = '2'
                if stored['metadata']['resourceVersion'] != document[1]['value']:
                    raise ValueError('resourceVersion test failed')
                stored['metadata']['labels']['argocd.argoproj.io/secret-type'] = 'railshot-application'
                writes.append(copy.deepcopy(document))
            return copy.deepcopy(stored) or None
        with patch('argo.kubectl', side_effect=kube):
            argo.register_cluster([review], 'control', credential)
            original = copy.deepcopy(stored['data'])
            credential['bearerToken'] = 'synthetic-unused-new-token'
            result = argo.register_cluster([review], 'control', credential, application_credential=True)
            self.assertEqual(result['status'], 'application_credential_registered')
            self.assertEqual(stored['data'], original)
            self.assertEqual(stored['metadata']['labels']['argocd.argoproj.io/secret-type'], 'railshot-application')
            self.assertEqual(stored['metadata']['uid'], 'same-secret-uid')
            argo.register_cluster([review], 'control', credential, application_credential=True)
            self.assertEqual(len(writes), 2)  # Already private: no credential write or label replay.
            with self.assertRaises(ValueError):
                argo.register_cluster([review], 'control', credential)  # Never silently restore Argo discovery.
            self.assertEqual(len(writes), 2)
            before = copy.deepcopy(stored)
            for field, value in [('project', 'other-project'), ('namespaces', app_id + ',other-namespace'),
                                 ('server', 'https://other.invalid:6443'), ('clusterResources', 'true')]:
                stored.clear(); stored.update(copy.deepcopy(before))
                stored['data'][field] = base64.b64encode(value.encode()).decode()
                with self.subTest(field=field), self.assertRaises(ValueError):
                    argo.register_cluster([review], 'control', credential, application_credential=True)
                self.assertEqual(len(writes), 2)
            stored.clear(); stored.update(copy.deepcopy(before))
            stored['metadata']['labels']['argocd.argoproj.io/secret-type'] = 'cluster'
            rotate_before_patch = True
            with self.assertRaisesRegex(ValueError, 'resourceVersion test failed'):
                argo.register_cluster([review], 'control', credential, application_credential=True)
            self.assertEqual(len(writes), 2)
            self.assertEqual(json.loads(base64.b64decode(stored['data']['config']))['bearerToken'],
                             'synthetic-concurrently-renewed-token')
            self.assertEqual(stored['metadata']['labels']['argocd.argoproj.io/secret-type'], 'cluster')
        for field in ('target', 'project', 'namespace'):
            invalid = copy.deepcopy(review)
            if field == 'target': invalid['receipt']['target_id'] = 'k3s-aws'
            if field == 'project': invalid['application']['spec']['project'] = ''
            if field == 'namespace': invalid['application']['spec']['destination']['namespace'] = 'other-namespace'
            with self.subTest(field=field), patch('argo.kubectl') as client, self.assertRaises(ValueError):
                argo.register_cluster([invalid], 'control', credential, application_credential=True)
            client.assert_not_called()

    def test_multiple_applications_keep_partial_results(self):
        second = copy.deepcopy(self.review)
        second['application']['metadata']['name'] = 'k3s-gcp-tenant-demo-demo'
        second['application']['metadata']['labels']['railshot.io/target'] = 'k3s-gcp'
        second['application']['spec']['destination']['server'] = 'https://192.0.2.2:6443'
        second['receipt']['target_id'] = 'k3s-gcp'
        second['workload']['items'][0]['spec']['template']['metadata']['labels']['railshot.io/target'] = 'k3s-gcp'
        second_path = self.root / 'gcp'; self.save_review(second, second_path)
        project = argo.projects([self.review, second])['items'][0]
        out = io.StringIO()
        with patch('sys.argv', ['argo.py', 'sync', str(self.directory), str(second_path), '--context', 'control', '--repo', str(self.root)]), \
                patch('argo.kubectl', return_value=project), patch('argo.verify_git'), \
                patch('argo.deploy', side_effect=[{'status': 'deployed', 'deployed': True}, ValueError('synthetic target unavailable')]), \
                patch('sys.stdout', out):
            self.assertEqual(argo.main(), 1)
        result = json.loads(out.getvalue())
        self.assertFalse(result['deployed']); self.assertTrue(result['applications'][0]['deployed'])
        self.assertEqual(result['applications'][1]['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
