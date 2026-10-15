"""Explicit prepared-three-node kubeadm profile; never adopts a legacy K3s VM."""
from contextlib import ExitStack
import importlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import run as ansible

class PlayFailure(ValueError):
    """A reaped normal Ansible failure, unlike a lost executor reply."""


PROFILE = 'kubeadm-1c2w'
VERSIONS = {'kubernetes': '1.36.5', 'cilium': '1.20.2', 'containerd': '2.3.6',
            'runc': '1.3.4', 'helm': '3.21.4'}


def nodes(request):
    return request['inventory']['control_plane'] + request['inventory']['workers']


def require(value, message):
    if not value:
        raise ansible.ContractError(message)


def validate(request):
    runtime, target, inv = request['runtime'], request['target'], request['inventory']
    require(runtime['profile'] == PROFILE and request['operation'] == 'runtime.install', 'kubeadm profile requires runtime.install')
    require(target['provider'] in ('aws', 'gcp', 'openstack', 'ncp') and target['architecture'] == 'amd64', 'kubeadm requires AWS/GCP/OpenStack amd64')
    require(isinstance(target.get('account_id'), str) and target['account_id'], 'kubeadm account/project binding required')
    require(len(inv['control_plane']) == 1 and len(inv['workers']) == 2, 'kubeadm profile requires one control-plane and two compute nodes')
    require(all(isinstance(n.get('availability_zone'), str) and n['availability_zone'] for n in nodes(request)), 'each node requires its availability zone')
    provider, account, region = target['provider'], target['account_id'], target['placement']
    if provider == 'aws':
        require(re.fullmatch(r'\d{12}', account) is not None, 'AWS account must have twelve digits')
    for node in nodes(request):
        az, resource = node['availability_zone'], node['resource_id']
        if provider == 'aws':
            require(az.startswith(region) and re.fullmatch(re.escape(region) + '[a-z]', az), 'AWS AZ differs from region')
            require(resource.startswith(f'arn:aws:ec2:{region}:{account}:instance/'), 'AWS resource must bind account and region')
        elif provider == 'gcp':
            require(az.rsplit('-', 1)[0] == region and resource.startswith(f'projects/{account}/zones/{az}/instances/'), 'GCP resource differs from project, region or AZ')
    cidrs = []
    for key, default in (('pod_cidr', '10.244.0.0/16'), ('service_cidr', '10.96.0.0/12')):
        try:
            network = ipaddress.ip_network(runtime.get(key, default), strict=True)
        except ValueError as exc:
            raise ansible.ContractError('invalid Kubernetes CIDR') from exc
        require(network.version == 4 and network.is_private and 12 <= network.prefixlen <= 24, 'IPv4 private Kubernetes CIDR /12 through /24 required')
        require(all(ipaddress.ip_address(n['private_ipv4']) not in network for n in nodes(request)), 'Kubernetes CIDR overlaps a node address')
        cidrs.append(network)
    require(not cidrs[0].overlaps(cidrs[1]), 'Pod and service CIDRs overlap')
    require(isinstance(runtime.get('storage'), dict) and isinstance(runtime.get('edge'), str), 'kubeadm requires shared storage and private edge configuration')
    for module in ('storage_profile', 'cluster_edge'):
        try:
            _addon(module).validate(request)
        except ImportError as exc:
            raise ansible.ContractError('kubeadm addon implementation unavailable: ' + module) from exc
    return request


def _addon(name):
    if name == 'cluster_edge':
        path = str(ansible.ROOT / 'deployment/scripts')
        if path not in sys.path:
            sys.path.insert(0, path)
    return importlib.import_module(name)


