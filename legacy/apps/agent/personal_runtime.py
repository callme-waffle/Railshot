"""Prepare one explicitly selected OpenStack VM using the existing Ansible runtime.

Provider credentials and general SSH keys stay on the customer's management host.
This module never installs Kubernetes on the management host or deploys an app.
"""
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deployment/bootstrap'))
from client_setup.state import atomic_private_write, read_private, private_directory

IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def require(value, code):
    if not value:
        raise ValueError(code)


def native(argv, *, input=None, timeout=60):
    result = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout, check=False)
    require(result.returncode == 0, 'RUNTIME_LOCAL_COMMAND_FAILED')
    return result.stdout


def normalized(value):
    return {k.lower().replace(' ', '_'): v for k, v in value.items()}


def identifier(value):
    require(isinstance(value, str) and IDENT.fullmatch(value), 'RUNTIME_IDENTIFIER_INVALID')
    return value


def choose(rows, title, reader=input):
    require(isinstance(rows, list) and 0 < len(rows) <= 10000, 'RUNTIME_CHOICES_UNAVAILABLE')
    print(title)
    for index, row in enumerate(rows, 1):
        row = normalized(row)
        name = str(row.get('name', row.get('id', '')))
        print(str(index) + '. ' + ''.join(c for c in name if c.isprintable())[:160])
    selected = reader('번호: ').strip()
    require(selected.isdigit() and 1 <= int(selected) <= len(rows), 'RUNTIME_SELECTION_REQUIRED')
    return identifier(normalized(rows[int(selected) - 1]).get('id'))


def private_reference(value):
    path = Path(value)
    require(path.is_absolute() and '..' not in path.parts and not any(c.isspace() for c in value), 'RUNTIME_PRIVATE_PATH_INVALID')
    require(bool(read_private(path)), 'RUNTIME_PRIVATE_FILE_EMPTY')
    return str(path)


def import_ssh_reference(value, destination, *, identity):
    """Copy a chosen sudo caller's SSH input into the root-only client store."""
    caller = os.environ.get('SUDO_UID', '')
    if value.startswith('~/') and caller.isdigit():
        value = str(Path(pwd.getpwuid(int(caller)).pw_dir) / value[2:])
    source = Path(value).expanduser()
    require(source.is_absolute() and not any(p.is_symlink() for p in (source, *source.parents)), 'RUNTIME_SSH_INPUT_UNSAFE')
    owners = {os.geteuid()}
    if caller.isdigit():
        owners.add(int(caller))
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid in owners and info.st_nlink == 1
                and not info.st_mode & (0o077 if identity else 0o022), 'RUNTIME_SSH_INPUT_UNSAFE')
        data = stream.read(1048577)
    require(0 < len(data) <= 1048576, 'RUNTIME_SSH_INPUT_INVALID')
    if destination.exists():
        require(read_private(destination) == data, 'RUNTIME_SSH_REFERENCE_CHANGED')
    else:
        atomic_private_write(destination, data)
    return str(destination)


def prompt_plan(cli, directory, project_id, reader=input):
    mode = reader('앱 실행용 별도 VM: 1 기존 VM 선택 / 2 새 VM 생성: ').strip()
    require(mode in ('1', '2'), 'RUNTIME_SELECTION_REQUIRED')
    plan = {'mode': 'existing' if mode == '1' else 'create'}
    if mode == '1':
        rows = []
        for row in cli.run(['server', 'list']):
            found = normalized(cli.run(['server', 'show', identifier(normalized(row).get('id'))]))
            if found.get('project_id', found.get('tenant_id')) == project_id:
                rows.append(found)
        plan['resource_id'] = choose(rows, '앱을 실행할 VM을 선택하십시오. OpenStack 관리 호스트는 선택할 수 없습니다.', reader)
        plan['identity_file'] = import_ssh_reference(reader('선택 VM의 SSH 개인키 파일(현재 사용자 또는 root 소유, 0600): ').strip(), directory / 'runtime_id_ed25519', identity=True)
    else:
        plan['image_id'] = choose(cli.run(['image', 'list']), 'Ubuntu 22.04/24.04 amd64 클라우드 이미지를 선택하십시오.', reader)
        plan['flavor_id'] = choose(cli.run(['flavor', 'list']), '2 CPU·2 GB 메모리·여유 디스크 10 GB 이상인 크기를 선택하십시오.', reader)
        plan['network_id'] = choose(cli.run(['network', 'list']), '관리 호스트에서 SSH로 도달할 수 있는 네트워크를 선택하십시오.', reader)
        plan['name'] = identifier(reader('새 실행 VM 이름: ').strip())
        source = ipaddress.ip_network(reader('이 네트워크로 접속하는 관리 호스트의 IPv4 주소 범위(/32): ').strip(), strict=True)
        require(source.version == 4 and source.prefixlen == 32, 'RUNTIME_MANAGEMENT_SOURCE_REQUIRED')
        plan['ssh_source_cidr'] = str(source)
    plan['ssh_user'] = identifier(reader('VM의 sudo 가능한 비 root SSH 사용자(예: ubuntu): ').strip())
    require(plan['ssh_user'] != 'root', 'RUNTIME_NONROOT_SSH_REQUIRED')
    # Existing guests use an independently verified host key. New guests prompt
    # for the provider-console verified public host key after their address exists.
    if mode == '1':
        plan['known_hosts_file'] = import_ssh_reference(reader('독립적으로 검증한 known_hosts 파일: ').strip(), directory / 'runtime_known_hosts', identity=False)
    return plan


