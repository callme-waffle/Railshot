#!/usr/bin/env python3
"""Trusted product worker: registered VMs -> HA database -> private app binding."""
import argparse
from collections import Counter
from contextlib import ExitStack
import copy
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time

import database
import inputs
import run as ansible


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_json(path):
    return json.loads(database.private_bytes(str(path)), object_pairs_hook=ansible.unique_pairs)


def private_dir(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('private absolute directory required')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private executor-owned directory required')
    return path


def load(registry_file, spec_file):
    registry, spec = read_json(registry_file), read_json(spec_file)
    required = {'version', 'request_id', 'cluster_name', 'app_name', 'nodes', 'client_cidrs'}
    if (not isinstance(spec, dict) or not required <= set(spec)
            or set(spec) - required - {'timeout_seconds'} or spec['version'] != 1
            or not isinstance(spec['cluster_name'], str)
            or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', spec['cluster_name'])
            or not isinstance(spec['app_name'], str)
            or not re.fullmatch(r'[a-z][a-z0-9-]{1,28}[a-z0-9]', spec['app_name'])):
        raise ValueError('invalid cluster specification')
    timeout = spec.get('timeout_seconds', 1200)
    if type(timeout) is not int or not 30 <= timeout <= 1800:
        raise ValueError('invalid cluster timeout')
    if (not isinstance(registry, dict) or set(registry) != {'version', 'targets'}
            or registry['version'] != 1 or not isinstance(registry['targets'], dict)):
        raise ValueError('invalid trusted registry')
    if not isinstance(spec['client_cidrs'], list) or not 1 <= len(spec['client_cidrs']) <= 32:
        raise ValueError('private client CIDRs required')
    for cidr in spec['client_cidrs']:
        network = ipaddress.IPv4Network(cidr, strict=True)
        if not any(network.subnet_of(private) for private in ansible.PRIVATE):
            raise ValueError('private client CIDRs required')
    # Validate node/role shapes before resolving paths from the trusted registry.
    body = {'request_id': spec['request_id'], 'target_id': spec['cluster_name'],
            'operation': 'database.configure', 'parameters': {'mode': 'patroni',
            'nodes': spec['nodes'], 'placements': [{'provider': 'aws', 'site': 'pending',
            'database_nodes': 0, 'dcs_voters': 0}], 'profile_id': spec['cluster_name']}}
    inputs.validate_job(body)
    nodes, counts, references = {}, Counter(), {}
    for selection in spec['nodes']:
        target_id = selection['target_id']
        target = registry['targets'].get(target_id)
        required_target = {'purpose', 'descriptor_file', 'ssh'}
        if (not isinstance(target, dict) or not required_target <= set(target)
                or set(target) - required_target - {'timeout_seconds'}
                or target['purpose'] != 'database'):
            raise ValueError('registered database target required')
        raw = database.private_bytes(target['descriptor_file'])
        descriptor = json.loads(raw, object_pairs_hook=ansible.unique_pairs)
        if descriptor.get('target_id') != target_id:
            raise ValueError('descriptor target binding mismatch')
        disk = descriptor.get('data_disk')
        if (descriptor.get('purpose') != 'database' or not isinstance(disk, dict)
                or disk.get('mount_path') != '/var/lib/postgresql' or disk.get('preservation') != 'retain'
                or not isinstance(disk.get('resource_id'), str) or not disk['resource_id']):
            raise ValueError('retained PostgreSQL data disk descriptor required')
        request = ansible.from_descriptor(descriptor, request_id=spec['request_id'],
            operation='guest.check', ssh=target['ssh'], timeout_seconds=target.get('timeout_seconds', timeout))
        references[target['descriptor_file']] = digest(raw)
        for key in ('identity_file', 'known_hosts_file'):
            path = request['inventory']['control_plane'][0]['ssh'][key]
            references[path] = digest(database.private_bytes(path))
        nodes[target_id] = request
        for role in selection['roles']:
            counts[request['target']['provider'], request['target']['placement'], role] += 1
    body['target_id'] = spec['nodes'][0]['target_id']
    body['parameters']['placements'] = [
        {'provider': provider, 'site': site, 'database_nodes': counts[provider, site, 'database'],
         'dcs_voters': counts[provider, site, 'dcs'], 'proxy_nodes': counts[provider, site, 'proxy']}
        for provider, site in sorted({(p, s) for p, s, _ in counts})]
    plan = inputs.database_plan(body, inputs.validate_job(body), nodes.__getitem__)
    if plan['blockers'] != [{'code': 'DATABASE_PROFILE_REQUIRED'}]:
        raise ValueError('HA requires DB >= 2, odd DCS >= 3 and a separate proxy')
    identity = {'spec': spec, 'registry': registry, 'nodes': nodes, 'files': references}
    return spec, body, nodes, digest(encoded(identity))


def command(argv, timeout=60):
    if ansible.execute(argv, timeout, ansible.child_env()):
        raise ValueError('private preparation command failed')


def generate(work, destination, spec, body, nodes):
    """Publish this whole directory once; interrupted preparation never rotates credentials."""
    openssl, vault = shutil.which('openssl'), shutil.which('ansible-vault')
    if not openssl or not vault:
        raise ValueError('openssl and ansible-vault are required')
    def write(name, value):
        ansible.durable_write(work / name, value if isinstance(value, bytes) else encoded(value))
    def reference(name):
        return str(destination / name)
    addresses = {key: value['inventory']['control_plane'][0]['private_ipv4'] for key, value in nodes.items()}
    proxies = sorted(item['target_id'] for item in spec['nodes'] if 'proxy' in item['roles'])
    certificates = {}
    # ponytail: certificates last 30 days; rotation needs a separately approved maintenance operation.
    for authority in ('etcd', 'patroni', 'postgres'):
        command([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '30',
            '-subj', '/CN=' + spec['cluster_name'][:48] + '-' + authority, '-keyout', str(work / (authority + '-ca.key')),
            '-out', str(work / (authority + '-ca.crt')), '-addext', 'basicConstraints=critical,CA:TRUE',
            '-addext', 'keyUsage=critical,keyCertSign,cRLSign'])
    for selection in spec['nodes']:
        name, roles = selection['target_id'], selection['roles']
        fields, purposes = {}, []
        if 'dcs' in roles:
            fields['etcd_ca_src'] = reference('etcd-ca.crt')
            purposes.append(('etcd', 'etcd', 'etcd_cert_src', 'etcd_key_src'))
        if 'database' in roles:
            fields.update(etcd_ca_src=reference('etcd-ca.crt'), patroni_api_ca_src=reference('patroni-ca.crt'),
                          postgres_tls_ca_src=reference('postgres-ca.crt'))
            purposes.extend([('etcd-client', 'etcd', 'patroni_etcd_cert_src', 'patroni_etcd_key_src'),
                ('api', 'patroni', 'patroni_api_cert_src', 'patroni_api_key_src'),
                ('postgres', 'postgres', 'postgres_tls_cert_src', 'postgres_tls_key_src')])
        if 'proxy' in roles:
            fields['patroni_api_ca_src'] = reference('patroni-ca.crt')
        for purpose, authority, cert_field, key_field in purposes:
            stem = name + '-' + purpose
            ips = sorted({addresses[name], *(addresses[key] for key in proxies if purpose == 'postgres')})
            write(stem + '.ext', ('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                'extendedKeyUsage=serverAuth,clientAuth\nsubjectAltName=DNS:' + name + ',' +
                ','.join('IP:' + ip for ip in ips) + '\n').encode())
            command([openssl, 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=' + name,
                     '-keyout', str(work / (stem + '.key')), '-out', str(work / (stem + '.csr'))])
            command([openssl, 'x509', '-req', '-in', str(work / (stem + '.csr')), '-CA', str(work / (authority + '-ca.crt')),
                '-CAkey', str(work / (authority + '-ca.key')), '-set_serial', str(secrets.randbits(128) or 1),
                '-days', '30', '-extfile', str(work / (stem + '.ext')), '-out', str(work / (stem + '.crt'))])
            fields[cert_field], fields[key_field] = reference(stem + '.crt'), reference(stem + '.key')
        certificates[name] = fields
    write('vault-password', (secrets.token_urlsafe(48) + '\n').encode())
    write('vault.yml', {key: secrets.token_urlsafe(32) for key in sorted(database.SECRETS)})
    command([vault, 'encrypt', '--vault-password-file', str(work / 'vault-password'), str(work / 'vault.yml')])
    profile = {'parameters': body['parameters'], 'cluster_name': spec['cluster_name'],
        'client_cidrs': sorted(set(spec['client_cidrs']) | {ip + '/32' for ip in addresses.values()}),
        'certificates': certificates, 'require_postgres_mount': True, 'vault_file': reference('vault.yml'),
        'vault_password_file': reference('vault-password'), 'timeout_seconds': spec.get('timeout_seconds', 1200)}
    write('profile.json', profile)
    identifier = spec['app_name'].replace('-', '_')[:24] + '_' + digest(encoded([spec['cluster_name'], spec['app_name']]))[:12]
    binding = {'version': 1, 'request_id': spec['request_id'], 'cluster_name': spec['cluster_name'],
        'app_name': spec['app_name'], 'host': addresses[proxies[0]], 'port': 5432, 'database': identifier,
        'migration': {'username': identifier + '_owner', 'password': secrets.token_urlsafe(32)},
        'runtime': {'username': identifier + '_runtime', 'password': secrets.token_urlsafe(32)},
        'sslmode': 'verify-full', 'sslrootcert': reference('postgres-ca.crt')}
    write('binding.json', binding)
    for path in work.iterdir():
        path.chmod(0o600)


def application(request, binding_file, runner=ansible.execute):
    result = database.result_for(request)
    result['stage'] = 'application_database'
    started = False
    try:
        with ExitStack() as stack:
            work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='railshot-db-app-')))
            deadline = time.monotonic() + request['timeout_seconds']
            binding = read_json(binding_file)
            if digest(encoded(binding)) != request['binding_sha256']:
                raise ValueError('binding changed')
            hosts = {}
            selections = {item['target_id']: item['roles'] for item in request['profile']['parameters']['nodes']}
            for original in request['database_nodes']:
                node = copy.deepcopy(original)
                physical = node['inventory']['control_plane'][0]
                if 'database' not in selections[physical['id']]:
                    continue
                for key in ('identity_file', 'known_hosts_file'):
                    reference = physical['ssh'][key]
                    raw = database.private_bytes(reference)
                    if digest(raw) != request['file_sha256'][reference]:
                        raise ValueError('SSH file changed after admission')
                    snapshot = work / digest(reference.encode())
                    ansible.durable_write(snapshot, raw)
                    physical['ssh'][key] = str(snapshot)
                forwarded = stack.enter_context(ansible.forwarded_port(physical['ssh'].get('transport_ref'), deadline))
                hosts.update(ansible.build_inventory(node, forwarded)['all']['children']['k3s_server']['hosts'])
            inventory = work / 'inventory.json'
            ansible.durable_write(inventory, encoded({'db_nodes': {'hosts': hosts}}))
            receipt = work / 'receipt.json'
            expected = {'request_id': request['request_id'], 'binding_sha256': request['binding_sha256'],
                        'nonce': secrets.token_hex(16), 'application_database_ready': True}
            variables = work / 'vars.json'
            ansible.durable_write(variables, encoded({'railshot_binding': binding,
                'railshot_application_receipt': expected, 'railshot_application_receipt_path': str(receipt)}))
            executable = shutil.which('ansible-playbook')
            if not executable:
                raise ValueError('Ansible unavailable')
            started = True
            rc = runner([executable, '-i', str(inventory), str(ansible.HERE / 'application-database.yml'),
                         '--extra-vars', '@' + str(variables)], max(0.001, deadline - time.monotonic()), ansible.child_env())
            if rc or read_json(receipt) != expected:
                raise ValueError('application database readiness unproven')
            result.update(status='succeeded', guest_ready=True, database_ready=True)
            return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ansible.fail(result, 'failed' if started else 'blocked', 'APPLICATION_DATABASE_UNPROVEN',
                            'Application database completion requires operator reconciliation', unknown=started)