def inventory(request, forwarded=None):
    ports = forwarded if isinstance(forwarded, dict) else {request['inventory']['control_plane'][0]['id']: forwarded}
    groups = {'kubeadm_control': {'hosts': {}}, 'kubeadm_compute': {'hosts': {}}}
    for role, group in (('control_plane', 'kubeadm_control'), ('workers', 'kubeadm_compute')):
        for node in request['inventory'][role]:
            # Reuse the existing audited SSH options without a second transport implementation.
            legacy = {'inventory': {'control_plane': [node]}, 'target': request['target']}
            host = ansible.build_inventory(legacy, ports.get(node['id']))['all']['children']['k3s_server']['hosts'][node['id']]
            host.update(railshot_resource_id=node['resource_id'], railshot_node_ip=node['private_ipv4'],
                        railshot_availability_zone=node['availability_zone'], railshot_node_role=role)
            groups[group]['hosts'][node['id']] = host
    groups['kubeadm_nodes'] = {'children': {'kubeadm_control': {}, 'kubeadm_compute': {}}}
    return {'all': {'children': groups}}


def variables(request):
    return {'railshot_request_id': request['request_id'], 'railshot_target_id': request['target']['id'],
            'railshot_initialization': request['target']['initialization'], 'railshot_versions': VERSIONS,
            'railshot_provider': request['target']['provider'], 'railshot_profile': PROFILE,
            'railshot_wait_timeout_seconds': request['runtime'].get('wait_timeout_seconds', 300),
            'railshot_pod_cidr': request['runtime'].get('pod_cidr', '10.244.0.0/16'),
            'railshot_service_cidr': request['runtime'].get('service_cidr', '10.96.0.0/12'),
            'railshot_api_address': request['inventory']['control_plane'][0]['private_ipv4'],
            'railshot_expected_nodes': [{k: n[k] for k in ('id', 'resource_id', 'private_ipv4', 'availability_zone')}
                                       for n in nodes(request)],
            'railshot_worker_ids': [n['id'] for n in request['inventory']['workers']]}


def plan(request):
    return {'valid': True, 'operation': request['operation'], 'target_id': request['target']['id'],
            'execution_supported': True, 'blockers': [],
            'inventory': {'all': {'children': {role: {'hosts': {n['id']: {
                'ansible_host': n['private_ipv4'], 'railshot_resource_id': n['resource_id'],
                'railshot_availability_zone': n['availability_zone']} for n in request['inventory'][source]}}
                for role, source in (('kubeadm_control', 'control_plane'), ('kubeadm_compute', 'workers'))}}},
            'variables': {key: value for key, value in variables(request).items() if key != 'railshot_request_id'}}


