"""Local HTTP journeys with fake Ansible receipts; no cloud or SSH calls."""
from http.client import HTTPConnection
import copy
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import api


class APIJourney(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = 'a-private-test-token-' + '1' * 32
        self.write('token', self.token)
        self.write('key', 'offline key placeholder')
        self.write('known_hosts', 'offline host key placeholder')
        descriptor = json.loads((api.ansible.ROOT / 'examples/ansible/aws-node-descriptor.json').read_text())
        self.write('descriptor.json', json.dumps(descriptor))
        self.write('targets.json', json.dumps({'version': 1, 'targets': {'demo-aws': {
            'descriptor_file': str(self.root / 'descriptor.json'),
            'ssh': {'user': 'railshot-operator', 'identity_file': str(self.root / 'key'),
                    'known_hosts_file': str(self.root / 'known_hosts')}}}}))
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []
        self.variables = []

    def write(self, name, content):
        path = self.root / name
        path.write_text(content); path.chmod(0o600)

    def executor(self, request, state_dir):
        self.calls.append(request)
        self.started.set()
        if not self.release.wait(5):
            raise RuntimeError('offline worker release timed out')

        def fake_receipt(argv, *_):
            variables = json.loads(Path(argv[-1][1:]).read_text())
            self.variables.append(variables)
            stage = Path(argv[3]).stem
            Path(variables['railshot_receipt_path']).write_text(json.dumps({
                'request_id': variables['railshot_request_id'], 'target_id': variables['railshot_target_id'],
                'node_id': variables['k3s_node_name'], 'nonce': variables['railshot_nonce'],
                'stage': stage, stage + '_ready': True}))
            return 0

        from contextlib import nullcontext
        with patch.object(api.ansible.shutil, 'which', return_value='/offline/ansible-playbook'), \
                patch.object(api.ansible, 'forwarded_port', return_value=nullcontext(50222)):
            return api.ansible.run(request, runner=fake_receipt, state_dir=state_dir)

    def server(self):
        server = api.create_server(targets_file=self.root / 'targets.json', token_file=self.root / 'token',
                                   state_dir=self.root / 'state', port=0, executor=self.executor)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def request(self, server, method, path, value=None, *, token=True, headers=None):
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        fields = {'Content-Type': 'application/json'}
        if token:
            fields['Authorization'] = 'Bearer ' + self.token
        fields.update(headers or {})
        connection.request(method, path, body=json.dumps(value) if value is not None else None, headers=fields)
        response = connection.getresponse()
        status, result, location = response.status, json.loads(response.read()), response.getheader('Location')
        connection.close()
        return status, result, location

    def test_authenticated_admission_completion_replay_and_rejections(self):
        (self.root / 'token').chmod(0o644)
        with self.assertRaises(api.ansible.ContractError):
            self.server()
        (self.root / 'token').chmod(0o600)
        server, thread = self.server()
        body = {'request_id': 'runtime-001', 'target_id': 'demo-aws', 'operation': 'runtime.install'}
        try:
            self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', body, token=False)[0], 401)
            self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', body, headers={'Origin': 'https://evil.invalid'})[0], 403)
            self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', body, headers={'Host': 'evil.invalid'})[0], 403)
            for key in ('ssh', 'descriptor_file', 'credentials', 'command', 'validate_only'):
                self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', {**body, key: '/private/key'})[0], 400)
            self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', {**body, 'target_id': '../other'})[0], 400)
            status, accepted, location = self.request(server, 'POST', '/v1/ansible/jobs', body)
            self.assertEqual(status, 202)
            self.assertEqual(location, '/v1/ansible/jobs/runtime-001')
            self.assertTrue(self.started.wait(2))
            self.assertEqual(self.request(server, 'GET', location)[1]['status'], 'running')
            self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', body)[0], 202)
            conflict = self.request(server, 'POST', '/v1/ansible/jobs', {**body, 'operation': 'guest.check'})
            self.assertEqual(conflict[:2], (409, {'error': {'code': 'REQUEST_ID_CONFLICT'}}))
            busy = self.request(server, 'POST', '/v1/ansible/jobs', {**body, 'request_id': 'runtime-002'})
            self.assertEqual(busy[:2], (409, {'error': {'code': 'EXECUTOR_BUSY'}}))
            self.release.set()
            deadline = time.monotonic() + 3
            while True:
                result = self.request(server, 'GET', location)[1]
                if result['status'] != 'running' or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertEqual(result['status'], 'succeeded')
            self.assertTrue(result['result']['guest_ready']); self.assertTrue(result['result']['runtime_ready'])
            self.assertFalse(result['result']['application_ready']); self.assertFalse(result['result']['public_http_verified'])
            for forbidden in (str(self.root), self.token, 'identity_file', 'known_hosts_file', 'descriptor_file'):
                self.assertNotIn(forbidden, json.dumps(result))
        finally:
            self.release.set(); server.shutdown(); server.server_close(); thread.join(2)
        restarted, thread = self.server()
        try:
            self.assertEqual(self.request(restarted, 'GET', location)[1]['status'], 'succeeded')
            self.assertEqual(self.request(restarted, 'POST', '/v1/ansible/jobs', body)[0], 200)
            self.assertEqual(len(self.calls), 1)
        finally:
            restarted.shutdown(); restarted.server_close(); thread.join(2)

    def test_interrupted_job_blocks_new_target_mutation_until_native_reconciliation(self):
        jobs = api.Jobs(self.root / 'targets.json', self.root / 'state', executor=self.executor)
        request = jobs.prepare({'request_id': 'interrupted-001', 'target_id': 'demo-aws', 'operation': 'runtime.install'})
        record = {'request_id': 'interrupted-001', 'target_id': 'demo-aws', 'operation': 'runtime.install',
                  'request_sha256': api.hashlib.sha256(api.encoded(request)).hexdigest(),
                  'status': 'running', 'created_at': time.time(),
                  'updated_at': time.time(), 'result': None, 'error': None}
        jobs.save(record); jobs.close()
        restarted = api.Jobs(self.root / 'targets.json', self.root / 'state', executor=self.executor)
        try:
            observed = restarted.get(record['request_id'])
            self.assertEqual(observed['status'], 'unknown')
            self.assertTrue(observed['error']['outcome_unknown'])
            with self.assertRaises(api.APIError) as failure:
                restarted.submit({'request_id': 'another-001', 'target_id': 'demo-aws', 'operation': 'runtime.install'})
            self.assertEqual(failure.exception.code, 'TARGET_RECONCILE_REQUIRED')
            self.assertEqual(self.calls, [])
            self.release.set()
            self.executor(request, self.root / 'state')  # Simulate the surviving native worker's completion.
            self.assertEqual(restarted.get(record['request_id'])['status'], 'succeeded')
            self.assertEqual(len(self.calls), 1)  # Status reconciliation did not dispatch another worker.
        finally:
            restarted.close()

    def test_aws_resource_aliases_share_active_locks_and_legacy_unknown_admission(self):
        config = json.loads((self.root / 'targets.json').read_text())
        descriptor = json.loads((self.root / 'descriptor.json').read_text())
        arn = descriptor['resource_id']
        self.assertTrue(arn.startswith('arn:aws:ec2:'))
        descriptor.update(target_id='raw-alias', execution_driver='aws-cli', resource_id=arn.rsplit('/', 1)[1])
        self.write('raw-alias.json', json.dumps(descriptor))
        config['targets']['raw-alias'] = {**config['targets']['demo-aws'],
            'descriptor_file': str(self.root / 'raw-alias.json')}
        self.write('targets.json', json.dumps(config))
        jobs = api.Jobs(self.root / 'targets.json', self.root / 'state', executor=self.executor)
        self.addCleanup(jobs.close)
        first_body = {'request_id': 'old-arn-request', 'target_id': 'demo-aws', 'operation': 'runtime.install'}
        next_body = {**first_body, 'request_id': 'new-raw-request', 'target_id': 'raw-alias'}
        first, second = jobs.prepare(first_body), jobs.prepare(next_body)
        shared = set(api.ansible.lock_keys(first)) & set(api.ansible.lock_keys(second))
        self.assertIn('transport:' + descriptor['transport_ref'], shared)
        self.assertIn('resource:' + descriptor['resource_id'], shared)
        calls = []
        def dispatch():
            calls.append(True)
            return api.ansible.base_result(second)
        # An actual flock from either spelling prevents another native operation.
        lock = jobs.state_dir / (api.hashlib.sha256(('resource:' + descriptor['resource_id']).encode()).hexdigest() + '.lock')
        with lock.open('w') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = api.ansible.run_locked(second, api.ansible.base_result(second),
                api.ansible.lock_keys(second), dispatch, state_dir=jobs.state_dir)
            self.assertEqual(result['error']['code'], 'TARGET_BUSY')
        # Persist the exact legacy key spelling; new aliases must still see unresolved effects.
        legacy_keys = ['target:demo-aws', 'resource:' + arn]
        unknown = api.ansible.fail(api.ansible.base_result(first), 'failed', 'EXECUTION_TIMEOUT', 'fixture', unknown=True)
        digest = api.hashlib.sha256(api.encoded(first)).hexdigest()
        native_path = jobs.state_dir / (api.hashlib.sha256(first_body['request_id'].encode()).hexdigest() + '.json')
        native_path.write_text(json.dumps({'request_sha256': digest, 'result': unknown, 'lock_keys': legacy_keys}))
        native_path.chmod(0o600)
        result = api.ansible.run_locked(second, api.ansible.base_result(second),
            api.ansible.lock_keys(second), dispatch, state_dir=jobs.state_dir)
        self.assertEqual(result['error']['code'], 'PREVIOUS_OUTCOME_UNKNOWN')
        record = {**first_body, 'request_sha256': digest, 'status': 'unknown', 'created_at': time.time(),
                  'updated_at': time.time(), 'result': None, 'error': None, 'lock_keys': legacy_keys}
        for has_keys in (True, False):
            if not has_keys: record.pop('lock_keys')
            jobs.save(record)
            with self.assertRaises(api.APIError) as failure: jobs.submit(next_body)
            self.assertEqual(failure.exception.code, 'TARGET_RECONCILE_REQUIRED')
        self.assertEqual(calls, [])
        self.assertEqual(self.calls, [])

    def register_openstack(self):
        config = json.loads((self.root / 'targets.json').read_text())
        examples = json.loads((api.ansible.ROOT / 'examples/ansible/api-targets.json').read_text())
        server = json.loads((api.ansible.ROOT / 'examples/ansible/openstack-server.json').read_text())
        for target_id, address in [('demo-openstack', '192.168.50.10'), ('db-onprem', '192.168.50.20')]:
            target = examples['targets'][target_id]
            target['ssh'] = config['targets']['demo-aws']['ssh']
            target['server_file'] = str(self.root / (target_id + '.json'))
            config['targets'][target_id] = target
            self.write(target_id + '.json', json.dumps({**server, 'id': target['resource_id'],
                'addresses': [{'network': 'management', 'address': address, 'version': 4}]}))
        self.write('targets.json', json.dumps(config))

    def test_openstack_preview_and_runtime_parameters_reach_existing_playbooks(self):
        self.register_openstack()
        server, thread = self.server()
        body = {'request_id': 'openstack-001', 'target_id': 'demo-openstack', 'operation': 'runtime.install',
                'parameters': {'wait_timeout_seconds': 180}}
        try:
            status, plan, _ = self.request(server, 'POST', '/v1/ansible/validate', body)
            self.assertEqual(status, 200)
            self.assertTrue(plan['execution_supported'])
            host = plan['inventory']['all']['children']['k3s_server']['hosts']['demo-openstack']
            self.assertEqual((host['ansible_host'], host['railshot_provider']), ('192.168.50.10', 'openstack'))
            self.assertEqual(plan['variables']['railshot_wait_timeout_seconds'], 180)
            self.assertEqual(self.calls, []); self.assertEqual(server.jobs.records, {})
            for forbidden in (str(self.root), self.token, 'identity_file', 'known_hosts_file'):
                self.assertNotIn(forbidden, json.dumps(plan))
            for params in ({'extra_vars': {'command': 'id'}}, {'wait_timeout_seconds': True},
                           {'wait_timeout_seconds': 601}, {'mode': 'standalone'}):
                self.assertEqual(self.request(server, 'POST', '/v1/ansible/validate', {**body, 'parameters': params})[0], 400)
            self.assertEqual(self.request(server, 'POST', '/v1/ansible/validate', {**body, 'operation': 'guest.check'})[0], 400)
            status, _, location = self.request(server, 'POST', '/v1/ansible/jobs', body)
            self.assertEqual(status, 202); self.assertTrue(self.started.wait(2))
            conflict = {**body, 'parameters': {'wait_timeout_seconds': 181}}
            self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', conflict)[0], 409)
            self.release.set()
            deadline = time.monotonic() + 3
            while True:
                result = self.request(server, 'GET', location)[1]
                if result['status'] not in ('queued', 'running') or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual([v['railshot_wait_timeout_seconds'] for v in self.variables], [180, 180])
            self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', body)[0], 200)
            self.assertEqual(len(self.calls), 1)
            snapshot = json.loads((self.root / 'demo-openstack.json').read_text())
            self.write('demo-openstack.json', json.dumps({**snapshot, 'project_id': 'another-project'}))
            rejected = self.request(server, 'POST', '/v1/ansible/jobs', {**body, 'request_id': 'wrong-project'})
            self.assertEqual(rejected[:2], (503, {'error': {'code': 'TARGET_CONFIGURATION_INVALID'}}))
            self.assertEqual(len(self.calls), 1)
        finally:
            self.release.set(); server.shutdown(); server.server_close(); thread.join(2)

    def test_operator_registered_openstack_relay_keeps_native_binding_and_rejects_endpoint_changes(self):
        self.register_openstack()
        config = json.loads((self.root / 'targets.json').read_text())
        target = config['targets']['demo-openstack']
        target['ssh'].update(connect_host='172.31.0.172', port=10022)
        target['management_endpoint'] = 'https://172.31.0.172:16443'
        self.write('targets.json', json.dumps(config))
        jobs = api.Jobs(self.root / 'targets.json', self.root / 'relay-state')
        try:
            request = jobs.resolve('demo-openstack', 'relay-check')
            node = request['inventory']['control_plane'][0]
            self.assertEqual(node['private_ipv4'], '192.168.50.10')
            self.assertEqual(node['resource_id'], target['resource_id'])
            self.assertEqual(node['ssh']['connect_host'], '172.31.0.172')
            for endpoint in ('http://172.31.0.172:16443', 'https://172.31.0.173:16443',
                             'https://172.31.0.172', 'https://172.31.0.172:0', 'https://172.31.0.172:65536',
                             'https://user@172.31.0.172:16443', 'https://172.31.0.172:16443/',
                             'https://172.31.0.172:16443?query', 'https://172.31.0.172:16443#fragment', None):
                jobs.targets['demo-openstack']['management_endpoint'] = endpoint
                with self.subTest(endpoint=endpoint), self.assertRaises(api.APIError) as failure:
                    jobs.resolve('demo-openstack', 'invalid-relay')
                self.assertEqual(failure.exception.code, 'TARGET_CONFIGURATION_INVALID')
            jobs.targets['demo-openstack']['management_endpoint'] = target['management_endpoint']
            server = json.loads((self.root / 'demo-openstack.json').read_text())
            self.write('demo-openstack.json', json.dumps({**server, 'project_id': 'foreign-project'}))
            with self.assertRaises(api.APIError):
                jobs.resolve('demo-openstack', 'foreign-server')
        finally:
            jobs.pool.shutdown(); jobs.instance_lock.close()
        config['targets']['demo-aws']['management_endpoint'] = 'https://172.31.0.172:16443'
        self.write('targets.json', json.dumps(config))
        with self.assertRaisesRegex(ValueError, 'invalid registered target'):
            api.Jobs(self.root / 'targets.json', self.root / 'invalid-relay-state')

    def test_database_inventory_is_external_and_cannot_dispatch_installation(self):
        self.register_openstack()
        body = json.loads((api.ansible.ROOT / 'examples/ansible/database-validate.json').read_text())
        server, thread = self.server()
        try:
            status, plan, _ = self.request(server, 'POST', '/v1/ansible/validate', body)
            self.assertEqual(status, 200)
            self.assertFalse(plan['execution_supported'])
            self.assertEqual(plan['blockers'], [{'code': 'DATABASE_STANDALONE_UNSUPPORTED'}])
            self.assertEqual(set(plan['inventory']['all']['children']), {'database', 'dcs', 'proxy'})
            self.assertEqual(plan['inventory']['all']['children']['dcs']['hosts'], {})
            self.assertEqual(plan['variables']['railshot_database']['port'], 5432)
            rejected = self.request(server, 'POST', '/v1/ansible/jobs', body)
            self.assertEqual(rejected[:2], (501, {'error': {'code': 'DATABASE_STANDALONE_UNSUPPORTED'}}))
            for edit in ('count', 'purpose', 'duplicate', 'missing', 'standalone-dcs'):
                bad = copy.deepcopy(body)
                if edit == 'count': bad['parameters']['placements'][0]['database_nodes'] = 3
                if edit == 'purpose': bad['parameters']['nodes'][0]['target_id'] = 'demo-openstack'
                if edit == 'duplicate': bad['parameters']['nodes'] *= 2
                if edit == 'missing': del bad['parameters']
                if edit == 'standalone-dcs':
                    bad['parameters']['nodes'][0]['roles'].append('dcs')
                    bad['parameters']['placements'][0]['dcs_voters'] = 1
                self.assertEqual(self.request(server, 'POST', '/v1/ansible/validate', bad)[0], 400, edit)
            patroni = copy.deepcopy(body)
            patroni['parameters']['mode'] = 'patroni'
            # Only placement mapping is validated; one DB with no DCS is not an HA claim.
            self.assertFalse(self.request(server, 'POST', '/v1/ansible/validate', patroni)[1]['execution_supported'])
            runtime = {'request_id': 'wrong-purpose', 'target_id': 'db-onprem', 'operation': 'runtime.install'}
            self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', runtime)[1]['error']['code'], 'TARGET_PURPOSE_MISMATCH')
            guest = {**runtime, 'operation': 'guest.check'}
            self.assertTrue(self.request(server, 'POST', '/v1/ansible/validate', guest)[1]['execution_supported'])
            self.assertEqual(self.calls, []); self.assertEqual(server.jobs.records, {})
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_openapi_example_matches_local_http_contract(self):
        spec = json.loads((api.ansible.ROOT / 'docs/api/ansible.openapi.json').read_text())
        self.assertEqual(spec['openapi'], '3.1.0')
        self.assertEqual(set(spec['paths']), {'/v1/ansible/validate', '/v1/ansible/jobs', '/v1/ansible/jobs/{request_id}'})
        self.assertEqual(spec['components']['schemas']['JobRequest'], json.loads(api.inputs.JOB_SCHEMA.read_text()))
        self.assertEqual(spec['security'], [{'operatorBearer': []}])
        post = spec['paths']['/v1/ansible/jobs']['post']
        schemas = spec['components']['schemas']
        body = post['requestBody']['content']['application/json']['example']
        self.assertEqual(set(body), set(schemas['JobRequest']['required']))
        server, thread = self.server()
        try:
            status, accepted, location = self.request(server, 'POST', '/v1/ansible/jobs', body)
            self.assertEqual(status, 202)
            self.assertIn(str(status), post['responses'])
            self.assertEqual(location, '/v1/ansible/jobs/' + body['request_id'])
            self.assertEqual(set(accepted), set(schemas['Job']['required']))
            self.assertIsInstance(accepted['created_at'], float)
            self.assertIsInstance(accepted['updated_at'], float)
            self.release.set()
            deadline = time.monotonic() + 3
            while True:
                _, completed, _ = self.request(server, 'GET', location)
                if completed['status'] not in ('queued', 'running') or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertEqual(completed['status'], 'succeeded')
            self.assertIn(completed['status'], schemas['Job']['properties']['status']['enum'])
            self.assertEqual(set(completed['result']), set(schemas['JobResult']['required']))
            for key, rule in schemas['JobResult']['properties'].items():
                if key not in completed['result']:
                    self.assertNotIn(key, schemas['JobResult']['required'])
                    continue
                if rule['type'] == 'boolean':
                    self.assertIs(type(completed['result'][key]), bool)
                if 'enum' in rule:
                    self.assertIn(completed['result'][key], rule['enum'])
                if 'const' in rule:
                    self.assertEqual(completed['result'][key], rule['const'])
            repeated_status, repeated, _ = self.request(server, 'POST', '/v1/ansible/jobs', body)
            self.assertEqual(repeated_status, 200)
            self.assertEqual(repeated, completed)
            self.assertFalse(repeated['result']['replayed'])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(set(schemas['HTTPError']['properties']['error']['required']), {'code'})
            for path in (location + '/', location + '?poll=1'):
                self.assertEqual(self.request(server, 'GET', path)[:2], (404, {'error': {'code': 'NOT_FOUND'}}))
            for headers, expected_status, code in (
                    ({'Content-Type': 'text/plain'}, 415, 'JSON_BODY_REQUIRED'),
                    ({'Content-Length': '0'}, 413, 'REQUEST_TOO_LARGE'),
                    ({'Content-Length': 'invalid'}, 411, 'CONTENT_LENGTH_REQUIRED')):
                with self.subTest(status=expected_status):
                    self.assertEqual(self.request(server, 'POST', '/v1/ansible/jobs', body, headers=headers)[:2],
                                     (expected_status, {'error': {'code': code}}))
                    self.assertIn(str(expected_status), post['responses'])
        finally:
            self.release.set(); server.shutdown(); server.server_close(); thread.join(2)

    def test_shared_executor_blocks_bad_key_permissions_before_cloud_calls(self):
        jobs = api.Jobs(self.root / 'targets.json', self.root / 'state', executor=self.executor)
        try:
            request = jobs.prepare({'request_id': 'bad-key-001', 'target_id': 'demo-aws', 'operation': 'runtime.install'})
            (self.root / 'key').chmod(0o644)
            result = api.execute_request(request, self.root / 'state')
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(result['error']['code'], 'SSH_REFERENCE_UNAVAILABLE')
            self.assertFalse(result['guest_ready']); self.assertFalse(result['runtime_ready'])
            self.assertEqual(next((self.root / 'state/http-jobs').glob('*.log')).stat().st_mode & 0o777, 0o600)
        finally:
            jobs.close()


if __name__ == '__main__':
    unittest.main()
