"""No-cloud checks for explicit, UID-bound metadata-only legacy adoption."""
import base64
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
spec = importlib.util.spec_from_file_location('environment_adopt', ROOT / 'deployment/scripts/environment-adopt.py')
adopt = importlib.util.module_from_spec(spec); spec.loader.exec_module(adopt)


class Kube:
    def __init__(self):
        self.objects = {}; self.patches = []; self.fail_after = None

    def add(self, document):
        value = copy.deepcopy(document); meta = value['metadata']
        meta.update(uid=f'00000000-0000-0000-0000-{len(self.objects) + 1:012d}', resourceVersion='1', generation=1)
        self.objects[meta.get('namespace', 'default'), value['kind'].lower(), meta['name']] = value
        return value

    def __call__(self, namespace, *args, document=None):
        if args[:2] == ('config', 'view'):
            return {'clusters': [{'cluster': {'certificate-authority-data': base64.b64encode(b'fixture-ca').decode()}}]}
        key = namespace, args[1].lower(), args[2]
        obj = self.objects.get(key)
        if obj is None:
            if '--ignore-not-found' in args:
                return None
            raise ValueError('fixture resource is absent')
        if args[0] == 'get':
            return copy.deepcopy(obj)
        if args[0] != 'patch':
            raise AssertionError('adoption may only get or patch: ' + str(args))
        if self.fail_after is not None and len(self.patches) >= self.fail_after:
            raise OSError('simulated uncertain patch')
        for operation in document:
            field = operation['path'].split('/')[-1]
            if operation['op'] == 'test':
                if obj['metadata'].get(field) != operation['value']:
                    raise ValueError('CAS conflict')
            else:
                assert operation['op'] == 'add' and operation['path'] == '/metadata/labels'
                obj['metadata']['labels'] = copy.deepcopy(operation['value'])
        obj['metadata']['resourceVersion'] = str(int(obj['metadata']['resourceVersion']) + 1)
        self.patches.append(copy.deepcopy(document))
        return copy.deepcopy(obj)


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.root.chmod(0o700)
        self.home = self.root / 'legacy'; self.shared = self.root / 'claims'; self.target_id = 'fixture-gcp'
        self.refs = {'registry': str(self.root / 'registry.json'), 'target_id': self.target_id,
            'config': str(self.root / 'config.json'), 'registration_dir': str(self.home),
            'policy_file': str(self.root / 'policy.json'), 'binding': None}
        self.policy = json.loads((ROOT / 'deployment/airgap/versions.json').read_text())
        self.request = {'target': {'provider': 'gcp'}}
        target = {'id': self.target_id, 'namespace': 'tenant', 'project': 'tenant-project', 'cluster_server': 'https://34.47.68.21:6443',
            'repo_url': 'https://github.com/owner/config', 'path': 'gitops/applications/fixture/' + self.target_id,
            'node_port': 30085, 'image_pull_secret': {'name': 'ghcr-pull', 'namespace': 'tenant'}}
        self.registered = {'app': 'fixture', 'target': target, 'public_http': {'url': 'https://app.example/health', 'expected_json': {'ready': True}}}
        self.cd = {'context': 'control', 'targets': {self.target_id: self.registered}}
        self.settings = {'state_dir': str(self.shared), 'observability_config_file': str(self.root / 'observer.json')}
        self.identity = {'descriptor': {'target_id': self.target_id, 'provider_kind': 'gcp', 'resource_id': 'instance-123',
            'addresses': {'private': '10.66.0.2', 'public': '34.47.68.21', 'metrics': '34.47.68.21'},
            'management_endpoint': target['cluster_server']}, 'environment_id': self.home.name}
        self.pull = {'auths': {'ghcr.io': {'auth': base64.b64encode(b'fixture:secret').decode()}}}
        self.observer = {'owner': 'fixture-observer', 'observer_ip': '10.66.0.3', 'prometheus_url': 'http://10.66.0.3:9090',
            'node_metrics_port': 31091, 'cluster_metrics_port': 31092, 'state_dir': str(self.root / 'observer-state')}
        self.loaded = ((self.request, self.cd, self.settings, self.pull, None, self.identity), self.observer, self.policy)
        self.node = {'runtime': self.policy['runtime'], 'cilium_images': self.policy['cilium_images'],
            'node_uid': 'node-uid', 'node_ip': '10.66.0.2', 'architecture': 'amd64', 'ready': True, 'helm_revision': 1}
        self.runtime = Kube(); self.control = Kube()
        for document in adopt.env.runtime_documents(target, 'unused', self.pull, None):
            document['metadata']['labels'] = {'legacy': 'preserved'}
            self.runtime.add(document)
        self.runtime.add({'kind': 'Deployment', 'metadata': {'namespace': 'tenant', 'name': 'fixture'},
            'spec': {'replicas': 1, 'template': {'metadata': {'labels': {'railshot.io/target': self.target_id}},
                'spec': {'containers': [{'image': 'fixture@sha256:' + 'a' * 64}]}}},
            'status': {'observedGeneration': 1, 'updatedReplicas': 1, 'availableReplicas': 1}})
        self.runtime.add({'kind': 'Service', 'metadata': {'namespace': 'tenant', 'name': 'fixture'},
            'spec': {'selector': {'railshot.io/target': self.target_id}, 'ports': [{'nodePort': 30085}]}})
        for document in adopt.observer.cluster({'node_ip': '10.66.0.2', 'observer_source_cidr': '10.66.0.3/32',
                'node_metrics_port': 31091, 'cluster_metrics_port': 31092})['items']:
            if document['kind'] == 'Deployment':
                document['status'] = {'observedGeneration': 1, 'availableReplicas': 1}
            if document['kind'] == 'DaemonSet':
                document['status'] = {'observedGeneration': 1, 'desiredNumberScheduled': 1, 'numberReady': 1}
            self.runtime.add(document)
        sa = self.runtime.objects['tenant', 'serviceaccount', adopt.env.SA]
        self.renewal = {'target_id': self.target_id, 'secret': 'railshot-' + self.target_id,
            'server': target['cluster_server'], 'project': target['project'], 'namespaces': ['tenant'],
            'service_account': {'name': adopt.env.SA, 'namespace': 'tenant', 'uid': sa['metadata']['uid']},
            'ca_sha256': hashlib.sha256(b'fixture-ca').hexdigest(), 'audiences': ['api'], 'tls_server_name': '10.66.0.2'}
        claims = {'sub': 'system:serviceaccount:tenant:' + adopt.env.SA, 'aud': ['api'], 'exp': int(time.time()) + 21600,
            'iat': int(time.time()), 'kubernetes.io': {'namespace': 'tenant', 'serviceaccount': {'name': adopt.env.SA, 'uid': sa['metadata']['uid']}}}
        token = 'e30.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.signature'
        credential = {'bearerToken': token, 'tlsClientConfig': {'insecure': False,
            'caData': base64.b64encode(b'fixture-ca').decode(), 'serverName': '10.66.0.2'}}
        self.control.add({'kind': 'ConfigMap', 'metadata': {'namespace': 'argocd', 'name': 'railshot-credentials'},
            'data': {'policy.json': json.dumps({'version': 1, 'targets': [self.renewal]})}})
        self.control.add({'kind': 'Secret', 'metadata': {'namespace': 'argocd', 'name': self.renewal['secret']},
            'data': {k: base64.b64encode(v.encode()).decode() for k, v in {'name': self.target_id, 'server': target['cluster_server'],
            'project': target['project'], 'clusterResources': 'false', 'namespaces': 'tenant', 'config': json.dumps(credential)}.items()}})
        self.control.add({'kind': 'CronJob', 'metadata': {'namespace': 'argocd', 'name': 'railshot-credentials'},
            'spec': {'jobTemplate': {'spec': {'template': {'spec': {'serviceAccountName': 'railshot-credentials',
            'containers': [{'command': ['python3', '/app/gitops/credentials.py', 'renew']}]}}}}}})
        self.control.add({'kind': 'Role', 'metadata': {'namespace': 'argocd', 'name': 'railshot-credentials'},
            'rules': [{'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['get', 'patch'], 'resourceNames': [self.renewal['secret']]}]})
        self.control.add({'kind': 'AppProject', 'metadata': {'namespace': 'argocd', 'name': target['project']},
            'spec': {'destinations': [{'server': target['cluster_server'], 'namespace': 'tenant'}], 'sourceRepos': [target['repo_url']], 'clusterResourceWhitelist': []}})
        self.control.add({'kind': 'Application', 'metadata': {'namespace': 'argocd', 'name': adopt.env.application_name(self.target_id, 'tenant', 'fixture')},
            'spec': {'project': target['project'], 'source': {'repoURL': target['repo_url'], 'path': target['path']},
                'destination': {'server': target['cluster_server'], 'namespace': 'tenant'}},
            'status': {'sync': {'status': 'Synced'}, 'health': {'status': 'Healthy'}}})
        @contextmanager
        def connection(_):
            yield []
        @contextmanager
        def kubectl(_):
            yield self.runtime
        self.enterContext(patch.object(adopt, 'load', return_value=self.loaded))
        self.enterContext(patch.object(adopt.update, 'connection', connection))
        self.node_call = self.enterContext(patch.object(adopt.update, 'node_call', side_effect=lambda prefix, payload:
            copy.deepcopy(self.node) if payload == {'action': 'inspect'} else self.fail('adoption called mutation')))
        self.enterContext(patch.object(adopt.env, 'runtime_kubectl', kubectl))
        self.enterContext(patch.object(adopt.env.argo, 'kubectl', lambda context, *a, **kw: self.control(*a, **kw)))
        self.customer = self.enterContext(patch.object(adopt.env.credentials, 'customer', return_value={'kind': 'PodList', 'items': []}))
        self.enterContext(patch.object(adopt.env.bridge, 'public_probe', return_value={'state': 'succeeded'}))
        self.plan_file = self.root / 'adoption.json'

    def make_plan(self):
        return adopt.plan(self.refs, self.plan_file)['plan_sha256']

    def shared_renewal(self, **changes):
        row = {**self.renewal, 'project': '', 'namespaces': ['tenant', 'app-' + 'a' * 24], **changes}
        self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'] = json.dumps(
            {'version': 1, 'targets': [row]})
        secret = self.control.objects['argocd', 'secret', self.renewal['secret']]
        for field in ('project', 'namespaces'):
            value = ','.join(row[field]) if field == 'namespaces' else row[field]
            secret['data'][field] = base64.b64encode(value.encode()).decode()
        return row

    def test_shared_cluster_adoption_preserves_two_legacy_namespaces_new_app_and_original_credential(self):
        self.shared_renewal(namespaces=['tenant', 'tenant-jihwan-atlas', 'app-' + 'a' * 24])
        originals = copy.deepcopy(self.control.objects)
        digest = self.make_plan()
        self.assertEqual(adopt.apply(self.plan_file, digest)['status'], 'verified')
        for kind, name in (('configmap', 'railshot-credentials'), ('secret', self.renewal['secret'])):
            key = 'argocd', kind, name
            self.assertEqual(self.control.objects[key]['data'], originals[key]['data'])
            self.assertEqual(self.control.objects[key]['metadata']['uid'], originals[key]['metadata']['uid'])
        self.assertTrue(all(len(call.args) == 4 for call in self.customer.call_args_list))

    def test_pending_shared_cluster_transition_cannot_be_adopted(self):
        renewal = self.shared_renewal(previous_scope={'project': self.renewal['project'], 'namespaces': ['tenant']})
        adopt.env.credentials.validate_policy({'version': 1, 'targets': [renewal]})
        with self.assertRaisesRegex(ValueError, 'transition requires reconciliation'):
            self.make_plan()
        self.assertFalse(self.plan_file.exists())
        self.assertFalse(self.runtime.patches + self.control.patches)
        self.customer.assert_not_called()

    def test_shared_cluster_adoption_rejects_scope_and_credential_identity_drift(self):
        for changes in (
                {'project': 'foreign-project'}, {'project': self.renewal['project']},
                {'namespaces': ['tenant', 'kube-system']}, {'namespaces': ['app-' + 'a' * 24]},
                {'server': 'https://34.47.68.22:6443'}, {'secret': 'railshot-other'},
                {'ca_sha256': 'b' * 64}, {'audiences': ['other-api']}, {'tls_server_name': '10.66.0.9'},
                {'service_account': {**self.renewal['service_account'], 'uid': '00000000-0000-0000-0000-999999999999'}}):
            with self.subTest(changes=changes):
                self.shared_renewal(**changes)
                with self.assertRaises(ValueError):
                    self.make_plan()
                self.assertFalse(self.plan_file.exists())
                self.assertFalse(self.runtime.patches + self.control.patches)

    def test_plan_and_apply_only_add_exact_labels_preserve_specs_and_credentials(self):
        originals = [copy.deepcopy(k.objects) for k in (self.runtime, self.control)]
        digest = self.make_plan()
        self.assertFalse(self.home.exists()); self.assertFalse(self.shared.exists())
        self.assertFalse(self.runtime.patches + self.control.patches)
        private = adopt.env.read_private(self.plan_file)
        self.assertTrue(all('original_labels' in row for row in private['snapshot']['objects']))
        result = adopt.apply(self.plan_file, digest)
        self.assertEqual(result['status'], 'verified')
        record = adopt.env.read_private(self.home / 'registration.json')
        self.assertEqual(record['status'], 'succeeded')
        self.assertEqual(record['input_sha256'], adopt.env.argo.document_hash(self.identity))
        self.assertEqual(record['cd_sha256'], hashlib.sha256((self.home / 'cd.json').read_bytes()).hexdigest())
        for kube, original in zip((self.runtime, self.control), originals):
            for key, value in kube.objects.items():
                self.assertEqual(adopt.content_hash(value), adopt.content_hash(original[key]))
            for patch_document in kube.patches:
                self.assertEqual([item['path'] for item in patch_document[:2]], ['/metadata/uid', '/metadata/resourceVersion'])
                self.assertTrue(all(item['op'] == 'test' or item['path'] == '/metadata/labels' for item in patch_document))
        self.assertTrue(all(len(call.args) == 4 for call in self.customer.call_args_list))  # No TokenRequest or rotation POST.
        with self.assertRaisesRegex(ValueError, 'operator recovery'):
            adopt.apply(self.plan_file, digest)

    def test_recreated_uid_rv_spec_and_plan_tampering_fail_before_any_patch(self):
        digest = self.make_plan(); obj = self.runtime.objects['default', 'namespace', 'tenant']; original = copy.deepcopy(obj)
        for field, value in [('uid', 'recreated'), ('resourceVersion', '2'), ('annotations', {'changed': 'yes'})]:
            with self.subTest(field=field):
                obj['metadata'][field] = value
                with self.assertRaisesRegex(ValueError, 'changed'):
                    adopt.apply(self.plan_file, digest)
                obj.clear(); obj.update(copy.deepcopy(original))
                self.assertFalse(self.runtime.patches + self.control.patches)
                self.assertFalse((self.home / 'registration.json').exists())
        with self.assertRaisesRegex(ValueError, 'digest'):
            adopt.apply(self.plan_file, '0' * 64)
        with patch.object(adopt.time, 'time', return_value=time.time() + 901):
            with self.assertRaisesRegex(ValueError, 'expired'):
                adopt.apply(self.plan_file, digest)

    def test_foreign_manager_owner_and_management_uid_cannot_be_adopted(self):
        for key in [('default', 'namespace', 'tenant'), ('default', 'namespace', 'railshot-observability')]:
            obj = self.runtime.objects[key]; original = copy.deepcopy(obj)
            for patch_meta in ({'labels': {'app.kubernetes.io/managed-by': 'another-manager'}}, {'ownerReferences': [{'uid': 'another'}]}):
                with self.subTest(key=key, patch_meta=patch_meta):
                    obj['metadata'].update(patch_meta)
                    with self.assertRaises(ValueError):
                        self.make_plan()
                    self.assertFalse(self.plan_file.exists())
                    obj.clear(); obj.update(copy.deepcopy(original))
        self.runtime.objects['tenant', 'serviceaccount', adopt.env.SA]['metadata']['uid'] = '00000000-0000-0000-0000-999999999999'
        with self.assertRaisesRegex(ValueError, 'ServiceAccount UID'):
            self.make_plan()
        self.assertFalse(self.runtime.patches + self.control.patches)

    def test_partial_unknown_patch_retains_rollback_plan_and_never_replays(self):
        digest = self.make_plan(); self.runtime.fail_after = 1
        result = adopt.apply(self.plan_file, digest)
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual(adopt.env.read_private(self.home / 'registration.json')['status'], 'unknown')
        receipt = adopt.env.read_private(self.home / 'adoption-receipt.json')
        self.assertIn('pending', receipt); self.assertTrue(receipt['patched'])
        self.assertTrue(self.plan_file.exists()); self.assertFalse((self.home / 'cd.json').exists())
        count = len(self.runtime.patches) + len(self.control.patches)
        with self.assertRaisesRegex(ValueError, 'operator recovery'):
            adopt.apply(self.plan_file, digest)
        self.assertEqual(count, len(self.runtime.patches) + len(self.control.patches))

    def test_absent_observer_is_pinned_and_left_absent_until_common_release(self):
        for key in list(self.runtime.objects):
            if key[0] == 'railshot-observability' or key[2] in ('railshot-observability', 'railshot-observer'):
                del self.runtime.objects[key]
        initial_keys = set(self.runtime.objects)
        digest = self.make_plan()
        plan = adopt.env.read_private(self.plan_file)
        self.assertTrue(plan['health']['observability']['missing'])
        self.assertEqual(plan['health']['observability']['collection_state'], 'pending')
        self.assertTrue(any(row.get('absent') for row in plan['snapshot']['objects']))
        self.assertEqual(adopt.apply(self.plan_file, digest)['status'], 'verified')
        self.assertEqual(set(self.runtime.objects), initial_keys)
        registration = adopt.env.read_private(self.home / 'registration.json')
        self.assertIs(registration['observability']['registered'], False)
        self.assertTrue(registration['observability']['reconciliation_required'])


class NodeOnlyAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target_id = 'empty-node'; self.home = self.root / 'registration'; self.shared = self.root / 'claims'
        self.policy = json.loads((ROOT / 'deployment/airgap/versions.json').read_text())
        self.registry_file = self.root / 'registry.json'; self.config_file = self.root / 'config.json'
        self.observer_file = self.root / 'observer.json'; self.policy_file = self.root / 'policy.json'
        self.refs = {'registry': str(self.registry_file), 'target_id': self.target_id, 'config': str(self.config_file),
                     'registration_dir': str(self.home), 'policy_file': str(self.policy_file), 'binding': None}
        self.ssh = {'identity_file': str(self.root / 'key'), 'known_hosts_file': str(self.root / 'known-hosts'), 'user': 'operator'}
        for path in self.ssh['identity_file'], self.ssh['known_hosts_file']:
            Path(path).write_text('test public binding'); Path(path).chmod(0o600)
        self.node = {**{k: self.policy[k] for k in ('runtime', 'cilium_images')}, 'node_uid': 'node-uid',
                     'node_name': 'node', 'node_ip': '10.66.0.2', 'architecture': 'amd64', 'helm_revision': 2,
                     'ready': True, 'api_ready': True, 'cilium_ready': True,
                     'management_ca_data': base64.b64encode(b'public-ca').decode()}
        self.observation = {'version': 1, 'owner': 'collector'}
        adopt.env.save(self.observer_file, self.observation); adopt.env.save(self.policy_file, self.policy)
        self.enterContext(patch.object(adopt.observer, 'settings', side_effect=lambda config: config))
        self.enterContext(patch.object(adopt.env, 'registered_node', side_effect=lambda *a: (copy.deepcopy(self.request), copy.deepcopy(self.descriptor))))
        @contextmanager
        def connection(_):
            yield []
        self.enterContext(patch.object(adopt.update, 'connection', connection))
        def inspect(prefix, payload):
            self.assertEqual(payload, {'action': 'inspect', 'include_ca': True})
            return copy.deepcopy(self.node)
        self.node_call = self.enterContext(patch.object(adopt.update, 'node_call', side_effect=inspect))
        self.tls = self.enterContext(patch.object(adopt.env.credentials, 'RegisteredHTTPSConnection'))
        self.enterContext(patch.object(adopt.update.ssl, 'create_default_context'))
        self.kube = self.enterContext(patch.object(adopt.env, 'runtime_kubectl', side_effect=AssertionError('adoption must not access app/observer objects')))
        self.control = self.enterContext(patch.object(adopt.env.argo, 'kubectl', side_effect=AssertionError('node-only must not access Argo')))
        self.app = self.enterContext(patch.object(adopt.update, 'app_health', side_effect=AssertionError('node-only has no app')))
        self.public = self.enterContext(patch.object(adopt.env.bridge, 'public_probe', side_effect=AssertionError('node-only has no HTTP probe')))
        self.documents = self.enterContext(patch.object(adopt.env, 'runtime_documents', side_effect=AssertionError('node-only has no application policy')))
        self.plan_file = self.root / 'plan.json'
        self.configure('aws')

    def configure(self, provider):
        self.request = {'target': {'provider': provider, 'architecture': 'amd64'}}
        self.descriptor = {'target_id': self.target_id, 'provider_kind': provider, 'resource_id': provider + '-resource',
                           'addresses': {'private': '10.66.0.2'}}
        management = {'server': 'https://10.66.0.2:6443'}
        if provider in ('gcp', 'openstack'):
            management = {'server': 'https://34.47.68.21:6443', 'tls_server_name': '10.66.0.2'}
            self.descriptor['management_endpoint'] = management['server']
        self.registry = {'version': 1, 'targets': {self.target_id: {'purpose': 'runtime', 'ssh': self.ssh}}}
        self.configuration = {'version': 2, 'scope': 'node-only', 'management': management,
                              'registration': {'state_dir': str(self.shared), 'observability_config_file': str(self.observer_file)}}
        adopt.env.save(self.registry_file, self.registry); adopt.env.save(self.config_file, self.configuration)

    def test_three_provider_empty_nodes_pin_identity_then_update_without_app_objects(self):
        # Run the real plan/apply/load/execute/verify control flow for each provider.
        # All node/network boundaries are fakes; all operator writes remain in TemporaryDirectory.
        for provider in ('aws', 'gcp', 'openstack'):
            with self.subTest(provider=provider):
                self.configure(provider)
                self.home = self.root / provider / 'registration'; self.shared = self.root / provider / 'claims'
                self.refs['registration_dir'] = str(self.home)
                self.configuration['registration']['state_dir'] = str(self.shared)
                adopt.env.save(self.config_file, self.configuration)
                plan_file = self.root / (provider + '-plan.json')
                planned = adopt.plan(self.refs, plan_file)
                self.assertEqual(planned['object_count'], 0)
                self.assertFalse(self.home.exists()); self.assertFalse(self.shared.exists())
                snapshot = adopt.env.read_private(plan_file)['snapshot']
                self.assertEqual(snapshot['runtime']['node_uid'], 'node-uid')
                self.assertEqual(snapshot['management']['ca_sha256'], hashlib.sha256(b'public-ca').hexdigest())
                self.assertEqual(adopt.apply(plan_file, planned['plan_sha256'])['status'], 'verified')
                self.assertEqual(self.tls.call_args.args[:2], ('34.47.68.21' if provider in ('gcp', 'openstack') else '10.66.0.2', 6443))
                self.assertEqual(self.tls.call_args.kwargs['server_name'], '10.66.0.2')
                record = adopt.env.read_private(self.home / 'registration.json')
                self.assertEqual((record['version'], record['scope'], record['status']), (2, 'node-only', 'succeeded'))
                self.assertFalse((self.home / 'cd.json').exists())
                self.assertFalse({'app', 'namespace', 'deployment_supported'} & set(record))
                self.assertFalse(record['observability']['registered'])
                source = {'version': 1, 'source_sha': 'a' * 40, 'from_policy': copy.deepcopy(self.policy), 'to_policy': copy.deepcopy(self.policy),
                          'from_policy_sha256': adopt.runtime.digest(self.policy), 'to_policy_sha256': adopt.runtime.digest(self.policy)}
                release_file = self.root / 'release.json'; adopt.env.save(release_file, source)
                args = SimpleNamespace(**self.refs, release=str(release_file), state_dir=str(self.home.parent / 'release'))
                loaded = adopt.update.load(args)
                self.assertIsNone(loaded[3]); self.assertEqual(loaded[-1]['node']['node_uid'], 'node-uid')
                adoption_path = self.home / 'adoption-receipt.json'; adoption = adopt.env.read_private(adoption_path)
                adopt.env.save(adoption_path, {**adoption, 'status': 'recovery_required'})
                with self.assertRaisesRegex(ValueError, 'adoption is incomplete'):
                    adopt.update.load(args)
                adopt.env.save(adoption_path, adoption)
                def node_call(prefix, payload):
                    if payload['action'] == 'apply':
                        return {'status': 'verified', 'changed': False}
                    return copy.deepcopy(self.node)
                @contextmanager
                def kubectl(_):
                    yield Mock(side_effect=AssertionError('only mocked observer-health may read Kubernetes'))
                observer = SimpleNamespace(product=Mock(return_value={}), register=Mock(return_value={'status': 'succeeded', 'registered': True}))
                with patch.object(adopt.update, 'node_call', side_effect=node_call), patch.object(adopt.update, 'stage_source', return_value='/source'), \
                        patch.object(adopt.env, 'runtime_kubectl', kubectl), patch.object(adopt.update, 'observer_health', return_value={'exporters_ready': True}), \
                        patch.object(adopt.update, 'collection_health', return_value={'collection_state': 'ready'}), \
                        patch.object(adopt.update.importlib.util, 'module_from_spec', return_value=observer), \
                        patch.object(adopt.update.importlib.util, 'spec_from_file_location', return_value=SimpleNamespace(loader=SimpleNamespace(exec_module=lambda x: None))):
                    result = adopt.update.execute(args)
                    self.assertEqual(result['status'], 'verified'); self.assertEqual(result['scope'], 'node-only')
                    self.assertEqual(result['application']['status'], 'not_applicable')
                    self.assertEqual(result['public_http']['status'], 'not_applicable')
                    self.assertNotIn('management_ca_data', json.dumps(result))
                    observation = observer.register.call_args.args[1]
                    self.assertEqual(set(observation), {'version', 'target_id', 'environment_id', 'node_ip', 'registry_file'})
                    original_files = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
                    with patch.object(adopt.env, 'save', side_effect=AssertionError('verify-only wrote state')):
                        self.assertTrue(adopt.update.verify(args)['verify_only'])
                        for key, value in (('node_uid', 'recreated'), ('management_ca_data', base64.b64encode(b'wrong-ca').decode()),
                                           ('cilium_ready', False), ('api_ready', False)):
                            original = self.node[key]; self.node[key] = value
                            with self.subTest(drift=key), self.assertRaisesRegex(ValueError, 'drifted'):
                                adopt.update.verify(args)
                            self.node[key] = original
                    self.assertEqual(original_files, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
                    if provider == 'aws':
                        next_args = SimpleNamespace(**{**vars(args), 'state_dir': str(self.home.parent / 'wrong-baseline')})
                        changed = copy.deepcopy(source); changed['source_sha'] = 'b' * 40
                        changed['from_policy']['runtime']['k3s_version'] = 'v1.34.10+k3s1'
                        changed['from_policy_sha256'] = adopt.runtime.digest(changed['from_policy'])
                        changed['upgrade'] = {'recovery_ack': True, 'k3s_binary_sha256': 'c' * 64}
                        adopt.env.save(release_file, changed)
                        result = adopt.update.execute(next_args)
                        self.assertEqual(result['status'], 'blocked'); self.assertFalse(result['mutation_started'])
                        # A later uncertain observer mutation is durable and cannot replay.
                        next_args.state_dir = str(self.home.parent / 'uncertain')
                        source['source_sha'] = 'c' * 40; adopt.env.save(release_file, source)
                        observer.register.side_effect = TimeoutError('unknown observer result')
                        result = adopt.update.execute(next_args)
                        self.assertEqual(result['status'], 'recovery_required')
                        count = observer.register.call_count
                        with self.assertRaisesRegex(ValueError, 'automatic retry forbidden'):
                            adopt.update.execute(next_args)
                        self.assertEqual(observer.register.call_count, count)
                self.kube.assert_not_called(); self.control.assert_not_called()
                self.app.assert_not_called(); self.public.assert_not_called(); self.documents.assert_not_called()

    def test_node_only_never_falls_back_from_legacy_or_mixed_config(self):
        for mutation in ({'version': 1}, {'scope': 'application'}, {'cd': {}}, {'management': {'server': 'https://other:6443'}}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                adopt.update.load_node_configuration(self.registry, self.target_id, {**self.configuration, **mutation})
        with self.assertRaises(ValueError):
            adopt.update.load_node_configuration(self.registry, self.target_id, self.configuration, '/private/database.json')

    def test_node_uid_ca_route_ssh_or_baseline_drift_invalidates_approved_plan(self):
        digest = adopt.plan(self.refs, self.plan_file)['plan_sha256']
        original = copy.deepcopy(self.node)
        for key, value in (('node_uid', 'another'), ('management_ca_data', base64.b64encode(b'another-ca').decode()),
                           ('node_ip', '10.66.0.3')):
            self.node[key] = value
            with self.subTest(drift=key), self.assertRaises(ValueError):
                adopt.apply(self.plan_file, digest)
            self.node = copy.deepcopy(original)
            self.assertFalse((self.home / 'registration.json').exists())
        Path(self.ssh['known_hosts_file']).write_text('changed trusted key')
        with self.assertRaisesRegex(ValueError, 'changed'):
            adopt.apply(self.plan_file, digest)
        self.assertFalse((self.shared / (self.target_id + '.json')).exists())
        self.kube.assert_not_called(); self.control.assert_not_called()

    def test_uncertain_node_claim_requires_recovery_and_cannot_be_replayed(self):
        digest = adopt.plan(self.refs, self.plan_file)['plan_sha256']
        self.node_call.side_effect = [copy.deepcopy(self.node), TimeoutError('node readback became unavailable')]
        self.assertEqual(adopt.apply(self.plan_file, digest)['status'], 'recovery_required')
        self.assertEqual(adopt.env.read_private(self.home / 'registration.json')['status'], 'unknown')
        self.assertTrue((self.shared / (self.target_id + '.json')).exists())
        count = self.node_call.call_count
        with self.assertRaisesRegex(ValueError, 'operator recovery'):
            adopt.apply(self.plan_file, digest)
        self.assertEqual(self.node_call.call_count, count)
        self.kube.assert_not_called(); self.control.assert_not_called()


if __name__ == '__main__':
    unittest.main()
