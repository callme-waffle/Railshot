"""Read-only golden-path observations, negative readiness cases and shared deadline."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo_preflight import Observer, ObservationError, inspect
from input_adapter import parse_input
from test_contract import fixture

DIGEST = 'docker.io/library/nginx@sha256:' + 'a' * 64


class PreparedNode:
    def __init__(self):
        self.calls = []
        self.nodes = {'items': [{'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                    'addresses': [{'type': 'InternalIP', 'address': '192.0.2.10'}]}}]}
        self.ds = {'metadata': {'generation': 1}, 'status': {'desiredNumberScheduled': 1,
                   'numberReady': 1, 'updatedNumberScheduled': 1, 'observedGeneration': 1}}
        self.pod = {'kind': 'Pod', 'metadata': {'name': 'sample', 'uid': 'pod-1', 'labels': {'app': 'sample'}},
                    'status': {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}],
                               'containerStatuses': [{'imageID': DIGEST}]}}
        self.resources = {'items': [
            {'kind': 'Deployment', 'metadata': {'name': 'railshot-workload', 'generation': 1},
             'spec': {'replicas': 1, 'selector': {'matchLabels': {'app': 'sample'}},
                      'template': {'metadata': {'labels': {'app': 'sample'}},
                                   'spec': {'containers': [{'ports': [{'name': 'http', 'containerPort': 80}]}]}}},
             'status': {'readyReplicas': 1, 'updatedReplicas': 1, 'availableReplicas': 1, 'observedGeneration': 1}},
            {'kind': 'Service', 'metadata': {'name': 'railshot-workload'},
             'spec': {'type': 'NodePort', 'selector': {'app': 'sample'},
                      'ports': [{'port': 80, 'nodePort': 30080, 'targetPort': 'http'}]}},
            self.pod,
            {'kind': 'EndpointSlice', 'metadata': {'labels': {'kubernetes.io/service-name': 'railshot-workload'}},
             'endpoints': [{'conditions': {'ready': True}, 'targetRef': {'uid': 'pod-1'}}]}]}
        self.http = 'Railshot Runtime OK\n\n200'

    def run(self, argv):
        self.calls.append(argv)
        return self.http if argv[0] == 'curl' else 'active\n'

    def kube(self, *args):
        self.calls.append(['k3s', 'kubectl', 'get', *args])
        if args[0] == 'nodes': return self.nodes
        if args[0] == 'daemonset': return self.ds
        if args[0] == 'pods': return {'items': [copy.deepcopy(self.pod)]}
        if args[0] == 'namespace': return {'status': {'phase': 'Active'}}
        return self.resources


class DemoPreflightTests(unittest.TestCase):
    def setUp(self):
        self.spec = parse_input(fixture()).spec
        self.node = PreparedNode()

    def test_golden_path_checks_service_backend_and_actual_image(self):
        result = inspect(self.spec, self.node, expected_image_id=DIGEST)
        self.assertEqual(result['runtime_status'], 'ready')
        self.assertEqual(result['endpoint'], 'http://192.0.2.10:30080/')
        self.assertEqual(len(result['checks']), 11)
        self.assertTrue(all(c['status'] == 'pass' for c in result['checks']))
        self.assertTrue(result['read_only'])
        self.assertEqual(result['public_exposure'], 'not_checked')
        for argv in self.node.calls:
            self.assertNotIn('apply', argv)
            self.assertNotIn('delete', argv)
            self.assertNotIn('exec', argv)
            self.assertNotIn('install', argv)

    def test_provider_context_does_not_change_observations(self):
        results = []
        for provider in ('aws', 'gcp', 'openstack'):
            data = fixture(provider)
            result = inspect(parse_input(data).spec, PreparedNode())
            results.append((result['runtime_status'], result['checks'], result['endpoint']))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    def test_unready_node_or_cilium_or_pod_is_not_ready(self):
        for field in ('node', 'cilium', 'pod', 'generation'):
            with self.subTest(field=field):
                node = PreparedNode()
                if field == 'node': node.nodes['items'][0]['status']['conditions'][0]['status'] = 'False'
                if field == 'cilium': node.ds['status']['numberReady'] = 0
                if field == 'pod': node.pod['status']['conditions'][0]['status'] = 'False'
                if field == 'generation': node.resources['items'][0]['metadata']['generation'] = 2
                self.assertEqual(inspect(self.spec, node)['runtime_status'], 'not_ready')

    def test_wrong_service_target_selector_or_port_is_not_ready(self):
        for field, value in (('targetPort', 8080), ('nodePort', 30081), ('selector', {'app': 'other'})):
            with self.subTest(field=field):
                node = PreparedNode()
                svc = node.resources['items'][1]['spec']
                if field == 'selector': svc[field] = value
                else: svc['ports'][0][field] = value
                self.assertEqual(inspect(self.spec, node)['runtime_status'], 'not_ready')

    def test_healthy_unrelated_backend_cannot_pass(self):
        self.node.resources['items'][3]['endpoints'][0]['targetRef']['uid'] = 'other-pod'
        self.assertEqual(inspect(self.spec, self.node)['runtime_status'], 'not_ready')

    def test_wrong_image_digest_is_not_ready(self):
        result = inspect(self.spec, self.node, expected_image_id=DIGEST.replace('a' * 64, 'b' * 64))
        self.assertEqual(result['runtime_status'], 'not_ready')

    def test_http_errors_redirects_and_unexpected_sample_body_fail(self):
        for output in ('Railshot Runtime OK\n500', '\n302', 'wrong backend\n200'):
            with self.subTest(output=output):
                self.node.http = output
                self.assertEqual(inspect(self.spec, self.node)['runtime_status'], 'not_ready')

    def test_missing_workload_is_unknown_not_fake_pass(self):
        self.node.resources['items'] = []
        result = inspect(self.spec, self.node)
        self.assertEqual(result['runtime_status'], 'unknown')
        self.assertEqual(result['checks'][-1]['status'], 'unknown')
        self.assertFalse(any(args[0] == 'curl' for args in self.node.calls))

    def test_command_timeout_stops_observation(self):
        with patch.object(self.node, 'run', side_effect=ObservationError('deadline exceeded')):
            result = inspect(self.spec, self.node)
        self.assertEqual(result['runtime_status'], 'unknown')
        self.assertEqual(len(result['checks']), 1)

    def test_deadline_bounds_real_subprocess(self):
        start = time.monotonic()
        observer = Observer(0.15)
        with self.assertRaises(ObservationError):
            observer.run([sys.executable, '-c', 'import time; time.sleep(10)'])
        self.assertLess(time.monotonic() - start, 2)
        with patch('demo_preflight.subprocess.run') as run, self.assertRaises(ObservationError):
            observer.run(['systemctl', 'is-active', 'k3s'])
        run.assert_not_called()

    def test_http_has_short_deadline_no_redirect_or_proxy(self):
        inspect(self.spec, self.node)
        command = self.node.calls[-1]
        self.assertEqual(command[0], 'curl')
        self.assertEqual(command[command.index('--max-time') + 1], '3')
        self.assertEqual(command[command.index('--noproxy') + 1], '*')
        self.assertNotIn('--location', command)


if __name__ == '__main__':
    unittest.main()
