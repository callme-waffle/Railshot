"""Native local subprocesses/fake cloud responses only; never contact an account."""
import base64
import copy
from contextlib import contextmanager
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location('bootstrap_platform', Path(__file__).resolve().parents[1] / 'bootstrap-platform.py')
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()

    def test_plan_never_allows_delete_replace_new_instance_or_deadline_bytes_change(self):
        before = {'id': bootstrap.CONTROL, 'ami': 'ami-fixed', 'root_block_device': [{'volume_id': 'vol-fixed'}],
                  'user_data': 'old-deadline', 'associate_public_ip_address': False}
        for actions, after in [(['delete'], None), (['delete', 'create'], before), (['create'], before),
                               (['update'], {**before, 'user_data': 'extended-deadline'}),
                               (['update'], {**before, 'associate_public_ip_address': True})]:
            with self.subTest(actions=actions, after=after), self.assertRaises(bootstrap.Blocked):
                bootstrap.plan_changes({'resource_changes': [{'address': 'aws_instance.control', 'type': 'aws_instance',
                    'change': {'actions': actions, 'before': before, 'after': after}}]})
        allowed = {'resource_changes': [{'address': 'aws_security_group.control', 'type': 'aws_security_group',
                    'change': {'actions': ['update'], 'before': {}, 'after': {}}}]}
        self.assertEqual(bootstrap.plan_changes(allowed)[0]['actions'], ['update'])

    def test_product_access_can_bootstrap_without_an_application_and_keeps_named_grants(self):
        captured = []
        def capture(documents):
            captured.extend(documents)
            raise StopIteration  # Inspect the actual bootstrap render before any credentials or remote writes.
        task = types.SimpleNamespace(root=Path(__file__).resolve().parents[3], objects=capture,
            config={'github': {'application': 'demo-application', 'target_id': 'k3s-aws'}})
        for application in ('demo-application', None):
            with self.subTest(application=application):
                captured.clear()
                task.config['github']['application'] = application
                with self.assertRaises(StopIteration):
                    bootstrap.Bootstrap.bootstrap_secrets(task)
                role = next(document for document in captured if document['kind'] == 'Role')
                self.assertEqual(role['metadata']['namespace'], 'argocd')
                self.assertEqual([rule for rule in role['rules'] if rule['resources'] == ['secrets']],
                    [{'apiGroups': [''], 'resources': ['secrets'], 'resourceNames': ['railshot-k3s-aws'], 'verbs': ['get']}])
                self.assertEqual([rule for rule in role['rules'] if rule['resources'] == ['applications']],
                    [] if application is None else [{'apiGroups': ['argoproj.io'], 'resources': ['applications'],
                        'resourceNames': [application], 'verbs': ['get', 'patch']}])
                self.assertTrue(all(rule.get('resourceNames') for rule in role['rules']))
                self.assertNotIn('REQUIRED', json.dumps(captured))

    def test_existing_secret_rotation_is_preserved_but_executor_drift_and_uid_change_are_refused(self):
        wanted = {'kind': 'Secret', 'metadata': {'name': 'railshot-github', 'namespace': 'railshot-system'},
                  'type': 'Opaque', 'data': {'token': 'old'}}
        actual = {**copy.deepcopy(wanted), 'metadata': {**wanted['metadata'], 'uid': 'kept'}, 'data': {'token': 'rotated'}}
        with patch.object(bootstrap, 'kube_get', return_value=actual), patch.object(bootstrap, 'kube') as mutate:
            self.assertEqual(bootstrap.ensure_object(wanted, {bootstrap.object_key(wanted): 'kept'}), 'kept')
            mutate.assert_not_called()
            with self.assertRaises(bootstrap.Blocked):
                bootstrap.ensure_object(wanted, {bootstrap.object_key(wanted): 'other'})
        wanted['metadata']['name'] = actual['metadata']['name'] = 'railshot-executors'
        with patch.object(bootstrap, 'kube_get', return_value=actual), self.assertRaises(bootstrap.Blocked):
            bootstrap.ensure_object(wanted, {bootstrap.object_key(wanted): 'kept'})

    def test_rebootstrap_never_reverts_a_registered_application_or_added_renewal_targets(self):
        wanted = {'kind': 'Application', 'metadata': {'name': 'app', 'namespace': 'argocd'}, 'spec': {'source': {'targetRevision': 'old'}}}
        actual = copy.deepcopy(wanted); actual['metadata']['uid'] = 'same'; actual['spec']['source']['targetRevision'] = 'deployed-new'
        with patch.object(bootstrap, 'kube_get', return_value=actual), patch.object(bootstrap, 'kube') as mutate:
            with self.assertRaises(bootstrap.Blocked):
                bootstrap.ensure_object(wanted, {bootstrap.object_key(wanted): 'same'}, True)
            mutate.assert_not_called()
        wanted = {'kind': 'ConfigMap', 'metadata': {'name': 'railshot-credentials', 'namespace': 'argocd'},
                  'data': {'kubeconfig': 'fixed-paths', 'policy.json': json.dumps({'version': 1, 'targets': [{'id': 'first'}]})}}
        actual = copy.deepcopy(wanted); actual['metadata']['uid'] = 'same'
        actual['data']['policy.json'] = json.dumps({'version': 1, 'targets': [{'id': 'first'}, {'id': 'later'}]})
        with patch.object(bootstrap, 'kube_get', return_value=actual), patch.object(bootstrap, 'kube') as mutate:
            self.assertEqual(bootstrap.ensure_object(wanted, {bootstrap.object_key(wanted): 'same'}), 'same')
            mutate.assert_not_called()

    def test_rebootstrap_preserves_shared_environment_without_reverting_its_scope(self):
        registration = {'target_id': 'k3s-aws', 'secret': 'railshot-k3s-aws',
            'server': 'https://10.0.0.17:6443', 'project': 'railshot', 'namespaces': ['tenant-demo'],
            'service_account': {'name': 'railshot-argocd', 'namespace': 'tenant-demo',
                'uid': '00000000-0000-0000-0000-000000000001'},
            'ca_sha256': 'a' * 64, 'audiences': ['api'], 'tls_server_name': '10.0.0.17'}
        wanted = {'kind': 'ConfigMap', 'metadata': {'name': 'railshot-credentials', 'namespace': 'argocd'},
            'data': {'kubeconfig': 'fixed-paths', 'policy.json': json.dumps({'version': 1, 'targets': [registration]})}}
        shared = {**registration, 'project': '', 'namespaces': ['tenant-demo', 'app-' + 'a' * 24]}
        actual = {**copy.deepcopy(wanted), 'metadata': {**wanted['metadata'], 'uid': 'same'}}
        actual['data']['policy.json'] = json.dumps({'version': 1, 'targets': [shared]})
        for baseline in (registration, {**registration, 'project': ''}, shared):
            with self.subTest(project=baseline['project'], namespaces=baseline['namespaces']):
                wanted['data']['policy.json'] = json.dumps({'version': 1, 'targets': [baseline]})
                with patch.object(bootstrap, 'kube_get', return_value=actual), patch.object(bootstrap, 'kube') as mutate:
                    self.assertEqual(bootstrap.ensure_object(wanted, {bootstrap.object_key(wanted): 'same'}), 'same')
                    mutate.assert_not_called()
        wanted['data']['policy.json'] = json.dumps({'version': 1, 'targets': [registration]})
        for changes in ({'project': 'other-project'}, {'project': registration['project']},
                {'namespaces': ['app-' + 'a' * 24]}, {'namespaces': ['tenant-demo', 'foreign']},
                {'namespaces': ['tenant-demo', 'tenant-demo']}, {'server': 'https://10.0.0.18:6443'},
                {'secret': 'railshot-other'}, {'target_id': 'other'}, {'ca_sha256': 'b' * 64},
                {'service_account': {**registration['service_account'], 'uid': '00000000-0000-0000-0000-000000000002'}},
                {'audiences': ['other-api']}, {'tls_server_name': '10.0.0.18'}, {'unknown': 'drift'}):
            with self.subTest(changes=changes):
                actual['data']['policy.json'] = json.dumps({'version': 1, 'targets': [{**shared, **changes}]})
                with patch.object(bootstrap, 'kube_get', return_value=actual), patch.object(bootstrap, 'kube') as mutate:
                    with self.assertRaisesRegex(bootstrap.Blocked, 'RENEWAL_POLICY_DIFFERS'):
                        bootstrap.ensure_object(wanted, {bootstrap.object_key(wanted): 'same'})
                    mutate.assert_not_called()
        app = 'app-' + 'a' * 24
        application = {**registration, 'target_id': app, 'secret': 'railshot-' + app,
            'project': app, 'namespaces': [app], 'service_account': {**registration['service_account'], 'namespace': app}}
        wanted['data']['policy.json'] = json.dumps({'version': 1, 'targets': [application]})
        actual['data']['policy.json'] = json.dumps({'version': 1, 'targets': [{**application, 'project': ''}]})
        with patch.object(bootstrap, 'kube_get', return_value=actual), patch.object(bootstrap, 'kube') as mutate:
            with self.assertRaisesRegex(bootstrap.Blocked, 'RENEWAL_POLICY_DIFFERS'):
                bootstrap.ensure_object(wanted, {bootstrap.object_key(wanted): 'same'})
            mutate.assert_not_called()

    def test_registration_role_is_created_empty_once_and_preserves_only_exact_named_grants(self):
        wanted = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role',
                  'metadata': {'name': 'railshot-product-registrations', 'namespace': 'argocd'}, 'rules': []}
        actual = copy.deepcopy(wanted); actual['metadata']['uid'] = 'registered'
        with patch.object(bootstrap, 'kube_get', side_effect=[None, actual]), patch.object(bootstrap, 'kube') as mutate:
            self.assertEqual(bootstrap.ensure_object(wanted, {}, True, 'claim'), 'registered')
            self.assertEqual(mutate.call_args.args[0], 'create')
            self.assertEqual(mutate.call_args.kwargs['document']['rules'], [])
        adopted = {bootstrap.object_key(wanted): 'registered'}
        actual['rules'] = [{'apiGroups': [group], 'resources': [resource], 'verbs': ['get', 'patch'], 'resourceNames': ['first', 'later']}
                           for group, resource in [('argoproj.io', 'appprojects'), ('argoproj.io', 'applications'), ('', 'secrets')]]
        delete_rule = {'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['delete'],
                       'resourceNames': ['railshot-app-' + 'a' * 24]}
        for rules in ([], actual['rules'], actual['rules'] + [delete_rule]):
            observed = {**actual, 'rules': rules}
            with patch.object(bootstrap, 'kube_get', return_value=observed) as read, patch.object(bootstrap, 'kube') as mutate:
                self.assertEqual(bootstrap.ensure_object(wanted, adopted, True), 'registered')
                self.assertEqual(read.call_count, 2)
                mutate.assert_not_called()
        invalid = []
        for field, value in [('apiGroups', ['other']), ('resources', ['pods']), ('verbs', ['get', 'patch', 'update']),
                             ('resourceNames', []), ('resourceNames', ['*']), ('resourceNames', ['Uppercase']), ('nonResourceURLs', ['/'])]:
            rules = copy.deepcopy(actual['rules']); rules[0][field] = value; invalid.append(rules)
        invalid.append(actual['rules'] + [actual['rules'][0]])
        invalid.append(actual['rules'] + [{**delete_rule, 'resourceNames': ['railshot-k3s-aws']}])
        invalid.append(actual['rules'] + [{**delete_rule, 'verbs': ['get', 'patch', 'delete']}])
        for rules in invalid:
            with self.subTest(rules=rules), patch.object(bootstrap, 'kube_get', return_value={**actual, 'rules': rules}), \
                    patch.object(bootstrap, 'kube') as mutate, self.assertRaisesRegex(bootstrap.Blocked, 'REGISTRATION_ROLE_DIFFERS'):
                bootstrap.ensure_object(wanted, adopted, True)
            mutate.assert_not_called()
        replaced = copy.deepcopy(actual); replaced['metadata']['uid'] = 'another'
        with patch.object(bootstrap, 'kube_get', side_effect=[actual, replaced]), patch.object(bootstrap, 'kube') as mutate, \
                self.assertRaisesRegex(bootstrap.Blocked, 'REGISTRATION_ROLE_READBACK_DIFFERS'):
            bootstrap.ensure_object(wanted, adopted, True)
        mutate.assert_not_called()
        other = copy.deepcopy(wanted); other['metadata']['name'] = 'another-role'
        with patch.object(bootstrap, 'kube_get', return_value=actual), patch.object(bootstrap, 'kube') as mutate, \
                self.assertRaisesRegex(bootstrap.Blocked, 'REGISTERED_OBJECT_DRIFT'):
            bootstrap.ensure_object(other, {bootstrap.object_key(other): 'registered'}, True)
        mutate.assert_not_called()

    def test_observer_binding_uses_imported_registrar_state_and_rejects_ambiguous_or_unsafe_paths(self):
        config = '/var/lib/railshot/config/app-db/'
        documents = {'config/app-db/profiles.json': {'version': 1, 'profiles': [{'deployment_file': config + 'deployment.json'}]},
                     'config/app-db/deployment.json': {'registration': {'observability_config_file': config + 'observer.json'}},
                     'config/app-db/observer.json': {'state_dir': '/var/lib/railshot/state/observer'}}
        def files():
            return [{'path': path, 'kind': 'source', 'data': base64.b64encode(json.dumps(value).encode()).decode()}
                    for path, value in documents.items()]
        self.assertEqual(bootstrap.observer_product_file(files()), '/var/lib/railshot/state/observer/product.json')
        for path in ('/private/observer', '/var/lib/railshot/config', '/var/lib/railshot/state/../config', '/var/lib/railshot/state//observer'):
            documents['config/app-db/observer.json']['state_dir'] = path
            with self.subTest(path=path), self.assertRaisesRegex(bootstrap.Blocked, 'OBSERVER_STATE_PATH_INVALID'):
                bootstrap.observer_product_file(files())
        documents['config/app-db/observer.json']['state_dir'] = '/var/lib/railshot/state/observer'
        documents['config/app-db/profiles.json']['profiles'].append({'deployment_file': config + 'second.json'})
        documents['config/app-db/second.json'] = {'registration': {'observability_config_file': config + 'other.json'}}
        documents['config/app-db/other.json'] = {'state_dir': '/var/lib/railshot/state/other'}
        with self.assertRaisesRegex(bootstrap.Blocked, 'MULTIPLE_OBSERVER_PRODUCT_FILES'):
            bootstrap.observer_product_file(files())
        del documents['config/app-db/other.json']
        with self.assertRaisesRegex(bootstrap.Blocked, 'OBSERVER_CONFIG_NOT_IMPORTED'):
            bootstrap.observer_product_file(files())
        documents['config/app-db/profiles.json']['profiles'] = [{'deployment_file': config + 'deployment.json'}]
        documents['config/app-db/deployment.json']['registration'] = {}
        self.assertIsNone(bootstrap.observer_product_file(files()))

    def test_observer_binding_migrates_once_without_replacing_existing_config(self):
        wanted = {'kind': 'ConfigMap', 'metadata': {'name': 'railshot-environments', 'namespace': 'railshot-system'},
                  'data': {'profiles_file': '/var/lib/railshot/config/app-db/profiles.json',
                           'observer_file': '/var/lib/railshot/state/observer/product.json'}}
        old = copy.deepcopy(wanted); old['metadata'].update(uid='same', resourceVersion='42')
        del old['data']['observer_file']; old['data']['operator_key'] = 'preserved'
        upgraded = copy.deepcopy(old); upgraded['data'].update(wanted['data'])
        adopted = {bootstrap.object_key(wanted): 'same'}
        with patch.object(bootstrap, 'kube_get', side_effect=[old, upgraded]), patch.object(bootstrap, 'kube') as mutate:
            self.assertEqual(bootstrap.ensure_object(wanted, adopted, True), 'same')
            self.assertEqual(mutate.call_args.args[:2], ('patch', 'configmap'))
            self.assertEqual(json.loads(mutate.call_args.args[-1]),
                             {'metadata': {'resourceVersion': '42'}, 'data': upgraded['data']})
        with patch.object(bootstrap, 'kube_get', return_value=upgraded), patch.object(bootstrap, 'kube') as mutate:
            self.assertEqual(bootstrap.ensure_object(wanted, adopted, True), 'same')
            mutate.assert_not_called()
            changed = copy.deepcopy(wanted); changed['data']['observer_file'] = '/var/lib/railshot/state/other/product.json'
            with self.assertRaisesRegex(bootstrap.Blocked, 'ENVIRONMENT_BINDING_CHANGED'):
                bootstrap.ensure_object(changed, adopted, True)
            mutate.assert_not_called()

    def test_import_native_program_preserves_evolving_state_and_rejects_changed_lineage(self):
        destination = self.home / 'pvc'; destination.mkdir()
        source = self.home / 'budget.db'
        with sqlite3.connect(source) as db:
            db.execute('create table reservations (operation_id text primary key)')
            db.execute('create table snapshots (scope text primary key, provider text, source_scope text)')
            db.execute('insert into snapshots values (?,?,?)', ('aws-current', 'aws', bootstrap.ACCOUNT))
        api = {'version': 1, 'operations': {}, 'keys': {}, 'bindings': {}, 'plans': {}}
        edge_binding = {'version': 1, 'state_dir': '/var/lib/railshot/edge/allocations',
                        'terraform_dir': '/var/lib/railshot/edge/module', 'variables_file': '/var/lib/railshot/edge/inputs.tfvars.json'}
        values = [('state/state.json', 'api', json.dumps(api).encode()),
                  ('infra/terraform.tfstate', 'terraform', b'{"version":4,"lineage":"original","serial":9}'),
                  ('config/edge.json', 'edge', json.dumps({**edge_binding, 'auto_apply': False}).encode()),
                  ('budget/ledger.sqlite', 'budget', source.read_bytes())]
        payload = {'package_id': 'a' * 32, 'manifest_sha256': 'b' * 64, 'destination_owner': 'control-pvc', 'files': [
            {'path': path, 'kind': kind, 'sha256': bootstrap.digest(raw), 'data': base64.b64encode(raw).decode(),
             **({'lineage': 'original'} if kind == 'terraform' else {}),
             **({'binding': edge_binding} if kind == 'edge' else {}),
             **({'scopes': {'aws-current': {'provider': 'aws', 'source_scope': bootstrap.ACCOUNT}}} if kind == 'budget' else {})}
            for path, kind, raw in values]}
        program = bootstrap.IMPORTER.replace("pathlib.Path('/var/lib/railshot')", repr(str(destination)))
        program = program.replace('root=' + repr(str(destination)), 'root=pathlib.Path(' + repr(str(destination)) + ')')
        def run():
            return subprocess.run([sys.executable, '-c', program], input=json.dumps(payload), text=True, capture_output=True)
        first = run(); self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(json.loads(first.stdout)['imported'])
        # The new writer has legitimately advanced the records. Rebootstrap must
        # validate identity/schema and must not copy the old snapshot back.
        api['operations']['accepted'] = {'status': 'unknown'}
        (destination / 'state/state.json').write_text(json.dumps(api))
        (destination / 'infra/terraform.tfstate').write_text('{"version":4,"lineage":"original","serial":10}')
        (destination / 'config/edge.json').write_text(json.dumps({**edge_binding, 'auto_apply': True}))
        second = run(); self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(json.loads(second.stdout)['imported'])
        self.assertIn('accepted', json.loads((destination / 'state/state.json').read_text())['operations'])
        self.assertTrue(json.loads((destination / 'config/edge.json').read_text())['auto_apply'])
        (destination / 'config/edge.json').write_text(json.dumps({**edge_binding, 'state_dir': '/another-owner', 'auto_apply': True}))
        self.assertNotEqual(run().returncode, 0)
        (destination / 'config/edge.json').write_text(json.dumps({**edge_binding, 'auto_apply': True}))
        (destination / 'infra/terraform.tfstate').write_text('{"version":4,"lineage":"foreign","serial":10}')
        self.assertNotEqual(run().returncode, 0)

    def test_failed_import_does_not_transfer_owner_or_overwrite_a_conflict(self):
        root = self.home / 'pvc'; (root / 'state').mkdir(parents=True)
        (root / 'state/source.source.json').write_bytes(b'existing')
        raw = b'exported'
        payload = {'package_id': 'a' * 32, 'manifest_sha256': 'b' * 64, 'destination_owner': 'pvc',
                   'files': [{'path': 'state/source.source.json', 'kind': 'source', 'sha256': bootstrap.digest(raw),
                              'data': base64.b64encode(raw).decode()}]}
        program = bootstrap.IMPORTER.replace("'/var/lib/railshot'", repr(str(root)))
        result = subprocess.run([sys.executable, '-c', program], input=json.dumps(payload), text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / '.bootstrap-import.json').exists())
        self.assertEqual((root / 'state/source.source.json').read_bytes(), b'existing')

    def test_import_requires_disabled_argo_and_no_terminating_or_other_pvc_writer(self):
        data = {'destination_freeze': {'application_uid': 'app-uid', 'deployment_uid': 'deployment-uid'}}
        app = {'metadata': {'uid': 'app-uid'}, 'spec': {'syncPolicy': {}}}
        deployment = {'metadata': {'uid': 'deployment-uid'}, 'spec': {'replicas': 0, 'selector': {'matchLabels': {'app': 'api'}}}}
        pods = []; hpas = []
        def get(kind, *_):
            if kind == 'persistentvolumeclaim':
                return {'metadata': {'uid': 'pvc-uid'}}
            if kind == 'pod':
                return None
            return app if kind == 'application' else deployment
        def kube(*args, document=None):
            if args[0] == 'create':
                pod = copy.deepcopy(document); pod['metadata']['uid'] = 'import-pod-uid'
                pods.append(pod); return pod
            return {'items': pods if args[1] == 'pods' else hpas}
        with patch.object(bootstrap, 'kube_get', side_effect=get), patch.object(bootstrap, 'kube', side_effect=kube):
            bootstrap.frozen_destination(data)
            app['spec']['syncPolicy']['automated'] = {}
            with self.assertRaisesRegex(bootstrap.Blocked, 'IMPORT_ARGO_NOT_FROZEN'):
                bootstrap.frozen_destination(data)
            app['spec']['syncPolicy']['automated'] = {'enabled': False}
            app['operation'] = {'sync': {}}
            with self.assertRaisesRegex(bootstrap.Blocked, 'IMPORT_ARGO_NOT_FROZEN'):
                bootstrap.frozen_destination(data)
            app.pop('operation')
            pods.append({'metadata': {'uid': 'terminating', 'labels': {'app': 'api'}, 'deletionTimestamp': 'now'}, 'spec': {}})
            with self.assertRaisesRegex(bootstrap.Blocked, 'IMPORT_DESTINATION_POD_STILL_PRESENT'):
                bootstrap.frozen_destination(data)
            pods[0] = {'metadata': {'uid': 'other-writer'}, 'spec': {'volumes': [{'persistentVolumeClaim': {'claimName': 'railshot-api'}}]}}
            with self.assertRaisesRegex(bootstrap.Blocked, 'IMPORT_DESTINATION_POD_STILL_PRESENT'):
                bootstrap.frozen_destination(data)
            pods.clear(); hpas.append({'spec': {'scaleTargetRef': {'kind': 'Deployment', 'name': 'railshot-api'}}})
            with self.assertRaisesRegex(bootstrap.Blocked, 'IMPORT_API_AUTOSCALER_PRESENT'):
                bootstrap.frozen_destination(data)
            hpas.clear()
            data.update(pvc_uid='pvc-uid', image='ghcr.io/example/api@sha256:' + 'a' * 64,
                        package={'package_id': 'b' * 32, 'manifest_sha256': 'c' * 64})
            with patch.object(bootstrap, 'native', side_effect=lambda command, **_: b'{"verified":true}' if 'exec' in command else b''):
                self.assertTrue(bootstrap.import_state(data)['verified'])
            self.assertEqual(pods[0]['spec']['securityContext']['fsGroupChangePolicy'], 'OnRootMismatch')

    def test_argo_reconciliation_restarts_consumers_once_and_preserves_other_configuration(self):
        workloads = [{'kind': kind, 'metadata': {'name': name, 'uid': name},
                      'spec': {'template': {'spec': {'containers': [{'image': 'argocd:pinned'}]}}}}
                     for kind, name in [('Deployment', 'argocd-repo-server'), ('StatefulSet', 'argocd-application-controller')]]
        actual = {w['metadata']['name']: copy.deepcopy(w) for w in workloads}
        config = {'metadata': {'uid': 'config', 'resourceVersion': '1'}, 'data': {'resource.respectRBAC': 'strict'}}
        def get(kind, name, *_):
            return copy.deepcopy(config if kind == 'configmap' else actual[name])
        def mutate(*args, document):
            if args[1] == 'configmap':
                config['data'].update(document['data']); config['metadata']['resourceVersion'] = '2'
            else:
                actual[args[2]]['spec']['template']['metadata'] = copy.deepcopy(document['spec']['template']['metadata'])
        with patch.object(bootstrap, 'kube_get', side_effect=get), patch.object(bootstrap, 'kube', side_effect=mutate) as writes, \
                patch.object(bootstrap, 'native') as native:
            bootstrap.argo_reconciliation(workloads)
            self.assertEqual(writes.call_count, 3)
            self.assertEqual(native.call_count, 2)
            self.assertEqual(config['data'], {'resource.respectRBAC': 'strict', 'timeout.reconciliation': '30s',
                                              'timeout.reconciliation.jitter': '5s'})
            writes.reset_mock()
            bootstrap.argo_reconciliation(workloads)
            writes.assert_not_called()
            actual['argocd-repo-server']['metadata']['uid'] = 'replaced'
            with self.assertRaisesRegex(bootstrap.Blocked, 'ARGO_IDENTITY_CHANGED'):
                bootstrap.argo_reconciliation(workloads)

    def test_argo_health_rollout_recovers_after_config_patch_and_checks_identity(self):
        controller = {'kind': 'StatefulSet', 'metadata': {'name': 'argocd-application-controller', 'uid': 'same-controller'},
                      'spec': {'template': {'spec': {'containers': [{'name': 'controller', 'image': 'argocd:pinned'}]}}}}
        for initial in ('false', 'true'):
            with self.subTest(initial=initial):
                params = {'metadata': {'uid': 'same-config', 'resourceVersion': '10'}, 'data': {'controller.resource.health.persist': initial, 'other': 'preserved'}}
                actual_controller = copy.deepcopy(controller)
                def get(kind, *_):
                    return copy.deepcopy(params if kind == 'configmap' else actual_controller)
                def patch_params(*args, document):
                    if args[1] == 'configmap':
                        params['data'].update(document['data']); params['metadata']['resourceVersion'] = '11'
                    else:
                        actual_controller['spec']['template']['metadata'] = copy.deepcopy(document['spec']['template']['metadata'])
                with patch.object(bootstrap, 'kube_get', side_effect=get), patch.object(bootstrap, 'kube', side_effect=patch_params) as mutate, \
                        patch.object(bootstrap, 'native') as native:
                    bootstrap.argo_health_persistence([controller])
                    self.assertEqual(mutate.call_count, 1 + int(initial != 'true'))
                    self.assertEqual(actual_controller['spec']['template']['metadata']['annotations']['railshot.io/argocd-health-config'],
                                     'same-config:' + params['metadata']['resourceVersion'])
                    self.assertEqual(params['data']['other'], 'preserved')
                    mutate.reset_mock()
                    bootstrap.argo_health_persistence([controller])
                    mutate.assert_not_called()
                changed = copy.deepcopy(actual_controller); changed['metadata']['uid'] = 'replacement'
                with patch.object(bootstrap, 'kube_get', side_effect=lambda kind, *_: params if kind == 'configmap' else changed), \
                        patch.object(bootstrap, 'native'), self.assertRaisesRegex(bootstrap.Blocked, 'ARGO_IDENTITY_CHANGED'):
                    bootstrap.argo_health_persistence([controller])

    def test_verified_handoff_enables_the_fixed_profile_path_and_rechecks_it_on_resume(self):
        source = {'source_owner': 'local', 'writers': {'api': 'stopped', 'terraform': 'frozen', 'edge': 'frozen', 'budget': 'frozen'}}
        destination = {'version': 1, 'destination_owner': 'pvc', 'application_uid': 'app', 'deployment_uid': 'deployment',
                       'pvc_uid': 'pvc', 'writers': {'api': 'stopped', 'argocd': 'frozen'}}
        bootstrap.write(self.home / 'source.json', source); bootstrap.write(self.home / 'destination.json', destination)
        documents = {'config/app-db/profiles.json': {'version': 1, 'profiles': [
            {'deployment_file': '/var/lib/railshot/config/app-db/deployment.json'}]},
            'config/app-db/deployment.json': {'registration': {
                'observability_config_file': '/var/lib/railshot/config/app-db/observer.json'}},
            'config/app-db/observer.json': {'state_dir': '/var/lib/railshot/state/observer'}}
        files = []
        archive = self.home / 'package.tar'
        with tarfile.open(archive, 'w') as stream:
            for path, document in documents.items():
                raw = json.dumps(document).encode()
                entry = tarfile.TarInfo(path); entry.size = len(raw)
                stream.addfile(entry, io.BytesIO(raw))
                files.append({'path': path, 'kind': 'source', 'sha256': bootstrap.digest(raw)})
        archive.chmod(0o600)
        package = {'version': 1, 'package_id': 'a' * 32, 'source_owner': 'local', 'destination_owner': 'pvc',
                   'freeze_receipt': str(self.home / 'source.json'), 'freeze_sha256': bootstrap.digest((self.home / 'source.json').read_bytes()),
                   'destination_freeze_receipt': str(self.home / 'destination.json'),
                   'destination_freeze_sha256': bootstrap.digest((self.home / 'destination.json').read_bytes()), 'archive': str(archive),
                   'files': files}
        bootstrap.write(self.home / 'package.json', package)
        bootstrap.write(self.home / 'executors.json', {'cd.json': '{}', 'kubeconfig': 'fixed'})
        task = bootstrap.Bootstrap(self.home / 'checkout', {'state_dir': str(self.home / 'private'),
            'import_package': str(self.home / 'package.json'), 'secrets': {'executors': str(self.home / 'executors.json')},
            'adopted_uids': {'Application/argocd/railshot-platform': 'app',
            'Deployment/railshot-system/railshot-api': 'deployment', 'PersistentVolumeClaim/railshot-system/railshot-api': 'pvc'}})
        calls = []
        def remote(role, action, data):
            calls.append(data['verify_only'])
            self.assertEqual(data['destination_freeze'], destination)
            return {'verified': True, 'package_id': package['package_id']}
        with patch.object(task, 'remote', side_effect=remote), patch.object(task, 'objects') as objects:
            task.handoff('ghcr.io/example/api@sha256:' + 'b' * 64)
            task.handoff('ghcr.io/example/api@sha256:' + 'b' * 64)
            self.assertEqual(calls, [False, True])
            self.assertEqual(objects.call_count, 2)
            document = objects.call_args.args[0][0]
            self.assertEqual(document['metadata']['name'], 'railshot-environments')
            self.assertEqual(document['data'], {'profiles_file': '/var/lib/railshot/config/app-db/profiles.json',
                                                'observer_file': '/var/lib/railshot/state/observer/product.json'})
            self.assertTrue(objects.call_args.kwargs['preserve_existing'])
        with patch.object(task, 'remote', return_value={'verified': False}) as remote, patch.object(task, 'objects') as objects:
            with self.assertRaisesRegex(bootstrap.Blocked, 'IMPORT_RECEIPT_DIFFERS'):
                task.handoff('ghcr.io/example/api@sha256:' + 'b' * 64)
            objects.assert_not_called()

    def test_terminal_import_pod_recovery_uses_uid_delete_and_never_deletes_running_pod(self):
        for phase in ('Succeeded', 'Running'):
            with self.subTest(phase=phase):
                data = {'pvc_uid': 'pvc', 'destination_freeze': {'deployment_uid': 'api'}, 'image': 'api@sha256:' + 'd' * 64,
                        'package': {'package_id': 'a' * 32, 'manifest_sha256': 'b' * 64}}
                current = {'metadata': {'name': 'railshot-bootstrap-import-' + 'a' * 12, 'uid': 'old', 'labels': bootstrap.LABELS,
                            'annotations': {'railshot.io/import-manifest': 'b' * 64}}, 'status': {'phase': phase},
                           'spec': {'automountServiceAccountToken': False,
                            'volumes': [{'name': 'state', 'persistentVolumeClaim': {'claimName': 'railshot-api'}}],
                            'containers': [{'image': data['image'], 'volumeMounts': [{'name': 'state', 'mountPath': '/var/lib/railshot'}]}]}}
                deleted = []; created = []
                def get(kind, *_):
                    if kind == 'pod': return current
                    return {'metadata': {'uid': 'pvc' if kind == 'persistentvolumeclaim' else 'api'}, 'spec': {'replicas': 0}}
                def kube(*args, document):
                    nonlocal current
                    if args[0] == 'delete':
                        self.assertEqual(document['preconditions']['uid'], current['metadata']['uid'])
                        deleted.append(current['metadata']['uid']); current = None; return {}
                    self.assertIsNone(current)
                    current = copy.deepcopy(document); current['metadata']['uid'] = 'new'; created.append(current)
                    return current
                def native(command, **_):
                    if 'exec' in command and phase == 'Running': raise bootstrap.Blocked('SIMULATED_INTERRUPT')
                    return b'{"verified":true}' if 'exec' in command else b''
                with patch.object(bootstrap, 'kube_get', side_effect=get), patch.object(bootstrap, 'kube', side_effect=kube), \
                        patch.object(bootstrap, 'native', side_effect=native), patch.object(bootstrap, 'frozen_destination'):
                    if phase == 'Running':
                        with self.assertRaisesRegex(bootstrap.Blocked, 'SIMULATED_INTERRUPT'): bootstrap.import_state(data)
                        self.assertEqual(deleted, []); self.assertEqual(created, [])
                    else:
                        self.assertTrue(bootstrap.import_state(data)['verified'])
                        self.assertEqual(deleted, ['old', 'new']); self.assertEqual(len(created), 1)

    def test_uncertain_dispatch_is_observed_once_and_never_reposted(self):
        settings = {'state_dir': str(self.home / 'private'), 'source': {'ref': 'a' * 40}, 'adopted_uids': {}}
        task = bootstrap.Bootstrap(self.home / 'checkout', settings)
        task.record['workflow'] = {'before': [1], 'actor': 42, 'run_id': None, 'source_sha': 'a' * 40,
                                   'requested_at': '2026-10-02T10:00:00Z', 'status': 'unknown'}
        calls = []
        names = ('dashboard', 'api', 'ci-runner')
        def gh(path, method='GET', body=None):
            calls.append((path, method))
            self.assertEqual(method, 'GET')
            if 'workflows/' in path:
                return {'workflow_runs': [{'id': 9, 'head_sha': 'a' * 40, 'actor': {'id': 42}, 'created_at': '2026-10-02T10:00:01Z'}]}
            if '/artifacts?' in path:
                return {'artifacts': [{'id': index, 'name': 'platform-image-' + name + '-' + 'a' * 40, 'expired': False}
                                      for index, name in enumerate(names)]}
            return {'head_sha': 'a' * 40, 'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success'}
        def native(command, **kwargs):
            index = int(command[-1].split('/')[-2]); component = names[index]
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr(component + '.json', json.dumps({component: 'ghcr.io/jasmin-softbank/railshot-' + component + '@sha256:' + 'b' * 64}))
            return stream.getvalue()
        with patch.object(task, 'gh', side_effect=gh), patch.object(bootstrap, 'native', side_effect=native):
            self.assertEqual(set(task.publication()), set(names))
        self.assertTrue(all(method == 'GET' for _, method in calls))
        self.assertEqual(task.record['workflow']['run_id'], 9)

    def test_read_only_resume_still_reads_actual_state_lineage(self):
        state = self.home / 'terraform.tfstate'
        bootstrap.write(state, {'lineage': 'replacement', 'version': 4})
        settings = {'state_dir': str(self.home / 'private'), 'adopted_uids': {},
                    'terraform': {'control': {'state': str(state), 'variables': '/unused', 'lineage': 'original'}}}
        task = bootstrap.Bootstrap(self.home / 'checkout', settings)
        task.record['attempts']['control'] = {'status': 'completed'}
        with patch.object(bootstrap, 'native') as external, self.assertRaises(bootstrap.Blocked):
            task.terraform('control')
        external.assert_not_called()

    def test_uncertain_first_verifier_apply_recovers_only_the_saved_plan_same_native_resources(self):
        state = self.home / 'terraform.tfstate'; bootstrap.write(state, {'lineage': 'newly-created', 'version': 4})
        settings = {'state_dir': str(self.home / 'private'), 'adopted_uids': {},
                    'terraform': {'platform-verification': {'state': str(state), 'variables': '/private/vars', 'lineage': None}}}
        task = bootstrap.Bootstrap(self.home / 'checkout', settings)
        directory = task.home / 'platform-verification'; directory.mkdir(mode=0o700)
        plan = directory / 'reviewed.tfplan'; plan.write_bytes(b'original-saved-plan')
        original = {'resource_changes': [{'address': 'aws_ssm_document.verify', 'type': 'aws_ssm_document',
                    'provider_name': 'registry.terraform.io/hashicorp/aws',
                    'change': {'actions': ['create'], 'after': {'name': 'Railshot-VerifyPlatform', 'id': None}, 'after_unknown': {'id': True}}}]}
        observed = copy.deepcopy(original); observed['resource_changes'][0]['change'] = {
            'actions': ['no-op'], 'before': {'name': 'Railshot-VerifyPlatform', 'id': 'actual-id'},
            'after': {'name': 'Railshot-VerifyPlatform', 'id': 'actual-id'}}
        task.record['attempts']['platform-verification'] = {'status': 'unknown', 'plan_sha256': bootstrap.digest(plan.read_bytes())}
        task.save()
        calls = []
        def native(command, **_):
            calls.append(command)
            if 'show' in command: return json.dumps(original if command[-1] == str(plan) else observed).encode()
            if 'output' in command: return b'{"example":{"value":"actual"}}'
            return b''
        with patch.object(bootstrap, 'native', side_effect=native), patch.object(task, 'save', side_effect=OSError('interrupted')), self.assertRaises(OSError):
            task.terraform('platform-verification')
        persisted = json.loads(task.path.read_text())
        self.assertEqual(persisted['attempts']['platform-verification']['status'], 'unknown')
        self.assertNotIn('lineages', persisted)
        task = bootstrap.Bootstrap(self.home / 'checkout', settings)
        with patch.object(bootstrap, 'native', side_effect=native):
            self.assertEqual(task.terraform('platform-verification'), {'example': 'actual'})
        self.assertEqual(task.record['lineages']['platform-verification'], 'newly-created')
        self.assertTrue(all('apply' not in command for command in calls))
        self.assertEqual(plan.read_bytes(), b'original-saved-plan')
        for corrupt in ('plan-bytes', 'native-identity'):
            with self.subTest(corrupt=corrupt):
                task.record.pop('lineages', None)
                task.record['attempts']['platform-verification']['status'] = 'unknown'
                if corrupt == 'plan-bytes': plan.write_bytes(b'other-plan')
                else:
                    plan.write_bytes(b'original-saved-plan')
                    observed['resource_changes'][0]['change']['after']['name'] = 'OtherDocument'
                with patch.object(bootstrap, 'native', side_effect=native), self.assertRaises(bootstrap.Blocked):
                    task.terraform('platform-verification')
                self.assertNotIn('lineages', task.record)

    def test_native_failure_does_not_expose_stderr_secret(self):
        with self.assertRaisesRegex(bootstrap.Blocked, '^NATIVE_COMMAND_FAILED$'):
            bootstrap.native([sys.executable, '-c', 'import sys; print("secret-example",file=sys.stderr);sys.exit(9)'])

    def test_metadata_drop_requires_matching_policy_verdict_endpoint_identity_and_flow(self):
        endpoint = {'id': 42, 'status': {'identity': {'id': 765}}}
        pod = {'status': {'podIP': '10.52.0.9'}}
        event = {'type': 'drop', 'reason': 'Policy denied by denylist', 'source': 42, 'srcLabel': 765,
                 'summary': {'l3': {'src': '10.52.0.9', 'dst': '169.254.169.254'}, 'l4': {'dst': '80'}}}
        self.assertTrue(bootstrap.metadata_drop(event, endpoint, pod))
        for field, wrong in [('reason', 'TTL exceeded'), ('source', 765), ('srcLabel', 42), ('type', 'trace')]:
            with self.subTest(field=field):
                self.assertFalse(bootstrap.metadata_drop({**event, field: wrong}, endpoint, pod))
        for layer, field, wrong in [('l3', 'src', '10.52.0.10'), ('l3', 'dst', '1.1.1.1'), ('l4', 'dst', '443')]:
            changed = copy.deepcopy(event); changed['summary'][layer][field] = wrong
            self.assertFalse(bootstrap.metadata_drop(changed, endpoint, pod))

    def test_metadata_native_policy_labels_and_frozen_api_deny_only_readback(self):
        import yaml
        source = yaml.safe_load((Path(__file__).resolve().parents[2] / 'manifests/product-metadata.yaml').read_text())
        document, binding = bootstrap.metadata_document(source)
        cilium = {'metadata': {'name': 'cilium', 'namespace': 'kube-system', 'labels': {'k8s-app': 'cilium'}}, 'spec': {'nodeName': 'control'}}
        pods = [cilium]
        for index, (ns, labels) in enumerate([('argocd', {}), ('railshot-system', {'app': 'railshot-dashboard'}),
                                             ('kube-system', {'k8s-app': 'kube-dns'}), ('kube-system', {'app': 'local-path-provisioner'})]):
            pods.append({'metadata': {'name': 'pod-' + str(index), 'namespace': ns, 'uid': 'uid-' + str(index), 'labels': labels},
                         'spec': {'nodeName': 'control'}, 'status': {'phase': 'Running', 'podIP': '10.52.0.' + str(index + 10)}})
        endpoints = [{'id': i, 'status': {'state': 'ready', 'identity': {'id': i + 100}, 'networking': {'addressing': [{'ipv4': p['status']['podIP']}]},
                      'policy': {'realized': {'policy-revision': 12}}}} for i, p in enumerate(pods[1:])]
        # Actual pinned daemon uses uppercase Labels on each internal PolicyEntry.
        rules = [{'Tier': 200, 'Labels': [{'key': 'io.cilium.k8s.policy.uid', 'value': 'policy-uid', 'source': 'k8s'}] + s['labels']}
                 for s in document['specs']]
        observed = rules
        calls = []
        def native(command, **_):
            calls.append(command)
            if command[-1] == 'version': return b'Cilium 1.20.2'
            if 'get' in command: return json.dumps({'revision': 12, 'policy': json.dumps(observed)}).encode()
            if 'list' in command: return json.dumps(endpoints).encode()
            return b''
        with patch.object(bootstrap, 'node_ready'), \
                patch.object(bootstrap, 'kube', return_value={'items': pods}), \
                patch.object(bootstrap, 'kube_get', return_value={**document, 'metadata': {'uid': 'policy-uid'}}), \
                patch.object(bootstrap, 'native', side_effect=native), \
                patch.object(bootstrap, 'denied_metadata', side_effect=lambda c, e, p: {'pod_uid': p['metadata']['uid']}) as deny:
            proof = bootstrap.metadata_policy({'node_name': 'control'}, {'document': source, 'uid': 'policy-uid'}, identity=False)
            self.assertEqual(proof['policy_revision'], 12)
            self.assertEqual(proof['spec_sha256'], binding)
            self.assertEqual(deny.call_count, 4)
            self.assertNotIn('api', proof)  # The API may remain frozen until handoff/release.
            stale = copy.deepcopy(rules)
            stale[0]['Labels'][1]['value'] = '0' * 64  # Same UID/revision/count, old desired specs.
            for rejected in (stale, rules[:-1], rules + [rules[0]], [rules[0]] * 5,
                             [{'labels': r['Labels']} for r in rules]):
                observed = rejected; calls.clear(); deny.reset_mock()
                with patch.object(bootstrap.time, 'monotonic', side_effect=[0, 61]), \
                        self.assertRaisesRegex(bootstrap.Blocked, 'CILIUM_METADATA_POLICY_NOT_IMPORTED'):
                    bootstrap.metadata_policy({'node_name': 'control'}, {'document': source, 'uid': 'policy-uid'})
                self.assertFalse(any('wait' in c for c in calls))
                deny.assert_not_called()

    def test_metadata_after_control_is_deny_only_and_final_requires_real_api(self):
        for stage in ('before', 'after', 'final'):
            with self.subTest(stage=stage), patch.object(bootstrap, 'guest_identity'), \
                    patch.object(bootstrap, 'metadata_policy', return_value={'verified': True}) as policy, \
                    patch.object(sys, 'stdout', io.StringIO()):
                bootstrap.guest({'node': {}, 'action': 'metadata-' + stage, 'data': {}})
            self.assertEqual(policy.call_args.kwargs, {'apply': stage == 'before', 'identity': stage == 'final'})

    def test_metadata_probe_timeout_without_a_real_policy_drop_fails(self):
        pod = {'metadata': {'name': 'dashboard', 'namespace': 'railshot-system', 'uid': 'pod-uid'}, 'status': {'podIP': '10.52.0.9'}}
        endpoint = {'id': 42, 'status': {'identity': {'id': 765}}}
        sandbox = {'status': {'state': 'SANDBOX_READY', 'metadata': {'uid': 'pod-uid'}, 'network': {'ip': '10.52.0.9'}}, 'info': {'pid': 1234}}
        calls = []
        def native(command, **_):
            calls.append(command)
            if 'pods' in command: return json.dumps({'items': [{'id': 'sandbox', 'state': 'SANDBOX_READY', 'metadata': {'uid': 'pod-uid'}}]}).encode()
            if 'inspectp' in command:
                self.assertEqual(command, ['k3s', 'crictl', 'inspectp', '-o', 'json', 'sandbox'])
                return json.dumps(sandbox).encode()
            return b'{"responded":false}'
        monitor = types.SimpleNamespace(pid=99999999, poll=lambda: None, wait=lambda **_: 0)
        with patch.object(bootstrap, 'native', side_effect=native), patch.object(bootstrap.subprocess, 'Popen', return_value=monitor), \
                patch.object(bootstrap.os, 'killpg'), patch.object(bootstrap.time, 'sleep'), \
                self.assertRaisesRegex(bootstrap.Blocked, 'METADATA_POLICY_DROP_NOT_OBSERVED'):
            bootstrap.denied_metadata({'metadata': {'name': 'cilium'}}, endpoint, pod)
        self.assertEqual(sum(c[0] == 'nsenter' for c in calls), 2)

    def test_api_identity_probe_keeps_tokens_private_and_rejects_foreign_or_static_credentials(self):
        for case in ('valid', 'foreign-role', 'static-key'):
            with self.subTest(case=case):
                outputs = iter([b'token-must-never-be-output', bootstrap.CONTROL.encode(), b'railshot-control-poc'])
                identity = {'Account': bootstrap.ACCOUNT, 'Arn': 'arn:aws:sts::' + bootstrap.ACCOUNT + ':assumed-role/' +
                            ('foreign' if case == 'foreign-role' else 'railshot-control-poc') + '/' + bootstrap.CONTROL}
                stdout = io.StringIO()
                with patch.dict(os.environ, {'AWS_ACCESS_KEY_ID': 'example'} if case == 'static-key' else {}, clear=True), \
                        patch.object(Path, 'exists', return_value=False), \
                        patch('urllib.request.urlopen', side_effect=lambda *a, **kw: io.BytesIO(next(outputs))) as request, \
                        patch.object(subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=json.dumps(identity).encode())), \
                        patch.object(sys, 'stdin', io.StringIO(json.dumps({'instance_id': bootstrap.CONTROL}))), patch.object(sys, 'stdout', stdout):
                    if case == 'valid': exec(bootstrap.API_IDENTITY, {})
                    else:
                        with self.assertRaises(SystemExit): exec(bootstrap.API_IDENTITY, {})
                    if case == 'static-key': request.assert_not_called()
                value = stdout.getvalue()
                self.assertNotIn('token-must-never-be-output', value)
                self.assertEqual(json.loads(value)['verified'], case == 'valid')

    def test_guest_payload_is_valid_python_only_on_stdin_and_has_bounded_remote_cgroup(self):
        settings = {'state_dir': str(self.home / 'private'), 'adopted_uids': {}, 'source': {'ref': 'a' * 40},
                    'control': {'instance_id': bootstrap.CONTROL, 'private_ip': '172.31.0.172',
                                'ssh': {'identity_file': '/private/key', 'known_hosts_file': '/private/hosts'}}}
        task = bootstrap.Bootstrap(self.home / 'checkout', settings)
        @contextmanager
        def forward(*args):
            yield 22001
        transport = types.SimpleNamespace(forwarded_port=forward)
        sentinel = 'private-input-example\nwith-newline'
        def run(command, *, body, timeout):
            self.assertNotIn(sentinel, str(command))
            self.assertIn('--property=RuntimeMaxSec=90', command[-1])
            self.assertIn('--property=KillMode=control-group', command[-1])
            text = body.decode()
            compile(text, 'guest-stdin', 'exec')
            self.assertNotIn('def main():', text)
            self.assertIn('private-input-example', text)
            return b'{"status":"ok","result":{"verified":true}}'
        with patch.dict(sys.modules, {'transport': transport}), patch.object(bootstrap, 'native', side_effect=run):
            self.assertEqual(task.remote('control', 'objects', {'secret': sentinel}, timeout=120), {'verified': True})


if __name__ == '__main__':
    unittest.main()
