"""Local preparation only: real publication validators/render with synthetic registration/native APIs."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import application_release as release
import applications
import environment as runtime
import test_applications as registration_fixtures
import test_argo as artifact_fixtures


class ApplicationReleaseTest(unittest.TestCase):
    def setUp(self):
        self.registration = registration_fixtures.ApplicationsTest(methodName='runTest')
        self.registration.setUp(); self.addCleanup(self.registration.doCleanups)
        self.registered = self.registration.register()
        self.assertEqual(self.registered['status'], 'succeeded')
        self.config_path = self.registration.config_path
        self.artifacts = artifact_fixtures.ArgoTest(methodName='runTest')
        self.artifacts.setUp(); self.addCleanup(self.artifacts.doCleanups)
        self.contents = {path.name: path.read_bytes() for path in (self.artifacts.root / 'published').iterdir()}
        self.spec = json.loads(self.contents['railshot.yaml'])
        self.spec['app'] = self.registered['app']
        self.spec['services'][0].update(port=7070, route='/application', health='/alive')
        self.deployment_id = '12345678-1234-1234-1234-123456789abc'
        self.request = self.publication()
        self.git = self.enterContext(patch.object(runtime.bridge, 'git', return_value='e' * 40))
        self.render = self.enterContext(patch.object(release.handoff, 'render', wraps=release.handoff.render))
        self.edge = self.enterContext(patch.object(runtime.bridge.edge, 'prepare', side_effect=AssertionError('no route mutation')))
        self.probe = self.enterContext(patch.object(runtime.bridge, 'public_probe', side_effect=AssertionError('no public request')))

    def publication(self):
        contents = copy.deepcopy(self.contents)
        spec_name = 'railshot.yaml' if 'railshot.yaml' in contents else 'jasmin.yaml'
        contents[spec_name] = json.dumps(self.spec).encode()
        manifest = json.loads(contents['manifest.json'])
        manifest['files'] = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()
                             if name in (spec_name, 'verdict.json')}
        contents['manifest.json'] = json.dumps(manifest).encode()
        receipt = json.loads(contents['handoff.json'])
        receipt.update(app=self.registered['app'], target_id=self.registered['target_id'], tenant='demo')
        receipt['files'] = {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items() if name != 'handoff.json'}
        receipt['registry'] = {'visibility': 'private', 'verification': 'authenticated_manifest_read',
            'images_sha256': receipt['files']['images.json'],
            'image_pull_secret': {'namespace': self.registered['namespace'], 'name': 'ghcr-pull'}}
        contents['handoff.json'] = json.dumps(receipt).encode()
        return {'deployment_id': self.deployment_id, 'application_id': self.registered['application_id'],
                'environment_id': self.registered['environment_id'],
                'publication': {**receipt, 'artifact_id': 7, 'images': json.loads(contents['images.json'])},
                'files': {name: base64.b64encode(raw).decode() for name, raw in contents.items()}}

    def prepare(self, request=None):
        return release.finalize(self.config_path, self.request if request is None else request)

    @property
    def prepared_dir(self):
        return self.registration.home() / 'deployments' / self.deployment_id

    def test_prepares_actual_spec_contract_without_route_mutation_or_success_claim(self):
        result = self.prepare()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['phase'], 'cd_prepared')
        self.assertEqual(result['public_route_state'], 'pending')
        self.assertNotIn('url', result)
        self.assertNotIn('error', result)
        self.assertNotIn('deployed', result)
        cd = runtime.bridge.read_config(self.prepared_dir / 'cd.json')
        registered = cd['targets'][self.registered['application_id']]
        self.assertEqual(registered['public_http'], {'url': 'https://' + self.registered['hostname'] + '/alive', 'expected_status': 200})
        self.assertNotIn('revision', registered['target'])
        route = json.loads((self.prepared_dir / 'route-request.json').read_bytes())
        self.assertEqual((route['port'], route['health_path'], route['route']), (7070, '/alive', '/application'))
        self.assertEqual(route['namespace'], self.registered['namespace'])
        self.assertEqual(route['node_port'], self.registered['node_port'])
        self.assertEqual(route['resource']['registry_target_id'], self.registered['environment_id'])
        self.assertEqual(route['resource']['resource_id'], self.registration.fixture.descriptor['resource_id'])
        self.assertEqual(route['source_commit'], self.request['publication']['source_commit'])
        self.assertEqual(route['images'], self.request['publication']['images'])
        self.assertEqual(route['publication_id']['artifact_id'], 7)
        self.assertEqual(route['files_sha256']['handoff.json'], hashlib.sha256(base64.b64decode(self.request['files']['handoff.json'])).hexdigest())
        self.assertEqual(route['public_route_state'], 'pending')
        self.render.assert_called_once()
        self.git.assert_called_once_with({k: v for k, v in cd.items() if k != '_sha256'}, 'rev-parse', '--verify', 'HEAD')
        self.registration.native.assert_not_called(); self.registration.execute.assert_not_called()
        self.edge.assert_not_called(); self.probe.assert_not_called()
        for file in ('cd.json', 'route-request.json', 'release.json'):
            self.assertEqual((self.prepared_dir / file).stat().st_mode & 0o777, 0o600)
            self.assertNotIn('synthetic-secret', (self.prepared_dir / file).read_text())
        self.assertFalse(list(self.prepared_dir.glob('.publication-*')))

    def test_exact_replay_reuses_bytes_without_git_or_renderer(self):
        result = self.prepare()
        before = {path.name: path.read_bytes() for path in self.prepared_dir.iterdir()}
        self.git.reset_mock(); self.render.reset_mock()
        self.assertEqual(self.prepare(), result)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.prepared_dir.iterdir()})
        self.git.assert_not_called(); self.render.assert_not_called()

    def test_changed_publication_or_health_cannot_overwrite_same_deployment(self):
        self.prepare()
        before = (self.prepared_dir / 'cd.json').read_bytes(), (self.prepared_dir / 'route-request.json').read_bytes()
        changed = copy.deepcopy(self.request); changed['publication']['artifact_id'] += 1
        with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_RELEASE_CONFLICT'):
            self.prepare(changed)
        self.spec['services'][0]['health'] = '/new-health'
        with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_RELEASE_CONFLICT'):
            self.prepare(self.publication())
        self.assertEqual(before, ((self.prepared_dir / 'cd.json').read_bytes(), (self.prepared_dir / 'route-request.json').read_bytes()))

    def test_new_deployment_may_use_new_health_without_overwriting_old(self):
        self.prepare(); old_path = self.prepared_dir; old_cd = (old_path / 'cd.json').read_bytes()
        self.deployment_id = '12345678-1234-1234-1234-123456789abd'
        self.spec['services'][0]['health'] = '/new-health'
        result = self.prepare(self.publication())
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(json.loads((self.prepared_dir / 'route-request.json').read_text())['health_path'], '/new-health')
        self.assertEqual((old_path / 'cd.json').read_bytes(), old_cd)

    def test_mismatched_id_tenant_source_and_image_fail_before_git(self):
        variants = []
        for field, value in (('app', 'other-app'), ('tenant', 'other'), ('target_id', 'other-target'),
                             ('source_commit', 'f' * 40), ('images', {'web': 'ghcr.io/example/web@sha256:' + 'f' * 64})):
            changed = copy.deepcopy(self.request); changed['publication'][field] = value; variants.append(changed)
        for field, value in (('application_id', 'app-' + '0' * 24), ('environment_id', 'other-environment'), ('deployment_id', '../escape')):
            variants.append({**self.request, field: value})
        for changed in variants:
            with self.subTest(changed=changed.keys()), self.assertRaises((ValueError, FileNotFoundError)):
                self.prepare(changed)
        self.git.assert_not_called()
        self.assertFalse((self.prepared_dir / 'cd.json').exists())

    def test_partial_gate_and_cross_namespace_pull_secret_are_rejected(self):
        self.contents['verdict.json'] = json.dumps({**json.loads(self.contents['verdict.json']), 'release_eligible': False}).encode()
        with self.assertRaisesRegex(ValueError, 'full release verdict'):
            self.prepare(self.publication())
        self.contents['verdict.json'] = json.dumps({**json.loads(self.contents['verdict.json']), 'release_eligible': True}).encode()
        request = self.publication()
        receipt = request['publication']; receipt['registry']['image_pull_secret']['namespace'] = 'another-app'
        original = json.loads(base64.b64decode(request['files']['handoff.json']))
        original['registry'] = receipt['registry']
        request['files']['handoff.json'] = base64.b64encode(json.dumps(original).encode()).decode()
        with self.assertRaisesRegex(ValueError, 'pull Secret'):
            self.prepare(request)
        self.assertFalse((self.prepared_dir / 'cd.json').exists())

    def test_registration_binding_and_current_policy_are_checked(self):
        binding_path = self.registration.home() / 'binding.json'
        original = binding_path.read_bytes()
        binding = json.loads(original); binding['registered']['target']['namespace'] = 'kube-system'
        runtime.save(binding_path, binding)
        with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_BINDING_CHANGED'):
            self.prepare()
        runtime.durable_write(binding_path, original)
        self.registration.config['environments'][self.registered['environment_id']]['target']['ingress_cidrs'] = ['10.1.0.0/16']
        self.registration.write_config()
        with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_BINDING_CHANGED'):
            self.prepare()
        self.git.assert_not_called()

    def test_unknown_registration_and_tampered_prepared_output_fail_closed(self):
        receipt_path = self.registration.home() / 'registration.json'
        receipt = json.loads(receipt_path.read_bytes())
        runtime.save(receipt_path, {**receipt, 'status': 'unknown'})
        with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_NOT_REGISTERED'):
            self.prepare()
        runtime.save(receipt_path, receipt)
        self.prepare()
        cd = json.loads((self.prepared_dir / 'cd.json').read_bytes()); cd['context'] = 'other-cluster'
        runtime.save(self.prepared_dir / 'cd.json', cd)
        with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_RELEASE_CHANGED'):
            self.prepare()

    def test_partial_local_write_never_becomes_success_on_retry(self):
        original = runtime.save
        def fail_route(path, value):
            if path.name == 'route-request.json': raise OSError('synthetic disk failure')
            return original(path, value)
        with patch.object(runtime, 'save', side_effect=fail_route):
            with self.assertRaises(OSError): self.prepare()
        self.assertEqual(json.loads((self.prepared_dir / 'release.json').read_bytes())['status'], 'preparing')
        with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_PREPARATION_INCOMPLETE'):
            self.prepare()
        self.edge.assert_not_called(); self.probe.assert_not_called()

    def test_historical_spec_bytes_keep_original_names_and_hashes(self):
        self.contents['jasmin.yaml'] = self.contents.pop('railshot.yaml')
        self.prepare(self.publication())
        route = json.loads((self.prepared_dir / 'route-request.json').read_bytes())
        self.assertIn('jasmin.yaml', route['files_sha256'])
        self.assertNotIn('railshot.yaml', route['files_sha256'])


if __name__ == '__main__':
    unittest.main()