def from_registry(config, *, request_id, operation='runtime.install'):
    """Bind operator-selected node snapshots to one immutable cluster scope."""
    required = {'version', 'cluster_id', 'provider', 'account_id', 'region', 'members', 'runtime'}
    require(isinstance(config, dict) and set(config) == required and config['version'] == 1, 'cluster specification v1 fields required')
    require(operation == 'runtime.install', 'prepared kubeadm specification supports runtime.install')
    provider, account, region = config['provider'], config['account_id'], config['region']
    request = {'schema_version': '1.0', 'request_id': request_id, 'operation': operation,
               'target': {'id': config['cluster_id'], 'provider': provider, 'account_id': account,
                          'placement': region, 'os': 'linux', 'architecture': 'amd64', 'initialization': 'preconfigured'},
               'inventory': {'control_plane': [], 'workers': []}, 'timeout_seconds': 1800,
               'runtime': {**config['runtime'], 'profile': PROFILE}}
    require(isinstance(config['members'], list), 'cluster members required')
    for member in config['members']:
        common = {'id', 'role', 'availability_zone', 'ssh'}
        require(isinstance(member, dict) and member.get('role') in ('control_plane', 'workers'), 'invalid member role')
        if provider in ('aws', 'gcp'):
            require(set(member) == common | {'descriptor_file'}, 'cloud member descriptor reference required')
            ansible.private_file(member['descriptor_file'], identity=True)
            descriptor = json.loads(Path(member['descriptor_file']).read_text(), object_pairs_hook=ansible.unique_pairs)
            require(descriptor.get('provider_kind') == provider and descriptor.get('target_id') == member['id'], 'member descriptor identity differs')
            legacy = ansible.from_descriptor(descriptor, request_id=request_id, operation='guest.check', ssh=member['ssh'])
            node = legacy['inventory']['control_plane'][0]
            location = descriptor['location']
            require(location.get('region', location.get('zone', '').rsplit('-', 1)[0]) == region, 'member snapshot region differs')
            require(location.get('availability_zone', location.get('zone')) == member['availability_zone'], 'member snapshot AZ differs')
            if provider == 'aws':
                require(node['resource_id'].startswith(f'arn:aws:ec2:{region}:{account}:instance/'), 'AWS snapshot account differs')
        elif provider == 'openstack':
            require(set(member) == common | {'server_file', 'resource_id', 'management_network'}, 'OpenStack member server reference required')
            ansible.private_file(member['server_file'], identity=True)
            server = json.loads(Path(member['server_file']).read_text(), object_pairs_hook=ansible.unique_pairs)
            require(server.get('region') == region, 'OpenStack snapshot region differs')
            require(server.get('availability_zone', server.get('OS-EXT-AZ:availability_zone')) == member['availability_zone'], 'OpenStack snapshot AZ differs')
            legacy = ansible.from_openstack(server, request_id=request_id, operation='guest.check', target_id=member['id'],
                 resource_id=member['resource_id'], project_id=account, management_network=member['management_network'],
                 placement=region, architecture='amd64', initialization='preconfigured', ssh=member['ssh'])
            node = legacy['inventory']['control_plane'][0]
        elif provider == 'ncp':
            require(set(member) == common | {'descriptor_file'}, 'NCP typed descriptor required')
            sys.path.insert(0, str(ansible.ROOT))
            from infrastructure.providers.ncp.runtime import node as ncp_node
            node = ncp_node(config, member)
        else:
            raise ansible.ContractError('unsupported cluster provider')
        node['availability_zone'] = member['availability_zone']
        request['inventory'][member['role']].append(node)
    return ansible.validate(request)


def addon_lock_keys(request):
    storage = request['runtime']['storage']
    provider = request['target']['provider']
    if provider == 'aws':
        key = storage.get('aws', {}).get('filesystem_id', request['target']['id'])
    elif provider == 'gcp':
        key = storage.get('gcp', {}).get('instance_name', request['target']['id'])
    elif provider == 'ncp':
        key = storage.get('ncp', {}).get('nas_volume_instance_no', request['target']['id'])
    else:
        ceph = storage.get('cephfs', {})
        key = ':'.join(str(ceph.get(k, '')) for k in ('cluster_id', 'fs_name', 'subvolume_group'))
    keys = ['storage:' + provider + ':' + request['target']['account_id'] + ':' + str(key),
            'edge:' + request['runtime']['edge']]
    if storage.get('state_dir'):
        keys.append('storage-state:' + str(Path(storage['state_dir']).resolve()))
    return keys


def result(request):
    return {**ansible.base_result(request), 'storage_backend_ready': False, 'storage_csi_ready': False,
            'storage_pvc_ready': False, 'storage_ready': False, 'edge_ready': False}


def receipt(path, request, stage, nonce):
    value = json.loads(path.read_text())
    require(isinstance(value, dict) and all(value.get(k) == expected for k, expected in {
        'request_id': request['request_id'], 'target_id': request['target']['id'], 'stage': stage, 'nonce': nonce}.items()), 'readiness receipt binding differs')
    if stage in ('guest', 'runtime'):
        require(value.get(stage + '_ready') is True and sorted(value.get('node_ids', [])) == sorted(n['id'] for n in nodes(request)), 'readiness receipt does not prove all three nodes')
    else:
        require(all(value.get(k) is True for k in ('storage_backend_ready', 'storage_csi_ready', 'storage_pvc_ready', 'storage_ready')), 'storage backend, CSI and PVC mobility readiness unproven')
    return value


