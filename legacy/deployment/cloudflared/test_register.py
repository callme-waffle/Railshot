"""No-network checks for binding, additive ingress and recovery after a partial patch."""
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location('tunnel_registration', Path(__file__).with_name('register.py'))
registration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(registration)


class Kube:
    def __init__(self, objects):
        self.objects = objects
        self.calls, self.patches = [], []
        self.fail_deployment = False
        self.ready = True

    def __call__(self, namespace, *args, document=None):
        self.calls.append(args)
        value = self.objects[args[1].lower()]
        assert namespace == value['metadata']['namespace'] and args[2] == value['metadata']['name']
        if args[0] == 'get':
            result = copy.deepcopy(value)
            if args[1].lower() == 'deployment' and self.ready:
                result['status'] = {'observedGeneration': result['metadata']['generation'],
                                    'replicas': 1, 'updatedReplicas': 1, 'readyReplicas': 1, 'availableReplicas': 1}
            return result
        assert args[0] == 'patch' and args[3:] == ('--type=json', '--patch-file=/dev/stdin', '-o', 'json')
        assert document[:2] == [{'op': 'test', 'path': '/metadata/' + key, 'value': value['metadata'][key]}
                               for key in ('uid', 'resourceVersion')]
        if args[1] == 'Deployment' and self.fail_deployment:
            self.fail_deployment = False
            raise RuntimeError('synthetic-sensitive-remote-diagnostic')
        updated = copy.deepcopy(value)
        for operation in document[2:]:
            assert operation['op'] == 'replace'
            parts = [part.replace('~1', '/').replace('~0', '~') for part in operation['path'].split('/')[1:]]
            parent = updated
            for part in parts[:-1]:
                parent = parent[part]
            assert parts[-1] in parent
            parent[parts[-1]] = operation['value']
        updated['metadata']['resourceVersion'] = str(int(value['metadata']['resourceVersion']) + 1)
        if args[1] == 'Deployment':
            updated['metadata']['generation'] += 1
        self.objects[args[1].lower()] = updated
        self.patches.append((args[1], copy.deepcopy(document)))
        return copy.deepcopy(updated)


class RegisterTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = {'version': 1, 'state_dir': str(self.root / 'state'), 'registry_file': str(self.root / 'registry.json'),
                       'environment_id': 'onprem-runtime', 'resource_id': 'server-1', 'runtime_private_address': '10.0.0.17',
                       'base_domain': 'railshot.io', 'namespace': 'railshot-edge', 'name': 'railshot-tunnel',
                       'tunnel_id': '11111111-2222-4333-8444-555555555555', 'credentials_secret': 'tunnel-credentials',
                       'origin_vip': '10.0.0.51', 'ca_configmap': 'origin-ca',
                       'configmap_uid': '22222222-2222-4333-8444-555555555555',
                       'deployment_uid': '33333333-2222-4333-8444-555555555555'}
        self.request = self.request_for('calculator')
        self.old = self.request_for('old-app')
        self.write('key', 'synthetic-key', raw=True)
        self.write('known_hosts', '10.0.0.17 ssh-ed25519 synthetic', raw=True)
        self.write('server.json', {'id': 'server-1', 'project_id': 'project-1', 'status': 'ACTIVE',
                                  'addresses': [{'network': 'management', 'address': '10.0.0.17', 'version': 4}]})
        self.registry = {'version': 1, 'targets': {'onprem-runtime': {
            'server_file': str(self.root / 'server.json'), 'resource_id': 'server-1', 'project_id': 'project-1',
            'management_network': 'management', 'placement': 'onprem-a', 'architecture': 'amd64',
            'initialization': 'cloud-init', 'purpose': 'runtime',
            'ssh': {'user': 'ubuntu', 'identity_file': str(self.root / 'key'), 'known_hosts_file': str(self.root / 'known_hosts'),
                    'connect_host': '172.31.0.172', 'port': 10022}}}}
        self.write('registry.json', self.registry)
        self.config_path = self.write('config.json', self.config)
        self.seed({self.old['hostname']: self.old['application_id']})
        self.native = None

        @contextmanager
        def connection(native):
            self.native = native
            yield self.kube
        self.enterContext(patch.object(registration.runtime, 'runtime_kubectl', connection))

    def write(self, name, value, raw=False):
        target = self.root / name
        target.write_text(value if raw else json.dumps(value))
        target.chmod(0o600)
        return target

    def request_for(self, app):
        env, tenant = self.config['environment_id'], 'demo'
        app_id = 'app-' + hashlib.sha256(json.dumps([env, tenant, app], separators=(',', ':')).encode()).hexdigest()[:24]
        return {'environment_id': env, 'application_id': app_id, 'app': app, 'tenant': tenant,
                'hostname': registration.service_name(app, tenant, env, self.config['base_domain'])['hostname']}

    def seed(self, owners):
        cm, deployment = registration.rendered(self.config, list(owners))
        cm['metadata'].update(uid=self.config['configmap_uid'], resourceVersion='1',
                              annotations={registration.OWNERS: json.dumps(owners), 'operator-note': 'keep'})
        deployment['metadata'].update(uid=self.config['deployment_uid'], resourceVersion='1', generation=1)
        deployment['spec']['template']['metadata']['annotations']['operator-note'] = 'keep'
        deployment['spec']['template']['spec']['dnsPolicy'] = 'ClusterFirst'  # API-defaulted field.
        self.kube = Kube({'configmap': cm, 'deployment': deployment})

    def ensure(self):
        return registration.ensure(self.config_path, self.request)

    def test_lifecycle_last_route_removal_preserves_tunnel_then_explicit_restore(self):
        self.seed({self.request['hostname']: self.request['application_id']})
        original = copy.deepcopy(self.kube.objects)
        snapshot = registration.lifecycle_plan(self.config_path, self.request, 'stop')
        registration.lifecycle_validate(self.config_path, self.request, 'stop', snapshot)
        self.assertFalse(self.kube.patches)
        receipt = registration.lifecycle_execute(self.config_path, self.request, 'stop', snapshot)
        self.assertFalse(receipt['route_present'])
        cm = self.kube.objects['configmap']
        self.assertEqual(json.loads(cm['data']['config.json'])['ingress'], [{'service': 'http_status:404'}])
        self.assertEqual(cm['metadata']['uid'], original['configmap']['metadata']['uid'])
        self.assertEqual(self.kube.objects['deployment']['spec']['template']['spec'], original['deployment']['spec']['template']['spec'])
        snapshot = registration.lifecycle_plan(self.config_path, self.request, 'start')
        receipt = registration.lifecycle_execute(self.config_path, self.request, 'start', snapshot)
        self.assertTrue(receipt['route_present'])

    def test_lifecycle_rejects_foreign_owner_or_stale_uid_before_patch(self):
        self.seed({self.request['hostname']: self.request['application_id']})
        snapshot = registration.lifecycle_plan(self.config_path, self.request, 'delete')
        self.kube.objects['configmap']['metadata']['uid'] = 'changed-uid'
        with self.assertRaises(registration.RegistrationError):
            registration.lifecycle_execute(self.config_path, self.request, 'delete', snapshot)
        self.assertFalse(self.kube.patches)
        self.seed({self.request['hostname']: self.old['application_id']})
        with self.assertRaises(registration.RegistrationError):
            registration.lifecycle_plan(self.config_path, self.request, 'delete')
        self.assertFalse(self.kube.patches)

    def test_add_preserves_existing_host_tls_and_uses_bound_runtime(self):
        before = copy.deepcopy(self.kube.objects)
        result = self.ensure()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['phase'], 'tunnel_configured')
        self.assertFalse(result['https_verified'])
        self.assertEqual(result['dns'], {'type': 'CNAME', 'content': self.config['tunnel_id'] + '.cfargotunnel.com', 'proxied': True})
        cm, deployment = self.kube.objects['configmap'], self.kube.objects['deployment']
        owners = json.loads(cm['metadata']['annotations'][registration.OWNERS])
        self.assertEqual(owners, {row['hostname']: row['application_id'] for row in (self.old, self.request)})
        ingress = json.loads(cm['data']['config.json'])['ingress']
        self.assertEqual(ingress[-1], {'service': 'http_status:404'})
        for rule in ingress[:-1]:
            self.assertEqual(rule['service'], 'https://10.0.0.51:443')
            self.assertEqual(rule['originRequest'], {'originServerName': rule['hostname'], 'httpHostHeader': rule['hostname'],
                                                   'noTLSVerify': False, 'caPool': '/etc/cloudflared/ca/ca.pem'})
        self.assertEqual(deployment['spec']['template']['spec'], before['deployment']['spec']['template']['spec'])
        self.assertEqual(cm['metadata']['annotations']['operator-note'], 'keep')
        self.assertEqual([op['path'] for _, ops in self.kube.patches for op in ops[2:]],
                         ['/data/config.json', '/metadata/annotations/railshot.io~1application-owners',
                          '/spec/template/metadata/annotations/railshot.io~1config-sha256'])
        node = self.native['inventory']['control_plane'][0]
        self.assertEqual((node['resource_id'], node['private_ipv4'], node['ssh']['connect_host']),
                         ('server-1', '10.0.0.17', '172.31.0.172'))
        self.assertEqual(self.ensure()['status'], 'succeeded')
        self.assertEqual(len(self.kube.patches), 2)  # Already configured is read-only remotely.
        saved = registration.runtime.read_private(self.root / 'state' / (self.request['application_id'] + '.json'))
        self.assertEqual(saved, result)

    def test_ownership_uid_tls_and_deployment_drift_block_before_any_patch(self):
        initial = copy.deepcopy(self.kube.objects)
        for case in ('owner', 'cm-uid', 'deployment-uid', 'tls', 'sni', 'vip', 'credential', 'extra-host'):
            with self.subTest(case=case):
                self.kube = Kube(copy.deepcopy(initial))
                cm, deployment = self.kube.objects['configmap'], self.kube.objects['deployment']
                if case == 'owner':
                    self.seed({self.old['hostname']: self.old['application_id'], self.request['hostname']: 'app-' + 'a' * 24})
                elif case.endswith('-uid'):
                    (cm if case == 'cm-uid' else deployment)['metadata']['uid'] = 'unexpected-uid'
                elif case == 'credential':
                    deployment['spec']['template']['spec']['volumes'][1]['secret']['secretName'] = 'other-secret'
                else:
                    config = json.loads(cm['data']['config.json'])
                    rule = config['ingress'][0]
                    if case == 'tls': rule['originRequest']['noTLSVerify'] = True
                    if case == 'sni': rule['originRequest']['originServerName'] = 'other.railshot.io'
                    if case == 'vip': rule['service'] = 'https://10.0.0.99:443'
                    if case == 'extra-host': config['ingress'].insert(0, {**rule, 'hostname': 'unowned.railshot.io'})
                    cm['data']['config.json'] = json.dumps(config)
                result = self.ensure()
                self.assertEqual(result['status'], 'blocked', result)
                self.assertEqual(self.kube.patches, [])

    def test_api_omitted_false_host_namespaces_are_safe_but_true_or_other_missing_fields_are_not(self):
        pod = self.kube.objects['deployment']['spec']['template']['spec']
        for key in ('hostNetwork', 'hostPID', 'hostIPC'):
            del pod[key]
        initial = copy.deepcopy(self.kube.objects)
        self.assertEqual(self.ensure()['status'], 'succeeded')
        self.assertEqual(self.ensure()['status'], 'succeeded')
        self.assertEqual(len(self.kube.patches), 2)
        for field in ('hostNetwork', 'hostPID', 'hostIPC', 'automountServiceAccountToken',
                      'allowPrivilegeEscalation', 'runAsNonRoot'):
            with self.subTest(field=field):
                self.kube = Kube(copy.deepcopy(initial))
                pod = self.kube.objects['deployment']['spec']['template']['spec']
                if field.startswith('host'):
                    pod[field] = True
                elif field == 'automountServiceAccountToken':
                    del pod[field]
                elif field == 'allowPrivilegeEscalation':
                    del pod['containers'][0]['securityContext'][field]
                else:
                    del pod['securityContext'][field]
                self.assertEqual(self.ensure()['status'], 'blocked')
                self.assertEqual(self.kube.patches, [])

    def test_partial_configmap_patch_retries_without_losing_hosts(self):
        old_hash = self.kube.objects['deployment']['spec']['template']['metadata']['annotations'][registration.HASH]
        self.kube.fail_deployment = True
        result = self.ensure()
        self.assertEqual(result['status'], 'unknown', result)
        self.assertTrue(result['error']['outcome_unknown'])
        self.assertNotIn('synthetic-sensitive', json.dumps(result))
        self.assertEqual([kind for kind, _ in self.kube.patches], ['ConfigMap'])
        self.assertEqual(self.kube.objects['deployment']['spec']['template']['metadata']['annotations'][registration.HASH], old_hash)
        self.assertEqual(self.ensure()['status'], 'succeeded')
        self.assertEqual([kind for kind, _ in self.kube.patches], ['ConfigMap', 'Deployment'])
        cm, deployment = self.kube.objects['configmap'], self.kube.objects['deployment']
        self.assertEqual(set(json.loads(cm['metadata']['annotations'][registration.OWNERS])), {self.old['hostname'], self.request['hostname']})
        self.assertEqual(deployment['spec']['template']['metadata']['annotations'][registration.HASH],
                         hashlib.sha256(cm['data']['config.json'].encode()).hexdigest())

    def test_invalid_request_or_runtime_binding_never_connects(self):
        request = copy.deepcopy(self.request)
        for key, value in [('hostname', 'calculator.railshot.io'), ('application_id', 'app-' + '0' * 24),
                           ('environment_id', 'other-runtime'), ('app', 'bad/app'), ('tenant', 'other')]:
            with self.subTest(key=key):
                self.request = {**request, key: value}
                self.assertEqual(self.ensure()['status'], 'blocked')
                self.assertIsNone(self.native)
        self.request = request
        for key, value in [('resource_id', 'other-server'), ('runtime_private_address', '10.0.0.99')]:
            with self.subTest(key=key):
                self.write('config.json', {**self.config, key: value})
                self.assertEqual(self.ensure()['status'], 'blocked')
                self.assertIsNone(self.native)

    def test_lock_must_be_private_owned_regular_and_not_already_held(self):
        state = registration.runtime.bridge.private_directory(self.root / 'state')
        lock = state / 'ingress.lock'
        lock.symlink_to(self.root / 'key')
        self.assertEqual(self.ensure()['status'], 'blocked')
        lock.unlink()
        lock.touch(mode=0o644)
        self.assertEqual(self.ensure()['error']['code'], 'TUNNEL_LOCK_INVALID')
        lock.chmod(0o600)
        with lock.open() as held:
            registration.fcntl.flock(held, registration.fcntl.LOCK_EX | registration.fcntl.LOCK_NB)
            self.assertEqual(self.ensure()['error']['code'], 'TUNNEL_BUSY')
        self.assertEqual(self.kube.calls, [])

    def test_rollout_wait_is_bounded_and_not_a_success_receipt(self):
        self.kube.ready = False
        # The old revision remains ready while the new template has not been observed.
        self.kube.objects['deployment']['status'] = {'observedGeneration': 1, 'replicas': 1,
                                                     'updatedReplicas': 1, 'readyReplicas': 1, 'availableReplicas': 1}
        clock = [0]
        with patch.object(registration.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(registration.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            result = self.ensure()
        self.assertEqual(result['status'], 'unknown', result)
        self.assertEqual(result['error']['code'], 'TUNNEL_ROLLOUT_UNVERIFIED')
        self.assertLessEqual(clock[0], 120)
        self.assertFalse((self.root / 'state' / (self.request['application_id'] + '.json')).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
