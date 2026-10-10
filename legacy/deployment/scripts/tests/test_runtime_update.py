"""No-cloud checks for explicit promotion, ownership, backups, and non-replay."""
from contextlib import ExitStack, contextmanager
import base64
import copy
import gzip
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import runtime_upgrade as node

spec = importlib.util.spec_from_file_location('runtime_update', ROOT / 'deployment/scripts/runtime-update.py')
update = importlib.util.module_from_spec(spec); spec.loader.exec_module(update)
POLICY = json.loads((ROOT / 'deployment/airgap/versions.json').read_text())


def release():
    return {'version': 1, 'source_sha': 'a' * 40, 'from_policy': copy.deepcopy(POLICY),
            'to_policy': copy.deepcopy(POLICY), 'from_policy_sha256': node.digest(POLICY), 'to_policy_sha256': node.digest(POLICY)}


def observation(policy=POLICY):
    return {**{k: policy[k] for k in ('runtime', 'cilium_images')}, 'node_uid': 'node-uid', 'node_name': 'node',
            'node_ip': '10.66.0.2', 'architecture': 'amd64', 'helm_revision': 2, 'ready': True}


class RuntimeReleaseTests(unittest.TestCase):
    def test_helm_tags_do_not_change_immutable_image_identity(self):
        state = copy.deepcopy(observation())
        state['cilium_images'] = {key: value.replace('@', ':v1.20.2@')
                                 for key, value in state['cilium_images'].items()}
        self.assertTrue(node.matches(state, POLICY))
        for image in ('quay.io/cilium/cilium:v1.20.2',
                      'quay.io/cilium/other@' + POLICY['cilium_images']['agent'].split('@')[1],
                      'quay.io/cilium/cilium:v1.20.2@sha256:' + '0' * 64,
                      POLICY['cilium_images']['agent'].replace('@', ':bad tag@')):
            changed = copy.deepcopy(state); changed['cilium_images']['agent'] = image
            self.assertFalse(node.matches(changed, POLICY))

    def test_inspection_requires_cilium_ready_even_without_upgrade(self):
        state = observation()
        helm = {'chart': {'metadata': {'version': state['runtime']['cilium_version']}}, 'version': 2}
        secret = {'data': {'release': base64.b64encode(base64.b64encode(gzip.compress(json.dumps(helm).encode()))).decode()}}
        def kube(*args):
            if args[:2] == ('get', 'nodes'):
                return {'items': [{'metadata': {'uid': 'node-uid', 'name': 'node'}, 'status': {
                    'conditions': [{'type': 'Ready', 'status': 'True'}],
                    'nodeInfo': {'kubeletVersion': state['runtime']['k3s_version'], 'architecture': 'amd64'},
                    'addresses': [{'type': 'InternalIP', 'address': '10.66.0.2'}]}}]}
            if 'secret' in args:
                return {'items': [secret]}
            key, name = {'cilium': ('agent', 'cilium-agent'), 'cilium-operator': ('operator', 'cilium-operator'),
                         'cilium-envoy': ('envoy', 'cilium-envoy')}[args[4]]
            return {'spec': {'template': {'spec': {'containers': [{'name': name, 'image': state['cilium_images'][key]}]}}}}
        def command(*args, **kwargs):
            if args == (node.K3S, '--version'):
                return 'k3s version ' + state['runtime']['k3s_version']
            if args[:2] == (node.CILIUM, 'version'):
                return 'cilium-cli: ' + state['runtime']['cilium_cli_version']
            if '--raw=/readyz' in args:
                return 'ok'
            if 'jsonpath={.clusters[0].cluster.certificate-authority-data}' in args:
                return base64.b64encode(b'public-ca').decode()
            return ''
        with patch.object(node, 'kube', side_effect=kube), patch.object(node, 'run', side_effect=command) as run:
            result = node.inspect(include_ca=True)
            self.assertTrue(result['cilium_ready']); self.assertTrue(result['api_ready'])
            run.assert_any_call(node.CILIUM, 'status', '--wait', '--wait-duration', '60s', timeout=75)
            self.assertIn('management_ca_data', result)
            def unhealthy(*args, **kwargs):
                if args[:2] == (node.CILIUM, 'status'):
                    raise ValueError('Cilium is not ready')
                return command(*args, **kwargs)
            run.side_effect = unhealthy
            with self.assertRaisesRegex(ValueError, 'Cilium is not ready'):
                node.inspect()

    def test_pins_explicit_forward_patch_and_source_policy(self):
        item = release(); self.assertFalse(node.validate(item, POLICY))
        item['from_policy']['runtime']['k3s_version'] = 'v1.34.10+k3s1'
        item['from_policy_sha256'] = node.digest(item['from_policy'])
        with self.assertRaisesRegex(ValueError, 'acknowledgement'):
            node.validate(item)
        item['upgrade'] = {'recovery_ack': True, 'k3s_binary_sha256': 'b' * 64}
        self.assertTrue(node.validate(item, POLICY))
        item['from_policy']['runtime']['k3s_version'] = 'v1.33.11+k3s1'
        item['from_policy_sha256'] = node.digest(item['from_policy'])
        with self.assertRaisesRegex(ValueError, 'forward patches'):
            node.validate(item)
        item = release(); item['to_policy']['runtime']['cilium_version'] = '1.20.99'
        with self.assertRaisesRegex(ValueError, 'digest'):
            node.validate(item)
        with self.assertRaisesRegex(ValueError, 'checked-out'):
            node.validate(release(), {})

    def test_same_policy_rechecks_guard_without_installing_or_backup(self):
        guard = Mock(); guard.check_host.return_value = ('10.42.0.0/16', '10.43.0.0/16')
        expected = {'node_uid': 'node-uid', 'node_ip': '10.66.0.2'}
        with patch.object(node, 'inspect', return_value=observation()), patch.object(node, 'load_guard', return_value=guard), patch.object(node, 'run') as mutate:
            result = node.apply(release(), expected, ROOT / 'deployment')
            self.assertEqual(result['status'], 'verified'); self.assertFalse(result['changed'])
            mutate.assert_not_called()
            guard.check_cluster.assert_called_once()
            with self.assertRaisesRegex(ValueError, 'binding'):
                node.apply(release(), {**expected, 'node_uid': 'foreign'}, ROOT / 'deployment')

    def test_failed_patch_keeps_sqlite_token_binary_backup_and_never_replays(self):
        item = release(); item['from_policy']['runtime']['k3s_version'] = 'v1.34.10+k3s1'
        item['from_policy_sha256'] = node.digest(item['from_policy'])
        item['upgrade'] = {'recovery_ack': True, 'k3s_binary_sha256': 'b' * 64}
        observed = observation(item['from_policy'])
        expected = {'node_uid': 'node-uid', 'node_ip': '10.66.0.2', 'target_id': 'gcp', 'resource_id': 'vm'}
        guard = Mock(); guard.check_host.return_value = ('10.42.0.0/16', '10.43.0.0/16')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); base = root / 'recovery'
            identity = {**expected, 'k3s_version': observed['runtime']['k3s_version'], 'cilium_version': observed['runtime']['cilium_version']}
            files = {'etc/railshot/runtime-identity.json': json.dumps(identity), 'etc/rancher/k3s/config.yaml': 'managed',
                     'var/lib/rancher/k3s/server/db/state.db': 'sqlite', 'var/lib/rancher/k3s/server/token': 'secret-token',
                     'usr/local/bin/k3s': 'old-binary', 'usr/local/lib/railshot-deployment/cilium': 'old-cli'}
            for name, text in files.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
            def command(*args, **kwargs):
                if args[:2] == (node.CILIUM, 'upgrade'):
                    raise ValueError('simulated failure')
                return ''
            helm = {'items': [{'metadata': {'labels': {'status': 'deployed'}}, 'data': {'release':
                base64.b64encode(base64.b64encode(gzip.compress(b'{"config":{}}'))).decode()}}]}
            with patch.object(node, 'inspect', return_value=observed), patch.object(node, 'load_guard', return_value=guard), \
                    patch.object(node, 'kube', return_value=helm), patch.object(node, 'run', side_effect=command) as commands, \
                    patch.object(node, 'download', side_effect=lambda url, path, checksum: path.write_text('new-binary')):
                with self.assertRaisesRegex(ValueError, 'simulated'):
                    node.apply(item, expected, ROOT / 'deployment', base, root)
                home = base / item['source_sha']
                self.assertEqual(json.loads((home / 'receipt.json').read_text())['status'], 'recovery_required')
                with tarfile.open(home / 'k3s-backup.tar') as archive:
                    self.assertEqual(archive.extractfile('var/lib/rancher/k3s/server/token').read(), b'secret-token')
                    self.assertEqual(archive.extractfile('usr/local/bin/k3s').read(), b'old-binary')
                count = commands.call_count
                with self.assertRaisesRegex(ValueError, 'operator recovery'):
                    node.apply(item, expected, ROOT / 'deployment', base, root)
                self.assertEqual(commands.call_count, count)
                self.assertEqual(json.loads((root / 'etc/railshot/runtime-identity.json').read_text()), identity)

    def test_unavailable_canonical_registrar_blocks_before_any_runtime_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            args = SimpleNamespace(state_dir=home / 'release', registration_dir=home / 'registration', target_id='aws')
            config = {'observability_config_file': '/private/observer'}
            observer = SimpleNamespace(product=Mock(side_effect=ValueError('canonical registrar unavailable')))
            with patch.object(update, 'load', return_value=(release(), {}, {'target': {'provider': 'aws'}}, {}, config, None, None, {})), \
                    patch.object(update.env, 'read_private', return_value={}), \
                    patch.object(update.importlib.util, 'module_from_spec', return_value=observer), \
                    patch.object(update.importlib.util, 'spec_from_file_location', return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda x: None))), \
                    patch.object(update, 'connection') as connection, patch.object(update, 'stage_source') as stage:
                result = update.execute(args)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['stage'], 'observer_preflight')
                self.assertFalse(result['mutation_started'])
                connection.assert_not_called(); stage.assert_not_called()
                observer.product.assert_called_once_with({}, require_api=True)
                self.assertFalse((args.registration_dir / 'runtime-release.json').exists())

    def test_policy_release_reapplies_existing_registration_and_failure_cannot_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / 'release'
            args = SimpleNamespace(state_dir=home, registration_dir=home.parent / 'registration', target_id='gcp', registry='/private/registry')
            record = {'input_sha256': 'c' * 64, 'environment_id': 'environment'}
            request = {'target': {'provider': 'gcp'}}
            registered = {'app': 'fixture', 'target': {'namespace': 'tenant'},
                          'public_http': {'url': 'https://app.example/health', 'expected_json': {'ready': True}}}
            cd = {'context': 'control', 'targets': {'gcp': registered}}
            identity = {'descriptor': {'addresses': {'private': '10.66.0.2'}, 'resource_id': 'vm'}}
            settings = {'observability_config_file': '/private/observer'}
            plan = release()
            plan['from_policy']['runtime']['k3s_version'] = 'v1.34.10+k3s1'
            plan['from_policy_sha256'] = node.digest(plan['from_policy'])
            plan['upgrade'] = {'recovery_ack': True, 'k3s_binary_sha256': 'b' * 64}
            live = observation(plan['from_policy'])
            def node_call(prefix, payload):
                nonlocal live
                if payload['action'] == 'apply':
                    live = observation(plan['to_policy'])
                    return {'status': 'verified'}
                return copy.deepcopy(live)
            @contextmanager
            def connection(_):
                yield []
            @contextmanager
            def kubectl(_):
                yield lambda *a, **kw: {'metadata': {'labels': {'railshot.io/registration': 'c' * 32}}}
            observer = SimpleNamespace(product=Mock(return_value={}), register=Mock(return_value={'registered': True, 'status': 'succeeded'}))
            with patch.object(update, 'load', return_value=(plan, record, request, cd, settings, {}, None, identity)), \
                    patch.object(update, 'connection', connection), patch.object(update, 'stage_source', return_value='/private/source'), \
                    patch.object(update, 'node_call', side_effect=node_call), \
                    patch.object(update.env, 'runtime_kubectl', kubectl), patch.object(update.env, 'runtime_documents', return_value=[{'kind': 'Role'}]), \
                    patch.object(update.env, 'owned_apply') as apply, patch.object(update.env, 'read_private', side_effect=lambda p: json.loads(Path(p).read_text()) if Path(p).exists() else {}), \
                    patch.object(update.importlib.util, 'module_from_spec', return_value=observer), \
                    patch.object(update.importlib.util, 'spec_from_file_location', return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda x: None))), \
                    patch.object(update, 'app_health', return_value={'ready': True}), patch.object(update, 'observer_health', return_value={'exporters_ready': True}), \
                    patch.object(update, 'collection_health', return_value={'collection_state': 'ready'}), \
                    patch.object(update, 'management_health', return_value={'read_verified': True}), \
                    patch.object(update.env.bridge, 'public_probe', return_value={'state': 'succeeded'}):
                result = update.execute(args)
                self.assertEqual(result['status'], 'verified'); apply.assert_called_once()
                self.assertEqual((result['source_sha'], result['provider'], result['target_id']), ('a' * 40, 'gcp', 'gcp'))
                snapshot = {str(p): p.read_bytes() for p in home.parent.rglob('*') if p.is_file()}
                with patch.object(update.env, 'save', side_effect=AssertionError('read-only verification must not write')):
                    proof = update.verify(args)
                    self.assertTrue(proof['verify_only']); self.assertIn('checked_at', proof)
                    drift = copy.deepcopy(observation()); drift['runtime']['k3s_version'] = 'v1.34.10+k3s1'
                    with patch.object(update, 'node_call', return_value=drift):
                        with self.assertRaisesRegex(ValueError, 'drifted'):
                            update.verify(args)
                    with patch.object(update.env.bridge, 'public_probe', return_value={'state': 'unverified'}):
                        with self.assertRaisesRegex(ValueError, 'HTTPS health drifted'):
                            update.verify(args)
                    with patch.object(update, 'collection_health', side_effect=ValueError('collection drifted')):
                        with self.assertRaisesRegex(ValueError, 'collection drifted'):
                            update.verify(args)
                self.assertEqual(snapshot, {str(p): p.read_bytes() for p in home.parent.rglob('*') if p.is_file()})
                apply.assert_called_once(); observer.register.assert_called_once()
                self.assertTrue(update.execute(args)['replayed']); apply.assert_called_once()
                marker = Path(args.registration_dir) / 'runtime-release.json'
                marker.write_text(json.dumps({'status': 'running', 'source_sha': 'd' * 40, 'receipt': '/another/release'}))
                with self.assertRaisesRegex(ValueError, 'stale success'):
                    update.execute(args)
                apply.assert_called_once()
                saved = json.loads((home / 'receipt.json').read_text()); saved['status'] = 'recovery_required'
                (home / 'receipt.json').write_text(json.dumps(saved))
                with self.assertRaisesRegex(ValueError, 'automatic retry forbidden'):
                    update.execute(args)
                apply.assert_called_once()

    def test_actual_observer_samples_require_all_scopes_fresh_and_finite(self):
        now = 1000
        values = {'node_up': 1, 'cluster_up': 1, 'http_up': 1, 'http': 1,
                  'cpu_percent': 20, 'memory_percent': 40, 'pods': 1,
                  **{key: now - 10 for key in ('node_observed', 'cluster_observed', 'http_observed',
                                              'cpu_observed', 'memory_observed', 'pods_observed', 'probe_observed')}}
        def response(source):
            return {'status': 'success', 'data': {'resultType': 'vector', 'result': [
                {'metric': {'railshot_metric': key}, 'value': [now, str(value)]} for key, value in source.items()]}}
        self.assertEqual(update.collection_values(response(values), now)['collection_state'], 'ready')
        for changed in ({'node_up': 0}, {'cluster_observed': 800}, {'cpu_percent': 'NaN'}, {'http': 0}, {'pods': 0}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                update.collection_values(response({**values, **changed}), now)
        missing = {key: value for key, value in values.items() if key != 'memory_percent'}
        with self.assertRaises(ValueError):
            update.collection_values(response(missing), now)

    def test_node_observation_has_no_application_or_http_requirement(self):
        row = {'target_id': 'gcp', 'node_instance': '10.66.0.2:31490', 'cluster_instance': '10.66.0.2:31491'}
        query = update.collection_query(row)
        self.assertNotIn('probe_success', query); self.assertNotIn('kube_pod_status_phase', query)
        with self.assertRaisesRegex(ValueError, 'partial'):
            update.collection_query({**row, 'app': 'incomplete'})
        now = 1000
        values = {'node_up': 1, 'cluster_up': 1, 'cpu_percent': 20, 'memory_percent': 40,
                  **{key: now - 10 for key in ('node_observed', 'cluster_observed', 'cpu_observed', 'memory_observed')}}
        def response(source):
            return {'status': 'success', 'data': {'resultType': 'vector', 'result': [
                {'metric': {'railshot_metric': key}, 'value': [now, str(value)]} for key, value in source.items()]}}
        result = update.collection_values(response(values), now, application=False)
        self.assertEqual(result['collection_state'], 'ready')
        self.assertNotIn('running_app_pods', result); self.assertNotIn('http_probe_success', result)
        for drift in ({'node_up': 0}, {'cluster_up': 0}, {'cpu_observed': 800}, {'memory_percent': 'NaN'}):
            with self.subTest(drift=drift), self.assertRaises(ValueError):
                update.collection_values(response({**values, **drift}), now, application=False)

    def test_node_collection_requires_exact_target_resource_and_environment(self):
        config = {'owner': 'collector', 'lifecycle': 'shared', 'expires_at': 'future', 'state_dir': '/private/observer',
                  'prometheus_url': 'http://172.31.0.172:9090', 'node_metrics_port': 31490, 'cluster_metrics_port': 31491}
        row = {'target_id': 'gcp', 'environment_id': 'node-registration', 'resource_id': 'vm',
               'node_instance': '10.66.0.2:31490', 'cluster_instance': '10.66.0.2:31491', 'prometheus_url': config['prometheus_url']}
        product = {'version': 1, 'collector': {k: config[k] for k in ('lifecycle', 'expires_at')}, 'targets': [row]}
        product['collector']['id'] = config['owner']
        registration = {'version': 2, 'scope': 'node-only', 'target_id': 'gcp', 'environment_id': 'node-registration'}
        identity = {'descriptor': {'resource_id': 'vm', 'addresses': {'private': '10.66.0.2'}}}
        observer = SimpleNamespace(settings=lambda value: value, metrics_host=lambda descriptor, ip: ip,
                                   product=Mock(side_effect=lambda config, **kw: product))
        response = Mock(status=200); response.read.return_value = b'{}'
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=False)
        with patch.object(update.importlib.util, 'module_from_spec', return_value=observer), \
                patch.object(update.importlib.util, 'spec_from_file_location', return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda x: None))), \
                patch.object(update.env, 'read_private', side_effect=lambda p: product if str(p).endswith('product.json') else config), \
                patch.object(update, 'build_opener') as opener, patch.object(update, 'collection_values', return_value={'collection_state': 'ready'}) as values:
            opener.return_value.open.return_value = response
            settings = {'observability_config_file': '/private/config'}
            self.assertEqual(update.collection_health(settings, registration, None, identity, wait_seconds=0)['collection_state'], 'ready')
            self.assertIs(values.call_args.kwargs['application'], False)
            for key in ('target_id', 'resource_id', 'environment_id', 'node_instance', 'prometheus_url'):
                original = row[key]; row[key] = 'different'
                with self.subTest(key=key), self.assertRaises(ValueError):
                    update.collection_health(settings, registration, None, identity, wait_seconds=0)
                row[key] = original
            for key in ('app', 'namespace', 'probe_url'):
                row[key] = 'partial'
                with self.subTest(partial=key), self.assertRaises(ValueError):
                    update.collection_health(settings, registration, None, identity, wait_seconds=0)
                del row[key]


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.state = self.home / 'failed'; self.state.mkdir(mode=0o700)
        self.registration_home = self.home / 'registration'; self.registration_home.mkdir(mode=0o700)
        (self.registration_home / 'runtime-release.lock').touch(mode=0o600)
        self.args = SimpleNamespace(state_dir=self.state, registration_dir=self.registration_home,
                                    target_id='gcp', registry='/private/registry')
        self.plan = release()
        self.registration = {'version': 2, 'scope': 'node-only', 'node_uid': 'node-uid',
                             'target_id': 'gcp', 'environment_id': 'registered-node',
                             'baseline_policy_sha256': self.plan['from_policy_sha256']}
        self.identity = {'descriptor': {'resource_id': 'registered-vm', 'addresses': {'private': '10.66.0.2'}}}
        observer_config = self.home / 'observer.json'; update.env.save(observer_config, {})
        self.loaded = (self.plan, self.registration, {'target': {'provider': 'gcp'}}, None,
                       {'observability_config_file': str(observer_config)}, None, None, self.identity)
        self.path = self.state / 'receipt.json'
        self.marker = self.registration_home / 'runtime-release.json'
        self.ack = self.state / 'reconciliation.json'
        self.failed = {'version': 1, 'status': 'recovery_required', 'scope': 'node-only', 'stage': 'observability',
                       'source_sha': self.plan['source_sha'], 'target_id': 'gcp', 'provider': 'gcp',
                       'input_sha256': node.digest({'release': self.plan, 'registration': self.registration, 'identity': self.identity}),
                       'from_policy_sha256': self.plan['from_policy_sha256'], 'to_policy_sha256': self.plan['to_policy_sha256'],
                       'runtime': {'status': 'verified', 'changed': False, 'before': observation(), 'after': observation()}}
        update.env.save(self.path, self.failed)
        # Preserve byte formatting as well as the parsed failure, including its missing final after field.
        self.path.write_text(json.dumps(self.failed, indent=2) + '\n')
        update.env.save(self.marker, {'status': 'running', 'source_sha': self.plan['source_sha'], 'receipt': str(self.path)})
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.load = self.stack.enter_context(patch.object(update, 'load', return_value=self.loaded))
        @contextmanager
        def connection(_): yield []
        @contextmanager
        def kube(_): yield Mock(side_effect=AssertionError('unexpected native write'))
        self.stack.enter_context(patch.object(update, 'connection', connection))
        self.stack.enter_context(patch.object(update.env, 'runtime_kubectl', kube))
        def node_call(prefix, payload):
            self.assertEqual(payload, {'action': 'inspect', 'include_ca': True})
            return observation()
        self.node_call = self.stack.enter_context(patch.object(update, 'node_call', side_effect=node_call))
        self.stack.enter_context(patch.object(update, 'node_management_health', return_value={'tls_verified': True}))
        self.stack.enter_context(patch.object(update, 'observer_health', return_value={'exporters_ready': True}))
        self.collection = self.stack.enter_context(patch.object(update, 'collection_health', return_value={'collection_state': 'ready'}))
        self.stage = self.stack.enter_context(patch.object(update, 'stage_source', side_effect=AssertionError('reconciliation staged source')))
        self.apply = self.stack.enter_context(patch.object(update.env, 'owned_apply', side_effect=AssertionError('reconciliation applied resources')))
        self.observer = SimpleNamespace(product=Mock(return_value={}), register=Mock(side_effect=AssertionError('reconciliation registered observer')))
        self.stack.enter_context(patch.object(update.importlib.util, 'module_from_spec', return_value=self.observer))
        self.stack.enter_context(patch.object(update.importlib.util, 'spec_from_file_location',
            return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda _: None))))

    def snapshot(self):
        return {str(path): path.read_bytes() for path in self.home.rglob('*') if path.is_file()}

    def test_preserves_failure_and_repeated_acknowledgement_is_live_but_write_free(self):
        original = self.path.read_bytes()
        proof = update.reconcile(self.args)
        self.assertEqual(proof['status'], 'reconciled')
        self.assertEqual(proof['failed_receipt_sha256'], update.hashlib.sha256(original).hexdigest())
        self.assertEqual(proof['input_sha256'], self.failed['input_sha256'])
        self.assertEqual(proof['to_policy'], POLICY)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(json.loads(self.marker.read_bytes()), {'status': 'reconciled', 'source_sha': self.plan['source_sha'],
                                                              'receipt': str(self.path), 'reconciliation': str(self.ack)})
        snapshot = self.snapshot(); inspections = self.node_call.call_count
        with patch.object(update.env, 'save', side_effect=AssertionError('replay wrote evidence')):
            self.assertEqual(update.reconcile(self.args)['status'], 'reconciled')
        self.assertGreater(self.node_call.call_count, inspections)
        self.assertEqual(self.snapshot(), snapshot)
        self.stage.assert_not_called(); self.apply.assert_not_called(); self.observer.register.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'automatic retry forbidden'): update.execute(self.args)
        with self.assertRaisesRegex(ValueError, 'verified release/target binding'): update.verify(self.args)
        self.assertEqual(self.snapshot(), snapshot)

    def test_foreign_uid_policy_registration_and_stale_collection_cannot_acknowledge(self):
        original = copy.deepcopy(self.loaded)
        for drift in ('uid', 'policy', 'registration', 'collector'):
            with self.subTest(drift=drift):
                self.load.return_value = copy.deepcopy(original)
                self.node_call.side_effect = None; self.node_call.return_value = observation()
                self.collection.side_effect = None
                if drift == 'uid': self.node_call.return_value['node_uid'] = 'replacement-node'
                elif drift == 'policy': self.node_call.return_value['runtime'] = {**POLICY['runtime'], 'k3s_version': 'v1.34.0+k3s1'}
                elif drift == 'registration': self.load.return_value[1]['environment_id'] = 'another-registration'
                else: self.collection.side_effect = ValueError('observer samples missing, stale, or unhealthy')
                snapshot = self.snapshot()
                with self.assertRaises(ValueError): update.reconcile(self.args)
                self.assertEqual(self.snapshot(), snapshot)
        self.stage.assert_not_called(); self.observer.register.assert_not_called()

    def test_changed_runtime_or_early_failure_is_never_reconciled(self):
        for field, value in (('stage', 'runtime_apply'), ('status', 'blocked'), ('scope', 'application'),
                             ('runtime', {**self.failed['runtime'], 'changed': True}),
                             ('runtime', {**self.failed['runtime'], 'after': {**observation(), 'node_uid': 'foreign'}})):
            with self.subTest(field=field, value=value):
                update.env.save(self.path, {**self.failed, field: value})
                snapshot = self.snapshot()
                with self.assertRaisesRegex(ValueError, 'no-op observation failure'): update.reconcile(self.args)
                self.assertEqual(self.snapshot(), snapshot)
        self.node_call.assert_not_called()

    def test_receipt_or_marker_change_during_health_readback_prevents_ack(self):
        for path in (self.path, self.marker):
            with self.subTest(path=path):
                original = path.read_bytes()
                def race(*_args, **_kwargs):
                    path.write_bytes(original + b' ')
                    return {'collection_state': 'ready'}
                self.collection.side_effect = race
                with self.assertRaisesRegex(ValueError, 'inputs changed during readback'): update.reconcile(self.args)
                self.assertFalse(self.ack.exists())
                path.write_bytes(original)

    def test_ack_write_then_marker_failure_resumes_without_rewriting_ack(self):
        save = update.env.save
        def interrupted(path, value):
            if Path(path) == self.marker: raise OSError('lost marker write')
            save(path, value)
        with patch.object(update.env, 'save', side_effect=interrupted):
            with self.assertRaises(OSError): update.reconcile(self.args)
        ack = self.ack.read_bytes(); original = self.path.read_bytes()
        with patch.object(update.env, 'save', wraps=save) as writes:
            self.assertEqual(update.reconcile(self.args)['status'], 'reconciled')
        self.assertEqual([Path(call.args[0]) for call in writes.call_args_list], [self.marker])
        self.assertEqual(self.ack.read_bytes(), ack); self.assertEqual(self.path.read_bytes(), original)

    def test_new_source_uses_reconciled_baseline_and_rejects_tampered_history(self):
        update.reconcile(self.args)
        original = self.path.read_bytes()
        future = copy.deepcopy(self.loaded); future[0]['source_sha'] = 'b' * 40
        self.load.return_value = future
        future_args = SimpleNamespace(**{**vars(self.args), 'state_dir': self.home / 'next'})
        self.path.write_bytes(original + b' ')
        with self.assertRaisesRegex(ValueError, 'evidence binding differs'): update.execute(future_args)
        self.path.write_bytes(original)
        self.stage.side_effect = None; self.stage.return_value = '/private/staged-source'
        self.observer.register.side_effect = None
        self.observer.register.return_value = {'registered': True, 'status': 'succeeded'}
        self.node_call.side_effect = lambda _, payload: ({'status': 'verified', 'changed': False} if payload['action'] == 'apply' else observation())
        result = update.execute(future_args)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['source_sha'], 'b' * 40)
        self.assertEqual(json.loads(self.marker.read_bytes())['source_sha'], 'b' * 40)
        self.assertEqual(self.path.read_bytes(), original)
        self.observer.register.assert_called_once()


if __name__ == '__main__':
    unittest.main()
