import base64
import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
import subprocess
from pathlib import Path

from publication import prepare, verify_registry, validate_registry, validate_target


class PublicationTest(unittest.TestCase):
    def test_registered_application_binding_selects_private_pull_namespace(self):
        binding = {'app': 'new-app', 'tenant': 'demo',
                   'image_pull_secret': {'namespace': 'new-namespace', 'name': 'ghcr-pull'}}
        configured = {'CONFIGURED_BINDINGS': json.dumps({'new-target': binding}),
                      'TARGET_ID': 'new-target', 'APP': 'new-app', 'TENANT': 'demo'}
        self.assertEqual(validate_target(configured), binding)
        for field in ('APP', 'TENANT', 'TARGET_ID'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_target({**configured, field: 'another'})
        env = {**configured, 'REGISTRY_PREFIX': 'ghcr.io/owner', 'REGISTRY_VISIBILITY': 'private',
               'GHCR_PULL_USERNAME': 'reader', 'GHCR_PULL_TOKEN': 'synthetic-token',
               'PULL_SECRET_NAMESPACE': 'old-namespace', 'PULL_SECRET_NAME': 'old-secret'}
        with patch('publication.subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
            result = verify_registry(json.dumps({'web': 'ghcr.io/owner/web@sha256:' + 'a' * 64}).encode(), env)
        self.assertEqual(result['image_pull_secret'], binding['image_pull_secret'])

    def test_operator_target_allowlist_and_legacy_single_target_fail_closed(self):
        configured = {'CONFIGURED_TARGET': 'legacy-target',
                      'CONFIGURED_TARGETS': '["k3s-aws","k3s-gcp"]'}
        for target in ('k3s-aws', 'k3s-gcp'):
            validate_target({**configured, 'TARGET_ID': target})
        validate_target({'CONFIGURED_TARGET': 'legacy-target', 'TARGET_ID': 'legacy-target'})
        for target in ('legacy-target', 'onprem', '', 'k3s-aws; false'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_target({**configured, 'TARGET_ID': target})
        for raw in ('[]', 'null', '{}', '"k3s-aws"', '["k3s-aws",false]',
                    '["k3s-aws","k3s-aws"]', '["k3s-aws","../bad"]', 'bad json'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_target({**configured, 'CONFIGURED_TARGETS': raw, 'TARGET_ID': 'k3s-aws'})
        with self.assertRaises(ValueError):
            validate_target({'TARGET_ID': 'k3s-aws'})

    @patch('publication.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
    def test_handoff_preserves_publisher_bytes_and_producer_identity(self, run):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'bundle'; bundle.mkdir()
            inputs = {'railshot.yaml': b'app: my-app\n', 'verdict.json': b'{"ok": true}\n',
                      'manifest.json': b'{"version": 1}\n'}
            for name, content in inputs.items():
                (bundle / name).write_bytes(content)
            images = root / 'images.json'; images.write_text(json.dumps({'web': 'ghcr.io/owner/web@sha256:' + 'c' * 64}))
            inputs['images.json'] = images.read_bytes()
            env = {'SOURCE_COMMIT': 'a' * 40, 'GITHUB_SHA': 'a' * 40, 'TARGET_ID': 'aws-demo',
                   'TENANT': 'demo', 'APP': 'my-app', 'GITHUB_RUN_ID': '123',
                   'GITHUB_RUN_ATTEMPT': '2', 'BUNDLE_ARTIFACT_ID': '777',
                   'REGISTRY_PREFIX': 'ghcr.io/owner', 'REGISTRY_VISIBILITY': 'public'}
            output = root / 'published'
            prepare(bundle, images, output, env)
            receipt = json.loads((output / 'handoff.json').read_text())
            self.assertEqual(receipt['version'], 2)
            self.assertEqual(receipt['status'], 'published')
            validate_registry(receipt['registry'], hashlib.sha256(images.read_bytes()).hexdigest())
            self.assertEqual(receipt['producer_attempt'], 2)
            self.assertEqual(receipt['bundle_artifact_id'], 777)
            self.assertEqual(receipt['source_commit'], env['SOURCE_COMMIT'])
            self.assertEqual(receipt['target_id'], 'aws-demo')
            self.assertEqual({p.name for p in output.iterdir()}, set(inputs) | {'handoff.json'})
            for name, content in inputs.items():
                self.assertEqual((output / name).read_bytes(), content)
                self.assertEqual(receipt['files'][name], hashlib.sha256(content).hexdigest())
            with self.assertRaises(FileExistsError):
                prepare(bundle, images, output, env)
            with self.assertRaisesRegex(ValueError, 'source commit mismatch'):
                prepare(bundle, images, root / 'bad', {**env, 'GITHUB_SHA': 'b' * 40})
            self.assertFalse((root / 'bad').exists())

            (bundle / 'railshot.yaml').rename(bundle / 'jasmin.yaml')
            legacy_output = root / 'legacy-published'
            legacy = prepare(bundle, images, legacy_output, env)
            self.assertEqual((legacy_output / 'jasmin.yaml').read_bytes(), inputs['railshot.yaml'])
            self.assertEqual(legacy['files']['jasmin.yaml'], receipt['files']['railshot.yaml'])
            self.assertNotIn('railshot.yaml', legacy['files'])
            (bundle / 'railshot.yaml').write_bytes(inputs['railshot.yaml'])
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                prepare(bundle, images, root / 'ambiguous', env)
            self.assertFalse((root / 'ambiguous').exists())

    def test_registry_access_is_digest_bound_isolated_and_fail_closed(self):
        images = json.dumps({'web': 'ghcr.io/owner/web@sha256:' + 'a' * 64,
                             'api': 'ghcr.io/owner/api@sha256:' + 'b' * 64}).encode()
        env = {'REGISTRY_PREFIX': 'ghcr.io/owner', 'REGISTRY_VISIBILITY': 'private',
               'GHCR_PULL_USERNAME': 'operator', 'GHCR_PULL_TOKEN': 'synthetic-pull-token',
               'GHCR_TOKEN': 'synthetic-push-token', 'DOCKER_CONFIG': '/unused-push-config',
               'DOCKER_AUTH_CONFIG': 'unused-inherited-auth', 'PULL_SECRET_NAMESPACE': 'tenant-demo',
               'PULL_SECRET_NAME': 'ghcr-pull'}
        configs = []
        def inspect(argv, **kwargs):
            child_env = kwargs['env']
            directory = Path(child_env['DOCKER_CONFIG'])
            configs.append(directory)
            self.assertNotEqual(str(directory), env['DOCKER_CONFIG'])
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / 'config.json').stat().st_mode & 0o777, 0o600)
            auth = json.loads((directory / 'config.json').read_text())['auths']['ghcr.io']
            if env['REGISTRY_VISIBILITY'] == 'private':
                self.assertEqual(base64.b64decode(auth['auth']).decode(), 'operator:synthetic-pull-token')
            else:
                self.assertEqual(auth, {})
            for key in ('GHCR_PULL_TOKEN', 'GHCR_TOKEN', 'DOCKER_AUTH_CONFIG'):
                self.assertNotIn(key, child_env)
            self.assertEqual(argv[:3], ['docker', 'manifest', 'inspect'])
            self.assertIn(argv[3], json.loads(images).values())
            return subprocess.CompletedProcess(argv, 0)
        with patch('publication.subprocess.run', side_effect=inspect) as run:
            private = verify_registry(images, env)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(private['verification'], 'authenticated_manifest_read')
            self.assertEqual(private['image_pull_secret'], {'namespace': 'tenant-demo', 'name': 'ghcr-pull'})
            self.assertNotIn('token', json.dumps(private))
            env['REGISTRY_VISIBILITY'] = 'public'
            self.assertEqual(verify_registry(images, env)['verification'], 'anonymous_manifest_read')
            self.assertEqual(run.call_count, 4)
        self.assertTrue(all(not path.exists() for path in configs))
        with patch('publication.subprocess.run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(ValueError, 'not readable'):
                verify_registry(images, env)
        with patch('publication.subprocess.run') as run:
            for changes in ({'GHCR_PULL_TOKEN': ''}, {'PULL_SECRET_NAME': ''},
                            {'PULL_SECRET_NAMESPACE': '../escape'}, {'REGISTRY_PREFIX': 'ghcr.io/other'}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    verify_registry(images, {**env, 'REGISTRY_VISIBILITY': 'private', **changes})
            run.assert_not_called()
        for change in ({'token': 'forbidden'}, {'images_sha256': '0' * 64},
                       {'verification': 'ready'}, {'image_pull_secret': {'namespace': 'tenant-demo', 'name': 'ghcr-pull', 'auth': 'forbidden'}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_registry({**copy.deepcopy(private), **change}, private['images_sha256'])


if __name__ == '__main__':
    unittest.main()
