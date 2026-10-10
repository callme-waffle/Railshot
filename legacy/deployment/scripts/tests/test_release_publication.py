"""Publication provenance and host admission boundaries without network or cloud writes."""
import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.request import Request
import zipfile

ROOT = Path(__file__).resolve().parents[3]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deployment/scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


admission = load('release_admission')
host = load('release_host')


def zipped(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return buffer.getvalue()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.sha = 'a' * 40; self.ref = 'refs/heads/main'
        self.run = {'id': 123, 'run_attempt': 2, 'head_sha': self.sha, 'head_branch': 'main',
                    'repository': {'full_name': admission.REPOSITORY, 'id': admission.REPOSITORY_ID},
                    'head_repository': {'full_name': admission.REPOSITORY, 'id': admission.REPOSITORY_ID},
                    'path': '.github/workflows/platform-publish.yml', 'event': 'workflow_dispatch', 'conclusion': None}
        self.images = {component: f'ghcr.io/jasmin-softbank/railshot-{component}@sha256:' + 'b' * 64
                       for component in admission.COMPONENTS}
        self.jobs = []; self.artifacts = []; self.bodies = {}
        for index, component in enumerate(admission.COMPONENTS, 1):
            body = zipped([(component + '.json', json.dumps({component: self.images[component]}))])
            self.bodies[index] = body
            self.jobs.append({'name': 'publish (' + component + ')', 'run_id': 123, 'head_sha': self.sha,
                              'status': 'completed', 'conclusion': 'success',
                              'started_at': '2026-10-03T01:00:00Z', 'completed_at': '2026-10-03T01:02:00Z'})
            self.artifacts.append({'id': index, 'name': f'platform-image-{component}-{self.sha}',
                'size_in_bytes': len(body), 'expired': False, 'digest': 'sha256:' + hashlib.sha256(body).hexdigest(),
                'created_at': '2026-10-03T01:01:00Z', 'workflow_run': {'id': 123, 'head_sha': self.sha,
                'head_branch': 'main', 'repository_id': admission.REPOSITORY_ID, 'head_repository_id': admission.REPOSITORY_ID}})

    def read(self, path):
        if path == 'actions/runs/123': return copy.deepcopy(self.run)
        if path == 'actions/runs/123/attempts/2/jobs?per_page=100':
            return {'total_count': len(self.jobs), 'jobs': copy.deepcopy(self.jobs)}
        self.assertEqual(path, 'actions/runs/123/artifacts?per_page=100')
        return {'total_count': len(self.artifacts), 'artifacts': copy.deepcopy(self.artifacts)}

    def verify(self, **kwargs):
        return admission.publication(self.sha, self.ref, 123, 2, self.images,
            read=kwargs.get('read', self.read), download=kwargs.get('download', self.bodies.__getitem__))

    def test_exact_three_published_images_and_nested_workflow_are_bound(self):
        proof = self.verify()
        self.assertEqual(proof['status'], 'verified')
        self.assertEqual(proof['run_attempt'], 2)
        self.assertEqual(set(proof['artifacts']), set(admission.COMPONENTS))
        self.run['path'] = '.github/workflows/railshot-ci.yml'
        self.run['event'] = 'push'
        for job in self.jobs: job['name'] = 'Build, smoke and publish images / ' + job['name']
        self.artifacts[0].pop('digest')  # Older metadata may omit a digest; archive hash is still recorded.
        self.assertEqual(self.verify()['artifacts']['dashboard']['sha256'], hashlib.sha256(self.bodies[1]).hexdigest())

    def test_wrong_source_attempt_repository_workflow_or_failed_run_is_rejected(self):
        for change in ({'head_sha': 'c' * 40}, {'id': 456}, {'run_attempt': 1}, {'head_branch': 'untrusted'},
                       {'head_repository': {'full_name': 'fork/Railshot', 'id': 3}},
                       {'path': '.github/workflows/other.yml'}, {'event': 'pull_request'}, {'conclusion': 'failure'}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.run); self.run.update(change)
                with self.assertRaisesRegex(ValueError, 'RUN_BINDING_MISMATCH'): self.verify()
                self.run = original

    def test_all_publishers_must_be_successful_in_the_requested_attempt(self):
        for change in ({'status': 'in_progress'}, {'conclusion': 'failure'}, {'head_sha': 'c' * 40},
                       {'run_id': 456}, {'name': 'build / images (dashboard)'}):
            with self.subTest(change=change):
                original = dict(self.jobs[0]); self.jobs[0].update(change)
                with self.assertRaisesRegex(ValueError, 'PUBLISHER_NOT_VERIFIED'): self.verify()
                self.jobs[0] = original
        self.jobs.append(dict(self.jobs[0]))
        with self.assertRaisesRegex(ValueError, 'PUBLISHER_NOT_VERIFIED'): self.verify()

    def test_expired_foreign_older_attempt_or_duplicate_artifact_is_rejected(self):
        for change in ({'expired': True}, {'created_at': '2026-10-02T01:01:00Z'},
                       {'workflow_run': {**self.artifacts[0]['workflow_run'], 'head_sha': 'c' * 40}},
                       {'digest': 'sha256:' + '0' * 64}):
            with self.subTest(change=change):
                original = copy.deepcopy(self.artifacts[0]); self.artifacts[0].update(change)
                with self.assertRaises(ValueError): self.verify()
                self.artifacts[0] = original
        self.artifacts.append(dict(self.artifacts[0]))
        with self.assertRaisesRegex(ValueError, 'ARTIFACT_NOT_UNIQUE'): self.verify()
        self.artifacts = self.artifacts[1:3]
        with self.assertRaisesRegex(ValueError, 'ARTIFACT_NOT_UNIQUE'): self.verify()

    def test_archive_requires_exact_single_file_and_exact_ssm_pin(self):
        self.artifacts[0].pop('digest')
        wrong = {'dashboard': self.images['dashboard'].replace('b' * 64, 'c' * 64)}
        for entries in ([('../dashboard.json', '{}')], [('dashboard.json', '{}'), ('extra', '{}')],
                        [('dashboard.json', json.dumps(wrong))], [('dashboard.json', 'x' * 4097)],
                        [('dashboard.json', '{"dashboard":"one","dashboard":"two"}')]):
            with self.subTest(entries=entries[:1]):
                self.bodies[1] = zipped(entries)
                with self.assertRaises(ValueError): self.verify()
        self.bodies[1] = b'not a ZIP'
        with self.assertRaisesRegex(ValueError, 'ARCHIVE_CONTENT_INVALID'): self.verify()
        self.bodies[1] = b'x' * (admission.ARCHIVE_LIMIT + 1)
        with self.assertRaisesRegex(ValueError, 'ARCHIVE_TOO_LARGE'): self.verify()

    def test_partial_listing_or_attempt_change_during_download_is_rejected(self):
        def partial(path):
            result = self.read(path)
            if 'artifacts?' in path: result['total_count'] = 101
            return result
        with self.assertRaisesRegex(ValueError, 'LIST_INCOMPLETE'): self.verify(read=partial)
        count = 0
        def changed(path):
            nonlocal count
            result = self.read(path)
            if path == 'actions/runs/123':
                count += 1
                if count == 2: result['run_attempt'] = 3
            return result
        with self.assertRaisesRegex(ValueError, 'RUN_BINDING_MISMATCH'): self.verify(read=changed)

    def test_redirect_strips_token_and_rejects_unknown_or_non_https_destinations(self):
        request = Request('https://api.github.com/repos/' + admission.REPOSITORY + '/actions/artifacts/1/zip',
                          headers={'Authorization': 'Bearer unit-test-token', 'Accept': 'application/json'})
        handler = admission.ArtifactRedirect()
        redirected = handler.redirect_request(request, None, 302, 'Found', {},
            'https://productionresultssa1.blob.core.windows.net/artifact?signature=unit-test')
        self.assertNotIn('authorization', {key.lower() for key, _ in redirected.header_items()})
        for url in ('http://productionresultssa1.blob.core.windows.net/a', 'https://attacker.invalid/a',
                    'https://blob.core.windows.net.attacker.invalid/a', 'https://user@x.blob.core.windows.net/a',
                    'https://x.blob.core.windows.net:8443/a'):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'REDIRECT_REJECTED'):
                handler.redirect_request(request, None, 302, 'Found', {}, url)
        with self.assertRaisesRegex(ValueError, 'API_REDIRECT_REJECTED'):
            admission.NoRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://github.com')

    def test_download_is_bounded_and_uses_only_constructed_github_artifact_url(self):
        response = mock.MagicMock(); response.__enter__.return_value = response
        response.read.return_value = b'x' * (admission.ARCHIVE_LIMIT + 1)
        opener = mock.Mock(); opener.open.return_value = response
        with mock.patch.object(admission, 'build_opener', return_value=opener), \
                mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'unit-test-token'}):
            with self.assertRaisesRegex(ValueError, 'ARCHIVE_TOO_LARGE'): admission.download_artifact(7)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/' + admission.REPOSITORY + '/actions/artifacts/7/zip')
        response.read.assert_called_once_with(admission.ARCHIVE_LIMIT + 1)


