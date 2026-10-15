"""Boundary and orchestration checks; native executors are explicitly simulated."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import kubeadm
import run as ansible
import api
import kubeadm_readiness
import kubeadm_network


def request():
    def node(name, index):
        return {'id': name, 'resource_id': f'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-{index:08x}',
                'private_ipv4': f'10.20.0.{index}', 'availability_zone': 'ap-northeast-2a',
                'ssh': {'user': 'ubuntu', 'port': 22, 'identity_file': '/tmp/key', 'known_hosts_file': '/tmp/known',
                        'transport_ref': f'ssm:ap-northeast-2:i-{index:08x}'}}
    return {'schema_version': '1.0', 'request_id': 'install.1', 'operation': 'runtime.install',
            'target': {'id': 'prepared', 'provider': 'aws', 'account_id': '123456789012', 'placement': 'ap-northeast-2',
                       'os': 'linux', 'architecture': 'amd64', 'initialization': 'preconfigured'},
            'inventory': {'control_plane': [node('control', 1)], 'workers': [node('compute-a', 2), node('compute-b', 3)]},
            'timeout_seconds': 1800, 'runtime': {'profile': kubeadm.PROFILE, 'wait_timeout_seconds': 300,
                                               'storage': {'aws': {'filesystem_id': 'shared'}}, 'edge': '/tmp/edge.json'}}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.addons = patch.object(kubeadm, '_addon', return_value=types.SimpleNamespace(validate=lambda r: {'binding_sha256': 'a' * 64}))
        self.addons.start(); self.addCleanup(self.addons.stop)

    def test_malformed_json_shapes_are_structured_invalid_without_dispatch(self):
        malformed = [None, [], 'text', 7, {'runtime': []}, {'runtime': None},
                     {'runtime': {'profile': kubeadm.PROFILE}, 'target': []}]
        with patch.object(ansible, 'execute', side_effect=AssertionError('invalid input cannot execute')):
            for value in malformed:
                with self.subTest(value=value):
                    output = ansible.run(value, validate_only=True)
                    self.assertEqual(output['status'], 'invalid')
                    self.assertEqual(output['error']['code'], 'INVALID_REQUEST')
                    self.assertFalse(output['runtime_ready'])

    def test_scope_topology_network_and_resource_binding(self):
        ansible.validate(request())
        changes = [('inventory', lambda r: r['inventory']['workers'].pop()),
                   ('provider', lambda r: r['target'].update(provider='onprem')),
                   ('architecture', lambda r: r['target'].update(architecture='arm64')),
                   ('account', lambda r: r['target'].update(account_id='999999999999')),
                   ('az', lambda r: r['inventory']['workers'][0].update(availability_zone='us-east-1a')),
                   ('cidr', lambda r: r['runtime'].update(service_cidr='10.244.0.0/16')),
                   ('node-cidr', lambda r: r['runtime'].update(pod_cidr='10.20.0.0/16')),
                   ('transport', lambda r: r['inventory']['workers'][0]['ssh'].update(transport_ref='ssm:ap-northeast-2:i-00000003'))]
        for name, mutate in changes:
            with self.subTest(name=name):
                value = request(); mutate(value)
                with self.assertRaises(ValueError): ansible.validate(value)

    def test_gcp_region_and_individual_az(self):
        value = request(); value['target'].update(provider='gcp', account_id='project-abc', placement='asia-northeast3')
        for node, zone in zip(kubeadm.nodes(value), ['asia-northeast3-a', 'asia-northeast3-b', 'asia-northeast3-c']):
            node.update(availability_zone=zone, resource_id=f'projects/project-abc/zones/{zone}/instances/{node["id"]}')
            node['ssh']['transport_ref'] = f'iap:project-abc/{zone}/{node["id"]}'
        ansible.validate(value)
        value['inventory']['workers'][1]['ssh']['transport_ref'] = 'iap:project-abc/asia-northeast3-a/compute-b'
        with self.assertRaises(ValueError): ansible.validate(value)

    def test_inventory_and_lock_include_all_members(self):
        value = request(); inventory = kubeadm.inventory(value, {'control': 4101, 'compute-a': 4102, 'compute-b': 4103})
        groups = inventory['all']['children']
        self.assertEqual(groups['kubeadm_control']['hosts']['control']['ansible_port'], 4101)
        self.assertEqual(groups['kubeadm_compute']['hosts']['compute-b']['ansible_port'], 4103)
        keys = ansible.lock_keys(value)
        for node in kubeadm.nodes(value): self.assertIn('resource:' + node['resource_id'], keys)
        self.assertIn('storage:aws:123456789012:shared', keys)

    def test_validation_never_marks_readiness(self):
        with patch.object(ansible, 'forwarded_port', side_effect=AssertionError('no remote validation')):
            output = ansible.run(request(), validate_only=True)
        self.assertEqual(output['status'], 'validated')
        self.assertFalse(any(output[key] for key in output if key.endswith('_ready') or key == 'public_http_verified'))

    def simulated(self, directory, *, stage_failure=None, missing_receipt=None):
        active, closed, calls = set(), [], []
        @contextmanager
        def tunnel(reference, deadline):
            active.add(reference)
            try: yield 4200 + len(active)
            finally: active.remove(reference); closed.append(reference)
        def runner(argv, timeout, env):
            if argv[0] == 'ssh': return 0
            self.assertEqual(len(active), 3)
            extra = json.loads(Path(argv[-1][1:]).read_text())
            stage = Path(argv[3]).stem.replace('kubeadm-', '')
            calls.append(stage)
            if stage == stage_failure: return 2
            if stage != missing_receipt:
                fields = {stage + '_ready': True, 'node_ids': [n['id'] for n in extra['railshot_expected_nodes']]}
                if stage == 'storage': fields = {key: True for key in ('storage_backend_ready', 'storage_csi_ready', 'storage_pvc_ready', 'storage_ready')}
                Path(extra['railshot_receipt_path']).write_text(json.dumps({
                    'request_id': extra['railshot_request_id'], 'target_id': extra['railshot_target_id'],
                    'nonce': extra['railshot_nonce'], 'stage': stage, **fields}))
            return 0
        def storage(*args, **kwargs):
            self.assertEqual(len(active), 3)
            return {'storage_backend_ready': True, 'variables': {'railshot_storage': {}}}
        def edge(*args, **kwargs):
            self.assertEqual(len(active), 3)
            calls.append('edge'); return {'edge_ready': True, 'public_http_verified': False}
        with patch.object(ansible, 'private_file'), patch.object(kubeadm.shutil, 'which', return_value='/fake/ansible-playbook'), patch.object(ansible, 'forwarded_port', side_effect=tunnel):
            result = kubeadm.run(request(), state_dir=directory, runner=runner, storage_prepare=storage, edge_provision=edge)
        self.assertEqual(len(closed), 3); self.assertEqual(active, set())
        return result, calls

    def test_full_orchestration_replay_preserves_truthful_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.simulated(directory)
            self.assertEqual(calls, ['guest', 'runtime', 'storage', 'edge'])
            self.assertEqual(result['status'], 'succeeded'); self.assertTrue(result['storage_ready'])
            self.assertFalse(result['public_http_verified']); self.assertFalse(result['application_ready'])
            with patch.object(ansible, 'forwarded_port', side_effect=AssertionError('replay cannot execute')):
                replay = kubeadm.run(request(), state_dir=directory)
            self.assertTrue(replay['replayed'])

    def test_registry_snapshots_bind_provider_account_region_and_actual_az(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'version': 1, 'cluster_id': 'prepared', 'provider': 'aws', 'account_id': '123456789012',
                      'region': 'ap-northeast-2', 'members': [], 'runtime': request()['runtime']}
            descriptors = []
            for node in kubeadm.nodes(request()):
                descriptor = {'schema_version': 'v1', 'execution_driver': 'terraform', 'provider_kind': 'aws',
                              'architecture': 'x86_64', 'target_id': node['id'], 'resource_id': node['resource_id'],
                              'transport_ref': node['ssh']['transport_ref'], 'addresses': {'private': node['private_ipv4']},
                              'location': {'region': 'ap-northeast-2', 'zone': node['availability_zone']}}
                path = root / (node['id'] + '.json'); path.write_text(json.dumps(descriptor)); path.chmod(0o600)
                descriptors.append((descriptor, path))
                config['members'].append({'id': node['id'], 'role': 'control_plane' if node['id'] == 'control' else 'workers',
                    'availability_zone': node['availability_zone'], 'ssh': {k: v for k, v in node['ssh'].items() if k != 'transport_ref'},
                    'descriptor_file': str(path)})
            result = kubeadm.from_registry(config, request_id='bind.1')
            self.assertEqual(len(result['inventory']['workers']), 2)
            descriptor, path = descriptors[-1]
            for field, bad in [('zone', 'ap-northeast-2b'), ('region', 'us-east-1')]:
                changed = copy.deepcopy(descriptor); changed['location'][field] = bad; path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): kubeadm.from_registry(config, request_id='bind.2')
            changed = copy.deepcopy(descriptor); changed['resource_id'] = changed['resource_id'].replace('123456789012', '999999999999')
            path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError): kubeadm.from_registry(config, request_id='bind.3')

    def test_api_registry_merge_and_effective_edge_hash_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            config = root / 'targets.json'
            config.write_text(json.dumps({'version': 1, 'targets': {'prepared': {'profile': kubeadm.PROFILE,
                'cluster_file': str(root / 'cluster.json'), 'purpose': 'runtime'}}})); config.chmod(0o600)
            cluster = root / 'cluster.json'; cluster.write_text('{}'); cluster.chmod(0o600)
            jobs = api.Jobs(config, root / 'state')
            try:
                with patch.object(kubeadm, 'from_registry', side_effect=AssertionError('unsupported operation cannot resolve snapshots')):
                    with self.assertRaises(api.APIError) as unsupported:
                        jobs.prepare({'request_id': 'guest.1', 'target_id': 'prepared', 'operation': 'guest.check'})
                self.assertEqual(unsupported.exception.status, 400)
                self.assertEqual(unsupported.exception.code, 'KUBEADM_OPERATION_UNSUPPORTED')
                body = {'request_id': 'install.1', 'target_id': 'prepared', 'operation': 'runtime.install',
                        'parameters': {'wait_timeout_seconds': 500}}
                with patch.object(kubeadm, 'from_registry', return_value=request()):
                    prepared = jobs.prepare(body)
                self.assertEqual(prepared['runtime']['profile'], kubeadm.PROFILE)
                self.assertEqual(prepared['runtime']['wait_timeout_seconds'], 500)
                with patch.object(jobs, 'prepare', return_value=prepared), patch.object(jobs.pool, 'submit'):
                    status, _ = jobs.submit(body)
                    self.assertEqual(status, 202)
                    jobs.records['install.1']['status'] = 'succeeded'; jobs.active = None
                    self.assertEqual(jobs.submit(body)[0], 200)
                    with patch.object(kubeadm, '_addon', return_value=types.SimpleNamespace(validate=lambda r: {'binding_sha256': 'b' * 64})):
                        with self.assertRaises(api.APIError) as error: jobs.submit(body)
                    self.assertEqual(error.exception.code, 'REQUEST_ID_CONFLICT')
            finally: jobs.close()

    def test_referenced_edge_content_changes_conflict_on_cli_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.simulated(directory)
            self.assertEqual(result['status'], 'succeeded')
            with patch.object(kubeadm, '_addon', return_value=types.SimpleNamespace(validate=lambda r: {'binding_sha256': 'b' * 64})):
                replay = kubeadm.run(request(), state_dir=directory)
            self.assertEqual(replay['error']['code'], 'REQUEST_ID_CONFLICT')

    def test_storage_failure_keeps_runtime_ready_and_skips_edge(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.simulated(directory, stage_failure='storage')
            self.assertEqual(calls, ['guest', 'runtime', 'storage'])
            self.assertEqual(result['status'], 'failed'); self.assertTrue(result['runtime_ready'])
            self.assertFalse(result['storage_ready']); self.assertFalse(result['edge_ready'])
            self.assertFalse(result['error']['outcome_unknown'])

    def test_lost_receipt_blocks_new_alias_request(self):
        with tempfile.TemporaryDirectory() as directory:
            result, calls = self.simulated(directory, missing_receipt='runtime')
            self.assertTrue(result['error']['outcome_unknown'])
            changed = request(); changed['request_id'] = 'install.2'; changed['target']['id'] = 'alias'
            output = kubeadm.run(changed, state_dir=directory)
            self.assertEqual(output['error']['code'], 'PREVIOUS_OUTCOME_UNKNOWN')

    def test_receipt_requires_exact_requested_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            proof = Path(directory) / 'proof.json'
            proof.write_text(json.dumps({'request_id': 'install.1', 'target_id': 'prepared', 'stage': 'runtime',
                                         'nonce': 'n', 'runtime_ready': True, 'node_ids': ['other']}))
            with self.assertRaises(ValueError): kubeadm.receipt(proof, request(), 'runtime', 'n')

    def test_network_failure_blocks_acceptance_and_cleans_only_owned_namespace(self):
        for scenario in ('ok', 'first_allowed_fail', 'last_allowed_fail', 'exec_denied_fail'):
            network_ok = scenario == 'ok'
            allowed_count = 0
            calls = []
            def native(argv, **kwargs):
                nonlocal allowed_count
                calls.append((argv, kwargs.get('input')))
                args = argv[3:]
                output, code = '{}', 0
                if args[:2] == ['create', '-f']:
                    output = json.dumps({'metadata': {'uid': 'probe-uid'}})
                elif 'get' in args and 'pod' in args:
                    name = args[args.index('pod') + 1]
                    output = json.dumps({'spec': {'nodeName': 'compute-a' if name == 'server' else 'compute-b'},
                                         'status': {'podIP': '10.244.1.2'}})
                elif 'exec' in args and 'denied' in args:
                    output = 'BLOCKED'
                    code = 1 if scenario == 'exec_denied_fail' else 0
                elif 'exec' in args and 'wget' in args:
                    allowed_count += 1
                    code = 1 if (scenario == 'first_allowed_fail' or (scenario == 'last_allowed_fail' and allowed_count == 3)) else 0
                    output = 'railshot-network-ready' if not code else ''
                return types.SimpleNamespace(stdout=output, returncode=code)
            if network_ok:
                result = kubeadm_network.verify(['compute-a', 'compute-b'], runner=native)
                self.assertTrue(result['network_policy_verified'])
            else:
                with self.assertRaises(ValueError): kubeadm_network.verify(['compute-a', 'compute-b'], runner=native)
            delete = [(argv, document) for argv, document in calls if 'delete' in argv]
            self.assertEqual(len(delete), 1)
            self.assertEqual(json.loads(delete[0][1])['preconditions'], {'uid': 'probe-uid'})

    def test_actual_readiness_rejects_foreign_or_unschedulable_node(self):
        expected = {'nodes': [{key: n[key] for key in ('id', 'private_ipv4')} for n in kubeadm.nodes(request())],
                    'workers': ['compute-a', 'compute-b'], 'version': 'v1.36.5'}
        items = []
        for n in expected['nodes']:
            control = n['id'] == 'control'
            items.append({'metadata': {'name': n['id'], 'labels': {'node-role.kubernetes.io/control-plane': ''} if control else {}},
                          'spec': {'taints': [{'key': 'node-role.kubernetes.io/control-plane', 'effect': 'NoSchedule'}] if control else []},
                          'status': {'nodeInfo': {'architecture': 'amd64', 'kubeletVersion': 'v1.36.5'},
                                     'addresses': [{'type': 'InternalIP', 'address': n['private_ipv4']}], 'conditions': [{'type': 'Ready', 'status': 'True'}]}})
        self.assertTrue(kubeadm_readiness.check_nodes({'items': items}, expected))
        bad = copy.deepcopy(items); bad[1]['status']['addresses'][0]['address'] = '10.20.0.99'
        with self.assertRaises(ValueError): kubeadm_readiness.check_nodes({'items': bad}, expected)
        bad = copy.deepcopy(items); bad[0]['spec']['taints'] = []
        with self.assertRaises(ValueError): kubeadm_readiness.check_nodes({'items': bad}, expected)


if __name__ == '__main__': unittest.main()
