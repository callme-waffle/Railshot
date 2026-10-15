"""Management endpoint identity checks; network reachability is not simulated."""
import copy
from pathlib import Path
import tempfile
import unittest

import environment


class ClusterManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.nodes = [{'id': 'node-' + str(i), 'resource_id': 'instance-' + str(i), 'private_ipv4': '10.1.0.' + str(i), 'ssh': {}} for i in (1, 2, 3)]
        self.cluster = {'members': []}
        self.addresses = {}
        for index, node in enumerate(self.nodes, 1):
            path = self.home / (node['id'] + '.json')
            address = '8.8.4.' + str(index)
            environment.save(path, {'resource_id': node['resource_id'], 'addresses': {'private': node['private_ipv4'], 'public': address}})
            self.cluster['members'].append({'id': node['id'], 'descriptor_file': str(path)})
            self.addresses[node['id']] = address
        self.request = {'target': {'provider': 'aws'}, 'inventory': {'control_plane': self.nodes[:1], 'workers': self.nodes[1:]}}
        self.selected = {'management_endpoint': 'https://8.8.4.1:6443', 'metrics_endpoints': self.addresses}
        self.resource = {'addresses': {'private': '10.1.0.1'}}

    def test_observed_control_and_three_node_routes_keep_private_tls_identity(self):
        environment.cluster_management(self.selected, self.cluster, self.request, self.resource)
        self.assertEqual(self.resource['management_endpoint'], 'https://8.8.4.1:6443')
        self.assertEqual(self.resource['addresses']['private'], '10.1.0.1')
        self.assertEqual(self.resource['node_metrics_addresses'], ['8.8.4.1', '8.8.4.2', '8.8.4.3'])

    def test_foreign_endpoint_or_missing_worker_does_not_become_a_route(self):
        for selected in ({**self.selected, 'management_endpoint': 'https://8.8.8.8:6443'},
                         {**self.selected, 'metrics_endpoints': {key: value for key, value in self.addresses.items() if key != 'node-3'}},
                         {**self.selected, 'metrics_endpoints': {**self.addresses, 'node-2': '8.8.8.8'}}):
            with self.subTest(selected=selected), self.assertRaises(Exception):
                environment.cluster_management(selected, self.cluster, self.request, copy.deepcopy(self.resource))

    def test_changed_native_resource_cannot_reuse_an_endpoint(self):
        path = self.home / 'node-1.json'
        snapshot = environment.read_private(path); snapshot['resource_id'] = 'another-instance'; environment.save(path, snapshot)
        with self.assertRaisesRegex(Exception, 'node binding'):
            environment.cluster_management(self.selected, self.cluster, self.request, self.resource)


if __name__ == '__main__': unittest.main()
