"""No-cloud lifecycle transactions: use real registration/control helpers and fake provider/runtime I/O."""
import copy
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import application_lifecycle as lifecycle
import applications
import environment as runtime
import test_applications


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_applications.ApplicationsTest(methodName='runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.assertEqual(self.fixture.register()['status'], 'succeeded')
        self.app = self.fixture.request(); self.home = self.fixture.home()
        self.binding = runtime.read_private(self.home / 'binding.json')
        self.control = self.fixture.fixture.control
        self.calls = []
        native = self.control.__call__
        def control(namespace, *args, document=None):
            self.calls.append((args[0], args[1]))
            if args[:2] == ('delete', '--raw'):
                resource, key = args[2].split('/')[-2:]
                kind = {'applications': 'application', 'secrets': 'secret', 'appprojects': 'appproject'}[resource]
                item = self.control.objects[namespace, kind, key]
                self.assertEqual(document['preconditions'], {k: item['metadata'][k] for k in ('uid', 'resourceVersion')})
                del self.control.objects[namespace, kind, key]
                return {'kind': 'Status', 'status': 'Success'}
            if args[0] == 'patch':
                item = self.control.objects[namespace, args[1], args[2]]
                self.assertEqual(document[:2], [{'op': 'test', 'path': '/metadata/uid', 'value': item['metadata']['uid']},
                                               {'op': 'test', 'path': '/metadata/resourceVersion', 'value': item['metadata']['resourceVersion']}])
                for operation in document[2:]:
                    field = item
                    parts = [part.replace('~1', '/').replace('~0', '~') for part in operation['path'].lstrip('/').split('/')]
                    for part in parts[:-1]:
                        field = field[part]
                    field[parts[-1]] = copy.deepcopy(operation['value'])
                return copy.deepcopy(item)
            return native(namespace, *args, document=document)
        self.enterContext(patch.object(runtime.argo, 'kubectl', lambda context, *args, **kwargs: control(*args, **kwargs)))
        app_id = self.app['application_id']; target = self.binding['registered']['target']
        app_name = runtime.application_name(app_id, app_id, self.app['app'])
        self.control.objects['argocd', 'application', app_name] = {
            'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'Application',
            'metadata': {'name': app_name, 'labels': {'railshot.io/target': app_id, 'app.kubernetes.io/managed-by': 'railshot'}},
            'spec': {'project': app_id, 'source': {'repoURL': target['repo_url'], 'path': target['path']},
                     'destination': {'server': target['cluster_server'], 'namespace': app_id}}}
        for obj in self.control.objects.values():
            obj.setdefault('metadata', {}).setdefault('uid', str(uuid.uuid4()))
            obj['metadata']['resourceVersion'] = '1'
        self.inventory = {'version': 1, 'application_id': app_id, 'namespace': {'kind': 'Namespace', 'name': app_id, 'uid': 'namespace-1'},
                          'service_account': {'name': runtime.SA, 'namespace': app_id, 'uid': 'sa-1'},
                          'records': [], 'storage': [], 'resources': [{'kind': 'Namespace', 'name': app_id}], 'retained': []}
        self.enterContext(patch.object(lifecycle.workloads, 'inventory', side_effect=lambda *args, **kwargs: copy.deepcopy(self.inventory)))
        self.execute = self.enterContext(patch.object(lifecycle.workloads, 'execute', return_value={'status': 'succeeded', 'residuals': []}))
        self.edge_plan = self.enterContext(patch.object(lifecycle.edge, 'plan', return_value={'resources': [], 'private': {'id': 'owned-edge', 'health_path': '/health'}}))
        self.edge_validate = self.enterContext(patch.object(lifecycle.edge, 'validate', create=True))
        self.edge_execute = self.enterContext(patch.object(lifecycle.edge, 'execute', return_value={'status': 'succeeded'}))
        self.enterContext(patch.object(runtime.bridge, 'public_probe', return_value={'state': 'succeeded'}))
        self.calls.clear()

    def request(self, action='delete', **kwargs):
        return {'version': 1, 'phase': 'plan', **self.app, 'action': action, 'operation_id': str(uuid.uuid4()), **kwargs}

    def plan(self, action='delete'):
        return lifecycle.lifecycle(self.fixture.config_path, self.request(action))

    def apply(self, plan, action='delete', **kwargs):
        request = self.request(action, phase='apply', plan_id=plan['plan_id'], plan_hash=plan['plan_hash'], delete_data=action == 'delete', **kwargs)
        return request, lifecycle.lifecycle(self.fixture.config_path, request)

    def shared_registration(self):
        cm = self.control.objects['argocd', 'configmap', 'railshot-credentials']
        policy = json.loads(cm['data']['policy.json'])
        shared = next(row for row in policy['targets'] if row['target_id'] == self.app['environment_id'])
        return shared, self.control.objects['argocd', 'secret', shared['secret']]

    def test_partial_delete_reconciles_and_resumes_remaining_original_resources(self):
        self.edge_execute.side_effect = RuntimeError('route transport unavailable before dispatch')
        request, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'unknown')
        name = runtime.application_name(self.app['application_id'], self.app['application_id'], self.app['app'])
        self.assertNotIn(('argocd', 'application', name), self.control.objects)
        before = copy.deepcopy(self.control.objects)
        self.edge_execute.side_effect = None
        with patch.object(lifecycle.edge, 'inspect_execution', return_value='not_started'), patch.object(lifecycle.workloads, 'deleted', return_value=False):
            inspected = lifecycle.lifecycle(self.fixture.config_path, {**request, 'phase': 'reconcile'})
            self.assertTrue(inspected['resumable'], inspected)
            self.assertEqual(before, self.control.objects)
            resumed = lifecycle.lifecycle(self.fixture.config_path, {**request, 'phase': 'resume'})
        self.assertEqual(resumed['status'], 'succeeded', resumed)
        self.assertEqual(runtime.read_private(self.home / 'lifecycle.json')['status'], 'deleted')

    def test_partial_delete_reconciliation_rejects_unknown_route_write(self):
        self.edge_execute.side_effect = RuntimeError('provider reply lost')
        request, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'unknown')
        before = copy.deepcopy(self.control.objects)
        with patch.object(lifecycle.edge, 'inspect_execution', side_effect=ValueError('route outcome unknown')):
            with self.assertRaises(ValueError):
                lifecycle.lifecycle(self.fixture.config_path, {**request, 'phase': 'reconcile'})
        self.assertEqual(before, self.control.objects)

    def test_lost_namespace_delete_reply_resumes_without_repeating_namespace_delete(self):
        self.execute.side_effect = RuntimeError('namespace delete reply lost')
        request, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'unknown')
        with patch.object(lifecycle.edge, 'inspect_execution', return_value='succeeded'), patch.object(lifecycle.workloads, 'deleted', return_value=True):
            inspected = lifecycle.lifecycle(self.fixture.config_path, {**request, 'phase': 'reconcile'})
            self.assertTrue(inspected['resumable'], inspected)
            resumed = lifecycle.lifecycle(self.fixture.config_path, {**request, 'phase': 'resume'})
        self.assertEqual(resumed['status'], 'succeeded', resumed)
        self.assertEqual(self.execute.call_count, 1)

    def test_shared_cluster_delete_preserves_anchor_and_other_namespaces(self):
        self.assertEqual(self.fixture.register('second-app')['status'], 'succeeded')
        shared, secret = self.shared_registration()
        before = copy.deepcopy(secret); original = copy.deepcopy(shared)
        _, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'succeeded', result)
        live = self.control.objects['argocd', 'secret', shared['secret']]
        self.assertEqual(live['metadata']['uid'], before['metadata']['uid'])
        self.assertEqual({k: v for k, v in live['data'].items() if k != 'namespaces'},
                         {k: v for k, v in before['data'].items() if k != 'namespaces'})
        expected = [ns for ns in original['namespaces'] if ns != self.app['application_id']]
        self.assertEqual(base64.b64decode(live['data']['namespaces']).decode().split(','), expected)
        policy = json.loads(self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        self.assertEqual(next(row for row in policy['targets'] if row['target_id'] == self.app['environment_id']),
                         {**original, 'namespaces': expected})
        for rule in self.control.objects['argocd', 'role', 'railshot-product-registrations']['rules']:
            if 'delete' in rule['verbs']:
                self.assertNotIn(shared['secret'], rule['resourceNames'])
        self.assertIn(shared['secret'], self.control.objects['argocd', 'role', 'railshot-credentials']['rules'][0]['resourceNames'])

    def test_registered_app_without_cd_journal_or_argo_application_can_be_deleted(self):
        app_id = self.app['application_id']
        name = runtime.application_name(app_id, app_id, self.app['app'])
        del self.control.objects['argocd', 'application', name]
        self.assertFalse((self.home / 'deployments').exists())
        plan = self.plan()
        self.assertEqual(plan['status'], 'planned')
        self.assertFalse(any(row['kind'] == 'Application' for row in plan['resources']))
        self.assertTrue(all(verb == 'get' for verb, _ in self.calls))
        _, result = self.apply(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['residuals'], [])
        self.assertEqual(runtime.read_private(self.home / 'lifecycle.json')['status'], 'deleted')
        self.execute.assert_called_once()
        self.assertNotIn(('argocd', 'appproject', app_id), self.control.objects)
        self.assertIn(('argocd', 'secret', 'railshot-' + self.app['environment_id']), self.control.objects)

    def test_shared_token_rotation_keeps_plan_valid_but_uid_drift_does_not(self):
        _, secret = self.shared_registration()
        plan = self.plan(); uid = secret['metadata']['uid']
        secret['metadata']['uid'] = str(uuid.uuid4()); self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'LIFECYCLE_PLAN_CHANGED'):
            self.apply(plan)
        self.assertTrue(all(verb == 'get' for verb, _ in self.calls))
        secret['metadata']['uid'] = uid
        config = json.loads(base64.b64decode(secret['data']['config']))
        config['bearerToken'] = config['bearerToken'].rsplit('.', 1)[0] + '.rotated'
        secret['data']['config'] = base64.b64encode(json.dumps(config).encode()).decode()
        _, result = self.apply(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        live = self.control.objects['argocd', 'secret', 'railshot-' + self.app['environment_id']]
        self.assertEqual(base64.b64decode(live['data']['namespaces']).decode(), 'old-app')

    def test_cleanup_grants_never_add_shared_delete_and_allow_other_registration(self):
        selected = lifecycle.control_inventory(self.binding, self.fixture.config['environments'][self.app['environment_id']])
        lifecycle.grant_cleanup(self.binding, selected)
        role = self.control.objects['argocd', 'role', 'railshot-product-registrations']
        for rule in role['rules']:
            if rule['verbs'] == ['delete']:
                self.assertEqual(len(rule['resourceNames']), 1)
                self.assertNotIn('railshot-' + self.app['environment_id'], rule['resourceNames'])
        self.assertEqual(self.fixture.register('second-app')['status'], 'succeeded')
        role = self.control.objects['argocd', 'role', 'railshot-product-registrations']
        rule = next(rule for rule in role['rules'] if rule['resources'] == ['secrets'] and rule['verbs'] == ['delete'])
        rule['resourceNames'].append('railshot-' + self.app['environment_id'])
        with self.assertRaisesRegex(ValueError, 'CONTROL_ROLE_CHANGED'):
            lifecycle.grant_cleanup(self.binding, selected)

    def test_private_credential_without_shared_membership_is_blocked(self):
        shared, secret = self.shared_registration()
        cm = self.control.objects['argocd', 'configmap', 'railshot-credentials']
        policy = json.loads(cm['data']['policy.json'])
        for row in policy['targets']:
            if row['target_id'] == shared['target_id']:
                row['namespaces'].remove(self.app['application_id'])
        cm['data']['policy.json'] = json.dumps(policy); self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'APPLICATION_SHARED_CREDENTIAL_CHANGED'):
            self.plan()
        self.assertTrue(all(verb == 'get' for verb, _ in self.calls))

    def test_partial_shared_removal_never_deletes_namespace_or_replays(self):
        self.shared_registration(); plan = self.plan()
        native = runtime.argo.kubectl
        def fail_policy(*args, document=None):
            if args[2] == 'replace' and document['kind'] == 'ConfigMap':
                raise RuntimeError('uncertain policy write')
            return native(*args, document=document)
        with patch.object(runtime.argo, 'kubectl', side_effect=fail_policy):
            request, result = self.apply(plan)
        self.assertEqual(result['status'], 'unknown', result)
        self.execute.assert_not_called()
        self.assertEqual(result['steps'][-1]['name'], 'remove-renewal')
        before = list(self.calls)
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, request), result)
        self.assertEqual(self.calls, before)

    def interrupted_shared_scope(self, phase):
        shared, _ = self.shared_registration(); plan = self.plan()
        native = runtime.argo.kubectl
        def interrupt(*args, document=None):
            if phase == 'secret' and args[2:5] == ('patch', 'secret', shared['secret']):
                raise RuntimeError('secret CAS rejected')
            if phase == 'final-policy' and args[2] == 'replace' and document['kind'] == 'ConfigMap':
                rows = json.loads(document['data']['policy.json'])['targets']
                if not any(row['target_id'] == self.app['application_id'] for row in rows):
                    raise RuntimeError('final policy CAS rejected')
            return native(*args, document=document)
        with patch.object(runtime.argo, 'kubectl', side_effect=interrupt):
            request, result = self.apply(plan)
        self.assertEqual(result['status'], 'unknown', result)
        self.execute.assert_not_called()
        policy = runtime.credentials.validate_policy(json.loads(
            self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json']))
        environment = next(row for row in policy['targets'] if row['target_id'] == self.app['environment_id'])
        self.assertIn('previous_scope', environment)
        self.assertTrue(any(row['target_id'] == self.app['application_id'] for row in policy['targets']))
        # Either side of the interrupted CAS remains an explicitly accepted renewal scope.
        canonical = self.control.objects['argocd', 'secret', shared['secret']]
        runtime.credentials.registration(canonical, environment, lifecycle.time.time())
        before = list(self.calls)
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, request), result)
        self.assertEqual(self.calls, before)

    def test_secret_cas_failure_preserves_shared_renewal(self):
        self.interrupted_shared_scope('secret')

    def test_final_policy_failure_preserves_shared_renewal(self):
        self.interrupted_shared_scope('final-policy')

    def test_delete_preserves_other_app_and_replay_never_mutates_again(self):
        previous = copy.deepcopy(self.control.objects['argocd', 'configmap', 'railshot-credentials'])
        plan = self.plan()
        self.assertTrue(all(verb == 'get' for verb, _ in self.calls))
        request, result = self.apply(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['residuals'], [])
        app_id = self.app['application_id']
        self.assertFalse(any(key[2] == 'railshot-' + app_id for key in self.control.objects))
        policy = json.loads(self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['policy.json'])
        expected = [{**t, 'namespaces': [ns for ns in t['namespaces'] if ns != app_id]}
                    if t['target_id'] == self.app['environment_id'] else t
                    for t in json.loads(previous['data']['policy.json'])['targets'] if t['target_id'] != app_id]
        self.assertEqual(policy['targets'], expected)
        self.assertEqual(self.control.objects['argocd', 'configmap', 'railshot-credentials']['data']['kubeconfig'], 'preserve-me')
        self.assertEqual(runtime.read_private(self.home / 'lifecycle.json')['status'], 'deleted')
        before = list(self.calls)
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, request), result)
        self.assertEqual(self.calls, before)
        self.edge_plan.assert_called_once()  # Apply validates the saved provider plan instead of minting another.
        with self.assertRaisesRegex(ValueError, 'APPLICATION_LIFECYCLE_BLOCKED'):
            self.fixture.register()

    def test_stop_then_start_restores_explicit_snapshot(self):
        _, canonical = self.shared_registration(); shared_before = copy.deepcopy(canonical)
        _, stopped = self.apply(self.plan('stop'), 'stop')
        self.assertEqual(stopped['status'], 'succeeded', stopped)
        with self.assertRaisesRegex(ValueError, 'APPLICATION_LIFECYCLE_BLOCKED'):
            applications.assert_deployable(self.home)
        _, started = self.apply(self.plan('start'), 'start')
        self.assertEqual(started['status'], 'succeeded', started)
        self.assertEqual(self.execute.call_args.kwargs['stopped_inventory'], self.inventory)
        applications.assert_deployable(self.home)
        self.assertIn(self.app['application_id'], json.loads(self.fixture.fixture.variable['value']))
        self.assertEqual(canonical, shared_before)

    def test_expired_or_drifted_plan_never_mutates(self):
        plan = self.plan(); self.calls.clear()
        self.inventory['namespace']['uid'] = 'replaced'
        with self.assertRaisesRegex(ValueError, 'LIFECYCLE_PLAN_CHANGED'):
            self.apply(plan)
        self.execute.assert_not_called(); self.edge_execute.assert_not_called()
        self.assertTrue(all(verb == 'get' for verb, _ in self.calls))
        self.inventory['namespace']['uid'] = 'namespace-1'
        path = self.home / 'lifecycle/plans' / (plan['plan_id'] + '.json')
        saved = runtime.read_private(path); saved['expires_at'] = datetime.fromtimestamp(0, timezone.utc).isoformat()
        runtime.save(path, saved); plan['plan_hash'] = applications.digest(saved)
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'LIFECYCLE_PLAN_EXPIRED'):
            self.apply(plan)
        self.assertEqual(self.calls, [])

    def test_provider_preflight_failure_has_zero_control_writes(self):
        plan = self.plan(); self.calls.clear()
        self.edge_validate.side_effect = ValueError('drift')
        with self.assertRaises(ValueError):
            self.apply(plan)
        self.assertTrue(all(verb == 'get' for verb, _ in self.calls))
        self.execute.assert_not_called()
        self.assertFalse((self.home / 'lifecycle.json').exists())

    def test_partial_cleanup_is_unknown_and_cannot_replay(self):
        self.execute.side_effect = RuntimeError('private-secret-value')
        request, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['residuals'])
        self.assertNotIn('private-secret-value', json.dumps(result))
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, request), result)
        self.execute.assert_called_once()
        with self.assertRaisesRegex(ValueError, 'APPLICATION_LIFECYCLE_BLOCKED'):
            self.plan()

    def test_last_registration_removes_role_rules_instead_of_granting_all_secrets(self):
        cm = self.control.objects['argocd', 'configmap', 'railshot-credentials']
        policy = json.loads(cm['data']['policy.json'])
        # Exercise the pre-migration registration whose final app can empty the policy.
        self.control.objects['argocd', 'secret', 'railshot-' + self.app['application_id']]['metadata']['labels'][
            'argocd.argoproj.io/secret-type'] = 'cluster'
        policy['targets'] = [t for t in policy['targets'] if t['target_id'] == self.app['application_id']]
        cm['data']['policy.json'] = json.dumps(policy)
        role = self.control.objects['argocd', 'role', 'railshot-credentials']
        role['rules'][0]['resourceNames'] = [policy['targets'][0]['secret']]
        _, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.control.objects['argocd', 'role', 'railshot-credentials']['rules'], [])
        empty = {'version': 1, 'targets': []}
        runtime.credentials.validate_policy(empty)
        rendered = runtime.credentials.render(empty, 'ghcr.io/jasmin-softbank/railshot-api@sha256:' + 'a' * 64)
        self.assertEqual(next(item for item in rendered['items'] if item['kind'] == 'Role')['rules'], [])

    def test_start_never_marks_ready_before_public_health(self):
        _, stopped = self.apply(self.plan('stop'), 'stop')
        self.assertEqual(stopped['status'], 'succeeded')
        plan = self.plan('start')
        with patch.object(runtime.bridge, 'public_probe', return_value={'state': 'unverified'}), \
                patch.object(lifecycle.time, 'monotonic', side_effect=[0, 121]):
            _, result = self.apply(plan, 'start')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['error']['code'], 'APPLICATION_PUBLIC_HEALTH_UNVERIFIED')
        self.assertNotIn(self.app['application_id'], json.loads(self.fixture.fixture.variable['value']))
        self.assertEqual(runtime.read_private(self.home / 'lifecycle.json')['status'], 'unknown')

    def test_unstarted_plan_cannot_delete_a_registration_that_appeared_later(self):
        self.app = self.fixture.request('queued-app'); self.home = self.fixture.home('queued-app')
        plan = self.plan()
        self.assertEqual(self.fixture.register('queued-app')['status'], 'succeeded')
        with self.assertRaisesRegex(ValueError, 'LIFECYCLE_PLAN_CHANGED'):
            self.apply(plan)
        self.execute.assert_not_called(); self.edge_execute.assert_not_called()

    def test_unstarted_registration_deletion_only_writes_local_tombstone(self):
        self.app = self.fixture.request('queued-app')
        self.home = self.fixture.home('queued-app')
        request, result = self.apply(self.plan())
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.calls, [])
        self.edge_plan.assert_not_called(); self.edge_execute.assert_not_called(); self.execute.assert_not_called()
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, request), result)
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, {**request, 'phase': 'reconcile'}), result)
        self.assertEqual(lifecycle.lifecycle(self.fixture.config_path, {**request, 'phase': 'resume'}), result)
        with self.assertRaisesRegex(ValueError, 'APPLICATION_LIFECYCLE_BLOCKED'):
            self.fixture.register('queued-app')

    def test_active_argo_sync_and_missing_confirmation_block(self):
        app = next(obj for (_, kind, _), obj in self.control.objects.items() if kind == 'application')
        app['operation'] = {'sync': {}}
        plan = self.plan()  # Deletion preview may precede cancellation/quiescence.
        with self.assertRaisesRegex(ValueError, 'APPLICATION_SYNC_ACTIVE'):
            self.apply(plan)
        del app['operation']
        plan = self.plan()
        request = self.request(phase='apply', plan_id=plan['plan_id'], plan_hash=plan['plan_hash'], delete_data=False)
        with self.assertRaisesRegex(ValueError, 'DELETE_DATA_CONFIRMATION_REQUIRED'):
            lifecycle.lifecycle(self.fixture.config_path, request)
        self.edge_execute.assert_not_called()
        self.execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