def create_once(cli, directory, stage, argv):
    """Persist intent before provider writes; uncertain creates are never replayed."""
    path = directory / 'runtime-create.json'
    records = json.loads(read_private(path)) if path.exists() else {}
    prior = records.get(stage)
    if prior:
        require(prior.get('argv') == argv and prior.get('status') == 'succeeded', 'RUNTIME_CREATE_OUTCOME_UNKNOWN')
        return prior['result']
    records[stage] = {'argv': argv, 'status': 'unknown'}
    atomic_private_write(path, json.dumps(records).encode())
    result = normalized(cli.run(argv))
    # Keep provider identifiers only, never full output or generated secrets.
    safe = {k: result[k] for k in ('id', 'name') if k in result}
    require(safe.get('id') or safe.get('name'), 'RUNTIME_CREATE_RESULT_INVALID')
    records[stage].update(status='succeeded', result=safe)
    atomic_private_write(path, json.dumps(records).encode())
    return safe


def create_vm(cli, config, plan, directory, runner=native):
    network = normalized(cli.run(['network', 'show', identifier(plan['network_id'])]))
    require(network.get('project_id', network.get('tenant_id')) == config['project_id'] or network.get('shared') is True,
            'RUNTIME_NETWORK_PROJECT_MISMATCH')
    image = normalized(cli.run(['image', 'show', identifier(plan['image_id'])]))
    require(str(image.get('status', '')).lower() == 'active', 'RUNTIME_IMAGE_NOT_ACTIVE')
    require(image.get('owner') == config['project_id'] or image.get('visibility') in ('public', 'shared'), 'RUNTIME_IMAGE_PROJECT_MISMATCH')
    flavor = normalized(cli.run(['flavor', 'show', identifier(plan['flavor_id'])]))
    require(int(flavor.get('vcpus', 0)) >= 2 and int(flavor.get('ram', 0)) >= 1800 and int(flavor.get('disk', 0)) >= 15,
            'RUNTIME_FLAVOR_TOO_SMALL')
    source = ipaddress.ip_network(plan['ssh_source_cidr'], strict=True)
    require(source.version == 4 and source.prefixlen == 32, 'RUNTIME_MANAGEMENT_SOURCE_REQUIRED')
    key = directory / 'runtime_id_ed25519'
    if not key.exists():
        require(not (directory / 'runtime-create.json').exists(), 'RUNTIME_SSH_KEY_MISSING_AFTER_CREATE')
        require(not key.with_suffix('.pub').exists(), 'RUNTIME_SSH_KEY_PARTIAL')
        runner(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)])
    private_reference(str(key))
    public = key.with_suffix('.pub')
    require(public.is_file() and not public.is_symlink(), 'RUNTIME_SSH_KEY_PARTIAL')
    prefix = 'railshot-' + config['target_id'][-32:] + '-' + str(config['generation'])
    create_once(cli, directory, 'keypair', ['keypair', 'create', '--public-key', str(public), prefix])
    remote_key = normalized(cli.run(['keypair', 'show', prefix])).get('public_key', '')
    require(remote_key.split()[:2] == public.read_text().split()[:2], 'RUNTIME_KEYPAIR_CHANGED')
    group = create_once(cli, directory, 'security-group', ['security', 'group', 'create', '--description', 'RailShot runtime management only', prefix])
    group_id = identifier(group['id'])
    remote_group = normalized(cli.run(['security', 'group', 'show', group_id]))
    require(remote_group.get('project_id', remote_group.get('tenant_id')) == config['project_id'], 'RUNTIME_GROUP_PROJECT_MISMATCH')
    for port in ('22', '6443'):
        create_once(cli, directory, 'ingress-' + port, ['security', 'group', 'rule', 'create', '--ingress', '--ethertype', 'IPv4',
                    '--protocol', 'tcp', '--dst-port', port, '--remote-ip', str(source), group_id])
    # Fixed cloud-init installs only the SSH access requested by the customer.
    # The existing Ansible runtime remains the sole Kubernetes installer.
    cloud = {'users': [{'name': identifier(plan['ssh_user']), 'lock_passwd': True, 'shell': '/bin/bash',
                       'sudo': ['ALL=(ALL) NOPASSWD:ALL'], 'ssh_authorized_keys': [public.read_text().strip()]}],
             'ssh_pwauth': False, 'disable_root': True,
             'runcmd': [['sh', '-c', 'printf "RAILSHOT_VM_HOST_KEY:' + prefix + ' " > /dev/console; cat /etc/ssh/ssh_host_ed25519_key.pub > /dev/console',
                         ]]}
    cloud_path = directory / 'runtime-cloud-init.json'
    atomic_private_write(cloud_path, ('#cloud-config\n' + json.dumps(cloud)).encode())
    server = create_once(cli, directory, 'server', ['server', 'create', '--image', plan['image_id'], '--flavor', plan['flavor_id'],
            '--network', plan['network_id'], '--key-name', prefix, '--security-group', group_id,
            '--property', 'railshot.target=' + config['target_id'], '--property', 'railshot.generation=' + str(config['generation']),
            '--user-data', str(cloud_path), identifier(plan['name'])])
    return identifier(server['id']), str(key)