class HostPublicationGateTests(unittest.TestCase):
    def test_node_only_edge_kind_reads_runtime_config_and_requires_v2_scope(self):
        target = {'provider': 'aws', 'scope': 'node-only', 'config_file': '/private/runtime.json'}
        with mock.patch.object(host.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps({'version': 2, 'scope': 'node-only'}))) as run:
            self.assertEqual(host.target_edge_kind(target), 'none')
            self.assertEqual(run.call_args.args[0][-1], target['config_file'])
            self.assertEqual(run.call_args.args[0][:4], ['sudo', '-n', '-u', 'railshot-operator'])
            for invalid in ({'version': 1}, {'version': 2, 'scope': 'application'}):
                run.return_value.stdout = json.dumps(invalid)
                with self.assertRaisesRegex(ValueError, 'RUNTIME_SCOPE_MISMATCH'):
                    host.target_edge_kind(target)
            run.reset_mock()
            with self.assertRaisesRegex(ValueError, 'TARGET_SCOPE_INVALID'):
                host.target_edge_kind({**target, 'edge_config_file': '/private/edge.json'})
            run.assert_not_called()
            run.return_value.stdout = json.dumps({'edge_kind': 'aws-relay'})
            self.assertEqual(host.target_edge_kind({'edge_config_file': '/private/edge.json'}), 'aws-relay')
            self.assertEqual(run.call_args.args[0][-1], '/private/edge.json')

    def test_failed_publication_blocks_config_workers_and_targets_and_clears_token(self):
        with tempfile.TemporaryDirectory() as directory:
            source_home = mock.Mock()
            source_home.resolve.return_value = source_home
            source_home.stat.return_value = SimpleNamespace(st_uid=0, st_mode=0o755)
            source_home.__fspath__ = mock.Mock(return_value=directory)
            def path(value):
                return source_home if value == '/opt/railshot-release/sources' else Path(value)
            sha = 'a' * 40
            config = {'SSM_SourceSha': sha, 'SSM_Revision': 'b' * 40,
                **{'SSM_' + name: 'c' * 64 for name in ('DashboardDigest', 'ApiDigest', 'RunnerDigest')},
                'SSM_PublicationRunId': '123', 'SSM_PublicationRunAttempt': '2'}
            def native(command, **kwargs):
                if command[0] == 'git':
                    return SimpleNamespace(returncode=0, stdout=sha if command[1] == 'rev-parse' else '')
                self.assertEqual(command[0], '/usr/local/bin/k3s')
                return SimpleNamespace(stdout=json.dumps({'data': {'token': base64.b64encode(b'gho_unit_test_token\n').decode()}}))
            def admitted(*args):
                self.assertEqual(os.environ['GITHUB_TOKEN'], 'gho_unit_test_token')
            checked = SimpleNamespace(admit=mock.Mock(side_effect=admitted), publication=mock.Mock(side_effect=ValueError('PUBLICATION_IMAGE_PIN_MISMATCH')))
            release = SimpleNamespace(private=mock.Mock(), execute=mock.Mock())
            tokens = SimpleNamespace(github_token=mock.Mock(wraps=load('platform_workers').github_token))
            with mock.patch.object(host, 'Path', side_effect=path), \
                    mock.patch.object(host.subprocess, 'run', side_effect=native), \
                    mock.patch.dict(os.environ, config, clear=True), \
                    mock.patch.object(sys, 'path', list(sys.path)), \
                    mock.patch.dict(sys.modules, {'release_admission': checked, 'multicloud_release': release,
                                                 'edge_update': mock.Mock(), 'platform_workers': tokens}):
                with self.assertRaisesRegex(ValueError, 'PUBLICATION_IMAGE_PIN_MISMATCH'):
                    host.execute('refs/heads/main')
                self.assertNotIn('GITHUB_TOKEN', os.environ)
            checked.admit.assert_called_once_with(sha, 'refs/heads/main')
            checked.publication.assert_called_once()
            tokens.github_token.assert_called_once_with('gho_unit_test_token\n')
            release.private.assert_not_called(); release.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
