import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

import argo
import bridge
import test_argo as fixtures


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArgoTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.repo, self.remote = self.root / 'repository', self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(self.repo)], check=True, capture_output=True)
        self.git('checkout', '-qb', 'main')
        self.git('config', 'user.name', 'Fixture'); self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'README.md').write_text('fixture\n')
        self.git('add', '.'); self.git('commit', '-qm', 'Initial'); self.git('push', '-q', 'origin', 'main')
        target = {'id': 'k3s-aws', 'namespace': 'tenant-demo', 'argocd_namespace': 'argocd', 'project': 'railshot',
                  'architecture': 'amd64', 'repo_url': 'https://github.com/example/config.git',
                  'cluster_server': 'https://192.0.2.1:6443', 'path': 'targets/k3s-aws/demo',
                  'node_port': 30080, 'ingress_cidrs': ['10.20.0.0/24'],
                  'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}}}
        self.config = {'version': 1, 'state_dir': str(self.root / 'state'), 'repository': str(self.repo),
                       '_sha256': 'f' * 64,
                       'branch': 'main', 'context': 'control', 'targets': {'k3s-aws': {
                           'target': target, 'app': 'demo', 'tenant': 'team',
                           'public_http': {'url': 'https://app.example/health', 'expected_json': {'status': 'ready'}}}}}
        directory = self.root / 'published'
        publication = json.loads((directory / 'handoff.json').read_bytes())
        publication.update(images=json.loads((directory / 'images.json').read_bytes()), artifact_id=3)
        self.request = {'action': 'apply', 'deployment_id': 'deployment-1', 'target_id': 'k3s-aws',
                        'config_sha256': 'f' * 64,
                        'publication': publication,
                        'files': {name: base64.b64encode((directory / name).read_bytes()).decode() for name in bridge.FILES}}
        self.native = argo.native
        self.calls = []
        self.active_deployment = 'deployment-1'
        self.retained_jobs = []

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'core.hooksPath=/dev/null', *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def advance_remote(self):
        writer = self.root / 'other-writer'
        subprocess.run(['git', 'clone', '-q', '--branch', 'main', str(self.remote), str(writer)],
                       check=True, capture_output=True)
        def git(*args):
            return subprocess.run(['git', '-C', str(writer), '-c', 'core.hooksPath=/dev/null', *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        git('config', 'user.name', 'Other writer'); git('config', 'user.email', 'other@example.invalid')
        (writer / 'other-app.json').write_text('{"owner":"another-application"}\n')
        git('add', '--', 'other-app.json'); git('commit', '-qm', 'Independent app release')
        git('push', '-q', 'origin', 'main')
        return git('rev-parse', 'HEAD')

    def test_historical_publication_crosses_bridge_without_renaming_bytes(self):
        request = copy.deepcopy(self.request)
        files = {name: base64.b64decode(value) for name, value in request['files'].items()}
        files['jasmin.yaml'] = files.pop('railshot.yaml')
        manifest = json.loads(files['manifest.json'])
        manifest['files']['jasmin.yaml'] = manifest['files'].pop('railshot.yaml')
        files['manifest.json'] = json.dumps(manifest).encode()
        receipt = json.loads(files['handoff.json'])
        receipt['files']['jasmin.yaml'] = receipt['files'].pop('railshot.yaml')
        receipt['files']['manifest.json'] = hashlib.sha256(files['manifest.json']).hexdigest()
        files['handoff.json'] = json.dumps(receipt).encode()
        request['publication'].update(receipt)
        request['files'] = {name: base64.b64encode(value).decode() for name, value in files.items()}
        _, accepted, _ = bridge.validate_request(self.config, request)
        self.assertEqual(accepted, files)
        request['files']['railshot.yaml'] = request['files']['jasmin.yaml']
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            bridge.validate_request(self.config, request)

    def native_local_only(self, args, **kwargs):
        self.calls.append(args)
        if args[-3:] == ['remote', 'get-url', 'origin']:
            return 'https://github.com/example/config.git\n'
        self.assertEqual(args[0], 'git')
        # The fixture's origin is a temporary local bare repository; no network writes.
        return self.native(args, **kwargs)

    def test_cloud_publications_keep_cluster_path_namespace_and_pull_secret_isolated(self):
        config = copy.deepcopy(self.config)
        template = config['targets'].pop('k3s-aws')
        requests = {}
        for index, provider in enumerate(('aws', 'gcp', 'openstack'), 1):
            target_id = 'k3s-' + provider
            registered = copy.deepcopy(template)
            registered['target'].update(id=target_id, namespace='app-' + provider, project='railshot-' + provider,
                cluster_server=f'https://10.{index}.0.2:6443', path=f'gitops/applications/demo/{target_id}', revision='e' * 40,
                image_pull_secret={'namespace': 'app-' + provider, 'name': 'pull-' + provider})
            config['targets'][target_id] = registered
            request = copy.deepcopy(self.request)
            request['target_id'] = target_id
            receipt = json.loads(base64.b64decode(request['files']['handoff.json']))
            receipt['target_id'] = target_id
            receipt['registry'].update(visibility='private', verification='authenticated_manifest_read',
                image_pull_secret=registered['target']['image_pull_secret'])
            request['publication'].update(receipt)
            request['files']['handoff.json'] = base64.b64encode(json.dumps(receipt).encode()).decode()
            requests[target_id] = request
        with patch('argo.native') as native:
            for target_id, request in requests.items():
                registered, files, _ = bridge.validate_request(config, request)
                published = self.root / target_id; published.mkdir()
                for name, content in files.items(): (published / name).write_bytes(content)
                rendered = bridge.handoff.render(published, registered['target'])
                application, workload = rendered['application']['spec'], rendered['workload']['items'][0]
                self.assertEqual(application['destination'], {'server': registered['target']['cluster_server'], 'namespace': registered['target']['namespace']})
                self.assertEqual(application['source']['path'], registered['target']['path'])
                self.assertEqual(application['project'], registered['target']['project'])
                self.assertEqual(workload['spec']['template']['spec']['imagePullSecrets'], [{'name': registered['target']['image_pull_secret']['name']}])
                for other in requests:
                    if other == target_id: continue
                    forged = {**request, 'target_id': other}
                    with self.assertRaisesRegex(ValueError, 'publication target mismatch'):
                        bridge.validate_request(config, forged)
                    with self.assertRaisesRegex(ValueError, 'artifact target mismatch'):
                        bridge.handoff.render(published, config['targets'][other]['target'])
            native.assert_not_called()

    def kubectl(self, context, namespace, *args, document=None):
        self.calls.append(['kubectl', *args])
        if args[0:2] == ('get', 'appproject'):
            return argo.projects([self.fixture.review])['items'][0]
        review = argo.load_review(self.root / 'state' / self.active_deployment / 'review')
        live = self.fixture.healthy(review)
        for job in self.retained_jobs:
            live['status']['resources'].append({'group': 'batch', 'kind': 'Job', 'namespace': job['metadata']['namespace'],
                'name': job['metadata']['name'], 'status': 'OutOfSync', 'requiresPruning': True, 'health': {'status': 'Healthy'}})
            live['status']['summary']['images'].append(job['spec']['template']['spec']['containers'][0]['image'])
        return live

    def test_apply_pins_git_argo_and_public_receipt_then_replay_only_observes(self):
        verified = {'state': 'succeeded', 'verified_at': '2026-10-02T12:00:00+00:00', 'url': 'https://app.example/health'}
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl), \
                patch('bridge.public_probe', return_value=verified), patch('bridge.site_probe', return_value=True) as site:
            result = bridge.execute(self.config, self.request)
            self.assertTrue(result['cd']['deployed'])
            self.assertEqual(result['public_http'], {**verified, 'site_url': 'https://app.example/health'})
            site.assert_called_once_with('https://app.example/health')
            self.assertEqual(result['cd']['revision'], self.git('rev-parse', 'HEAD'))
            remote_revision = subprocess.check_output(['git', '--git-dir', str(self.remote), 'rev-parse', 'main'], text=True).strip()
            self.assertEqual(result['cd']['revision'], remote_revision)
            self.calls.clear()
            repeated = bridge.execute(self.config, self.request)
            self.assertTrue(repeated['cd']['deployed'])
            self.assertFalse(any('push' in args or 'commit' in args or 'patch' in args or 'apply' in args for args in self.calls))
            bad = copy.deepcopy(self.request); bad['publication']['artifact_id'] = 4
            with self.assertRaisesRegex(ValueError, 'binding conflict'):
                bridge.execute(self.config, bad)

    def test_clean_behind_checkout_fast_forwards_before_app_commit_without_losing_other_release(self):
        before = self.git('rev-parse', 'HEAD')
        remote_revision = self.advance_remote()
        self.assertNotEqual(before, remote_revision)
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl), \
                patch('bridge.public_probe', return_value={'state': 'unverified', 'verified_at': None, 'url': None}):
            result = bridge.execute(self.config, self.request)
        self.assertTrue(result['cd']['deployed'])
        self.assertEqual(self.git('rev-parse', 'HEAD^'), remote_revision)
        self.assertEqual(self.git('rev-parse', 'HEAD'), result['cd']['revision'])
        self.assertEqual(json.loads((self.repo / 'other-app.json').read_text()), {'owner': 'another-application'})
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual([args[-3:] for args in self.calls if 'merge' in args], [['merge', '--ff-only', remote_revision]])
        remote_head = subprocess.check_output(['git', '--git-dir', str(self.remote), 'rev-parse', 'main'], text=True).strip()
        self.assertEqual(remote_head, result['cd']['revision'])

    def test_ahead_or_diverged_checkout_is_not_reset_merged_or_pushed(self):
        original_remote = self.git('rev-parse', 'HEAD')
        (self.repo / 'local-only.txt').write_text('preserve unpublished work\n')
        self.git('add', '--', 'local-only.txt'); self.git('commit', '-qm', 'Unpublished local commit')
        local_head = self.git('rev-parse', 'HEAD')
        for state in ('ahead', 'diverged'):
            expected_remote = self.advance_remote() if state == 'diverged' else original_remote
            self.calls.clear()
            with self.subTest(state=state), patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl') as kube:
                result = bridge.execute(self.config, {**self.request, 'deployment_id': state})
                self.assertEqual(result['cd']['state'], 'blocked')
                self.assertEqual(result['error']['code'], 'CD_PREPARATION_FAILED')
                self.assertFalse(result['error']['outcome_unknown'])
                kube.assert_not_called()
            self.assertEqual(self.git('rev-parse', 'HEAD'), local_head)
            self.assertEqual((self.repo / 'local-only.txt').read_text(), 'preserve unpublished work\n')
            self.assertFalse(any(command in args for args in self.calls for command in ('merge', 'reset', 'push', 'commit')))
            remote_head = subprocess.check_output(['git', '--git-dir', str(self.remote), 'rev-parse', 'main'], text=True).strip()
            self.assertEqual(remote_head, expected_remote)

    def test_fast_forward_does_not_bypass_clean_checkout_or_registered_branch(self):
        self.advance_remote()
        before = self.git('rev-parse', 'HEAD')
        for state in ('dirty', 'wrong-branch'):
            if state == 'dirty':
                (self.repo / 'README.md').write_text('local edit\n')
            else:
                (self.repo / 'README.md').write_text('fixture\n')
                self.git('checkout', '-qb', 'another-branch')
            self.calls.clear()
            with self.subTest(state=state), patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl') as kube:
                result = bridge.execute(self.config, {**self.request, 'deployment_id': state})
                self.assertEqual(result['cd']['state'], 'blocked')
                kube.assert_not_called()
            self.assertEqual(self.git('rev-parse', 'HEAD'), before)
            self.assertFalse(any(command in args for args in self.calls for command in ('fetch', 'merge', 'reset', 'push', 'commit')))

    def test_status_health_requires_current_argo_revision_images_and_site_before_url(self):
        self.config['targets']['k3s-aws']['public_http'] = {'url': 'https://app.example/health', 'expected_status': 200}
        files = {name: base64.b64decode(value) for name, value in self.request['files'].items()}
        spec = json.loads(files['railshot.yaml']); spec['services'][0]['route'] = '/app'
        files['railshot.yaml'] = json.dumps(spec).encode()
        manifest = json.loads(files['manifest.json'])
        manifest['files']['railshot.yaml'] = hashlib.sha256(files['railshot.yaml']).hexdigest()
        files['manifest.json'] = json.dumps(manifest).encode()
        receipt = json.loads(files['handoff.json'])
        receipt['files'] = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items() if name != 'handoff.json'}
        files['handoff.json'] = json.dumps(receipt).encode()
        self.request['publication'].update(receipt)
        self.request['files'] = {name: base64.b64encode(raw).decode() for name, raw in files.items()}
        verified = {'state': 'succeeded', 'verified_at': '2026-10-03T00:00:00Z', 'url': 'https://app.example/health'}
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl), \
                patch('bridge.public_probe', return_value=verified) as health, patch('bridge.site_probe', return_value=False) as site:
            result = bridge.execute(self.config, self.request)
            self.assertTrue(result['cd']['deployed'])
            self.assertEqual(result['public_http'], {'state': 'unverified', 'verified_at': None, 'url': None})
            site.assert_called_once_with('https://app.example/app')
            site.return_value = True
            result = bridge.execute(self.config, {**self.request, 'action': 'observe'})
            self.assertEqual(result['public_http'], {**verified, 'site_url': 'https://app.example/app'})
            for field in ('revision', 'images'):
                def stale(context, namespace, *args, document=None):
                    live = self.kubectl(context, namespace, *args, document=document)
                    if args[0:2] == ('get', 'application'):
                        if field == 'revision':
                            live['status']['sync']['revision'] = 'a' * 40
                        else:
                            live['status']['summary']['images'] = ['ghcr.io/example/web@sha256:' + 'a' * 64]
                    return live
                health.reset_mock(); site.reset_mock()
                with self.subTest(field=field), patch('argo.kubectl', side_effect=stale):
                    result = bridge.execute(self.config, {**self.request, 'action': 'observe'})
                    self.assertFalse(result['cd']['deployed'])
                    self.assertEqual(result['public_http']['state'], 'not_run')
                    health.assert_not_called(); site.assert_not_called()

    def test_public_contract_rejects_ambiguous_unbound_or_invalid_expectations_before_push(self):
        valid = {'url': 'https://app.example/health', 'expected_status': 200}
        invalid = [{'expected_status': status} for status in (True, '200', 200.0, 206, 500)] + [
            {'url': url} for url in ('http://app.example/health', 'https://user:secret@app.example/health',
                                     'https://app.example/wrong', 'https://app.example/health?x=1',
                                     'https://app.example/health#fragment', 'https://*.example/health')] + [
            {'expected_json': {}}, {'extra': True}]
        for index, fields in enumerate(invalid):
            with self.subTest(fields=fields):
                self.config['targets']['k3s-aws']['public_http'] = {**valid, **fields}
                self.calls.clear()
                request = {**self.request, 'deployment_id': f'invalid-{index}'}
                with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl') as kube:
                    result = bridge.execute(self.config, request)
                self.assertEqual(result['cd']['state'], 'blocked')
                kube.assert_not_called()
                self.assertFalse(any('push' in args for args in self.calls))

    def resume_after_completion(self, phase):
        original = bridge.durable_write
        def crash(path, data, *args, **kwargs):
            original(path, data, *args, **kwargs)
            if Path(path).name == 'state.json' and json.loads(data)['phase'] == phase:
                raise KeyboardInterrupt('process lost after durable completion')
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl), \
                patch('bridge.durable_write', side_effect=crash), self.assertRaises(KeyboardInterrupt):
            bridge.execute(self.config, self.request)
        self.calls.clear()
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl), \
                patch('bridge.public_probe', return_value={'state': 'unverified', 'verified_at': None, 'url': None}), \
                patch('argo.deploy', wraps=argo.deploy) as deploy:
            bridge.execute(self.config, {**self.request, 'action': 'observe'})
            self.assertTrue(all(call.kwargs['sync'] is False for call in deploy.call_args_list))
            deploy.reset_mock()
            result = bridge.execute(self.config, self.request)
            self.assertTrue(result['cd']['deployed'])
            self.assertEqual(sum(call.kwargs['sync'] for call in deploy.call_args_list), int(phase == 'pushed'))
        self.assertFalse(any('push' in args or 'commit' in args for args in self.calls))

    def test_resume_after_confirmed_push_starts_only_remaining_sync(self):
        self.resume_after_completion('pushed')

    def test_resume_after_sync_request_does_not_resubmit_sync(self):
        self.resume_after_completion('sync_requested')

    def test_uncertain_push_is_never_repeated_and_forged_input_never_dispatches(self):
        def failed(args, **kwargs):
            if 'push' in args:
                self.calls.append(args)
                raise RuntimeError('synthetic-sensitive-native-output')
            return self.native_local_only(args, **kwargs)
        with patch('argo.native', side_effect=failed), patch('argo.kubectl', side_effect=self.kubectl):
            result = bridge.execute(self.config, self.request)
            self.assertTrue(result['error']['outcome_unknown'])
            self.assertNotIn('synthetic', json.dumps(result))
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=RuntimeError('unavailable')):
            self.calls.clear()
            repeated = bridge.execute(self.config, self.request)
            self.assertEqual(repeated['cd']['state'], 'unknown')
            self.assertFalse(any('push' in args for args in self.calls))
        bad = copy.deepcopy(self.request); bad['publication']['target_id'] = 'unregistered'
        with patch('argo.native') as native, self.assertRaises(ValueError):
            bridge.execute(self.config, bad)
        native.assert_not_called()
        changed = {**self.config, '_sha256': 'e' * 64}
        with patch('argo.native') as native, self.assertRaisesRegex(ValueError, 'config changed'):
            bridge.execute(changed, self.request)
        native.assert_not_called()

    def test_database_migration_redeploys_new_images_and_commands_without_replaying_old_jobs(self):
        self.fixture.prepare(database=True)
        self.config['targets']['k3s-aws']['target']['database'] = self.fixture.review['receipt']['database']
        directory = self.root / 'published'
        publication = json.loads((directory / 'handoff.json').read_bytes())
        publication.update(images=json.loads((directory / 'images.json').read_bytes()), artifact_id=3)
        self.request.update(publication=publication, files={name: base64.b64encode((directory / name).read_bytes()).decode()
                                                           for name in bridge.FILES})
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl), \
                patch('bridge.public_probe', return_value={'state': 'unverified', 'verified_at': None, 'url': None}):
            result = bridge.execute(self.config, self.request)
            self.assertEqual(result['cd']['migration']['state'], 'succeeded')
            self.calls.clear()
            self.assertTrue(bridge.execute(self.config, self.request)['cd']['deployed'])
            self.assertFalse(any('push' in args or 'patch' in args or 'apply' in args for args in self.calls))
            for index, command in enumerate((['python', 'migrate.py'], ['python', 'next.py']), start=2):
                previous = argo.load_review(self.root / 'state' / self.active_deployment / 'review')['workload']['items'][-1]
                self.retained_jobs.append(previous)
                self.fixture.prepare(database=True, image_digest='f' * 64, migration_command=command)
                publication = json.loads((directory / 'handoff.json').read_bytes())
                publication.update(images=json.loads((directory / 'images.json').read_bytes()), artifact_id=3)
                self.active_deployment = 'deployment-' + str(index)
                self.request.update(deployment_id=self.active_deployment, publication=publication,
                    files={name: base64.b64encode((directory / name).read_bytes()).decode() for name in bridge.FILES})
                self.calls.clear()
                result = bridge.execute(self.config, self.request)
                self.assertTrue(result['cd']['deployed'])
                self.assertEqual(result['cd']['migration']['state'], 'succeeded')
                self.assertNotIn(result['cd']['migration']['name'], [job['metadata']['name'] for job in self.retained_jobs])
                self.assertTrue(any('push' in args for args in self.calls))
                self.assertFalse(any('delete' in args or '--force' in args for args in self.calls))
                self.assertEqual(self.git('status', '--porcelain'), '')
                revision = result['cd']['revision']; self.calls.clear()
                repeated = bridge.execute(self.config, self.request)
                self.assertEqual(repeated['cd']['revision'], revision); self.assertTrue(repeated['cd']['deployed'])
                self.assertFalse(any('push' in args or 'patch' in args or 'apply' in args for args in self.calls))

    def test_foreign_git_workload_is_not_overwritten_or_pushed(self):
        directory = self.repo / 'targets/k3s-aws/demo'; directory.mkdir(parents=True)
        workload = copy.deepcopy(self.fixture.review['workload'])
        for item in workload['items']:
            item['metadata']['name'] = 'another-app'
        path = directory / 'workload.json'; path.write_text(json.dumps(workload))
        self.git('add', '.'); self.git('commit', '-qm', 'Foreign app'); self.git('push', '-q', 'origin', 'main')
        original = path.read_bytes()
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl):
            result = bridge.execute(self.config, self.request)
        self.assertEqual(result['cd']['state'], 'blocked')
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(any('push' in args for args in self.calls))

    def test_real_local_http_checks_json_or_bounded_status_and_never_follows_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            body, status, calls = b'{"status":"ready"}', 200, []
            def do_GET(self):
                self.calls.append(self.path)
                self.send_response(self.status)
                if self.status == 302:
                    self.send_header('Location', '/other')
                self.end_headers(); self.wfile.write(self.body)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        local = f'http://127.0.0.1:{server.server_port}/health'
        opener = build_opener(ProxyHandler({}), bridge.NoRedirect())
        class LocalTLSBoundary:
            def open(self, request, **kwargs):
                self_request = type(request)(local, headers=dict(request.headers))
                return opener.open(self_request, **kwargs)
        # Production still requires HTTPS; only the transport is replaced with this local HTTP server.
        with patch('bridge.build_opener', return_value=LocalTLSBoundary()):
            config = self.config['targets']['k3s-aws']['public_http']
            self.assertEqual(bridge.public_probe(config, '/health')['state'], 'succeeded')
            Handler.body = b'{"status":"wrong"}'
            self.assertEqual(bridge.public_probe(config, '/health')['state'], 'unverified')
            status_only = {'url': config['url'], 'expected_status': 200}
            for body in (b'<html>ready</html>', b'OK', b''):
                Handler.body = body
                self.assertEqual(bridge.public_probe(status_only, '/health')['state'], 'succeeded')
            for status, body in ((206, b'OK'), (500, b'failure'), (200, b'x' * 65537), (302, b'')):
                with self.subTest(status=status, size=len(body)):
                    Handler.status, Handler.body = status, body
                    Handler.calls.clear()
                    self.assertEqual(bridge.public_probe(status_only, '/health')['state'], 'unverified')
                    self.assertEqual(Handler.calls, ['/health'])
                    self.assertFalse(bridge.site_probe(config['url']))
            Handler.status, Handler.body = 200, b'<html>ready</html>'
            self.assertTrue(bridge.site_probe(config['url']))
        with self.assertRaises(ValueError):
            bridge.public_probe({'url': local, 'expected_json': {}}, '/health')

    def test_edge_allocation_is_bound_and_only_initial_apply_can_mutate_routes(self):
        registered = self.config['targets']['k3s-aws']
        registered['edge'] = {'config_path': '/private/edge.json', 'allocation_path': '/private/allocation.json',
                              'config_sha256': 'a' * 64}
        verified = {'state': 'succeeded', 'verified_at': '2026-10-02T00:00:00Z',
                    'url': 'https://new-app.railshot.io/health', 'site_url': 'https://new-app.railshot.io/'}
        with patch('argo.native', side_effect=self.native_local_only), patch('argo.kubectl', side_effect=self.kubectl), \
                patch('edge.validate_binding') as bind, patch('edge.ensure') as ensure, \
                patch('edge.observe', return_value=verified) as observe, patch('bridge.public_probe') as legacy:
            result = bridge.execute(self.config, self.request)
            self.assertEqual(result['public_http'], verified)
            ensure.assert_called_once_with(registered['edge'])
            state = json.loads((self.root / 'state/deployment-1/state.json').read_bytes())
            self.assertEqual(state['edge_request']['deployment_id'], self.request['deployment_id'])
            ensure.reset_mock()
            bridge.execute(self.config, {**self.request, 'action': 'observe'})
            ensure.assert_not_called(); legacy.assert_not_called()
            self.assertEqual(observe.call_args.args[1]['publication']['source_commit'], self.request['publication']['source_commit'])
            self.assertEqual(bind.call_count, 2)
            observe.side_effect = ValueError('route binding differs')
            self.assertIsNone(bridge.execute(self.config, {**self.request, 'action': 'observe'})['public_http']['url'])
        with patch('edge.validate_binding', side_effect=ValueError('mismatch')), patch('argo.native') as native:
            with self.assertRaises(ValueError):
                bridge.execute(self.config, self.request)
            native.assert_not_called()

    def test_node_adapter_calls_apply_once_then_read_only_observe(self):
        executable = self.root / 'synthetic-python'
        executable.write_text('#!' + sys.executable + '\n' + '''import json,sys
from pathlib import Path
request=json.load(sys.stdin)
path=Path(sys.argv[-1])
config=json.loads(path.read_text())
config['calls'].append(request['action'])
path.write_text(json.dumps(config))
done=request['action']=='observe'
print(json.dumps({'cd':{'state':'deployed' if done else 'progressing','revision':'a'*40,'deployed':done},
 'public_http':{'state':'succeeded' if done else 'not_run','verified_at':'2026-10-02T00:00:00Z' if done else None,
 'url':'https://app.example/health' if done else None}}))
''')
        executable.chmod(0o700)
        calls = self.root / 'adapter-calls.json'
        calls.write_text(json.dumps({**self.config, 'calls': []})); calls.chmod(0o600)
        adapter = Path(__file__).resolve().parents[1] / 'apps/api/src/cd.js'
        program = '''
import assert from 'node:assert/strict';
const {createCdAdapter}=await import(process.argv[1]);
const adapter=createCdAdapter({configPath:process.argv[2],python:process.argv[3],timeoutMs:10000,
 loadPublished:async()=>[{path:'handoff.json',content:Buffer.from('{}')}]});
assert.equal(adapter.targets['k3s-aws'].applicationName,'demo');
assert.equal(adapter.targets['k3s-aws'].deploymentScope,'registered_application');
const request={deploymentId:'deployment-1',app:'demo',targetId:'k3s-aws',sourceCommit:'a'.repeat(40),
 publication:{app:'demo',tenant:'team',target_id:'k3s-aws',source_commit:'a'.repeat(40)}};
const result=await adapter(request);
assert.equal(result.cd.deployed,true);assert.equal(result.public_http.state,'succeeded');
await assert.rejects(adapter({...request,app:'wrong'}),/binding differs/);
'''
        subprocess.run(['node', '--input-type=module', '-e', program, adapter.as_uri(), str(calls), str(executable)],
                       check=True, capture_output=True, text=True, timeout=15)
        self.assertEqual(json.loads(calls.read_text())['calls'], ['apply', 'observe'])


if __name__ == '__main__':
    unittest.main()
