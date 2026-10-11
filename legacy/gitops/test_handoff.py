import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from handoff import render, FILES, TRUST
from execution import GATE_ORDER


class HandoffTest(unittest.TestCase):
    def test_digest_target_and_unsupported_workload_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {'apiVersion': 'railshot/v0', 'app': 'demo', 'services': [
                {'name': 'web', 'build': {'dockerfile': 'Dockerfile'}, 'port': 8080, 'route': '/', 'health': '/health'}]}
            verdict = {'release_eligible': True, 'ok': True, 'status': 'PASS', 'source_sha256': 'a' * 64,
                       'layers': [{'layer': x, 'ok': True} for x in GATE_ORDER],
                       'images': {'web': 'local/web:test'}, 'image_ids': {'web': 'sha256:' + 'b' * 64}}

            def prepare():
                data = {'railshot.yaml': json.dumps(spec), 'verdict.json': json.dumps(verdict),
                        'images.json': json.dumps({'web': 'ghcr.io/example/web@sha256:' + 'c' * 64})}
                manifest = {'version': 1, 'trust': TRUST, 'source_sha256': verdict['source_sha256'],
                            'images': {'web': {'id': verdict['image_ids']['web'], 'local_ref': 'local/web:test'}},
                            'files': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in data.items() if k != 'images.json'}}
                data['manifest.json'] = json.dumps(manifest)
                for name, content in data.items():
                    (root / name).write_text(content)
                receipt = {'version': 2, 'status': 'published', 'run_id': 1, 'producer_attempt': 1, 'bundle_artifact_id': 2,
                           'source_commit': 'd' * 40, 'target_id': 'aws-demo', 'tenant': 'team', 'app': 'demo',
                           'files': {k: hashlib.sha256((root / k).read_bytes()).hexdigest() for k in FILES}}
                receipt['registry'] = {'visibility': 'public', 'verification': 'anonymous_manifest_read',
                                       'images_sha256': receipt['files']['images.json'], 'image_pull_secret': None}
                (root / 'handoff.json').write_text(json.dumps(receipt))
                return receipt

            target = {'id': 'aws-demo', 'namespace': 'tenant-demo', 'argocd_namespace': 'argocd', 'project': 'railshot',
                      'architecture': 'amd64', 'repo_url': 'https://github.com/example/config.git',
                      'cluster_server': 'https://kubernetes.default.svc', 'path': 'targets/aws-demo/demo',
                      'revision': 'e' * 40, 'node_port': 30080, 'ingress_cidrs': ['10.20.0.0/24'],
                      'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}}}
            receipt = prepare()
            result = render(root, target)
            container = result['workload']['items'][0]['spec']['template']['spec']['containers'][0]
            self.assertFalse(container['securityContext']['readOnlyRootFilesystem'])
            spec['services'][0]['storage'] = {'mountPath': '/var/opt/memos', 'sizeGi': 1}
            prepare()
            persistent = render(root, target)
            import argo
            argo.validate_workload(persistent['workload'], 'demo', 'tenant-demo', 'aws-demo')
            deployment = persistent['workload']['items'][0]
            self.assertEqual(deployment['spec']['strategy'], {'type': 'Recreate'})
            self.assertIn({'name': 'data', 'mountPath': '/var/opt/memos'},
                          deployment['spec']['template']['spec']['containers'][0]['volumeMounts'])
            claim = persistent['workload']['items'][-1]
            self.assertEqual(claim['kind'], 'PersistentVolumeClaim')
            self.assertEqual(claim['metadata']['name'], 'demo-data')
            self.assertEqual(claim['spec']['storageClassName'], 'railshot-persistent')
            claim['spec']['storageClassName'] = 'unreviewed'
            with self.assertRaisesRegex(ValueError, 'restricted persistent'):
                argo.validate_workload(persistent['workload'], 'demo', 'tenant-demo', 'aws-demo')
            spec['services'][0].pop('storage'); receipt = prepare()
            # Historical artifacts keep the old filename and hash bindings.
            (root / 'railshot.yaml').rename(root / 'jasmin.yaml')
            legacy_manifest = json.loads((root / 'manifest.json').read_bytes())
            legacy_manifest['files']['jasmin.yaml'] = legacy_manifest['files'].pop('railshot.yaml')
            (root / 'manifest.json').write_text(json.dumps(legacy_manifest))
            legacy_receipt = copy.deepcopy(receipt)
            legacy_receipt['files']['jasmin.yaml'] = legacy_receipt['files'].pop('railshot.yaml')
            legacy_receipt['files']['manifest.json'] = hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest()
            (root / 'handoff.json').write_text(json.dumps(legacy_receipt))
            self.assertEqual(render(root, target)['workload'], result['workload'])
            (root / 'railshot.yaml').write_bytes((root / 'jasmin.yaml').read_bytes())
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                render(root, target)
            (root / 'jasmin.yaml').unlink()
            receipt = prepare()
            self.assertFalse(result['deployed'])
            self.assertEqual(result['bundle_artifact_id'], 2)
            self.assertEqual(result['producer_attempt'], 1)
            self.assertNotIn('syncPolicy', result['application']['spec'])
            self.assertEqual(result['status'], 'rendered_for_review')
            self.assertNotIn('imagePullSecrets', result['workload']['items'][0]['spec']['template']['spec'])
            self.assertEqual(result['registry'], receipt['registry'])
            self.assertEqual(result['http'], {'route': '/', 'health_path': '/health', 'path_mode': 'preserve',
                                              'container_port': 8080, 'node_port': 30080})
            self.assertEqual(result['application']['metadata']['name'], 'aws-demo-tenant-demo-demo')
            self.assertEqual(result['workload']['items'][0]['spec']['template']['spec']['containers'][0]['image'],
                             'ghcr.io/example/web@sha256:' + 'c' * 64)
            for field, value in [('id', 'onprem-demo'), ('architecture', 'arm64'), ('revision', 'main'),
                                 ('project', 'default'), ('path', '../escape'), ('ingress_cidrs', ['0.0.0.0/0']),
                                 ('namespace', 'argocd'), ('path_mode', 'rewrite')]:
                bad = copy.deepcopy(target)
                bad[field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    render(root, bad)

            spec['services'][0]['route'] = '/health'
            receipt = prepare()
            path_result = render(root, target)
            self.assertEqual(path_result['http']['route'], '/health')
            self.assertEqual(path_result['workload']['items'][0]['spec']['template']['spec']['containers'][0]
                             ['readinessProbe']['httpGet']['path'], '/health')
            for field in ('route', 'health'):
                for value in ('//outside.example', '/a/../b', '/a%2fb', '/a?token=x', '/a#b', '/a\nb'):
                    old = spec['services'][0][field]
                    spec['services'][0][field] = value
                    prepare()
                    with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                        render(root, target)
                    spec['services'][0][field] = old
            receipt = prepare()

            private = copy.deepcopy(receipt)
            private['registry'].update(visibility='private', verification='authenticated_manifest_read',
                                       image_pull_secret={'namespace': target['namespace'], 'name': '1-ghcr-pull'})
            private_target = {**target, 'image_pull_secret': copy.deepcopy(private['registry']['image_pull_secret'])}
            (root / 'handoff.json').write_text(json.dumps(private))
            result = render(root, private_target)
            self.assertEqual(result['workload']['items'][0]['spec']['template']['spec']['imagePullSecrets'],
                             [{'name': '1-ghcr-pull'}])
            self.assertFalse(result['deployed'])
            self.assertEqual(result['status'], 'rendered_for_review')
            self.assertEqual(result['registry'], private['registry'])
            for pull_secret in (None, {'namespace': 'other', 'name': '1-ghcr-pull'},
                                {'namespace': target['namespace'], 'name': 'other'},
                                {**private_target['image_pull_secret'], 'token': 'synthetic-secret'}):
                with self.subTest(target_pull_secret=pull_secret), self.assertRaises(ValueError):
                    render(root, {**target, 'image_pull_secret': pull_secret})
            with self.assertRaises(ValueError):
                render(root, {**private_target, 'namespace': 'other'})

            for changes in ({'visibility': 'public'}, {'verification': 'anonymous_manifest_read'},
                            {'images_sha256': '0' * 64}, {'image_pull_secret': None},
                            {'image_pull_secret': {'namespace': target['namespace'], 'name': '../credential'}},
                            {'image_pull_secret': {'namespace': target['namespace'], 'name': 'a' * 64}},
                            {'image_pull_secret': {**private_target['image_pull_secret'], 'token': 'synthetic-secret'}},
                            {'token': 'synthetic-secret'}):
                bad = copy.deepcopy(private)
                bad['registry'].update(changes)
                (root / 'handoff.json').write_text(json.dumps(bad))
                with self.subTest(registry_changes=changes), self.assertRaises(ValueError):
                    render(root, private_target)
            for changes in ({'version': 1}, {'registry': None}):
                (root / 'handoff.json').write_text(json.dumps({**receipt, **changes}))
                with self.subTest(receipt_changes=changes), self.assertRaises(ValueError):
                    render(root, target)
            prepare()
            (root / 'images.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                render(root, target)
            spec['resources'] = {'postgres': {'size': 'small'}}
            prepare()
            with self.assertRaisesRegex(ValueError, 'PostgreSQL binding'):
                render(root, target)
            binding = {'host': '10.20.0.10', 'port': 5432, 'runtime_secret': 'demo-runtime',
                       'migration_secret': 'demo-migration', 'ca_secret': 'demo-ca'}
            spec['services'][0]['migrate'] = {'command': ['python', 'migrate.py']}
            prepare()
            result = render(root, {**target, 'database': binding})
            job = result['workload']['items'][-1]
            self.assertEqual(job, render(root, {**target, 'database': binding})['workload']['items'][-1])
            spec['services'][0]['migrate']['command'] = ['python', 'next.py']; prepare()
            self.assertNotEqual(job['metadata']['name'], render(root, {**target, 'database': binding})['workload']['items'][-1]['metadata']['name'])
            for changes in ({'host': '0.0.0.0'}, {'host': '8.8.8.8'}, {'host': '127.0.0.1'}, {'port': 443},
                            {'password': 'must-not-cross-git'}, {'migration_secret': 'demo-runtime'}, {'ca_secret': '../secret'}):
                with self.subTest(binding_changes=changes), self.assertRaises(ValueError):
                    render(root, {**target, 'database': {**binding, **changes}})


if __name__ == '__main__':
    unittest.main()
