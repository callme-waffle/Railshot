"""Execute actual input-validation tasks locally without changing target servers."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / 'infrastructure/ansible/roles'


def check_inputs(tmp_path, case):
    variables = {
        'cluster_name': 'test-db',
        'client_cidrs': ['192.0.2.0/24'],
        'vault_postgres_password': 'synthetic-password-123',
        'vault_replication_password': 'synthetic-password-456',
        'vault_patroni_api_password': 'synthetic-password-789',
    }
    children = {}
    for group, names, offset in [('db_nodes', ['db01', 'db02'], 10),
                                 ('etcd_nodes', ['etcd01', 'etcd02', 'etcd03'], 20),
                                 ('proxy_nodes', ['proxy01'], 30)]:
        children[group] = {'hosts': {name: {
            'private_ip': f'192.0.2.{offset+i}', 'site': f'site-{i}',
            'ansible_connection': 'local', 'ansible_python_interpreter': sys.executable,
        } for i, name in enumerate(names)}}
    if case == 'async':
        variables['replication_mode'] = 'asynchronous'
    elif case == 'missing_cluster':
        del variables['cluster_name']
    elif case == 'duplicate_ip':
        children['db_nodes']['hosts']['db02']['private_ip'] = '192.0.2.10'
    elif case == 'invalid_mode':
        variables['replication_mode'] = 'typo'
    elif case == 'insufficient_etcd':
        del children['etcd_nodes']['hosts']['etcd03']
    inventory = tmp_path / 'inventory.yml'
    inventory.write_text(yaml.safe_dump({'all': {'vars': variables, 'children': children}}))
    expected = 'asynchronous' if case == 'async' else 'synchronous'
    playbook = tmp_path / 'validate.yml'
    playbook.write_text(yaml.safe_dump([{
        'name': 'Validate synthetic inventory', 'hosts': 'db01',
        'gather_facts': False, 'become': False,
        'roles': [{'role': 'deployment_defaults'}],
        'tasks': [
            {'name': 'Run real input validation', 'ansible.builtin.include_role': {
                'name': 'preflight', 'tasks_from': 'validate_inputs'}},
            {'name': 'Assert inventory precedence over role defaults',
             'ansible.builtin.assert': {'that': [f"replication_mode == '{expected}'"]}},
        ],
    }]))
    config = tmp_path / 'ansible.cfg'
    config.write_text(f'[defaults]\nroles_path = {ROLES}\n')
    env = os.environ.copy()
    env.update(ANSIBLE_CONFIG=str(config), ANSIBLE_HOME=str(tmp_path / 'home'),
               ANSIBLE_LOCAL_TEMP=str(tmp_path / 'local'),
               ANSIBLE_REMOTE_TEMP=str(tmp_path / 'remote'), ANSIBLE_NOCOLOR='1')
    executable = shutil.which('ansible-playbook')
    assert executable, 'ansible-playbook is required'
    return subprocess.run([executable, '-i', str(inventory), str(playbook)],
                          capture_output=True, text=True, env=env, timeout=60)


@pytest.mark.parametrize('case', ['default', 'async'])
def test_valid_input_and_inventory_override(tmp_path, case):
    result = check_inputs(tmp_path, case)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('case', [
    'missing_cluster', 'duplicate_ip', 'invalid_mode', 'insufficient_etcd',
])
def test_invalid_input_fails_before_mutation(tmp_path, case):
    result = check_inputs(tmp_path, case)
    assert result.returncode != 0
    assert 'changed=0' in result.stdout, result.stdout + result.stderr
    assert 'Assertion failed' in result.stdout or 'Invalid topology' in result.stdout