def console_host_key(cli, resource, config, runner=native, sleeper=time.sleep, timeout=600):
    """Trust only console output from the project-bound instance just created."""
    from infrastructure.providers.openstack.cli import OpenStackCLI
    from apps.agent.install_forced_command import authorized_key_line
    prefix = 'RAILSHOT_VM_HOST_KEY:railshot-' + config['target_id'][-32:] + '-' + str(config['generation']) + ' '
    deadline = time.monotonic() + timeout
    while True:
        before = normalized(cli.run(['server', 'show', resource]))
        require(before.get('id') == resource and before.get('project_id', before.get('tenant_id')) == config['project_id'], 'RUNTIME_SERVER_PROJECT_MISMATCH')
        captured = []
        def capture(argv, **kwargs):
            result = cli.runner(argv, **kwargs)
            captured.append(result.stdout)
            return result
        probe = OpenStackCLI(cli.auth, runner=capture, timeout=cli.timeout)
        probe.run(['console', 'log', 'show', resource, '--lines', '200'], json_output=False)
        keys = set()
        for line in ''.join(captured).splitlines():
            if line.startswith(prefix):
                public = line[len(prefix):].strip()
                authorized_key_line(public, '127.0.0.1', '/usr/bin/python3', '/opt/railshot/personal', '/etc/railshot-personal')
                keys.add(' '.join(public.split()[:2]))
        require(len(keys) <= 1, 'RUNTIME_CONSOLE_HOST_KEY_CONFLICT')
        if keys:
            after = normalized(cli.run(['server', 'show', resource]))
            require(after.get('id') == resource and after.get('project_id', after.get('tenant_id')) == config['project_id'], 'RUNTIME_SERVER_PROJECT_MISMATCH')
            return keys.pop()
        require(time.monotonic() < deadline, 'RUNTIME_CONSOLE_HOST_KEY_UNAVAILABLE')
        sleeper(5)


def server_addresses(server):
    raw = server.get('addresses', {})
    rows = []
    if isinstance(raw, str):
        for part in raw.split(';'):
            if '=' not in part:
                continue
            network, values = part.split('=', 1)
            rows.extend((network.strip(), value.strip()) for value in values.split(','))
    elif isinstance(raw, dict):
        for network, values in raw.items():
            rows.extend((network, value.get('addr') if isinstance(value, dict) else value) for value in values)
    elif isinstance(raw, list):
        rows.extend((value.get('network'), value.get('address')) for value in raw)
    result = []
    for network, value in rows:
        try:
            address = ipaddress.ip_address(value)
        except (ValueError, TypeError):
            continue
        if address.version == 4 and any(address in pool for pool in PRIVATE):
            result.append({'network': network, 'address': str(address), 'version': 4})
    require(result, 'RUNTIME_PRIVATE_ADDRESS_REQUIRED')
    return result


def local_addresses(runner=native):
    return {entry['local'] for row in json.loads(runner(['ip', '-j', '-4', 'address', 'show']))
            for entry in row.get('addr_info', []) if entry.get('family') == 'inet'}


