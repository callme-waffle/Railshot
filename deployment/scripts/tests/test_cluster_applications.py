"""Offline ownership/state contracts. No provider or SSH calls are made."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import applications
import cluster_applications as routes
import cluster_edge
import environment


class SharedRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = 'app-' + 'a' * 24
        self.route = {'host': 'demo.example.com', 'node_port': 30100, 'health_path': '/health', 'priority': 100,
                      'target_private_ip': '10.0.0.2', 'target_security_group_id': 'sg-workers'}
        self.values = {'vpc_id': 'vpc-owned', 'compute_nodes': {
            'worker-a': {'private_ip': '10.0.0.2', 'security_group_id': 'sg-workers'},
            'worker-b': {'private_ip': '10.0.0.3', 'security_group_id': 'sg-workers'}},
            'routes': {self.app: self.route}}
        self.record = {'state': {'owned_resources': {'aws_lb.app': 'owned-lb'}}, 'apps': {}, 'values': self.values}

    def plan(self, creates=()):
        return {'variables': {key: {'value': value} for key, value in self.values.items()}, 'resource_changes': [
            {'address': 'aws_lb.app', 'type': 'aws_lb', 'mode': 'managed', 'change': {
                'actions': ['no-op'], 'before': {'id': 'owned-lb'}, 'after': {'id': 'owned-lb'}}}, *creates]}

    def attachment(self, address, ip):
        return {'address': address, 'type': 'aws_lb_target_group_attachment', 'mode': 'managed', 'change': {
            'actions': ['create'], 'before': None, 'after': {'target_id': ip, 'port': 30100}}}

    def check(self, document, action='apply'):
        return routes.checked_plan(document, self.record, self.values, 'aws', self.app, self.route, action)

    def test_two_workers_only_and_control_plane_rejected(self):
        primary = 'aws_lb_target_group_attachment.app[' + json.dumps(self.app) + ']'
        secondary = 'aws_lb_target_group_attachment.compute[' + json.dumps(self.app + ':worker-b') + ']'
        self.assertEqual(len(self.check(self.plan([self.attachment(primary, '10.0.0.2'), self.attachment(secondary, '10.0.0.3')]))), 2)
        with self.assertRaisesRegex(applications.RegistrationError, 'NON_COMPUTE_TARGET'):
            self.check(self.plan([self.attachment(secondary, '10.0.0.1')]))

    def test_foreign_app_and_shared_lb_delete_are_rejected(self):
        foreign = self.attachment('aws_lb_target_group_attachment.app["other-app"]', '10.0.0.2')
        with self.assertRaisesRegex(applications.RegistrationError, 'SHARED_RESOURCE_CHANGED'):
            self.check(self.plan([foreign]))
        document = self.plan(); document['resource_changes'][0]['change'].update(actions=['delete'], after=None)
        with self.assertRaisesRegex(applications.RegistrationError, 'SHARED_RESOURCE_CHANGED'):
            self.check(document, 'delete')

    def test_omitted_owned_resource_and_cross_scope_variables_are_rejected(self):
        document = self.plan(); document['resource_changes'] = []
        with self.assertRaisesRegex(applications.RegistrationError, 'OWNED_RESOURCE_OMITTED'):
            self.check(document)
        document = self.plan(); document['variables']['vpc_id']['value'] = 'vpc-foreign'
        with self.assertRaisesRegex(applications.RegistrationError, 'VARIABLES_CHANGED'):
            self.check(document)

    def test_other_app_route_is_unchanged_and_requested_route_deletes_only_its_id(self):
        address = 'aws_lb_listener_rule.app[' + json.dumps(self.app) + ']'
        self.record['state']['owned_resources'][address] = 'owned-rule'
        document = self.plan([{'address': address, 'type': 'aws_lb_listener_rule', 'mode': 'managed', 'change': {
            'actions': ['delete'], 'before': {'id': 'owned-rule'}, 'after': None}}])
        self.assertEqual(self.check(document, 'delete'), [{'address': address, 'actions': ['delete']}])
        document['resource_changes'][-1]['change']['before']['id'] = 'different-rule'
        with self.assertRaisesRegex(applications.RegistrationError, 'FOREIGN_DELETE'):
            self.check(document, 'delete')

    def test_new_route_cannot_reuse_bootstrap_host_or_port(self):
        prepared = {'config': {'provider': 'aws', 'worker_bindings': {'worker-arn': {'security_group_id': 'sg-workers'}}},
                    'workers': [{'resource_id': 'worker-arn', 'private_ipv4': '10.0.0.2'}]}
        request = {'application_id': 'app-' + 'b' * 24, 'hostname': 'demo.example.com', 'node_port': 30200, 'health_path': '/'}
        with self.assertRaisesRegex(applications.RegistrationError, 'ROUTE_CONFLICT'):
            routes.new_route(prepared, self.record, request)

    def test_unknown_apply_is_not_replayed_and_native_applied_receipt_skips_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); work = root / 'app-plans' / 'review'; work.mkdir(parents=True, mode=0o700)
            work.parent.chmod(0o700)
            plan_file = work / 'change.tfplan'; plan_file.write_bytes(b'reviewed-plan'); plan_file.chmod(0o600)
            plan = {'private': {'work': str(work), 'state': {'old': True}, 'candidate': self.values,
                               'plan_sha256': hashlib.sha256(plan_file.read_bytes()).hexdigest()}}
            environment.save(work / 'review.json', plan)
            environment.save(work / 'intent.json', {'phase': 'applying'})
            record = {'state': {'old': True}, 'values': self.values}
            calls = []
            with self.assertRaisesRegex(applications.RegistrationError, 'RECONCILIATION_REQUIRED'):
                routes.apply_native({}, root, record, plan, lambda *args, **kwargs: calls.append(args))
            self.assertEqual(calls, [])
            environment.save(work / 'intent.json', {'phase': 'native_applied', 'state': record['state']})
            self.assertEqual(routes.apply_native({}, root, record, plan, lambda *args, **kwargs: calls.append(args)), record)
            self.assertEqual(calls, [])

    def test_application_namespace_blocks_host_network_and_privileged_pods(self):
        documents = environment.runtime_documents({'namespace': self.app, 'image_pull_secret': {'name': 'ghcr-pull'}}, self.app, {}, None)
        self.assertEqual(documents[0]['metadata']['labels']['pod-security.kubernetes.io/enforce'], 'baseline')
        self.assertFalse(any('namespaces' in row.get('resources', []) for document in documents for row in document.get('rules', [])))


if __name__ == '__main__':
    unittest.main()

class EdgeTeardownRecoveryTests(unittest.TestCase):
    def test_refresh_can_finish_remaining_owned_subset_but_rejects_replacement(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = {'binding_sha256': 'scope', 'config': {'provider': 'aws'}}
            record = {'apps': {}, 'state': {'owned_resources': {'aws_lb.app': 'old-lb', 'aws_lb_target_group.app': 'old-tg'}}}
            routes.runtime.save(root / 'teardown.json', {'status': 'unknown', 'environment_id': 'env-test',
                'binding_sha256': 'scope', 'record': record, 'dns': []})
            calls = []
            actual_id = ['foreign-replacement']
            def runner(argv, **kwargs):
                calls.append(argv[1])
                if argv[1] == 'plan':
                    (root / 'teardown.tfplan').write_bytes(b'approved-native-plan')
                    return ''
                if argv[1] == 'show':
                    return json.dumps({'resource_changes': [{'address': 'aws_lb.app', 'mode': 'managed', 'change': {
                        'actions': ['delete'], 'before': {'id': actual_id[0]}, 'after': None}}]})
                if argv[1] == 'apply':
                    routes.runtime.save(root / 'terraform.tfstate', {'resources': []})
                    return ''
                raise AssertionError(argv)
            with patch.object(routes, 'scope', return_value=({}, prepared, root)), \
                 patch.object(routes, 'executor', side_effect=lambda *args: (ExitStack(), runner, None)):
                with self.assertRaisesRegex(Exception, 'OWNERSHIP_CHANGED'):
                    routes.teardown({}, 'env-test')
                self.assertNotIn('apply', calls)
                actual_id[0] = 'old-lb'
                result = routes.teardown({}, 'env-test')
                self.assertEqual(result['status'], 'succeeded')
                self.assertEqual(calls.count('apply'), 1)

if __name__ == '__main__': unittest.main()