def kubectl(request, ports, env, deadline):
    """Fixed control-plane SSH callback using the already owned transport."""
    host = inventory(request, ports)['all']['children']['kubeadm_control']['hosts'][nodes(request)[0]['id']]
    prefix = ['ssh', *shlex.split(host['ansible_ssh_common_args']), '-i', host['ansible_ssh_private_key_file'],
              '-p', str(host['ansible_port']), '-o', 'ConnectTimeout=10',
              host['ansible_user'] + '@' + host['ansible_host']]
    def remote(namespace, *args, document=None):
        remaining = deadline - time.monotonic()
        require(remaining > 0, 'execution deadline exceeded before Kubernetes edge action')
        command = ['sudo', '-n', '/usr/bin/kubectl', '--kubeconfig=/etc/kubernetes/admin.conf',
                   '--request-timeout=20s', '-n', namespace, *args]
        value = subprocess.run([*prefix, shlex.join(command)], env=env,
            input=None if document is None else json.dumps(document).encode(),
            capture_output=True, check=False, timeout=min(60, remaining))
        require(value.returncode == 0 and len(value.stdout) <= ansible.MAX_BYTES, 'Kubernetes edge command failed')
        return json.loads(value.stdout) if value.stdout.strip() else None
    return remote


def _execute(request, *, runner, state_dir, storage_prepare=None, edge_provision=None, expected_edge_binding=None):
    output = result(request)
    deadline = time.monotonic() + request['timeout_seconds']
    output['stage'] = 'executor_preflight'
    try:
        for node in nodes(request):
            ansible.private_file(node['ssh']['identity_file'], identity=True)
            ansible.private_file(node['ssh']['known_hosts_file'])
        executable = shutil.which('ansible-playbook')
        require(executable, 'ansible-playbook unavailable')
    except (ValueError, OSError) as exc:
        return ansible.fail(output, 'blocked', 'EXECUTOR_PREFLIGHT_FAILED', str(exc))
    mutated, uncertain = False, False
    try:
        with ExitStack() as stack:
            ports = {node['id']: stack.enter_context(ansible.forwarded_port(node['ssh'].get('transport_ref'), deadline)) for node in nodes(request)}
            work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='railshot-kubeadm-')))
            inv_path = work / 'inventory.json'; inv_path.write_text(json.dumps(inventory(request, ports))); inv_path.chmod(0o600)
            env = ansible.child_env()
            def play(stage, extra=None):
                nonlocal mutated, uncertain
                require(deadline > time.monotonic(), 'execution deadline exceeded before next stage')
                output['stage'] = stage
                nonce = secrets.token_hex(16)
                proof = work / (stage + '-receipt.json')
                var_path = work / (stage + '-vars.json')
                var_path.write_text(json.dumps({**variables(request), **(extra or {}), 'railshot_nonce': nonce,
                                               'railshot_receipt_path': str(proof)})); var_path.chmod(0o600)
                output['steps'].append({'stage': stage, 'status': 'running'})
                mutated = mutated or stage != 'guest'
                uncertain = stage != 'guest'
                rc = runner([executable, '-i', str(inv_path), str(ansible.HERE / ('kubeadm-' + stage + '.yml')),
                             '--extra-vars', '@' + str(var_path)], max(1, deadline - time.monotonic()), env)
                if rc:
                    uncertain = False
                    raise PlayFailure('kubeadm ' + stage + ' playbook failed')
                observed = receipt(proof, request, stage, nonce)
                uncertain = False
                output['steps'][-1].update(status='succeeded')
                return observed
            play('guest'); output['guest_ready'] = True
            bootstrap = secrets.token_hex(3) + '.' + secrets.token_hex(8)
            try:
                play('runtime', {'railshot_bootstrap_token': bootstrap})
                output['runtime_ready'] = True
            finally:
                # The normal play deletes the token. After a failed play, remove only
                # this execution's randomly generated token; never enumerate secrets.
                if deadline > time.monotonic():
                    control = inventory(request, ports)['all']['children']['kubeadm_control']['hosts'][nodes(request)[0]['id']]
                    command = ['ssh', *shlex.split(control['ansible_ssh_common_args']), '-i', control['ansible_ssh_private_key_file'],
                               '-p', str(control['ansible_port']), control['ansible_user'] + '@' + control['ansible_host'],
                               shlex.join(['sudo', '-n', '/usr/bin/python3', '/var/lib/railshot/kubeadm/cleanup.py', bootstrap])]
                    runner(command, max(1, deadline - time.monotonic()), env)
            require(deadline > time.monotonic(), 'execution deadline exceeded before storage backend')
            output['stage'] = 'storage_backend'; mutated = True
            prepare = storage_prepare or _addon('storage_profile').prepare
            prepared = prepare(request, work=work, env=env, deadline=deadline)
            require(prepared.get('storage_backend_ready') is True or request['target']['provider'] == 'openstack', 'shared storage backend preparation unproven')
            output['storage_backend_ready'] = prepared.get('storage_backend_ready') is True
            stored = play('storage', prepared['variables'])
            for key in ('storage_backend_ready', 'storage_csi_ready', 'storage_pvc_ready', 'storage_ready'):
                output[key] = stored.get(key) is True
            require(output['storage_ready'], 'shared PVC mobility not proven')
            require(deadline > time.monotonic(), 'execution deadline exceeded before edge')
            output['stage'] = 'edge'
            current_edge = _addon('cluster_edge').validate(request)['binding_sha256']
            require(expected_edge_binding is None or current_edge == expected_edge_binding, 'edge operator configuration changed during execution')
            provision = edge_provision or _addon('cluster_edge').provision
            edge = provision(request, state_dir=Path(state_dir or ansible.STATE_DIR) / 'cluster-edge',
                             deadline=deadline, env=env, expected_binding_sha256=expected_edge_binding,
                             kube=kubectl(request, ports, env, deadline))
            require(edge.get('edge_ready') is True, 'public edge configuration unproven')
            output['edge_ready'] = True
            output['public_http_verified'] = edge.get('public_http_verified') is True
            output['steps'].append({'stage': 'edge', 'status': 'succeeded', 'public_http_verified': output['public_http_verified']})
            output['stage'] = 'transport_cleanup'
            uncertain = True
        uncertain = False
        output['stage'] = 'edge'
        output['status'] = 'succeeded'
        return output
    except (OSError, ValueError, subprocess.TimeoutExpired, ImportError) as exc:
        if output['steps'] and output['steps'][-1]['status'] == 'running':
            output['steps'][-1]['status'] = 'failed'
        return ansible.fail(output, 'failed' if mutated else 'blocked', 'KUBEADM_STAGE_FAILED',
                            'Stage failed; inspect private executor logs and reconcile before retry (' + type(exc).__name__ + ')', unknown=bool(getattr(exc, 'unknown', uncertain) or getattr(exc, '__notes__', [])))


def run(request, *, validate_only=False, runner=ansible.execute, state_dir=None, storage_prepare=None, edge_provision=None):
    try:
        ansible.validate(request)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return ansible.fail(result({}), 'invalid', 'INVALID_REQUEST', str(exc))
    output = result(request)
    if validate_only:
        output['status'] = 'validated'
        return output
    expected_edge_binding = _addon('cluster_edge').validate(request)['binding_sha256']
    return ansible.run_locked(request, output, ansible.lock_keys(request),
        lambda: _execute(request, runner=runner, state_dir=state_dir,
                         storage_prepare=storage_prepare, edge_provision=edge_provision, expected_edge_binding=expected_edge_binding), state_dir=state_dir)