def ssh_command(runtime):
    ssh = runtime['ssh']
    require(ssh['host'] == runtime['private_ipv4'] and ssh['port'] == 22 and identifier(ssh['user']) != 'root', 'RUNTIME_SSH_BINDING_INVALID')
    return ['ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=15',
            '-o', 'UserKnownHostsFile=' + private_reference(ssh['known_hosts_file']), '-i', private_reference(ssh['identity_file']),
            '-p', '22', ssh['user'] + '@' + ssh['host']]


INSPECT = '''import json, pathlib, subprocess, platform
def command(args):
    p=subprocess.run(args,capture_output=True,text=True,timeout=45)
    if p.returncode: raise ValueError('runtime inspection failed')
    return p.stdout
present=any(pathlib.Path(p).exists() for p in ('/usr/local/bin/k3s','/etc/rancher/k3s','/var/lib/rancher/k3s','/etc/systemd/system/k3s.service'))
result={'machine_id':pathlib.Path('/etc/machine-id').read_text().strip(), 'management_host':pathlib.Path('/opt/stack').exists(), 'architecture':platform.machine(),'present':present,'ready':False}
if present:
    try:
        ready=command(['/usr/local/bin/k3s','kubectl','--request-timeout=20s','get','--raw=/readyz']).strip()=='ok'
        nodes=json.loads(command(['/usr/local/bin/k3s','kubectl','--request-timeout=20s','get','nodes','-o','json']))['items']
        result['node_addresses']=[a['address'] for n in nodes for a in n['status']['addresses'] if a['type']=='InternalIP']
        result['ready']=ready and bool(nodes) and all(any(c['type']=='Ready' and c['status']=='True' for c in n['status']['conditions']) for n in nodes)
    except Exception: pass
print(json.dumps(result))
'''


def inspect_vm(runtime, runner=native):
    require(runtime['private_ipv4'] not in local_addresses(runner), 'RUNTIME_MANAGEMENT_HOST_FORBIDDEN')
    result = json.loads(runner([*ssh_command(runtime), 'sudo -n /usr/bin/python3 -'], input=INSPECT, timeout=120))
    local_id = Path('/etc/machine-id').read_text().strip()
    require(result['machine_id'] != local_id and not result['management_host'], 'RUNTIME_MANAGEMENT_HOST_FORBIDDEN')
    require(result['architecture'] == 'x86_64', 'RUNTIME_ARCHITECTURE_UNSUPPORTED')
    if result['present']:
        require(result['ready'] and runtime['private_ipv4'] in result.get('node_addresses', []), 'RUNTIME_EXISTING_CLUSTER_UNHEALTHY')
    return result


