"""Exercise the shared PVC authority with real files/flock and fake cluster transports."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import register as registrar
import environment
import argo

API_CONFIG = '/var/lib/railshot/config/observer-registrar.json'


class RegistrarAuthorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as root:
            cert = Path(root) / 'ca.crt'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(Path(root) / 'key.pem'), '-out', str(cert), '-days', '1',
                '-subj', '/CN=registrar-test'], check=True, capture_output=True)
            cls.ca_pem = cert.read_text()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / 'pvc'; self.state.mkdir(mode=0o700)
        self.collector_file = self.root / 'collector-prometheus.json'
        self.config = {'version': 1, 'owner': 'shared-observer', 'lifecycle': 'shared',
            'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), 'state_dir': str(self.state),
            'prometheus_url': 'http://10.77.0.20:9090', 'observer_ip': '10.77.0.20',
            'observer_registry_file': str(self.root / 'registry.json'), 'observer_target_id': 'observer-aws',
            'observer_directory': '/home/operator/observer', 'node_metrics_port': 31490, 'cluster_metrics_port': 31491}
        for name in ('key', 'known-hosts'):
            path = self.root / name; path.write_text('offline test fixture'); path.chmod(0o600)
        template = json.loads((registrar.ROOT / 'examples/ansible/aws-node-descriptor.json').read_text())
        registry = {'version': 1, 'targets': {}}
        self.descriptors = {}
        for index, (target, address) in enumerate((('existing-node', '10.77.0.10'), ('normal-api', '10.77.0.11'),
                                                   ('host-delta', '10.77.0.12'), ('observer-aws', '10.77.0.20')), 1):
            instance = f'i-{index:017x}'
            descriptor = {**template, 'target_id': target, 'addresses': {'private': address},
                          'resource_id': template['resource_id'].rsplit('/', 1)[0] + '/' + instance,
                          'transport_ref': 'ssm:ap-northeast-2:' + instance}
            self.descriptors[target] = descriptor
            path = self.root / (target + '.json'); self.write(path, descriptor)
            registry['targets'][target] = {'purpose': 'runtime', 'descriptor_file': str(path),
                'ssh': {'user': 'operator', 'identity_file': str(self.root / 'key'), 'known_hosts_file': str(self.root / 'known-hosts')}}
        self.write(self.root / 'registry.json', registry)
        config_file = self.root / 'observer-registrar.json'; self.write(config_file, self.config)
        read_private = environment.read_private
        # Only the API container's exact config mount maps to this test's private
        # file. Every PVC/registry/descriptor read uses the real private-file gate.
        self.enterContext(patch.object(environment, 'read_private', side_effect=lambda path, **kwargs:
            read_private(config_file if str(path) == API_CONFIG else path, **kwargs)))
        self.enterContext(patch.dict(os.environ, {'RAILSHOT_OBSERVER_PRODUCT_FILE': str(self.state / 'product.json')}))
        self.enterContext(patch('runtime_health.configure_runtime_healthz',
            side_effect=lambda request, descriptor, *args: self.health_binding(descriptor['target_id'])))
        self.old, _ = registrar.registration_row(self.config, self.request('existing-node'), self.descriptors['existing-node'])
        self.old['healthz_url'] = self.health_binding('existing-node')['healthz_url']
        self.write(self.state / 'runtime-healthz.json', {'version': 1, 'targets': {'existing-node': self.health_binding('existing-node')}})
        self.write(self.state / 'product.json', {'version': 1, 'collector': registrar.collector(self.config), 'targets': [self.old]})
        self.syncs = []
        @contextmanager
        def ssh(*_):
            yield ['fake-ssh']
        self.enterContext(patch.object(registrar, 'observer_ssh', ssh))
        self.native = self.enterContext(patch.object(argo, 'native', side_effect=self.remote))

    def write(self, path, value):
        registrar.durable_write(path, json.dumps(value).encode())

    def request(self, target, *, app=False):
        result = {'version': 1, 'target_id': target, 'environment_id': 'env-' + target,
                  'node_ip': self.descriptors[target]['addresses']['private'], 'registry_file': str(self.root / 'registry.json')}
        if app:
            result.update(app='calculator', namespace='tenant-app', probe_url='https://calculator.example/health')
        return result

    def health_binding(self, target):
        ip = self.descriptors[target]['addresses']['private']
        return {'healthz_url': f'https://{ip}:6443/healthz', 'server_name': ip, 'ca_pem': self.ca_pem}

    def api_payload(self, target):
        return {'version': 1, 'action': 'commit', 'config_file': API_CONFIG,
                'collector': registrar.collector_route(self.config), 'request': self.request(target),
                'descriptor': self.descriptors[target], 'health_binding': self.health_binding(target)}

    def remote(self, command, *, document=None, timeout=None):
        self.assertEqual(command[0], 'fake-ssh')
        if document is not None:
            self.syncs.append(copy.deepcopy(document)); self.write(self.collector_file, document['prometheus'])
            return json.dumps({'synced': True})
        return self.collector_file.read_text()

    def test_concurrent_local_registration_and_delta_share_actual_lock_and_preserve_rows(self):
        # Even an old pending desired file may not erase committed product rows.
        self.write(self.state / 'desired.json', {'version': 1, 'targets': []})
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        policy = None
        def kube(namespace, *args, document=None):
            nonlocal policy
            if args[:2] == ('get', 'pods'):
                return {'items': [{'metadata': {'name': 'cilium'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}]}
            if args[:2] == ('get', 'services'):
                return {'items': []}
            if args[0] == 'apply' and document['kind'] == 'CiliumClusterwideNetworkPolicy':
                policy = {**document, 'metadata': {**document['metadata'], 'uid': 'policy-uid'}}
            if args[:2] == ('get', 'CiliumClusterwideNetworkPolicy'):
                return policy
            if args[0] == 'exec' and 'get' in args:
                return {'revision': 7, 'policy': json.dumps([{'Labels': [
                    {'source': 'k8s', 'key': 'io.cilium.k8s.policy.uid', 'value': 'policy-uid'}, *policy['spec']['labels']]}])}
            return None
        @contextmanager
        def runtime(_):
            yield kube
        def remote(command, *, document=None, timeout=None):
            if document is not None and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5), 'test failed to release the first writer')
            return self.remote(command, document=document, timeout=timeout)
        self.native.side_effect = remote
        first_request = self.request('normal-api', app=True)
        def delta():
            second_started.set()
            return registrar.api_request(self.api_payload('host-delta'))
        with patch.object(environment, 'runtime_kubectl', runtime), ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(registrar.register, self.config, first_request, self.root / 'normal-receipt.json')
            try:
                self.assertTrue(entered.wait(5))
                # Prove the native flock is held; neither flock nor storage is mocked.
                with (self.state / 'registration.lock').open('r') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                second = executor.submit(delta)
                self.assertTrue(second_started.wait(5)); self.assertFalse(second.done())
            finally:
                release.set()
            self.assertTrue(first.result(timeout=5)['registered'])
            self.assertEqual(second.result(timeout=5)['status'], 'succeeded')
        product = environment.read_private(self.state / 'product.json')
        desired = environment.read_private(self.state / 'desired.json')
        self.assertEqual(product['targets'], desired['targets'])
        self.assertEqual({row['target_id'] for row in product['targets']}, {'existing-node', 'normal-api', 'host-delta'})
        self.assertIn(self.old, product['targets'])
        self.assertEqual(json.loads(self.collector_file.read_text()), registrar.scrape_config(product['targets']))
        self.assertEqual(len(self.syncs), 2)
        self.assertEqual(set(self.syncs[-1]['certificates']), {'existing-node', 'normal-api', 'host-delta'})
        bindings = environment.read_private(self.state / 'runtime-healthz.json')['targets']
        self.assertEqual(set(bindings), {'existing-node', 'normal-api', 'host-delta'})
        self.assertEqual((self.state / 'runtime-healthz.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(len([r for r in product['targets'] if r['target_id'] == 'normal-api']), 2)
        for name in ('product.json', 'desired.json'):
            self.assertNotIn('ca_pem', (self.state / name).read_text())

    def test_wrong_canonical_environment_or_collector_fails_before_sync_or_callback(self):
        row, _ = registrar.registration_row(self.config, self.request('host-delta'), self.descriptors['host-delta'])
        initial = (self.state / 'product.json').read_bytes()
        callbacks = []
        for change in ('environment', 'collector'):
            with self.subTest(change=change):
                config = {**self.config, **({'owner': 'other-observer'} if change == 'collector' else {})}
                binding = str(self.root / 'wrong-product.json') if change == 'environment' else str(self.state / 'product.json')
                with patch.dict(os.environ, {'RAILSHOT_OBSERVER_PRODUCT_FILE': binding}), self.assertRaises(ValueError):
                    registrar.commit_row(config, row, lambda: callbacks.append('mutated'))
                self.assertFalse((self.state / 'desired.json').exists())
                self.assertEqual((self.state / 'product.json').read_bytes(), initial)
        self.assertEqual(callbacks, []); self.native.assert_not_called()

    def test_api_handler_rejects_wrong_full_route_or_canonical_environment_before_commit(self):
        initial = (self.state / 'product.json').read_bytes()
        for change in ('environment', 'owner', 'observer_directory', 'prometheus_url', 'node_metrics_port'):
            payload = self.api_payload('host-delta')
            if change != 'environment':
                payload['collector'][change] = 32000 if change == 'node_metrics_port' else 'another-value'
            binding = str(self.root / 'wrong-product.json') if change == 'environment' else str(self.state / 'product.json')
            with self.subTest(change=change), patch.dict(os.environ, {'RAILSHOT_OBSERVER_PRODUCT_FILE': binding}), self.assertRaises(ValueError):
                registrar.api_request(payload)
            self.assertEqual((self.state / 'product.json').read_bytes(), initial)
            self.assertFalse((self.state / 'desired.json').exists())
        with self.assertRaises(ValueError):
            registrar.api_request({**self.api_payload('host-delta'), 'config_file': str(self.root / 'observer-registrar.json')})
        self.native.assert_not_called()

    def host(self):
        image = 'ghcr.io/jasmin-softbank/railshot-api@sha256:' + 'a' * 64
        binding = {'context': 'control', 'deployment_uid': '11111111-1111-1111-1111-111111111111',
                   'pvc_uid': '22222222-2222-2222-2222-222222222222', 'config_file': '/var/lib/railshot/config/observer.json'}
        self.host_config = {**self.config, 'state_dir': str(self.root / 'host-copy'), 'api_registrar': binding}
        self.pod_uid = 'pod-uid'; self.pvc_uid = binding['pvc_uid']; self.api_image = image
        def kubectl(context, namespace, *args):
            self.assertEqual((context, namespace), ('control', 'railshot-system'))
            if args[1] == 'deployment':
                return {'metadata': {'uid': binding['deployment_uid']}, 'spec': {'template': {'spec': {'containers': [{'name': 'api', 'image': self.api_image}]}}}}
            if args[1] == 'pvc':
                return {'metadata': {'uid': self.pvc_uid}}
            if args[1] == 'replicasets':
                return {'items': [{'metadata': {'uid': 'rs-uid', 'ownerReferences': [{'kind': 'Deployment', 'uid': binding['deployment_uid'], 'controller': True}]}}]}
            self.assertEqual(args[1], 'pods')
            return {'items': [{'metadata': {'name': 'api-pod', 'uid': self.pod_uid,
                'ownerReferences': [{'kind': 'ReplicaSet', 'uid': 'rs-uid', 'controller': True}]},
                'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]},
                'spec': {'serviceAccountName': 'railshot-product', 'volumes': [{'name': 'state', 'persistentVolumeClaim': {'claimName': 'railshot-api'}}],
                    'containers': [{'name': 'api', 'image': self.api_image, 'volumeMounts': [{'name': 'state', 'mountPath': '/var/lib/railshot'}]}]}}]}
        self.enterContext(patch.object(argo, 'kubectl', side_effect=kubectl))
        self.enterContext(patch.dict(os.environ, {'RAILSHOT_RELEASE_API_IMAGE': image}))
        return environment.read_private(self.state / 'product.json')

    def test_host_pod_replacement_or_timeout_sends_once_and_never_writes_local_product(self):
        product = self.host(); request = self.request('host-delta'); descriptor = self.descriptors['host-delta']
        row, _ = registrar.registration_row(self.host_config, request, descriptor)
        response = {'status': 'succeeded', 'product': {**product, 'targets': [*product['targets'], row]}}
        initial = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for mode in ('pod_replaced', 'timeout'):
            self.pod_uid = 'pod-uid'
            def execute(command, **kwargs):
                self.assertIn('--api-registrar', command)
                self.assertEqual(json.loads(kwargs['input'])['request'], request)
                if mode == 'timeout':
                    raise subprocess.TimeoutExpired(command, kwargs['timeout'])
                self.pod_uid = 'replacement-pod'
                return SimpleNamespace(returncode=0, stdout=json.dumps(response))
            with self.subTest(mode=mode), patch.object(registrar.subprocess, 'run', side_effect=execute) as execute_mock:
                with self.assertRaises((ValueError, subprocess.TimeoutExpired)):
                    registrar.api_exchange(self.host_config, request=request, descriptor=descriptor,
                                           health_binding=self.health_binding('host-delta'))
                execute_mock.assert_called_once()
            self.assertFalse((self.root / 'host-copy').exists())
            self.assertEqual(initial, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_host_requires_canonical_api_and_exact_pvc_image_before_exec(self):
        product = self.host()
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'succeeded', 'product': product}))
        with patch.object(registrar.subprocess, 'run', return_value=response) as execute:
            with self.assertRaises(ValueError):
                registrar.product(self.config, require_api=True)
            for key, value in (('pvc_uid', 'other-pvc'), ('api_image', 'ghcr.io/jasmin-softbank/railshot-api@sha256:' + 'b' * 64)):
                original = getattr(self, key); setattr(self, key, value)
                with self.subTest(binding=key), self.assertRaises(ValueError):
                    registrar.product(self.host_config, require_api=True)
                setattr(self, key, original)
            execute.assert_not_called()
            self.assertEqual(registrar.product(self.host_config, require_api=True), product)
            execute.assert_called_once()
            self.assertEqual(json.loads(execute.call_args.kwargs['input'])['action'], 'read')
        self.assertFalse((self.root / 'host-copy').exists())

    def test_host_sends_one_health_binding_and_accepts_the_enriched_node_row(self):
        product = self.host(); request = self.request('host-delta')
        descriptor = self.descriptors['host-delta']; binding = self.health_binding('host-delta')
        row, _ = registrar.registration_row(self.host_config, request, descriptor)
        product['targets'].append({**row, 'healthz_url': binding['healthz_url']})
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'succeeded', 'product': product}))
        with patch.object(registrar.subprocess, 'run', return_value=response) as execute:
            self.assertEqual(registrar.api_exchange(self.host_config, request=request,
                descriptor=descriptor, health_binding=binding), product)
            execute.assert_called_once()
            payload = json.loads(execute.call_args.kwargs['input'])
            self.assertEqual(payload['health_binding'], binding)
            self.assertEqual(payload['request'], request)
            self.assertNotIn('targets', payload)
        self.assertFalse((self.root / 'host-copy').exists())


if __name__ == '__main__':
    unittest.main()