def run(registry_file, spec_file, state_dir, *, database_runner=database.run, application_runner=application):
    spec, body, nodes, input_digest = load(registry_file, spec_file)
    if Path(state_dir).resolve().is_relative_to(ansible.ROOT):
        raise ValueError('private cluster state must stay outside the repository')
    root = private_dir(state_dir)
    destination = root / spec['cluster_name']
    lock_fd = os.open(root / (spec['cluster_name'] + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix='.prepare-', dir=root) as directory:
                work = Path(directory)
                generate(work, destination, spec, body, nodes)
                manifest = {'input_sha256': input_digest, 'files': {
                    path.name: digest(database.private_bytes(str(path))) for path in work.iterdir()}}
                ansible.durable_write(work / 'manifest.json', encoded(manifest))
                os.rename(work, destination)
        private_dir(destination)
        manifest = read_json(destination / 'manifest.json')
        if manifest['input_sha256'] != input_digest:
            raise ValueError('CLUSTER_INPUT_CONFLICT')
        for name, expected in manifest['files'].items():
            if Path(name).name != name or digest(database.private_bytes(str(destination / name))) != expected:
                raise ValueError('CLUSTER_FILES_CHANGED')
        _, request = database.prepare(body, {spec['cluster_name']: str(destination / 'profile.json')}, nodes.__getitem__)
        if request is None:
            raise ValueError('cluster execution profile unavailable')
        result = database_runner(request, state_dir=root / 'jobs')
        if result['status'] != 'succeeded':
            return result
        binding_file = destination / 'binding.json'
        binding_digest = manifest['files']['binding.json']
        app_request = {**request, 'request_id': digest((spec['request_id'] + ':application').encode()),
                       'binding_sha256': binding_digest}
        result = ansible.run_locked(app_request, database.result_for(app_request), ansible.lock_keys(app_request),
            lambda: application_runner(app_request, binding_file), state_dir=root / 'jobs')
        if result['status'] != 'succeeded':
            return result
        receipt = {'status': 'succeeded', 'request_id': spec['request_id'], 'target_id': body['target_id'],
                   'database_ready': True, 'binding_file': str(binding_file), 'binding_sha256': binding_digest}
        ansible.durable_write(destination / 'receipt.json', encoded(receipt))
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry-file', required=True)
    parser.add_argument('--spec-file', required=True)
    parser.add_argument('--state-dir', required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        result = run(args.registry_file, args.spec_file, args.state_dir)
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        result = {'status': 'blocked', 'database_ready': False, 'error': {'code': 'CLUSTER_PREPARATION_BLOCKED'}}
    print(json.dumps(result, separators=(',', ':')))
    return 0 if result['status'] == 'succeeded' else 3


if __name__ == '__main__':
    raise SystemExit(main())
