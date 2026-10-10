import base64
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bootstrap import PREPARE, HERE
from register import registration_row, merge_rows, scrape_config, settings
import register as registration


class RegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as root:
            cert = Path(root) / 'ca.crt'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(Path(root) / 'key.pem'), '-out', str(cert), '-days', '1',
                '-subj', '/CN=observer-test'], check=True, capture_output=True)
            cls.ca_pem = cert.read_text()

    def setUp(self):
        self.config = {'version': 1, 'owner': 'shared-observer', 'lifecycle': 'shared', 'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), 'state_dir': '/private/observer',
            'prometheus_url': 'http://192.0.2.20:9090', 'observer_ip': '192.0.2.20',
            'observer_registry_file': '/private/targets.json', 'observer_target_id': 'observer-aws',
            'observer_directory': '/home/railshot-operator/observer', 'node_metrics_port': 30910, 'cluster_metrics_port': 30911}
        self.request = {'version': 1, 'target_id': 'new-aws', 'environment_id': 'env-1', 'app': 'demo-app',
            'namespace': 'tenant-demo', 'node_ip': '192.0.2.10', 'probe_url': 'https://app.example.com/health',
            'registry_file': '/private/targets.json', 'context': 'control'}
        self.descriptor = {'target_id': 'new-aws', 'resource_id': 'instance-1', 'addresses': {'private': '192.0.2.10'}}
        self.health_binding = {'healthz_url': 'https://192.0.2.10:6443/healthz', 'server_name': '192.0.2.10', 'ca_pem': self.ca_pem}
        self.configure_health = Mock(return_value=self.health_binding)
        health = patch.dict(sys.modules, {'runtime_health': SimpleNamespace(configure_runtime_healthz=self.configure_health)})
        health.start()
        self.addCleanup(health.stop)

    def test_optional_traffic_registration_is_app_bound_and_idempotent(self):
        base, _ = registration_row(self.config, self.request, self.descriptor)
        row, _ = registration_row(self.config, {**self.request, 'traffic_port': 30940}, self.descriptor)
        self.assertEqual(row['traffic_instance'], '192.0.2.10:30940')
        rows = merge_rows([base], row)
        self.assertEqual(merge_rows(rows, base), rows)
        self.assertEqual(merge_rows(rows, row), rows)
        job = next(j for j in scrape_config(rows)['scrape_configs'] if j['job_name'] == 'app_traffic')
        self.assertEqual(job['static_configs'], [{'targets': ['192.0.2.10:30940'],
            'labels': {'app': 'demo-app', 'target_id': 'new-aws'}}])
        with self.assertRaises(ValueError):
            merge_rows(rows, {**row, 'traffic_instance': '192.0.2.11:30940'})
        for port in (True, 9400, 65536):
            with self.assertRaises(ValueError):
                registration_row(self.config, {**self.request, 'traffic_port': port}, self.descriptor)

    def test_application_binding_preserves_runtime_and_other_apps(self):
        base, _ = registration_row(self.config, self.request, self.descriptor)
        row, _ = registration_row(self.config, {**self.request, 'application_id': 'app-one',
            'traffic_port': 30940}, self.descriptor)
        node = {key: value for key, value in base.items() if key not in ('app', 'namespace', 'probe_url')}
        other = {**base, 'app': 'other-app', 'namespace': 'other-namespace'}
        rows = merge_rows([node, base, other], row)
        self.assertEqual(rows[0], node)
        self.assertEqual(rows[2], other)
        self.assertEqual(merge_rows(rows, base), rows)
        self.assertEqual(merge_rows(rows, row), rows)
        self.assertEqual(row['target_id'], 'new-aws')
        job = next(j for j in scrape_config(rows)['scrape_configs'] if j['job_name'] == 'app_traffic')
        self.assertEqual(job['static_configs'][0]['labels'], {'app': 'demo-app', 'target_id': 'app-one'})
        with self.assertRaises(ValueError):
            merge_rows(rows, {**row, 'application_id': 'another-app'})
        for invalid in (True, '', 'invalid/id'):
            with self.assertRaises(ValueError):
                registration_row(self.config, {**self.request, 'application_id': invalid}, self.descriptor)
        node_request = {key: value for key, value in self.request.items() if key not in ('app', 'namespace', 'probe_url')}
        with self.assertRaises(ValueError):
            registration_row(self.config, {**node_request, 'application_id': 'app-one'}, self.descriptor)

    def test_identity_conflicts_and_duplicate_registration_preserve_shared_rows(self):
        settings(self.config)
        row, _ = registration_row(self.config, self.request, self.descriptor)
        rows = merge_rows([], row)
        self.assertEqual(merge_rows(rows, row), rows)
        for changed in ({'environment_id': 'other'}, {'resource_id': 'instance-2'}, {'node_instance': '192.0.2.99:30910'}):
            with self.assertRaises(ValueError):
                merge_rows(rows, {**row, **changed})
        upgraded = merge_rows(rows, {**row, 'healthz_url': self.health_binding['healthz_url']})
        self.assertEqual(merge_rows(upgraded, row), upgraded)
        with self.assertRaises(ValueError):
            merge_rows(upgraded, {**row, 'healthz_url': 'https://192.0.2.99:6443/healthz'})
        with self.assertRaises(ValueError):
            registration_row(self.config, {**self.request, 'node_ip': '192.0.2.11'}, self.descriptor)
        with self.assertRaises(ValueError):
            registration_row(self.config, {**self.request, 'probe_url': 'http://a:b@internal/secret'}, self.descriptor)
        other = {**row, 'target_id': 'next-aws', 'node_instance': '192.0.2.11:30910', 'cluster_instance': '192.0.2.11:30911'}
        result = scrape_config(merge_rows(rows, other))
        self.assertEqual(result['scrape_configs'][0]['static_configs'][0]['targets'], ['192.0.2.10:30910', '192.0.2.11:30910'])
        self.assertEqual(len(rows), 1)


    def test_direct_observer_ssh_keeps_identity_and_default_transport(self):
        request = {'inventory': {'control_plane': [{'private_ipv4': self.config['observer_ip'],
            'ssh': {'transport_ref': 'ssm:ap-northeast-2:i-12345678'}}]}}
        references = []
        @contextmanager
        def forward(reference, deadline):
            self.assertGreater(deadline - registration.time.monotonic(), 350)
            references.append(reference)
            yield 2222 if reference else None
        def inventory(value, port):
            return {'all': {'children': {'k3s_server': {'hosts': {'observer': {
                'ansible_ssh_common_args': '-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/private/known_hosts',
                'ansible_ssh_private_key_file': '/private/observer_key', 'ansible_port': port or 22,
                'ansible_user': 'railshot-operator',
                'ansible_host': '127.0.0.1' if port else value['inventory']['control_plane'][0]['private_ipv4']}}}}}}
        ansible = SimpleNamespace(forwarded_port=forward, build_inventory=inventory)
        with registration.observer_ssh(self.config, request, ansible) as prefix:
            self.assertEqual(prefix[-1], 'railshot-operator@127.0.0.1')
        direct = {**self.config, 'observer_transport': 'direct'}
        settings(direct)
        with registration.observer_ssh(direct, request, ansible) as prefix:
            self.assertEqual(prefix[-1], 'railshot-operator@192.0.2.20')
            self.assertIn('HostKeyAlias=192.0.2.20', prefix)
            self.assertIn('StrictHostKeyChecking=yes', prefix)
            self.assertIn('/private/observer_key', prefix)
        self.assertEqual(references, ['ssm:ap-northeast-2:i-12345678', None])
        self.assertEqual(request['inventory']['control_plane'][0]['ssh']['transport_ref'], references[0])
        request['inventory']['control_plane'][0]['ssh']['connect_host'] = '192.0.2.99'
        with self.assertRaises(ValueError):
            with registration.observer_ssh(direct, request, ansible):
                self.fail('unregistered observer address accepted')
        with self.assertRaises(ValueError):
            settings({**direct, 'observer_transport': 'arbitrary'})

    def test_public_gcp_metrics_use_only_provisioned_address_and_explicit_nat_source(self):
        descriptor = {**self.descriptor, 'provider_kind': 'gcp', 'management_endpoint': 'https://34.47.68.21:6443',
            'addresses': {**self.descriptor['addresses'], 'public': '34.47.68.21', 'metrics': '34.47.68.21'}}
        config = {**self.config, 'observer_source_cidr': '52.78.97.236/32'}
        settings(config)
        row, rendered = registration_row(config, self.request, descriptor)
        self.assertEqual(row['node_instance'], '34.47.68.21:30910')
        self.assertEqual(row['cluster_instance'], '34.47.68.21:30911')
        self.assertEqual(row['node_ip'], self.request['node_ip'])
        self.assertEqual(rendered['observer_source_cidr'], '52.78.97.236/32')
        self.assertEqual(row['prometheus_url'], self.config['prometheus_url'])
        for changed in ({'metrics': '8.8.8.8'}, {'public': '8.8.8.8'}, {'public': '224.0.0.1', 'metrics': '224.0.0.1'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                registration_row(config, self.request, {**descriptor, 'addresses': {**descriptor['addresses'], **changed}})
        with self.assertRaises(ValueError):
            registration_row(config, self.request, {**descriptor, 'management_endpoint': 'https://34.47.68.21:443'})
        private = copy.deepcopy(descriptor); del private['addresses']['metrics']; private.pop('management_endpoint')
        default, rendering = registration_row(self.config, self.request, private)
        self.assertEqual(default['node_instance'], self.request['node_ip'] + ':30910')
        self.assertEqual(rendering['observer_source_cidr'], self.config['observer_ip'] + '/32')

    def test_observer_source_override_is_one_rfc1918_or_global_nonmulticast_ipv4(self):
        for value in ('52.78.97.236/32', '172.31.0.172/32', '10.1.2.3/32', '192.168.1.2/32'):
            self.assertEqual(settings({**self.config, 'observer_source_cidr': value})['observer_source_cidr'], value)
        for value in ('0.0.0.0/0', '52.78.97.0/24', '224.0.0.1/32', '239.1.2.3/32', '127.0.0.1/32',
                      '169.254.169.254/32', '192.0.2.20/32', '::1/128', '52.78.97.236'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings({**self.config, 'observer_source_cidr': value})

    def test_bootstrap_is_empty_and_idempotent_without_rotating_password_or_promoting_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'observer'
            config = {**self.config, 'observer_directory': str(directory), 'lifecycle': 'acceptance'}
            payload = {'config': config, 'files': {name: base64.b64encode((HERE / name).read_bytes()).decode()
                for name in ('render.py', 'compose.yaml')}}
            def prepare():
                return subprocess.run([sys.executable, '-c', PREPARE], input=json.dumps(payload), text=True, capture_output=True)
            self.assertEqual(prepare().returncode, 0)
            password = (directory / 'secrets/grafana_password').read_bytes()
            self.assertEqual(json.loads((directory / 'prometheus.json').read_text())['scrape_configs'], [])
            self.assertTrue((directory / 'runtime-ca').is_dir())
            self.assertIn('192.0.2.20:9090:9090', (directory / 'compose.yaml').read_text())
            self.assertIn('127.0.0.1:3000:3000', (directory / 'compose.yaml').read_text())
            self.assertEqual(prepare().returncode, 0)
            self.assertEqual((directory / 'secrets/grafana_password').read_bytes(), password)
            config['lifecycle'] = 'shared'
            self.assertNotEqual(prepare().returncode, 0)
            self.assertEqual((directory / 'secrets/grafana_password').read_bytes(), password)

    def test_failed_sync_preserves_desired_binding_and_conflicting_retry_never_mutates(self):
        with tempfile.TemporaryDirectory() as root:
            root = str(Path(root).resolve())
            config = {**self.config, 'state_dir': root}
            output = Path(root) / 'receipt.json'
            writes = []
            def kube(namespace, *args, document=None):
                if args[:2] == ('get', 'pods'):
                    return {'items': [{'metadata': {'name': 'cilium'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}]}
                if args[:2] == ('get', 'services'):
                    return {'items': []}
                if args[0] == 'apply':
                    writes.append(document)
                return None
            @contextmanager
            def runtime(_):
                yield kube
            environment = SimpleNamespace(read_private=lambda path: json.loads(Path(path).read_text()), runtime_kubectl=runtime)
            with patch.dict(sys.modules, {'environment': environment, 'argo': SimpleNamespace(native=None)}), patch.object(registration, 'node_request', return_value=({}, self.descriptor)), patch.object(registration, 'sync_observer', side_effect=RuntimeError('offline')), patch.object(registration, 'wait_network_policy'):
                with self.assertRaises(RuntimeError):
                    registration.register(config, self.request, output)
                self.assertFalse((Path(root) / 'product.json').exists())
                self.assertEqual(json.loads(output.read_text())['status'], 'unknown')
                self.assertEqual(json.loads((Path(root) / 'desired.json').read_text())['targets'][0]['environment_id'], 'env-1')
                before = len(writes)
                with self.assertRaises(ValueError):
                    registration.register(config, {**self.request, 'environment_id': 'env-2'}, output)
                self.assertEqual(len(writes), before)
            with patch.dict(sys.modules, {'environment': environment, 'argo': SimpleNamespace(native=None)}), patch.object(registration, 'node_request', return_value=({}, self.descriptor)), patch.object(registration, 'sync_observer'), patch.object(registration, 'wait_network_policy'):
                self.assertTrue(registration.register(config, self.request, output)['registered'])
                published = json.loads((Path(root) / 'product.json').read_text())['targets']
                node = next(row for row in published if not row.get('app'))
                self.assertEqual(node['healthz_url'], self.health_binding['healthz_url'])
                self.assertEqual(node['environment_id'], 'env-1')
                # A new app on the same runtime keeps the original node metadata.
                registration.register(config, {**self.request, 'app': 'another-app', 'environment_id': 'env-2'}, output)
                published = json.loads((Path(root) / 'product.json').read_text())['targets']
                self.assertEqual(next(row for row in published if not row.get('app')), node)
                self.assertEqual(len(published), 3)
                self.assertEqual(scrape_config([node])['scrape_configs'][-1]['job_name'], 'runtime_healthz')

    def test_node_only_registration_preserves_node_and_cluster_without_a_fake_probe(self):
        request = {key: value for key, value in self.request.items() if key not in {'app', 'namespace', 'probe_url', 'context'}}
        row, rendering = registration_row(self.config, request, self.descriptor)
        self.assertFalse({'app', 'namespace', 'probe_url'} & row.keys())
        self.assertEqual(rendering['probe_urls'], [])
        jobs = scrape_config([row])['scrape_configs']
        self.assertEqual([job['job_name'] for job in jobs], ['node', 'cluster'])
        app_row, _ = registration_row(self.config, self.request, self.descriptor)
        for first, second in [(row, app_row), (app_row, row)]:
            merged = merge_rows(merge_rows([], first), second)
            self.assertEqual(merged, [first, second])
            self.assertEqual(merge_rows(merged, second), merged)
            for key, value in [('resource_id', 'different'), ('prometheus_url', 'http://192.0.2.99:9090'),
                               ('node_instance', '192.0.2.99:30910'), ('cluster_instance', '192.0.2.99:30911'),
                               ('node_ip', '192.0.2.99')]:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    merge_rows([first], {**second, key: value})
        self.assertEqual(scrape_config([row, app_row])['scrape_configs'][-1]['static_configs'],
                         [{'targets': [self.request['probe_url']]}])
        with self.assertRaises(ValueError):
            registration_row(self.config, {**request, 'probe_url': self.request['probe_url']}, self.descriptor)


    def test_gcp_uses_verified_public_route_and_actual_observer_source(self):
        config = {**self.config, 'observer_source_cidr': '8.8.8.8/32'}
        settings(config)
        descriptor = {**self.descriptor, 'provider_kind': 'gcp', 'management_endpoint': 'https://8.8.4.4:6443',
            'addresses': {**self.descriptor['addresses'], 'public': '8.8.4.4', 'metrics': '8.8.4.4'}}
        row, rendering = registration_row(config, self.request, descriptor)
        self.assertEqual(row['node_instance'], '8.8.4.4:30910')
        self.assertEqual(row['node_ip'], self.request['node_ip'])
        self.assertEqual(rendering['observer_source_cidr'], '8.8.8.8/32')
        for source in ['0.0.0.0/0', '198.51.100.0/24', '127.0.0.1/32']:
            with self.assertRaises(ValueError):
                settings({**config, 'observer_source_cidr': source})
    def test_node_health_upgrade_survives_failed_sync_without_publishing_trust(self):
        request = {k: v for k, v in self.request.items() if k not in {'app', 'namespace', 'probe_url'}}
        row, _ = registration_row(self.config, request, self.descriptor)
        binding = {'healthz_url': 'https://192.0.2.10:6443/healthz', 'server_name': '192.0.2.10', 'ca_pem': self.ca_pem}
        @contextmanager
        def runtime(_):
            def kube(namespace, *args, **kw):
                if args[:2] == ('get', 'pods'):
                    return {'items': [{'metadata': {'name': 'cilium'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}]}
                return {'items': []} if args[:2] == ('get', 'services') else None
            with patch.object(registration, 'wait_network_policy'):
                yield kube
        with tempfile.TemporaryDirectory() as root:
            state = Path(root).resolve()
            config = {**self.config, 'state_dir': str(state)}
            product = state / 'product.json'
            product.write_text(json.dumps({'version': 1, 'collector': registration.collector(config), 'targets': [row]}))
            before = product.read_bytes()
            environment = SimpleNamespace(read_private=lambda p: json.loads(Path(p).read_text()), runtime_kubectl=runtime)
            with patch.dict(sys.modules, {'environment': environment, 'argo': SimpleNamespace(native=None),
                    'runtime_health': SimpleNamespace(configure_runtime_healthz=lambda *args: binding)}), \
                    patch.object(registration, 'node_request', return_value=({}, self.descriptor)), \
                    patch.object(registration, 'sync_observer', side_effect=RuntimeError('offline')) as sync:
                with self.assertRaises(RuntimeError):
                    registration.register(config, request, state / 'receipt.json')
                self.assertEqual(product.read_bytes(), before)
                pending = json.loads((state / 'desired.json').read_text())['targets'][0]
                self.assertEqual(pending['healthz_url'], binding['healthz_url'])
                self.assertEqual(sync.call_args.kwargs['bindings'], {'new-aws': binding})
                sync.side_effect = None
                registration.register(config, request, state / 'receipt.json')
                self.assertEqual(json.loads(product.read_text())['targets'], [pending])
                for name in ('product.json', 'desired.json', 'receipt.json'):
                    self.assertNotIn('ca_pem', (state / name).read_text())
                    self.assertNotIn('server_name', (state / name).read_text())
                self.assertEqual((state / 'runtime-healthz.json').stat().st_mode & 0o777, 0o600)
                # A later app registration retains the node health binding and trust module.
                registration.register(config, self.request, state / 'receipt.json')
                self.assertEqual(sync.call_args.kwargs['bindings'], {'new-aws': binding})
                jobs = sync.call_args.args[2]['scrape_configs']
                health = next(job for job in jobs if job['job_name'] == 'runtime_healthz')
                self.assertEqual(health['static_configs'], [{'targets': [binding['healthz_url']],
                    'labels': {'module': 'runtime_healthz_new-aws'}}])
                self.assertIn({'source_labels': ['module'], 'target_label': '__param_module'}, health['relabel_configs'])
                self.assertIn({'source_labels': ['__param_target'], 'target_label': 'instance'}, health['relabel_configs'])
                self.assertEqual(next(job for job in jobs if job['job_name'] == 'http')['params'], {'module': ['http_2xx']})
                with patch.dict(sys.modules, {'runtime_health': SimpleNamespace(configure_runtime_healthz=lambda *a: self.fail('conflict reached mutation'))}):
                    with self.assertRaises(ValueError):
                        registration.register(config, {**request, 'environment_id': 'different'}, state / 'receipt.json')

    def test_runtime_trust_modules_reject_credentials_and_keep_strict_tls(self):
        binding = {'healthz_url': 'https://192.0.2.10:6443/healthz', 'server_name': '192.0.2.10', 'ca_pem': self.ca_pem}
        modules = registration.blackbox({'new-aws': binding})['modules']
        self.assertEqual(modules['http_2xx'], registration.blackbox()['modules']['http_2xx'])
        self.assertEqual(modules['runtime_healthz_new-aws']['http']['tls_config'], {
            'ca_file': '/etc/blackbox/runtime-ca/new-aws.crt', 'server_name': '192.0.2.10', 'insecure_skip_verify': False})
        for change in ({'token': 'private'}, {'ca_pem': self.ca_pem + '\n-----BEGIN PRIVATE KEY-----\nabc'},
                       {'healthz_url': 'https://token:secret@192.0.2.10:6443/healthz'}, {'server_name': '127.0.0.1'}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                registration.blackbox({'new-aws': {**binding, **change}})

    def test_runtime_sync_explicitly_allows_the_full_remote_transaction(self):
        native = Mock(return_value='{"synced":true}')
        with patch.object(registration, 'observer_ssh') as ssh:
            ssh.return_value.__enter__.return_value = ['ssh', 'observer']
            registration.sync_observer(self.config, {}, {'scrape_configs': []}, None, native,
                                       bindings={'new-aws': self.health_binding})
        self.assertEqual(native.call_args.kwargs['timeout'], 300)
        self.assertEqual(native.call_args.kwargs['document']['certificates'], {'new-aws': self.ca_pem})

    def test_runtime_sync_rolls_back_files_and_rejects_operator_compose_changes(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            compose = (HERE / 'compose.yaml').read_text()
            old = compose.replace('    volumes:\n      - ./blackbox.json:/etc/blackbox/blackbox.json:ro\n'
                '      - ./runtime-ca:/etc/blackbox/runtime-ca:ro', '    volumes: ["./blackbox.json:/etc/blackbox/blackbox.json:ro"]')
            custom = registration.blackbox()
            custom['modules']['http_2xx']['timeout'] = '4s'
            custom['modules']['operator_probe'] = {'prober': 'tcp', 'timeout': '2s'}
            before = {'compose.yaml': old, 'prometheus.json': '{"old":true}', 'blackbox.json': json.dumps(custom)}
            for name, value in before.items():
                (directory / name).write_text(value)
            (directory / 'owner').write_text('shared-observer:shared\n')
            binding = {'healthz_url': 'https://192.0.2.10:6443/healthz', 'server_name': '192.0.2.10', 'ca_pem': self.ca_pem}
            payload = {'directory': root, 'owner': 'shared-observer:shared', 'compose': compose, 'previous_compose': old,
                'prometheus': {'scrape_configs': []}, 'blackbox': registration.blackbox({'new-aws': binding}),
                'certificates': {'new-aws': self.ca_pem}}
            calls = []
            def docker(args, **kwargs):
                calls.append(args)
                if '--config.check' in args:
                    raise subprocess.CalledProcessError(1, args)
            def sync():
                with patch('sys.stdin', io.StringIO(json.dumps(payload))), patch('sys.stdout', io.StringIO()):
                    exec(registration.SYNC_RUNTIME, {})
            with patch('subprocess.run', side_effect=docker), self.assertRaises(subprocess.CalledProcessError):
                sync()
            self.assertEqual({name: (directory / name).read_text() for name in before}, before)
            self.assertFalse((directory / 'runtime-ca/new-aws.crt').exists())
            with patch('subprocess.run'):
                sync()
            self.assertEqual((directory / 'runtime-ca/new-aws.crt').read_text(), self.ca_pem)
            observed = json.loads((directory / 'blackbox.json').read_text())['modules']
            self.assertEqual(observed['http_2xx'], custom['modules']['http_2xx'])
            self.assertEqual(observed['operator_probe'], custom['modules']['operator_probe'])
            self.assertEqual(observed['runtime_healthz_new-aws'], payload['blackbox']['modules']['runtime_healthz_new-aws'])
            (directory / 'compose.yaml').write_text(compose + '# operator change\n')
            with patch('subprocess.run') as run, self.assertRaises(AssertionError):
                sync()
            run.assert_not_called()

    def test_network_policy_must_be_imported_and_realized_before_exporters(self):
        for mode in ('cilium_missing', 'stale_policy', 'wait_failed', 'ready'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = str(Path(directory).resolve())
                config = {**self.config, 'state_dir': root}
                writes, events = [], []
                policy = None
                def kube(namespace, *args, document=None):
                    nonlocal policy
                    if args[:2] == ('get', 'pods'):
                        return {'items': [] if mode == 'cilium_missing' else [{'metadata': {'name': 'cilium'},
                            'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}]}
                    if args[:2] == ('get', 'services'):
                        return {'items': []}
                    if args[0] == 'apply':
                        writes.append(document['kind']); events.append(document['kind'])
                        if document['kind'] == 'CiliumClusterwideNetworkPolicy':
                            policy = {**document, 'metadata': {**document['metadata'], 'uid': 'policy-uid'}}
                    if args[:2] == ('get', 'CiliumClusterwideNetworkPolicy'):
                        return policy
                    if args[0] == 'exec':
                        if 'get' in args:
                            labels = [{'source': 'k8s', 'key': 'io.cilium.k8s.policy.uid', 'value': 'policy-uid'},
                                      *policy['spec']['labels']]
                            if mode == 'stale_policy':
                                labels = [*labels[:1], {**labels[1], 'value': 'older-policy'}]
                            return {'revision': 7, 'policy': json.dumps([{'Labels': labels}])}
                        if 'sh' in args:
                            events.append('policy_wait')
                            if mode == 'wait_failed':
                                raise RuntimeError('policy not realized')
                    return None
                @contextmanager
                def runtime(_):
                    yield kube
                environment = SimpleNamespace(read_private=lambda path: json.loads(Path(path).read_text()), runtime_kubectl=runtime)
                with patch.dict(sys.modules, {'environment': environment, 'argo': SimpleNamespace(native=None)}), \
                        patch.object(registration, 'node_request', return_value=({}, self.descriptor)), \
                        patch.object(registration, 'sync_observer'), \
                        patch.object(registration.time, 'monotonic', side_effect=[0, 61]):
                    if mode == 'ready':
                        self.assertTrue(registration.register(config, self.request, Path(root) / 'receipt.json')['registered'])
                        self.assertLess(events.index('policy_wait'), events.index('DaemonSet'))
                    else:
                        with self.assertRaises((RuntimeError, ValueError)):
                            registration.register(config, self.request, Path(root) / 'receipt.json')
                        self.assertEqual(writes, [] if mode == 'cilium_missing' else ['CiliumClusterwideNetworkPolicy'])
                        self.assertFalse((Path(root) / 'product.json').exists())


if __name__ == '__main__':
    unittest.main()
