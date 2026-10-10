"""HA profile admission and real local Vault/process checks; no cloud or SSH calls."""
from contextlib import nullcontext
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

import api
import database
import test_api


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.http = test_api.APIJourney()
        self.http.setUp()
        self.addCleanup(self.http.doCleanups)
        self.root = self.http.root
        self.calls = []
        config = json.loads((self.root / 'targets.json').read_text())
        template = json.loads((self.root / 'descriptor.json').read_text())
        targets, certificates = {}, {}
        selections = [('db1', ['database', 'dcs']), ('db2', ['database', 'dcs']), ('db3', ['dcs', 'proxy'])]
        for i, (name, roles) in enumerate(selections, 1):
            resource = 'i-0123456789abcdef' + str(i)
            descriptor = {**template, 'target_id': name, 'resource_id': resource,
                          'transport_ref': 'ssm:ap-northeast-2:' + resource,
                          'addresses': {'private': f'10.77.0.{i}'}}
            self.http.write(name + '.json', json.dumps(descriptor))
            targets[name] = {**config['targets']['demo-aws'], 'purpose': 'database',
                             'descriptor_file': str(self.root / (name + '.json'))}
            fields = set().union(*(database.TLS[role] for role in roles))
            certificates[name] = {}
            for field in fields:
                path = name + '-' + field
                self.http.write(path, 'private TLS fixture, never sent to a real host')
                certificates[name][field] = str(self.root / path)
        self.parameters = {'mode': 'patroni', 'nodes': [{'target_id': name, 'roles': roles} for name, roles in selections],
                           'placements': [{'provider': 'aws', 'site': 'ap-northeast-2',
                                           'database_nodes': 2, 'dcs_voters': 3, 'proxy_nodes': 1}]}
        self.http.write('vault-password', 'test-vault-password-private\n')
        self.secret_values = {key: 'private-test-value-' + key for key in database.SECRETS}
        self.http.write('vault.yml', '$ANSIBLE_VAULT;1.1;AES256\noffline encrypted fixture\n')
        vault = patch.object(database, 'decrypt_vault', return_value=self.secret_values)
        vault.start(); self.addCleanup(vault.stop)
        self.profile = {'parameters': self.parameters, 'cluster_name': 'offline-ha',
                        'client_cidrs': ['10.77.0.0/24'], 'certificates': certificates,
                        'vault_file': str(self.root / 'vault.yml'),
                        'vault_password_file': str(self.root / 'vault-password'), 'timeout_seconds': 30}
        self.http.write('profile.json', json.dumps(self.profile))
        self.http.write('targets.json', json.dumps({'version': 1, 'targets': targets,
                        'database_profiles': {'ha-demo': str(self.root / 'profile.json')}}))
        self.body = {'request_id': 'database-001', 'target_id': 'db1', 'operation': 'database.configure',
                     'parameters': {**self.parameters, 'profile_id': 'ha-demo'}}

    def jobs(self):
        jobs = api.Jobs(self.root / 'targets.json', self.root / 'state')
        self.addCleanup(jobs.close)
        return jobs

    def runner(self, argv, timeout, env):
        self.calls.append(argv)
        self.assertGreater(timeout, 0)
        self.assertEqual(Path(argv[3]).name, 'database.yml')
        inventory = json.loads(Path(argv[2]).read_text())['all']['children']
        self.assertEqual({name: len(value['hosts']) for name, value in inventory.items()},
                         {'db_nodes': 2, 'etcd_nodes': 3, 'proxy_nodes': 1})
        for group in inventory.values():
            for host in group['hosts'].values():
                self.assertIn('StrictHostKeyChecking=yes', host['ansible_ssh_common_args'])
                self.assertIn('ProxyCommand=none', host['ansible_ssh_common_args'])
        variables = json.loads(Path(argv[-1][1:]).read_text())
        self.assertEqual({k: variables[k] for k in database.SECRETS}, self.secret_values)
        Path(variables['railshot_database_receipt_path']).write_text(json.dumps(variables['railshot_database_receipt']))
        return 0

    def execute(self, request, state_dir, runner=None):
        with patch.object(api.ansible, 'forwarded_port', return_value=nullcontext(50222)), \
                patch.object(api.ansible.shutil, 'which', return_value='/offline/ansible-playbook'):
            return database.run(request, state_dir=state_dir, runner=runner or self.runner)

    def test_http_ha_completion_replay_and_config_hash_binding_without_secret_readback(self):
        spec = json.loads((api.ansible.ROOT / 'docs/api/ansible.openapi.json').read_text())

        def validate_response(path, method, status, value):
            schema = spec['paths'][path][method]['responses'][str(status)]['content']['application/json']['schema']
            Draft202012Validator({**schema, 'components': spec['components']}).validate(value)

        self.http.executor = self.execute
        server, thread = self.http.server()
        try:
            self.assertEqual(self.http.request(server, 'POST', '/v1/ansible/jobs', self.body, token=False)[0], 401)
            status, plan, _ = self.http.request(server, 'POST', '/v1/ansible/validate', self.body)
            self.assertEqual(status, 200); self.assertTrue(plan['execution_supported'])
            validate_response('/v1/ansible/validate', 'post', status, plan)
            status, accepted, location = self.http.request(server, 'POST', '/v1/ansible/jobs', self.body)
            self.assertEqual(status, 202)
            validate_response('/v1/ansible/jobs', 'post', status, accepted)
            deadline = time.monotonic() + 10
            while True:
                status, result, _ = self.http.request(server, 'GET', location)
                validate_response('/v1/ansible/jobs/{request_id}', 'get', status, result)
                if result['status'] not in ('queued', 'running') or time.monotonic() > deadline: break
                time.sleep(.01)
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertTrue(result['result']['database_ready'])
            for key in ('runtime_ready', 'application_ready', 'public_http_verified'):
                self.assertFalse(result['result'][key])
            for value in (str(self.root), *self.secret_values.values(), 'vault_file', 'identity_file'):
                self.assertNotIn(value, json.dumps([plan, result]))
            status, replayed, _ = self.http.request(server, 'POST', '/v1/ansible/jobs', self.body)
            self.assertEqual(status, 200)
            validate_response('/v1/ansible/jobs', 'post', status, replayed)
            self.assertEqual(len(self.calls), 1)
            self.http.write('db1-etcd_ca_src', 'changed private certificate')
            self.assertEqual(self.http.request(server, 'POST', '/v1/ansible/jobs', self.body)[0], 409)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_invalid_profile_topology_secret_inputs_and_file_permissions_are_rejected(self):
        jobs = self.jobs()
        for key in ('extra_vars', 'vault_file', 'credentials', 'playbook'):
            body = copy.deepcopy(self.body); body['parameters'][key] = 'not-allowed'
            with self.assertRaises(api.APIError) as exc: jobs.prepare(body)
            self.assertEqual(exc.exception.code, 'INVALID_JOB_REQUEST')
        body = copy.deepcopy(self.body); body['parameters']['placements'][0]['proxy_nodes'] = 2
        with self.assertRaises(api.APIError): jobs.prepare(body)
        profile = copy.deepcopy(self.profile); profile['parameters']['nodes'][0]['roles'] = ['database']
        self.http.write('profile.json', json.dumps(profile))
        with self.assertRaises(api.APIError) as exc: jobs.prepare(self.body)
        self.assertEqual(exc.exception.code, 'DATABASE_PROFILE_MISMATCH')
        self.http.write('profile.json', json.dumps(self.profile))
        (self.root / 'db1-etcd_ca_src').chmod(0o644)
        with self.assertRaises(api.APIError) as exc: jobs.prepare(self.body)
        self.assertEqual(exc.exception.code, 'DATABASE_PROFILE_INVALID')
        self.assertEqual(jobs.records, {})

    def test_all_physical_nodes_lock_and_changed_references_block_before_mutation(self):
        jobs = self.jobs(); request = jobs.prepare(self.body)
        resource = request['database_nodes'][2]['inventory']['control_plane'][0]['resource_id']
        path = jobs.state_dir / (hashlib.sha256(('resource:' + resource).encode()).hexdigest() + '.lock')
        with path.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.execute(request, jobs.state_dir)['error']['code'], 'TARGET_BUSY')
        self.http.write('db1-etcd_ca_src', 'changed after preparation')
        result = self.execute(request, jobs.state_dir)
        self.assertEqual(result['error']['code'], 'DATABASE_PROFILE_CHANGED')
        self.assertEqual(self.calls, [])

    def test_alias_with_secondary_private_ip_cannot_count_as_another_database_member(self):
        first = json.loads((self.root / 'db1.json').read_text())
        second = json.loads((self.root / 'db2.json').read_text())
        # Both registrations are independently valid and have distinct private addresses.
        second.update(execution_driver='aws-cli', resource_id=first['resource_id'], transport_ref=first['transport_ref'])
        first['resource_id'] = 'arn:aws:ec2:ap-northeast-2:123456789012:instance/' + first['resource_id']
        self.http.write('db1.json', json.dumps(first)); self.http.write('db2.json', json.dumps(second))
        jobs = self.jobs()
        for target_id in ('db1', 'db2'): jobs.resolve(target_id, 'alias-test', purpose='database')
        with self.assertRaises(api.APIError) as failure: jobs.prepare(self.body)
        self.assertEqual(failure.exception.code, 'INVALID_JOB_REQUEST')
        self.assertEqual(jobs.records, {})

    def test_timeout_releases_worker_and_blocks_new_requests_overlapping_any_node(self):
        jobs = self.jobs()
        def timeout(*_): raise subprocess.TimeoutExpired('offline-ansible', .01)
        jobs.executor = lambda req, state: self.execute(req, state, timeout)
        jobs.submit(self.body)
        deadline = time.monotonic() + 10
        while jobs.active is not None and time.monotonic() < deadline: time.sleep(.01)
        self.assertIsNone(jobs.active)
        result = jobs.get(self.body['request_id'])
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['error']['code'], 'EXECUTION_TIMEOUT')
        body = {**self.body, 'request_id': 'different-request', 'target_id': 'db2'}
        with self.assertRaises(api.APIError) as exc: jobs.submit(body)
        self.assertEqual(exc.exception.code, 'TARGET_RECONCILE_REQUIRED')
        prepared = jobs.prepare(body)
        self.assertEqual(self.execute(prepared, jobs.state_dir)['error']['code'], 'PREVIOUS_OUTCOME_UNKNOWN')

    def test_worker_exception_is_durable_unknown_and_releases_global_busy(self):
        jobs = self.jobs()
        def interrupted(*_): raise RuntimeError('secret diagnostic must stay private')
        jobs.executor = interrupted
        jobs.submit(self.body)
        deadline = time.monotonic() + 3
        while jobs.active is not None and time.monotonic() < deadline: time.sleep(.01)
        self.assertIsNone(jobs.active)
        result = jobs.get(self.body['request_id'])
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('secret diagnostic', json.dumps(result))
        self.assertEqual(json.loads(jobs.path(self.body['request_id']).read_text())['status'], 'unknown')

    def test_exit_zero_without_receipt_is_unknown_and_never_database_ready(self):
        jobs = self.jobs(); request = jobs.prepare(self.body)
        result = self.execute(request, jobs.state_dir, lambda *_: 0)
        self.assertEqual(result['error']['code'], 'READINESS_UNPROVEN')
        self.assertTrue(result['error']['outcome_unknown']); self.assertFalse(result['database_ready'])


