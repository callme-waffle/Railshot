import base64
import copy
import json
import unittest
from unittest.mock import patch

import argo
import logs
import test_argo as fixtures


class LogsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ArgoTest()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.review = self.fixture.review
        root = self.fixture.root
        directory = root / 'deployment-1'; directory.mkdir(mode=0o700)
        self.fixture.save_review(self.review, directory / 'review'); (directory / 'review').chmod(0o700)
        self.config = {'state_dir': str(root), 'context': 'control', 'targets': {'k3s-aws': {
            'app': 'demo', 'tenant': 'team', 'target': {'id': 'k3s-aws', 'namespace': 'tenant-demo',
                'argocd_namespace': 'argocd', 'project': 'railshot', 'repo_url': 'https://github.com/example/config.git',
                'path': 'targets/k3s-aws/demo', 'cluster_server': 'https://192.0.2.1:6443'}}}}
        self.value = {'deployment_id': 'deployment-1', 'target_id': 'k3s-aws', 'app': 'demo',
                      'source_commit': 'd' * 40, 'run_id': '1', 'revision': 'e' * 40}
        self.live = self.fixture.healthy()
        self.deployment = copy.deepcopy(self.review['workload']['items'][0])
        self.deployment['metadata'].update(uid='deployment-uid', generation=1)
        template = self.deployment['spec']['template']
        self.rs = {'kind': 'ReplicaSet', 'metadata': {'name': 'demo-rs', 'namespace': 'tenant-demo', 'uid': 'rs-uid',
            'ownerReferences': [{'kind': 'Deployment', 'uid': 'deployment-uid', 'controller': True}]},
            'spec': {'template': copy.deepcopy(template)}}
        self.pod = copy.deepcopy(template)
        self.pod.update(kind='Pod', status={'containerStatuses': [{'name': 'web', 'imageID': 'containerd://sha256:' + 'c' * 64}]})
        self.pod['metadata'].update(name='demo-rs-pod', namespace='tenant-demo', uid='pod-uid',
            ownerReferences=[{'kind': 'ReplicaSet', 'uid': 'rs-uid', 'controller': True}])
        self.calls = []; self.server_name = None
        data = {'name': 'k3s-aws', 'server': 'https://192.0.2.1:6443', 'project': 'railshot',
                'namespaces': 'tenant-demo', 'clusterResources': 'false',
                'config': json.dumps({'bearerToken': 'synthetic-sensitive-token', 'tlsClientConfig': {
                    'insecure': False, 'caData': base64.b64encode(b'CA').decode()}})}
        self.secret = {'kind': 'Secret', 'metadata': {'name': 'railshot-k3s-aws', 'namespace': 'argocd',
                        'labels': logs.credentials.LABELS},
                       'data': {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}}

    def control(self, context, namespace, *args):
        self.assertEqual(context, 'control'); self.assertEqual(namespace, 'argocd')
        self.calls.append(args)
        self.assertEqual(args[0], 'get')
        return self.secret if args[1] == 'secret' else self.live

    def customer(self, server, ca, token, path, *, server_name=None):
        self.assertEqual(server_name, self.server_name)
        self.assertEqual((server, ca, token), ('https://192.0.2.1:6443', b'CA', 'synthetic-sensitive-token'))
        self.calls.append(path)
        if '/deployments/' in path:
            return self.deployment
        if '/replicasets?' in path:
            return {'kind': 'ReplicaSetList', 'items': [self.rs]}
        if '/pods?' in path:
            return {'kind': 'PodList', 'items': [self.pod]}
        self.assertTrue(path.endswith('/pods/demo-rs-pod'))
        return self.pod

    def execute(self, tail=None):
        with patch('argo.kubectl', side_effect=self.control), patch('credentials.customer', side_effect=self.customer), \
                patch('logs.tail', side_effect=tail, return_value='2026-10-03T00:00:00Z listening on 8080\n') as read:
            self.last_read = read
            output = logs.execute(self.config, self.value)
        return output, read

    def test_only_exact_review_and_owned_current_pod_can_supply_bounded_logs(self):
        output, read = self.execute()
        self.assertEqual(output['state'], 'ready')
        self.assertEqual(output['entries'][0]['pod'], 'demo-rs-pod')
        path = read.call_args.args[1]
        self.assertIn('/namespaces/tenant-demo/pods/demo-rs-pod/log?', path)
        self.assertIn('tailLines=100', path); self.assertIn('follow=false', path); self.assertIn('limitBytes=10922', path)
        self.assertNotIn('synthetic-sensitive', json.dumps(output))
        self.assertTrue(all(not isinstance(call, tuple) or call[0] == 'get' for call in self.calls))
        for key, value in [('app', 'foreign'), ('revision', 'f' * 40), ('run_id', '2'), ('source_commit', 'c' * 40),
                           ('deployment_id', '../foreign')]:
            changed = {**self.value, key: value}
            with self.subTest(key=key), patch('argo.kubectl') as kube, self.assertRaises((ValueError, FileNotFoundError)):
                logs.execute(self.config, changed)
            kube.assert_not_called()
        self.live['spec']['source']['targetRevision'] = 'a' * 40
        with self.assertRaises(ValueError):
            self.execute()
        self.last_read.assert_not_called()

    def test_app_credential_labels_keep_exact_project_and_single_namespace(self):
        app_id = 'app-' + 'a' * 24
        review = copy.deepcopy(self.review)
        review['receipt']['target_id'] = review['application']['spec']['project'] = app_id
        review['application']['spec']['destination']['namespace'] = app_id
        self.secret['metadata']['name'] = 'railshot-' + app_id
        for key in ('name', 'project', 'namespaces'):
            self.secret['data'][key] = base64.b64encode(app_id.encode()).decode()
        original = copy.deepcopy(self.secret)
        for kind in ('cluster', 'railshot-application'):
            self.secret = copy.deepcopy(original)
            self.secret['metadata']['labels'] = {**logs.credentials.LABELS, 'argocd.argoproj.io/secret-type': kind}
            with patch('argo.kubectl', side_effect=self.control):
                auth, options = logs.customer_auth(self.config, review)
            self.assertEqual(auth, ('https://192.0.2.1:6443', b'CA', 'synthetic-sensitive-token'))
            self.assertEqual(options, {})
            for key, value in [('project', ''), ('project', 'other-project'), ('namespaces', app_id + ',other-namespace')]:
                self.secret['data'][key] = base64.b64encode(value.encode()).decode()
                with self.subTest(kind=kind, field=key), patch('argo.kubectl', side_effect=self.control), self.assertRaises(ValueError):
                    logs.customer_auth(self.config, review)
                self.secret['data'][key] = original['data'][key]

    def test_projectless_shared_cluster_keeps_legacy_review_and_namespace_binding(self):
        self.secret['data']['project'] = ''
        self.secret['data']['namespaces'] = base64.b64encode(b'tenant-demo,tenant-other').decode()
        output, read = self.execute()
        self.assertEqual(output['state'], 'ready')
        self.assertIn('/namespaces/tenant-demo/', read.call_args.args[1])
        for key, value in [('project', 'foreign'), ('name', 'other-cluster'), ('server', 'https://192.0.2.2:6443'),
                           ('namespaces', 'tenant-other')]:
            original = self.secret['data'][key]
            self.secret['data'][key] = base64.b64encode(value.encode()).decode()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.execute()
            self.last_read.assert_not_called()
            self.secret['data'][key] = original
        self.secret['metadata']['labels'] = {**logs.credentials.LABELS, 'argocd.argoproj.io/secret-type': 'railshot-application'}
        with self.assertRaises(ValueError):
            self.execute()
        self.last_read.assert_not_called()

    def test_registered_private_tls_name_reaches_every_metadata_and_log_request(self):
        auth = json.loads(base64.b64decode(self.secret['data']['config']))
        auth['tlsClientConfig']['serverName'] = self.server_name = '10.66.0.2'
        self.secret['data']['config'] = base64.b64encode(json.dumps(auth).encode()).decode()
        output, read = self.execute()
        self.assertEqual(output['state'], 'ready')
        self.assertEqual(read.call_args.kwargs, {'server_name': self.server_name})
        with patch('logs.ssl.create_default_context') as tls, patch('credentials.RegisteredHTTPSConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200; response.read.return_value = b'listening\n'
            self.assertEqual(logs.tail(read.call_args.args[0], read.call_args.args[1], **read.call_args.kwargs), 'listening\n')
            tls.assert_called_once_with(cadata='CA')
            factory.assert_called_once_with('192.0.2.1', 6443, server_name='10.66.0.2', context=tls.return_value, timeout=10)
            factory.return_value.close.assert_called_once()
            response.status = 302
            with self.assertRaises(ValueError):
                logs.tail(read.call_args.args[0], '/fixed', **read.call_args.kwargs)
        auth['tlsClientConfig']['serverName'] = '34.47.68.21'
        self.secret['data']['config'] = base64.b64encode(json.dumps(auth).encode()).decode()
        with self.assertRaises(ValueError):
            self.execute()

    def test_foreign_pod_owner_or_image_is_never_read_and_rollout_race_discards_output(self):
        self.rs['metadata']['ownerReferences'][0]['uid'] = 'foreign'
        output, read = self.execute()
        self.assertEqual(output['entries'], []); read.assert_not_called()
        self.rs['metadata']['ownerReferences'][0]['uid'] = 'deployment-uid'
        self.pod['status']['containerStatuses'][0]['imageID'] = 'sha256:' + 'b' * 64
        with self.assertRaisesRegex(ValueError, 'digest differs'):
            self.execute()
        self.last_read.assert_not_called()
        self.pod['status']['containerStatuses'][0]['imageID'] = 'sha256:' + 'c' * 64
        def replaced(*_args):
            self.live['status']['sync']['revision'] = 'f' * 40
            return 'must not escape after another deployment\n'
        output, read = self.execute(tail=replaced)
        self.assertEqual(output['state'], 'unavailable'); self.assertEqual(output['entries'], [])

    def test_redacts_credentials_and_tls_transport_rejects_redirect_and_oversize(self):
        raw = ('normal application output\npassword="hello world" DB_PASSWORD=shh\nAuthorization: Bearer bearer-secret\n'
               'postgresql://user:db-secret@db/app\n{"api_key":"key-secret"}\n'
               '-----BEGIN RSA PRIVATE KEY-----\nprivate-data\n-----END RSA PRIVATE KEY-----\n'
               'github_pat_abcdef12345678901234567890\n')
        value = logs.redact(raw)
        self.assertIn('normal application output', value)
        for secret in ('hello world', 'shh', 'bearer-secret', 'db-secret', 'key-secret', 'private-data', 'github_pat_'):
            self.assertNotIn(secret, value)
        class Response:
            status, body = 200, raw.encode()
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def read(self, maximum): return self.body[:maximum]
        response = Response()
        with patch('logs.ssl.create_default_context') as tls, patch('logs.request.build_opener') as factory:
            factory.return_value.open.return_value = response
            text = logs.tail(('https://registered.example:6443', b'CA', 'private-bearer'), '/fixed/log?tailLines=100')
            self.assertEqual(text, value)
            tls.assert_called_once_with(cadata='CA')
            self.assertTrue(any(isinstance(h, logs.credentials.NoRedirect) for h in factory.call_args.args))
            self.assertIsNone(logs.credentials.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other'))
            response.status = 302
            with self.assertRaises(ValueError): logs.tail(('https://registered.example', b'CA', 'private-bearer'), '/fixed')
            response.status, response.body = 200, b'a' * (logs.LIMIT + 1)
            with self.assertRaises(ValueError): logs.tail(('https://registered.example', b'CA', 'private-bearer'), '/fixed')


if __name__ == '__main__':
    unittest.main()
