"""Typed caller inputs and Ansible variable mapping; no provisioning or remote calls."""
from collections import Counter
import json

import run as ansible

JOB_SCHEMA = ansible.ROOT / 'contracts/ansible-job.schema.json'


def validate_job(body):
    ansible.check_schema(body, json.loads(JOB_SCHEMA.read_text()))
    return body.get('parameters', {})


def host_summary(request):
    node = request['inventory']['control_plane'][0]
    return node['id'], {'ansible_host': node['private_ipv4'],
                        'railshot_resource_id': node['resource_id'],
                        'railshot_provider': request['target']['provider'],
                        'railshot_site': request['target']['placement']}


def runtime_plan(request):
    node_id, host = host_summary(request)
    unsupported = ansible.support_error(request)
    return {'valid': True, 'operation': request['operation'], 'target_id': request['target']['id'],
            'execution_supported': unsupported is None,
            'blockers': [{'code': unsupported[0]}] if unsupported else [],
            'inventory': {'all': {'children': {'k3s_server': {'hosts': {node_id: host}},
                                             'k3s_workers': {'hosts': {}}}}},
            'variables': ansible.playbook_variables(request)}


def database_plan(body, parameters, resolve):
    """Resolve approved VM references into an external DB inventory, not K8s objects.

    The executor separately binds an approved HA profile to the team's playbook.
    This public preview contains no SSH, TLS or Vault references.
    """
    groups = {role: {'hosts': {}} for role in ('database', 'dcs', 'proxy')}
    counts, node_ids, resources, addresses = Counter(), set(), set(), set()
    for selection in parameters['nodes']:
        target_id, roles = selection['target_id'], selection['roles']
        if target_id in node_ids or len(roles) != len(set(roles)):
            raise ansible.ContractError('Duplicate database node or role')
        if 'database' in roles and 'proxy' in roles:
            raise ansible.ContractError('The HA proxy must be separate from database hosts')
        request = resolve(target_id)
        node_id, host = host_summary(request)
        physical_keys = {key for key in ansible.lock_keys(request) if not key.startswith('target:')}
        if resources & physical_keys or host['ansible_host'] in addresses:
            raise ansible.ContractError('Duplicate physical database node or management address')
        resources.update(physical_keys); addresses.add(host['ansible_host']); node_ids.add(target_id)
        for role in roles:
            groups[role]['hosts'][node_id] = host
            counts[(host['railshot_provider'], host['railshot_site'], role)] += 1
    if body['target_id'] not in node_ids:
        raise ansible.ContractError('The job target must be one of the selected database nodes')
    declared, sites = Counter(), set()
    for placement in parameters['placements']:
        site = (placement['provider'], placement['site'])
        if site in sites or placement['database_nodes'] + placement['dcs_voters'] + placement.get('proxy_nodes', 0) == 0:
            raise ansible.ContractError('Duplicate or empty database placement')
        sites.add(site)
        declared[(*site, 'database')] = placement['database_nodes']
        declared[(*site, 'dcs')] = placement['dcs_voters']
        declared[(*site, 'proxy')] = placement.get('proxy_nodes', 0)
    if +declared != +counts:
        raise ansible.ContractError('Placement counts must match the resolved node roles')
    database_count, voters = len(groups['database']['hosts']), len(groups['dcs']['hosts'])
    if database_count == 0:
        raise ansible.ContractError('At least one database node is required')
    if parameters['mode'] == 'standalone':
        if database_count != 1 or voters or groups['proxy']['hosts']:
            raise ansible.ContractError('Standalone DB uses one external database VM and no DCS')
    blocker = ('DATABASE_STANDALONE_UNSUPPORTED' if parameters['mode'] == 'standalone' else
               'DATABASE_HA_TOPOLOGY_REQUIRED' if database_count < 2 or voters < 3 or voters % 2 == 0
               or not groups['proxy']['hosts'] else 'DATABASE_PROFILE_REQUIRED')
    return {'valid': True, 'operation': body['operation'], 'target_id': body['target_id'],
            'execution_supported': False, 'blockers': [{'code': blocker}],
            'inventory': {'all': {'children': groups}},
            'variables': {'railshot_database': {'mode': parameters['mode'], 'port': 5432,
                          'placements': parameters['placements']}}}