def ansible_adapter():
    folder = ROOT / 'infrastructure/ansible'
    sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location('personal_runtime_ansible', folder / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_cluster(runtime, server, state, runner=native, adapter=None):
    observed = inspect_vm(runtime, runner)
    if observed['present']:
        return {'reused': True}
    if adapter is None:
        # Enrollment has already completed. Slow Ansible dependency downloads
        # cannot consume the one-time enrollment token's validity window.
        binary = Path(sys.executable).parent / 'ansible-playbook'
        if not binary.is_file():
            require(sys.prefix != sys.base_prefix, 'RUNTIME_VIRTUALENV_REQUIRED')
            runner([sys.executable, '-m', 'pip', 'install', 'ansible-core==2.19.13'], timeout=900)
        os.environ['PATH'] = str(binary.parent) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin')
    adapter = adapter or ansible_adapter()
    request = adapter.from_openstack(server, request_id='personal-runtime-' + runtime['target_id'] + '-' + str(runtime['generation']),
        operation='runtime.install', target_id=runtime['target_id'], resource_id=runtime['resource_id'], project_id=runtime['project_id'],
        management_network=runtime['management_network'], placement=runtime['placement'], architecture=runtime['architecture'],
        initialization=runtime['initialization'], ssh={k: runtime['ssh'][k] for k in ('user', 'port', 'identity_file', 'known_hosts_file')}, timeout_seconds=1800)
    # The reviewed adapter owns request journaling, physical-node locks, guest
    # checks and runtime.yml. Do not replay an unknown prior installation.
    result = adapter.run(request, state_dir=state / 'runtime-ansible')
    require(result.get('status') == 'succeeded' and result.get('runtime_ready') is True, 'RUNTIME_ANSIBLE_NOT_READY')
    require(inspect_vm(runtime, runner)['ready'], 'RUNTIME_POST_INSTALL_UNVERIFIED')
    return {'reused': False}


def prepare(config, cli, directory, state, plan_path=None, reader=input, runner=native, sleeper=time.sleep, progress=lambda stage: None):
    progress('client_selection')
    require(cli.run(['token', 'issue']).get('project_id') == config['project_id'], 'RUNTIME_PROJECT_MISMATCH')
    stored_plan = directory / 'runtime-plan.json'
    if stored_plan.exists():
        plan = json.loads(read_private(stored_plan))
    else:
        plan = json.loads(read_private(Path(plan_path))) if plan_path else prompt_plan(cli, directory, config['project_id'], reader)
        require(plan.get('mode') in ('existing', 'create') and isinstance(plan.get('ssh_user'), str)
                and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', plan['ssh_user']) and plan['ssh_user'] != 'root', 'RUNTIME_PLAN_INVALID')
        atomic_private_write(stored_plan, json.dumps(plan).encode())
    if plan['mode'] == 'create':
        progress('client_installation')
        resource, identity = create_vm(cli, config, plan, directory, runner)
    else:
        resource, identity = identifier(plan['resource_id']), private_reference(plan['identity_file'])
    deadline = time.monotonic() + 600
    while True:
        server = normalized(cli.run(['server', 'show', resource]))
        require(server.get('id') == resource and server.get('project_id', server.get('tenant_id')) == config['project_id'], 'RUNTIME_SERVER_PROJECT_MISMATCH')
        if server.get('status') == 'ACTIVE':
            break
        require(plan['mode'] == 'create' and server.get('status') in ('BUILD', 'REBUILD') and time.monotonic() < deadline, 'RUNTIME_SERVER_NOT_ACTIVE')
        sleeper(5)
    addresses = server_addresses(server)
    if 'management_network' in plan:
        candidates = [row for row in addresses if row['network'] == plan['management_network']]
    elif len(addresses) == 1:
        candidates = addresses
    else:
        for index, row in enumerate(addresses, 1):
            print(str(index) + '. ' + row['network'] + ' ' + row['address'])
        choice = reader('관리 호스트에서 접근할 VM 네트워크 주소 번호: ').strip()
        require(choice.isdigit() and 1 <= int(choice) <= len(addresses), 'RUNTIME_ADDRESS_SELECTION_REQUIRED')
        candidates = [addresses[int(choice) - 1]]
    require(len(candidates) == 1, 'RUNTIME_ADDRESS_AMBIGUOUS')
    chosen = candidates[0]
    require(chosen['address'] not in local_addresses(runner), 'RUNTIME_MANAGEMENT_HOST_FORBIDDEN')
    hosts = plan.get('known_hosts_file')
    if not hosts:
        hosts = str(directory / 'runtime_known_hosts')
        if not Path(hosts).exists():
            require(plan['mode'] == 'create', 'RUNTIME_KNOWN_HOSTS_REQUIRED')
            key = console_host_key(cli, resource, config, runner, sleeper)
            atomic_private_write(Path(hosts), (chosen['address'] + ' ' + ' '.join(key.split()[:2]) + '\n').encode())
    placement = plan.get('placement', server.get('os-ext-az:availability_zone', 'nova'))
    runtime = {**{k: config[k] for k in ('target_id', 'generation', 'project_id')}, 'version': 1,
        'resource_id': resource, 'private_ipv4': chosen['address'], 'management_network': chosen['network'],
        'placement': placement, 'architecture': 'amd64', 'initialization': 'cloud-init' if plan['mode'] == 'create' else 'preconfigured',
        'ssh': {'user': plan['ssh_user'], 'host': chosen['address'], 'port': 22,
                'identity_file': identity, 'known_hosts_file': private_reference(hosts)}}
    runtime_path = directory / 'runtime.json'
    if runtime_path.exists():
        require(json.loads(read_private(runtime_path)) == runtime, 'RUNTIME_BINDING_CHANGED')
    else:
        atomic_private_write(runtime_path, json.dumps(runtime).encode())
    server.update(project_id=config['project_id'], addresses=addresses)
    if plan['mode'] == 'create':
        deadline = time.monotonic() + 600
        while True:
            try:
                runner([*ssh_command(runtime), 'sudo -n /usr/bin/true'], timeout=25)
                break
            except (ValueError, subprocess.TimeoutExpired):
                require(time.monotonic() < deadline, 'RUNTIME_SSH_NOT_READY')
                sleeper(5)
    progress('client_installation')
    ensure_cluster(runtime, server, state, runner)
    progress('client_verification')
    return runtime
