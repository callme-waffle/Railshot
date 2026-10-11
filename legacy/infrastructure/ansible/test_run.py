"""Offline boundary tests: fake receipts are local test fixtures, not deployment evidence."""
import copy
import configparser
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('railshot_ansible', HERE / 'run.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
ROOT = HERE.parents[1]


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = patch.object(adapter, 'STATE_DIR', Path(self.temp.name) / 'state')
        state.start(); self.addCleanup(state.stop)
        self.request = json.loads((ROOT / 'examples/ansible/runtime-single-node.json').read_text())
        for name, field in [('identity', 'identity_file'), ('known_hosts', 'known_hosts_file')]:
            p = Path(self.temp.name) / name
            p.write_text('offline fixture, not a credential\n'); p.chmod(0o600)
            self.request['inventory']['control_plane'][0]['ssh'][field] = str(p)
        self.commands = []

    def runner(self, argv, timeout, env):
        self.commands.append(argv)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, self.request['timeout_seconds'])
        self.assertEqual(env['ANSIBLE_HOST_KEY_CHECKING'], 'True')
        variables = json.loads(Path(argv[-1][1:]).read_text())
        stage = Path(argv[3]).stem
        Path(variables['railshot_receipt_path']).write_text(json.dumps({
            'request_id': variables['railshot_request_id'], 'target_id': variables['railshot_target_id'],
            'node_id': variables['k3s_node_name'], 'nonce': variables['railshot_nonce'],
            'stage': stage, stage + '_ready': True}))
        return 0

    def test_openstack_uses_get_result_project_and_one_approved_private_address(self):
        server = json.loads((ROOT / 'examples/ansible/openstack-server.json').read_text())
        profile = {'request_id': 'os-001', 'operation': 'runtime.install', 'target_id': 'demo-openstack',
                   'resource_id': server['id'], 'project_id': server['project_id'], 'management_network': 'management',
                   'placement': 'onprem-a', 'architecture': 'amd64', 'initialization': 'cloud-init',
                   'ssh': {k: v for k, v in self.request['inventory']['control_plane'][0]['ssh'].items() if k != 'port'}}
        converted = adapter.from_openstack(server, **profile)
        node = converted['inventory']['control_plane'][0]
        self.assertEqual(node['ssh']['port'], 22)
        self.assertEqual(node['private_ipv4'], '192.168.50.10')
        invalid = [{'id': 'another-resource'}, {'project_id': 'another-project'}, {'status': 'BUILD'},
                   {'addresses': []}, {'addresses': server['addresses'] * 2},
                   {'addresses': [{'network': 'public', 'address': '192.168.50.10', 'version': 4}]},
                   {'addresses': [{'network': 'management', 'address': '8.8.8.8', 'version': 4}]},
                   {'addresses': [{'network': 'management', 'address': '192.168.50.10', 'version': True}]}]
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(adapter.ContractError):
                adapter.from_openstack({**server, **fields}, **profile)
        with self.assertRaises(adapter.ContractError):
            adapter.from_openstack({'resource_id': server['id'], 'status': 'accepted'}, **profile)

    def test_local_receipts_only_admit_matching_completed_stages(self):
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=self.runner)
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['guest_ready']); self.assertTrue(result['runtime_ready'])
        self.assertFalse(result['application_ready']); self.assertFalse(result['public_http_verified'])
        self.assertEqual([Path(x[3]).name for x in self.commands], ['guest.yml', 'runtime.yml'])
        self.assertFalse(any('site.yml' in x for cmd in self.commands for x in cmd))

    def test_exit_zero_without_readiness_receipt_is_not_success(self):
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=lambda *_: 0)
        self.assertEqual(result['error']['code'], 'READINESS_UNPROVEN')
        self.assertFalse(result['guest_ready']); self.assertFalse(result['runtime_ready'])

    def test_wrong_request_receipt_is_rejected(self):
        def stale(argv, timeout, env):
            self.runner(argv, timeout, env)
            variables = json.loads(Path(argv[-1][1:]).read_text())
            p = Path(variables['railshot_receipt_path']); receipt = json.loads(p.read_text())
            receipt['request_id'] = 'another-attempt'; p.write_text(json.dumps(receipt))
            return 0
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=stale)
        self.assertEqual(result['error']['code'], 'READINESS_UNPROVEN')

    def test_guest_failure_prevents_runtime(self):
        calls = []
        def failed(*args): calls.append(args); return 4
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=failed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['error']['code'], 'GUEST_CHECK_FAILED')
        self.assertFalse(result['runtime_ready'])

    def test_partial_runtime_failure_retains_only_guest_readiness(self):
        def partial(argv, timeout, env):
            return self.runner(argv, timeout, env) if Path(argv[3]).stem == 'guest' else 2
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=partial)
        self.assertTrue(result['guest_ready']); self.assertFalse(result['runtime_ready'])
        self.assertTrue(result['error']['outcome_unknown']); self.assertFalse(result['error']['retryable'])

    def test_runtime_timeout_is_bounded_and_not_retried(self):
        def timeout(argv, seconds, env):
            if Path(argv[3]).stem == 'guest': return self.runner(argv, seconds, env)
            raise subprocess.TimeoutExpired(argv, seconds)
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            result = adapter.run(self.request, runner=timeout)
        self.assertEqual(result['error']['code'], 'EXECUTION_TIMEOUT')
        self.assertTrue(result['error']['outcome_unknown']); self.assertTrue(result['guest_ready'])

    def test_validation_never_executes_or_claims_readiness(self):
        result = adapter.run(self.request, validate_only=True, runner=lambda *_: self.fail('must not run'))
        self.assertEqual(result['status'], 'validated')
        self.assertFalse(result['guest_ready']); self.assertFalse(result['runtime_ready'])

    def test_arbitrary_playbook_extra_vars_and_shell_paths_are_rejected(self):
        for field in ('playbook', 'extra_vars', 'shell'):
            request = copy.deepcopy(self.request); request[field] = 'anything'
            self.assertEqual(adapter.run(request, validate_only=True)['status'], 'invalid')
        self.request['inventory']['control_plane'][0]['ssh']['identity_file'] = '/tmp/key;touch/tmp/marker'
        self.assertEqual(adapter.run(self.request, validate_only=True)['status'], 'invalid')

    def test_public_loopback_and_ipv6_addresses_are_rejected(self):
        for address in ('192.0.2.10', '127.0.0.1', '169.254.1.2', '::1', '8.8.8.8'):
            self.request['inventory']['control_plane'][0]['private_ipv4'] = address
            self.assertEqual(adapter.run(self.request, validate_only=True)['status'], 'invalid')

    def test_worker_request_is_explicitly_blocked(self):
        worker = copy.deepcopy(self.request['inventory']['control_plane'][0])
        worker.update(id='worker-1', resource_id='vm-worker', private_ipv4='10.77.0.11')
        self.request['inventory']['workers'].append(worker)
        result = adapter.run(self.request, validate_only=True)
        self.assertEqual(result['status'], 'blocked'); self.assertEqual(result['error']['code'], 'SINGLE_NODE_ONLY')

    def test_duplicate_identity_and_bool_timeout_rejected(self):
        self.request['inventory']['workers'] = [copy.deepcopy(self.request['inventory']['control_plane'][0])]
        self.assertEqual(adapter.run(self.request, validate_only=True)['status'], 'invalid')
        self.request['inventory']['workers'] = []; self.request['timeout_seconds'] = True
        self.assertEqual(adapter.run(self.request, validate_only=True)['status'], 'invalid')

    def test_placement_is_recorded_without_fabricating_patroni(self):
        request = json.loads((ROOT / 'examples/ansible/patroni-placement-blocked.json').read_text())
        result = adapter.run(request, runner=lambda *_: self.fail('must not run'))
        self.assertEqual(result['error']['code'], 'PATRONI_PLAYBOOK_UNAVAILABLE')
        self.assertFalse(result['guest_ready']); self.assertFalse(result['runtime_ready'])
        # A different count is not silently rewritten to the meeting's 3+2 example.
        request['patroni']['placements'][0]['dcs_voters'] = 2
        self.assertEqual(adapter.run(request, validate_only=True)['status'], 'blocked')

    def test_ssh_identity_and_host_verification_are_explicit(self):
        inv = adapter.build_inventory(self.request)
        host = inv['all']['children']['k3s_server']['hosts']['demo-cp']
        flags = host['ansible_ssh_common_args']
        for value in ('StrictHostKeyChecking=yes', 'IdentitiesOnly=yes', 'IdentityAgent=none', 'ProxyCommand=none', '-F /dev/null'):
            self.assertIn(value, flags)
        self.assertIn('known_hosts', flags)
        with patch.dict(os.environ, {'ANSIBLE_HOST_KEY_CHECKING': 'False', 'AWS_SECRET_ACCESS_KEY': 'fixture', 'SSH_AUTH_SOCK': 'fixture'}):
            env = adapter.child_env()
        self.assertEqual(env['ANSIBLE_HOST_KEY_CHECKING'], 'True')
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env); self.assertNotIn('SSH_AUTH_SOCK', env)

    def test_openstack_relay_changes_only_connection_and_pins_the_original_guest(self):
        self.request['target']['provider'] = 'openstack'
        before = copy.deepcopy(self.request)
        node = self.request['inventory']['control_plane'][0]
        node['ssh'].update(connect_host='172.31.0.172', port=10022)
        adapter.validate(self.request)
        host = adapter.build_inventory(self.request)['all']['children']['k3s_server']['hosts'][node['id']]
        self.assertEqual((host['ansible_host'], host['ansible_port']), ('172.31.0.172', 10022))
        for flag in ('HostKeyAlias=' + node['private_ipv4'], 'StrictHostKeyChecking=yes',
                     'ProxyCommand=none', 'ProxyJump=none', 'IdentityAgent=none'):
            self.assertIn(flag, host['ansible_ssh_common_args'])
        self.assertEqual(adapter.lock_keys(self.request), adapter.lock_keys(before))
        for key in ('id', 'resource_id', 'private_ipv4'):
            self.assertEqual(node[key], before['inventory']['control_plane'][0][key])
        variables = adapter.playbook_variables(self.request)
        self.assertEqual(variables['railshot_node_ip'], node['private_ipv4'])
        self.assertEqual(variables['k3s_api_host'], node['private_ipv4'])
        self.assertEqual(variables, adapter.playbook_variables(before))
        for provider in ('aws', 'gcp', 'azure', 'proxmox'):
            invalid = copy.deepcopy(self.request); invalid['target']['provider'] = provider
            with self.subTest(provider=provider), self.assertRaises(adapter.ContractError):
                adapter.validate(invalid)
        for address in ('127.0.0.1', '203.0.113.1', '::1', 'relay.example.test', '172.31.0.172 -o StrictHostKeyChecking=no', None):
            invalid = copy.deepcopy(self.request)
            invalid['inventory']['control_plane'][0]['ssh']['connect_host'] = address
            with self.subTest(address=address), self.assertRaises(adapter.ContractError):
                adapter.validate(invalid)
        node['ssh'].update(port=22, transport_ref='ssm:ap-northeast-2:i-0123456789abcdef0')
        with self.assertRaisesRegex(adapter.ContractError, 'without transport_ref'):
            adapter.validate(self.request)

    def test_key_permissions_block_before_execution(self):
        Path(self.request['inventory']['control_plane'][0]['ssh']['identity_file']).chmod(0o644)
        result = adapter.run(self.request, runner=lambda *_: self.fail('must not run'))
        self.assertEqual(result['error']['code'], 'SSH_REFERENCE_UNAVAILABLE')

    def test_cli_stdin_and_duplicate_keys(self):
        command = [sys.executable, str(HERE / 'run.py'), '--validate-only']
        good = subprocess.run(command, input=json.dumps(self.request), capture_output=True, text=True, timeout=5)
        self.assertEqual(good.returncode, 0)
        self.assertEqual(json.loads(good.stdout)['status'], 'validated')
        bad = subprocess.run(command, input='{"schema_version":"1.0","schema_version":"2.0"}', capture_output=True, text=True, timeout=5)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stdout)['error']['code'], 'INVALID_JSON')

    def test_real_local_process_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.execute([sys.executable, '-c', 'import time; time.sleep(30)'], 0.05, adapter.child_env())

    def test_native_cloud_tunnels_preserve_host_identity_and_ignore_shell(self):
        from transport import tunnel_command, transport_parts
        aws = tunnel_command('ssm:ap-northeast-2:i-0123456789abcdef0', 50222)
        self.assertEqual(aws[:3], ['aws', 'ssm', 'start-session'])
        self.assertEqual(json.loads(aws[-1]), {'portNumber': ['22'], 'localPortNumber': ['50222']})
        gcp = tunnel_command('iap:railshot-demo/asia-northeast3-a/railshot-gcp', 50223)
        self.assertEqual(gcp[:3], ['gcloud', 'compute', 'start-iap-tunnel'])
        self.assertIn('127.0.0.1:50223', gcp)
        for value in ('ssh:host', 'ssm:ap-northeast-2:i-01234567;echo', 'iap:p/z/host --command x'):
            with self.assertRaises(ValueError):
                transport_parts(value)
        host = adapter.build_inventory(self.request, 50222)['all']['children']['k3s_server']['hosts']['demo-cp']
        self.assertEqual((host['ansible_host'], host['ansible_port']), ('127.0.0.1', 50222))
        self.assertIn('HostKeyAlias=10.77.0.10', host['ansible_ssh_common_args'])
        self.assertIn('ProxyCommand=none', host['ansible_ssh_common_args'])

    def test_cloud_tunnel_is_closed_when_the_playbook_raises(self):
        import transport
        process = MagicMock(pid=12345)
        process.poll.return_value = None
        session_id = 'fixture-owned-0123456789abcdef0'
        def launch(*args, **kwargs):
            kwargs['stdout'].write(('\nStarting session with SessionId: ' + session_id + '\n').encode())
            kwargs['stdout'].flush()
            return process
        with patch.object(transport.shutil, 'which', side_effect=lambda name: '/trusted/' + name), \
                patch.object(transport.subprocess, 'Popen', side_effect=launch) as start, \
                patch.object(transport.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, json.dumps({'SessionId': session_id}).encode())) as cleanup, \
                patch.object(transport.socket, 'create_connection'), patch.object(transport.os, 'killpg') as stop:
            with self.assertRaises(RuntimeError):
                with transport.forwarded_port('ssm:ap-northeast-2:i-0123456789abcdef0', transport.time.monotonic() + 30) as port:
                    self.assertGreater(port, 0)
                    raise RuntimeError('playbook fixture')
        self.assertEqual([args.args for args in stop.call_args_list],
                         [(12345, transport.signal.SIGTERM), (12345, transport.signal.SIGKILL)])
        self.assertEqual(start.call_args.kwargs['env']['AWS_PAGER'], '')
        self.assertEqual(cleanup.call_args.args[0][1:3], ['ssm', 'terminate-session'])
        self.assertEqual(cleanup.call_args.args[0][6], session_id)

    def test_terraform_descriptors_only_convert_bound_cloud_references(self):
        ssh = self.request['inventory']['control_plane'][0]['ssh']
        for provider in ('aws', 'gcp'):
            descriptor = json.loads((ROOT / f'examples/ansible/{provider}-node-descriptor.json').read_text())
            request = adapter.from_descriptor(descriptor, request_id='descriptor-test', operation='runtime.install', ssh=ssh)
            self.assertEqual(adapter.run(request, validate_only=True)['status'], 'validated')
            self.assertEqual(request['target']['architecture'], 'amd64')
            request['inventory']['control_plane'][0]['resource_id'] = 'another-vm'
            self.assertEqual(adapter.run(request, validate_only=True)['status'], 'invalid')
            descriptor['transport_ref'] = None
            with self.assertRaises(ValueError):
                adapter.from_descriptor(descriptor, request_id='descriptor-test', operation='guest.check', ssh=ssh)

    def test_persistent_request_prevents_reexecution_and_changed_inputs(self):
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            first = adapter.run(self.request, runner=self.runner)
            second = adapter.run(self.request, runner=lambda *_: self.fail('must not repeat'))
            self.assertFalse(first['replayed']); self.assertTrue(second['replayed'])
            self.assertEqual({**first, 'replayed': True}, second)
            self.assertEqual(first['steps'][1]['receipt']['stage'], 'runtime')
            self.assertEqual(len(self.commands), 2)
            self.request['timeout_seconds'] -= 1
            result = adapter.run(self.request, runner=lambda *_: self.fail('must not run'))
        self.assertEqual(result['error']['code'], 'REQUEST_ID_CONFLICT')

    def test_operator_aws_cli_descriptor_keeps_resource_region_and_transport_binding(self):
        ssh = self.request['inventory']['control_plane'][0]['ssh']
        descriptor = json.loads((ROOT / 'examples/ansible/aws-node-descriptor.json').read_text())
        descriptor['execution_driver'] = 'aws-cli'
        request = adapter.from_descriptor(descriptor, request_id='operator-aws', operation='guest.check', ssh=ssh)
        self.assertEqual(request['target']['provider'], 'aws')
        for field, value in [('resource_id', 'i-00000000000000000'),
                             ('transport_ref', 'ssm:us-east-1:i-0123456789abcdef0'),
                             ('execution_driver', 'arbitrary-command')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                adapter.from_descriptor({**descriptor, field: value}, request_id='operator-aws', operation='guest.check', ssh=ssh)
        gcp = json.loads((ROOT / 'examples/ansible/gcp-node-descriptor.json').read_text())
        with self.assertRaises(ValueError):
            adapter.from_descriptor({**gcp, 'execution_driver': 'aws-cli'}, request_id='wrong-driver', operation='guest.check', ssh=ssh)

    def test_incomplete_job_and_busy_target_do_not_start_ssh(self):
        adapter.STATE_DIR.mkdir(mode=0o700)
        identity = 'target:' + self.request['target']['id']
        lock_path = adapter.STATE_DIR / (hashlib.sha256(identity.encode()).hexdigest() + '.lock')
        with lock_path.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = adapter.run(self.request, runner=lambda *_: self.fail('must not run'))
        self.assertEqual(result['error']['code'], 'TARGET_BUSY')
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        with patch.object(adapter.shutil, 'which', return_value='/trusted/ansible-playbook'):
            with self.assertRaises(KeyboardInterrupt):
                adapter.run(self.request, runner=interrupted)
            result = adapter.run(self.request, runner=lambda *_: self.fail('must not retry'))
        self.assertEqual(result['error']['code'], 'PREVIOUS_OUTCOME_UNKNOWN')
        self.assertTrue(result['error']['outcome_unknown'])

    def test_runtime_uses_native_scripts_and_shared_lock_without_application(self):
        playbook = (HERE / 'runtime.yml').read_text()
        for path in ('bootstrap/preflight.sh', 'bootstrap/install-k3s.sh', 'cilium/install.sh',
                     'bootstrap/health.sh', 'cilium/health.sh', 'airgap/versions.json',
                     '/run/railshot-deployment.lock'):
            self.assertIn(path, playbook)
        self.assertIn("loop: ['', scripts, bootstrap, cilium, airgap]", playbook)
        for old in ('/runtime.sh', 'deploy-sample', 'metadata.name == k3s_node_name'):
            self.assertNotIn(old, playbook)
        self.assertIn('InternalIP', playbook)
        self.assertIn('nodeInfo.architecture', playbook)


class CIBootTests(unittest.TestCase):
    def test_policy_and_docker_restarts_rerun_real_probes_after_builder_setup(self):
        tasks = yaml.safe_load((HERE / 'ci.yml').read_text())[0]['tasks']
        unit = next(task['ansible.builtin.copy']['content'] for task in tasks
                    if task.get('ansible.builtin.copy', {}).get('dest') ==
                    '/etc/systemd/system/railshot-ci-verify.service')
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(unit)
        services = {'docker.service', 'railshot-ci-network.service'}
        for key in ('Requires', 'After', 'PartOf'):
            self.assertTrue(services <= set(config['Unit'][key].split()), key)
        self.assertIn('network-online.target', config['Unit']['After'].split())
        self.assertEqual(config['Service']['ExecStart'], '/usr/local/sbin/test-ci-network')
        self.assertEqual(config['Service']['TimeoutStartSec'], '600')
        self.assertIn('railshot-ci-network.service', config['Install']['WantedBy'].split())
        self.assertNotIn('ConditionPathExists', config['Unit'])
        builder = next(i for i, task in enumerate(tasks)
                       if task['name'].startswith('Prepare the dedicated bounded BuildKit'))
        start = next(i for i, task in enumerate(tasks)
                     if task.get('ansible.builtin.systemd_service', {}).get('name') == 'railshot-ci-verify')
        self.assertGreater(start, builder)
        self.assertEqual(tasks[start]['ansible.builtin.systemd_service']['state'], 'restarted')
        self.assertTrue(tasks[start]['ansible.builtin.systemd_service']['enabled'])


if __name__ == '__main__':
    unittest.main()