class NativeBoundaries(unittest.TestCase):
    def test_sigterm_drains_bounded_worker_and_persists_unknown_before_exit(self):
        http = test_api.APIJourney(); http.setUp()
        self.addCleanup(http.doCleanups)
        pid_file = http.root / 'native.pid'
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0)); port = reservation.getsockname()[1]
        script = '''import api,subprocess,sys
pid_path=sys.argv.pop(1)
def bounded(request,state_dir):
    result=api.ansible.base_result(request)
    try:
        api.ansible.execute([sys.executable,'-c',
            "import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)",
            pid_path],1.0,api.ansible.child_env())
    except subprocess.TimeoutExpired:
        return api.ansible.fail(result,'failed','EXECUTION_TIMEOUT','bounded fixture',unknown=True)
api.ansible.run=bounded
raise SystemExit(api.main())
'''
        command = [sys.executable, '-c', script, str(pid_file), '--targets-file', str(http.root / 'targets.json'),
                   '--token-file', str(http.root / 'token'), '--state-dir', str(http.root / 'state'), '--port', str(port)]
        env = {**api.ansible.child_env(), 'PYTHONPATH': str(api.ansible.HERE), 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1): break
                except OSError:
                    if time.monotonic() > deadline: self.fail('API did not start')
                    time.sleep(.02)
            body = {'request_id': 'sigterm-001', 'target_id': 'demo-aws', 'operation': 'runtime.install'}
            self.assertEqual(http.request(SimpleNamespace(server_port=port), 'POST', '/v1/ansible/jobs', body)[0], 202)
            while not pid_file.exists() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(pid_file.exists())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=5), 0)
            record = next((http.root / 'state/http-jobs').glob('*.json'))
            self.assertEqual(json.loads(record.read_text())['status'], 'unknown')
            state = subprocess.run(['ps', '-p', pid_file.read_text(), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith('Z'), state)
        finally:
            if process.poll() is None: os.killpg(process.pid, signal.SIGKILL); process.wait()
            if pid_file.exists():
                try: os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError: pass

    @unittest.skipUnless(shutil.which('ansible-vault'), 'Native Vault check runs in the Ansible CI job')
    def test_real_vault_accepts_only_the_three_credential_values(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory); password = work / 'password'; vault = work / 'vault.yml'
            password.write_text('test-encryption-password'); password.chmod(0o600)
            expected = {key: 'test-secret-credential-' + key for key in database.SECRETS}
            for values, valid in [(expected, True), ({**expected, 'ansible_connection': 'local'}, False),
                                  ({**expected, 'vault_postgres_password': '{{ lookup("pipe", "id") }}'}, False)]:
                vault.write_text(json.dumps(values)); vault.chmod(0o600)
                subprocess.run(['ansible-vault', 'encrypt', '--vault-password-file', str(password), str(vault)],
                               check=True, capture_output=True, timeout=20, env=api.ansible.child_env())
                if valid:
                    self.assertEqual(database.decrypt_vault(vault, password, 10), expected)
                else:
                    with self.assertRaises(ValueError): database.decrypt_vault(vault, password, 10)

    def test_executor_timeout_kills_spawned_children(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'child.pid'
            script = ('import subprocess,sys,time; from pathlib import Path; '
                      'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                      'Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)')
            with self.assertRaises(subprocess.TimeoutExpired):
                api.ansible.execute([sys.executable, '-c', script, str(pid_file)], .3, api.ansible.child_env())
            pid = int(pid_file.read_text())
            state = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith('Z'), state)

    @unittest.skipUnless(shutil.which('ansible-playbook'), 'Native playbook check runs in the Ansible CI job')
    def test_prior_proxy_failure_prevents_the_database_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory); (work / 'playbooks').mkdir()
            (work / 'database.yml').write_text((api.ansible.HERE / 'database.yml').read_text())
            (work / 'playbooks/site.yml').write_text('''---
- hosts: proxy_nodes
  gather_facts: false
  any_errors_fatal: true
  tasks:
    - ansible.builtin.fail:
        msg: expected isolated failure
''')
            inventory = {'all': {'children': {group: {'hosts': {name: {'ansible_connection': 'local'}}}
                         for group, name in [('proxy_nodes', 'proxy'), ('db_nodes', 'db')]}}}
            (work / 'inventory.json').write_text(json.dumps(inventory))
            receipt = work / 'receipt.json'
            variables = {'railshot_database_receipt_path': str(receipt), 'railshot_database_receipt': {'database_ready': True}}
            (work / 'vars.json').write_text(json.dumps(variables))
            result = subprocess.run(['ansible-playbook', '-i', str(work / 'inventory.json'), str(work / 'database.yml'),
                                     '--extra-vars', '@' + str(work / 'vars.json')], env=api.ansible.child_env(),
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('expected isolated failure', result.stdout)
            self.assertFalse(receipt.exists())


if __name__ == '__main__': unittest.main()
