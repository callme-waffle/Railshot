#!/usr/bin/env python3
"""Outbound personal environment client. No remote shell or arbitrary command API."""
import argparse
import base64
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import pwd
import tempfile
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'deployment/bootstrap'))
from client_setup.state import atomic_private_write, private_directory, read_private
from client_setup.credentials import CredentialStore
from infrastructure.providers.openstack.cli import OpenStackCLI

VERSION = '1.0.0'
CONFIG = Path('/etc/railshot-personal')
STATE = Path('/var/lib/railshot-personal')
PAYLOAD = Path('/opt/railshot/personal')
UNIT = Path('/etc/systemd/system/railshot-personal.service')
RUNTIME_UNIT = Path('/etc/systemd/system/railshot-personal-runtime.service')
WG = Path('/etc/wireguard/railshot0.conf')
MARKER = '# Managed by RailShot personal client v1\n'
CAPABILITIES = ['openstack.execute', 'client.remove']
CLI_USER = 'railshot-openstack'
CLI_HOME = Path('/var/lib/railshot-personal-cli')
IDENT = re.compile(r'[A-Za-z0-9_-]{1,128}')


def require(condition, code='CLIENT_INVALID'):
    if not condition:
        raise ValueError(code)


def run(args, *, input=None, timeout=30):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout, check=False)
    require(result.returncode == 0, 'CLIENT_COMMAND_FAILED')
    return result.stdout.strip()


def https_url(value, *, test_allow_http=False):
    parsed = urllib.parse.urlsplit(value)
    schemes = ('https', 'http') if test_allow_http is True else ('https',)
    require(parsed.scheme in schemes and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and not any(c.isspace() for c in value), 'HTTPS_REQUIRED')
    return value.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('HTTP_REDIRECT_REJECTED')


def api(base, path, token, body=None, *, test_allow_http=False, method='POST'):
    require(path.startswith('/api/v1/') and '\n' not in token and '\r' not in token)
    require(method in ('GET', 'POST'))
    request = urllib.request.Request(https_url(base, test_allow_http=test_allow_http) + path,
                                    data=None if method == 'GET' else json.dumps(body).encode(),
                                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method=method)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
        raw = response.read(1048577)
        require(len(raw) <= 1048576, 'RESPONSE_TOO_LARGE')
        result = json.loads(raw)
        require(isinstance(result, dict))
        if 'data' in result:
            result = result['data']
        require(isinstance(result, dict))
        return result


def wireguard_key(value):
    require(isinstance(value, str) and len(base64.b64decode(value, validate=True)) == 32)
    return value


def validate_tunnel(tunnel):
    wireguard_key(tunnel['server_public_key'])
    address = ipaddress.ip_interface(tunnel['address'])
    allowed = tunnel['allowed_ips']
    if isinstance(allowed, list):
        require(len(allowed) == 1)
        allowed = allowed[0]
    server = ipaddress.ip_network(allowed)
    require(address.version == server.version == 4 and address.network.prefixlen == server.prefixlen == 32
            and address.ip != server.network_address)
    require(re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{1,5}', tunnel['endpoint']))
    require(1 <= int(tunnel['endpoint'].rsplit(':', 1)[1]) <= 65535)
    return str(address), str(server)


def install_tunnel(config, runner=run):
    tunnel = config['tunnel']
    address, allowed = validate_tunnel(tunnel)
    secret = wireguard_key(read_private(CONFIG / 'wireguard.key').decode().strip())
    content = (MARKER + '[Interface]\nPrivateKey = ' + secret + '\nAddress = ' + address + '\n\n[Peer]\n'
               + 'PublicKey = ' + tunnel['server_public_key'] + '\nEndpoint = ' + tunnel['endpoint']
               + '\nAllowedIPs = ' + allowed + '\nPersistentKeepalive = 25\n')
    require(not any(p.is_symlink() for p in (WG, *WG.parents)))
    if WG.exists():
        require(WG.stat().st_uid == 0 and not WG.stat().st_mode & 0o077 and WG.read_text().startswith(MARKER), 'WIREGUARD_NOT_OWNED')
    WG.parent.mkdir(mode=0o700, exist_ok=True)
    # /etc/wireguard can be a root-owned shared directory; never change its mode.
    require(WG.parent.stat().st_uid == 0 and not WG.parent.stat().st_mode & 0o022)
    fd = os.open(str(WG) + '.pending', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(WG) + '.pending', WG)
    finally:
        if Path(str(WG) + '.pending').exists():
            Path(str(WG) + '.pending').unlink()
    runner(['systemctl', 'enable', '--now', 'wg-quick@railshot0'])


def checks(config, runner=run, cli_factory=OpenStackCLI):
    result = {'tunnel': False, 'openstack': False, 'runtime': False}
    try:
        rows = runner(['wg', 'show', 'railshot0', 'latest-handshakes']).splitlines()
        result['tunnel'] = any(len(parts) == 2 and parts[0] == config['tunnel']['server_public_key']
                               and 0 < int(parts[1]) <= time.time() and time.time() - int(parts[1]) <= 180
                               for parts in (row.split() for row in rows))
    except Exception:
        pass
    try:
        cli_factory(CredentialStore(CONFIG).load()).run(['server', 'list'])
        result['openstack'] = True
    except Exception:
        pass
    try:
        result['runtime'] = runner(['systemctl', 'is-active', 'railshot-personal-runtime']) == 'active'
    except Exception:
        pass
    return result


def save_config(config):
    atomic_private_write(CONFIG / 'client.json', json.dumps(config).encode())


def enroll(args):
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    test_allow_http = getattr(args, 'test_allow_http', False) is True
    api_url = https_url(args.api_url, test_allow_http=test_allow_http)
    private_directory(CONFIG)
    private_directory(STATE)
    existing = CONFIG / 'client.json'
    if existing.exists():
        config = json.loads(read_private(existing))
        require(config['api_url'] == api_url and config['enrollment_id'] == args.enrollment_id
                and config.get('test_allow_http', False) is test_allow_http, 'INSTALLATION_BINDING_CHANGED')
        install_tunnel(config)
        install_runtime_access(config)
        install_service()
        wait_control_ready(config)
        prepare_runtime(config, args)
        return
    require(IDENT.fullmatch(args.enrollment_id))
    from client_setup.personal_identity import prepare_personal_identity
    identity = prepare_personal_identity(CONFIG, STATE, project_id=args.project_id,
                                         test_allow_http=test_allow_http)
    require(isinstance(identity, dict) and set(identity) == {'auth', 'project_id', 'ownership'},
            'OPENSTACK_IDENTITY_INVALID')
    auth = identity['auth']
    https_url(auth['auth_url'], test_allow_http=test_allow_http)
    project_id = identity['project_id']
    require(IDENT.fullmatch(project_id))
    token_info = OpenStackCLI(auth).run(['token', 'issue'])
    require(token_info.get('project_id') == project_id, 'OPENSTACK_PROJECT_MISMATCH')
    key_path = CONFIG / 'wireguard.key'
    if not key_path.exists():
        atomic_private_write(key_path, (wireguard_key(run(['wg', 'genkey'])) + '\n').encode())
    public_key = wireguard_key(run(['wg', 'pubkey'], input=read_private(key_path).decode()))
    token = os.environ.pop('RAILSHOT_ENROLLMENT_TOKEN', '') or getpass.getpass('RailShot 일회용 등록 토큰: ')
    body = {'public_key': public_key, 'client_version': VERSION, 'project_id': project_id, 'capabilities': CAPABILITIES}
    host_key = CONFIG / 'runtime_host_key'
    if not host_key.exists():
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(host_key)])
    require(host_key.stat().st_uid == 0 and not host_key.stat().st_mode & 0o077 and not host_key.is_symlink())
    body['runtime'] = {'ssh_host_key': ' '.join(host_key.with_suffix('.pub').read_text().split()[:2])}
    if args.profile_id:
        body['runtime']['profile_id'] = args.profile_id
    response = api(args.api_url, '/api/v1/enrollments/' + args.enrollment_id + '/claims', token, body, test_allow_http=test_allow_http)
    del token
    require(IDENT.fullmatch(response['target_id']) and type(response['generation']) is int and response['generation'] > 0)
    require(isinstance(response['client_token'], str) and response['client_token'])
    validate_tunnel(response['tunnel'])
    config = {k: response[k] for k in ('target_id', 'generation', 'client_token', 'tunnel')}
    if response.get('runtime_access'):
        config['runtime_access'] = response['runtime_access']
    config.update(api_url=api_url, enrollment_id=args.enrollment_id, project_id=project_id, test_allow_http=test_allow_http)
    # Save the issued credential before any OS configuration. A subsequent local
    # failure is resumable without replaying the consumed enrollment credential.
    save_config(config)
    install_tunnel(config)
    install_runtime_access(config)
    install_service()
    wait_control_ready(config)
    prepare_runtime(config, args)


def prepare_runtime(config, args):
    from apps.agent.personal_runtime import prepare
    from apps.agent.runtime_access import install
    path = '/api/v1/targets/' + config['target_id'] + '/runtimes'
    options = {'test_allow_http': config.get('test_allow_http') is True}
    current = api(config['api_url'], path, config['client_token'], method='GET', **options)
    if current.get('deployable') is True and current.get('runtime_preparation', {}).get('status') == 'succeeded':
        return
    existing = current.get('runtime_preparation', {})
    evidence_path = CONFIG / 'runtime-evidence.json'
    if existing.get('status') in ('unknown', 'blocked') and (evidence_path.exists() or existing.get('client_reported_ready')):
        if not evidence_path.exists():
            # Older clients did not persist their submitted evidence. Recover it
            # from the immutable local binding and installed relay host key.
            runtime = json.loads(read_private(CONFIG / 'runtime.json'))
            evidence = {k: runtime[k] for k in ('resource_id', 'private_ipv4', 'management_network', 'placement', 'architecture', 'initialization')}
            evidence.update(ssh_user='railshot-runtime', ssh_port=2223,
                            ssh_host_key=' '.join((CONFIG / 'runtime_access_host_key.pub').read_text().split()[:2]))
            atomic_private_write(evidence_path, json.dumps(evidence).encode())
        api(config['api_url'], path, config['client_token'], {'generation': config['generation'], 'action': 'reconcile'}, **options)
        deadline = time.monotonic() + 240
        while True:
            current = api(config['api_url'], path, config['client_token'], method='GET', **options)
            existing = current.get('runtime_preparation', {})
            if existing.get('status') not in ('queued', 'running'):
                break
            require(time.monotonic() < deadline, 'RUNTIME_RECONCILIATION_PENDING')
            time.sleep(5)
        if existing.get('status') != 'succeeded':
            require(existing.get('resumable') is True, 'RUNTIME_PRIOR_OUTCOME_UNKNOWN')
            api(config['api_url'], path, config['client_token'], {'generation': config['generation'],
                'action': 'resume', 'evidence': json.loads(read_private(evidence_path))}, **options)
            existing = {'status': 'queued', 'stage': 'registration'}
    require(existing.get('status') != 'unknown', 'RUNTIME_PRIOR_OUTCOME_UNKNOWN')
    submitted = existing.get('stage') in ('registration', 'verification', 'complete')
    if not submitted:
        print('OpenStack 제어 연결을 확인했습니다. 앱 실행용 별도 VM을 준비합니다.', flush=True)
        stage = 'client_selection'
        def progress(value):
            nonlocal stage
            stage = value
            api(config['api_url'], path, config['client_token'], {'generation': config['generation'],
                'progress': {'stage': value, 'status': 'running'}}, **options)
        try:
            runtime = prepare(config, OpenStackCLI(CredentialStore(CONFIG).load()), CONFIG, STATE,
                              plan_path=getattr(args, 'runtime_config', None), progress=progress)
            access = install(config, runtime)
            evidence = {k: runtime[k] for k in ('resource_id', 'private_ipv4', 'management_network', 'placement', 'architecture', 'initialization')}
            evidence.update({k: access[k] for k in ('ssh_user', 'ssh_port', 'ssh_host_key')})
        except Exception as exc:
            code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', str(exc)) else 'RUNTIME_CLIENT_PREPARATION_FAILED'
            try:
                api(config['api_url'], path, config['client_token'], {'generation': config['generation'],
                    'progress': {'stage': stage, 'status': 'blocked', 'blockers': [code]}}, **options)
            except Exception:
                pass
            raise
        atomic_private_write(evidence_path, json.dumps(evidence).encode())
        api(config['api_url'], path, config['client_token'], {'generation': config['generation'], 'evidence': evidence}, **options)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        state = api(config['api_url'], path, config['client_token'], method='GET', **options)
        preparation = state.get('runtime_preparation', {})
        if state.get('status') == 'ready' and state.get('deployable') is True and preparation.get('status') == 'succeeded':
            print('클러스터 접속·전용 인증·배포 권한 검증을 마쳤습니다. 테스트 앱은 배포하지 않았습니다.', flush=True)
            return
        require(preparation.get('status') not in ('blocked', 'failed', 'unknown'), 'RUNTIME_REGISTRATION_BLOCKED')
        time.sleep(5)
    raise ValueError('RUNTIME_REGISTRATION_UNVERIFIED')


def install_cli_account(config):
    marker = CONFIG / 'cli-account.json'
    try:
        account = pwd.getpwnam(CLI_USER)
    except KeyError:
        run(['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', str(CLI_HOME), '--shell', '/bin/sh', CLI_USER], timeout=180)
        # Disabled password authentication, but account is not marked locked for
        # OpenSSH public-key authentication with UsePAM=no.
        run(['usermod', '--password', '*', CLI_USER], timeout=180)
        account = pwd.getpwnam(CLI_USER)
        atomic_private_write(marker, json.dumps({'user': CLI_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}).encode())
    require(marker.exists(), 'CLI_ACCOUNT_NOT_OWNED')
    owned = json.loads(read_private(marker))
    require(owned == {'user': CLI_USER, 'uid': account.pw_uid, 'gid': account.pw_gid}
            and account.pw_uid != 0 and account.pw_dir == str(CLI_HOME) and account.pw_shell == '/bin/sh', 'CLI_ACCOUNT_NOT_OWNED')
    control = {key: config[key] for key in ('project_id', 'target_id', 'generation')}
    if CLI_HOME.exists():
        require(not CLI_HOME.is_symlink() and CLI_HOME.stat().st_uid == account.pw_uid
                and not CLI_HOME.stat().st_mode & 0o077, 'CLI_HOME_UNSAFE')
        require(json.loads((CLI_HOME / 'control.json').read_text()) == control, 'CLI_BINDING_CHANGED')
        return
    staging = Path(tempfile.mkdtemp(prefix='.railshot-cli-', dir=CLI_HOME.parent))
    try:
        CredentialStore(staging).save(CredentialStore(CONFIG).load())
        atomic_private_write(staging / 'control.json', json.dumps(control).encode())
        for path in staging.iterdir():
            os.chown(path, account.pw_uid, account.pw_gid)
        os.chown(staging, account.pw_uid, account.pw_gid)
        staging.rename(CLI_HOME)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def install_runtime_access(config):
    access = config.get('runtime_access')
    if not access:
        return
    from apps.agent.install_forced_command import authorized_key_line
    address, allowed = validate_tunnel(config['tunnel'])
    install_cli_account(config)
    # Reuse strict key/source validation. SSH executes only the unprivileged
    # project-bound OpenStack adapter; no root shell or K3s access is installed.
    line = authorized_key_line(access['public_key'], allowed.split('/')[0],
                               str(PAYLOAD / '.venv/bin/python'), str(PAYLOAD), str(CONFIG))
    fixed = str(PAYLOAD / '.venv/bin/python') + ' -I ' + str(PAYLOAD / 'apps/agent/openstack_control.py')
    start = line.index('command="') + len('command="')
    end = line.index('"', start)
    line = line[:start] + fixed + line[end:]
    key_path = CLI_HOME / 'authorized_keys'
    account = pwd.getpwnam(CLI_USER)
    # The account owns its directory. Never follow a replaceable destination
    # symlink while writing as root; rename replaces the directory entry itself.
    fd, temporary = tempfile.mkstemp(prefix='.authorized-', dir=CLI_HOME)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(line + '\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), account.pw_uid, account.pw_gid)
        os.replace(temporary, key_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    text = ('Port 2222\nListenAddress ' + address.split('/')[0] + '\n'
            'HostKey ' + str(CONFIG / 'runtime_host_key') + '\n'
            'PidFile /run/railshot-personal-runtime.pid\n'
            'AuthorizedKeysFile ' + str(CLI_HOME / 'authorized_keys') + '\n'
            'PermitRootLogin no\nAllowUsers railshot-openstack\nPasswordAuthentication no\n'
            'KbdInteractiveAuthentication no\nPubkeyAuthentication yes\nAuthenticationMethods publickey\nUsePAM no\n'
            'PermitTTY no\nForceCommand ' + fixed + '\n'
            'AllowTcpForwarding no\nAllowAgentForwarding no\nX11Forwarding no\nPermitTunnel no\n'
            'PermitUserEnvironment no\nPermitUserRC no\n')
    atomic_private_write(CONFIG / 'runtime_sshd_config', text.encode())
    run(['/usr/sbin/sshd', '-t', '-f', str(CONFIG / 'runtime_sshd_config')])
    content = (MARKER + '[Unit]\nDescription=RailShot project OpenStack CLI access\nAfter=wg-quick@railshot0.service\n'
               '[Service]\nType=simple\nExecStart=/usr/sbin/sshd -D -e -f ' + str(CONFIG / 'runtime_sshd_config')
               + '\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n')
    require(not RUNTIME_UNIT.is_symlink(), 'SERVICE_NOT_OWNED')
    if RUNTIME_UNIT.exists():
        require(not RUNTIME_UNIT.is_symlink() and RUNTIME_UNIT.read_text().startswith(MARKER), 'SERVICE_NOT_OWNED')
    RUNTIME_UNIT.write_text(content)
    RUNTIME_UNIT.chmod(0o644)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'railshot-personal-runtime'])


def wait_control_ready(config, transport=api, checker=checks, sleeper=time.sleep, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = transport(config['api_url'], '/api/v1/targets/' + config['target_id'] + '/heartbeats',
                             config['client_token'], {'generation': config['generation'], 'client_version': VERSION,
                             'project_id': config['project_id'], 'checks': checker(config), 'capabilities': CAPABILITIES},
                             test_allow_http=config.get('test_allow_http') is True)
        if response.get('connection_status', response.get('status')) == 'ready':
            return
        require(response.get('status') not in ('deleted', 'deleting', 'attention'), 'CONTROL_CONNECTION_FAILED')
        sleeper(5)
    raise ValueError('CONTROL_CONNECTION_UNVERIFIED')


def install_service():
    require(ROOT == PAYLOAD, 'PAYLOAD_LOCATION_INVALID')
    content = (MARKER + '[Unit]\nDescription=RailShot personal environment client\nAfter=network-online.target wg-quick@railshot0.service\n'
               '[Service]\nType=simple\nExecStart=' + str(PAYLOAD / '.venv/bin/python') + ' ' + str(PAYLOAD / 'apps/agent/personal.py')
               + ' daemon\nRestart=on-failure\nRestartSec=15\nUMask=0077\nNoNewPrivileges=yes\nProtectHome=yes\n'
               '[Install]\nWantedBy=multi-user.target\n')
    require(not UNIT.is_symlink(), 'SERVICE_NOT_OWNED')
    if UNIT.exists():
        require(not UNIT.is_symlink() and UNIT.read_text().startswith(MARKER), 'SERVICE_NOT_OWNED')
    UNIT.write_text(content)
    UNIT.chmod(0o644)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'railshot-personal'])


def receipt(config, result):
    return api(config['api_url'], '/api/v1/targets/' + config['target_id'] + '/receipts', config['client_token'], result,
               test_allow_http=config.get('test_allow_http') is True)


def removal_module():
    from apps.agent import personal_remove
    return personal_remove


def handle_inspection(config, command, sender):
    require(IDENT.fullmatch(command.get('operation_id', '')) and command.get('reconciliation_id') == command['id'])
    require(command.get('generation') == config['generation'], 'GENERATION_MISMATCH')
    previous = command.get('previous_attempt_id')
    require(previous is None or isinstance(previous, str) and IDENT.fullmatch(previous))
    job_path = STATE / ('job-' + command['operation_id'] + '.json')
    require(job_path.exists(), 'REMOVAL_JOB_UNKNOWN')
    job = json.loads(read_private(job_path))
    require(job.get('attempt_id') == previous, 'REMOVAL_ATTEMPT_CHANGED')
    inspection = removal_module().inspect_intact(config, command['operation_id'], previous)
    record = {'operation_id': command['operation_id'], 'generation': config['generation'],
              'previous_attempt_id': previous, 'checked_at': time.time(), 'inspection': inspection}
    atomic_private_write(STATE / ('job-inspect-' + command['id'] + '.json'), json.dumps(record).encode())
    sender(config, {'operation_id': command['operation_id'], 'generation': config['generation'],
                    'status': 'inspected', 'reconciliation_id': command['id'], 'inspection': inspection,
                    'steps': [], 'residuals': ['client'], 'client_removed': False})


def handle_command(config, command, sender=receipt, launch=None):
    require(isinstance(command, dict) and IDENT.fullmatch(command.get('id', '')), 'COMMAND_INVALID')
    if command.get('kind') == 'environment.inspect':
        return handle_inspection(config, command, sender)
    require(command.get('kind') == 'environment.delete' and command.get('applications') == [], 'APPLICATIONS_NOT_REMOVED')
    require(command.get('generation', config['generation']) == config['generation'], 'GENERATION_MISMATCH')
    require(command.get('delete_data') in (False, True), 'COMMAND_INVALID')
    require(command.get('operation_id', command['id']) == command['id'], 'COMMAND_INVALID')
    attempt = command.get('attempt_id')
    require(attempt is None or isinstance(attempt, str) and IDENT.fullmatch(attempt))
    job_path = STATE / ('job-' + command['id'] + '.json')
    fingerprint = hashlib.sha256(json.dumps({k: v for k, v in command.items()
                                            if k not in ('attempt_id', 'reconciliation_id')}, sort_keys=True).encode()).hexdigest()
    result = {'operation_id': command['id'], 'generation': config['generation'], 'status': 'running',
              'steps': [], 'residuals': ['client'], 'client_removed': False,
              **({'attempt_id': attempt} if attempt is not None else {})}
    if job_path.exists():
        job = json.loads(read_private(job_path))
        require(job['fingerprint'] == fingerprint, 'JOB_ID_CONFLICT')
        if job.get('attempt_id') == attempt:
            if job.get('receipt'):
                sender(config, job['receipt'])
            else:
                if removal_module().helper_active(command['id'], attempt) is not True:
                    result.update(status='unknown', residuals=['client_removal_unverified'])
                sender(config, result)
            return
        reconciliation = command.get('reconciliation_id')
        require(attempt is not None and isinstance(reconciliation, str) and IDENT.fullmatch(reconciliation), 'REMOVAL_RECONCILIATION_REQUIRED')
        saved = json.loads(read_private(STATE / ('job-inspect-' + reconciliation + '.json')))
        require(saved['operation_id'] == command['id'] and saved['generation'] == config['generation']
                and saved['previous_attempt_id'] == job.get('attempt_id') and 0 <= time.time() - saved['checked_at'] <= 300
                and saved['inspection'] == {'state': 'intact', 'preflight_ok': True, 'helper_active': False},
                'REMOVAL_RECONCILIATION_REQUIRED')
        require(removal_module().inspect_intact(config, command['id'], job.get('attempt_id')) == saved['inspection'],
                'REMOVAL_STATE_CHANGED')
    atomic_private_write(job_path, json.dumps({'fingerprint': fingerprint, 'status': 'removing', 'attempt_id': attempt}).encode())
    sender(config, result)
    (launch or launch_removal)({**config, **({'attempt_id': attempt} if attempt else {})}, command['id'])


def launch_removal(config, operation_id):
    require(IDENT.fullmatch(operation_id))
    attempt = config.get('attempt_id')
    require(attempt is None or isinstance(attempt, str) and IDENT.fullmatch(attempt))
    identity = operation_id + ('.' + attempt if attempt else '')
    source = Path(__file__).with_name('personal_remove.py')
    target = Path('/run') / ('railshot-remove-' + identity + '.py')
    secret = target.with_suffix('.json')
    require(not target.exists() and not secret.exists(), 'REMOVAL_ALREADY_STARTED')
    # A stdlib-only helper outlives the installed Python environment and removes
    # its /run files before sending the final HTTPS receipt with in-memory auth.
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o700)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(source.read_bytes())
        stream.flush(); os.fsync(stream.fileno())
    fd = os.open(secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({**config, 'operation_id': operation_id}, stream)
        stream.flush(); os.fsync(stream.fileno())
    run(['systemd-run', '--unit=railshot-remove-' + identity, '--collect', '--property=Type=exec',
         '/usr/bin/python3', str(target), str(secret)])


class HostMetrics:
    """Management-host measurements only; unreadable values remain null."""
    def __init__(self, proc=Path('/proc'), clock=time.monotonic, disk=shutil.disk_usage):
        self.proc, self.clock, self.disk = proc, clock, disk
        self.previous = None

    def collect(self):
        result = {key: None for key in ('cpu_percent', 'memory_percent', 'disk_percent',
                  'network_receive_bytes_per_second', 'network_transmit_bytes_per_second')}
        current = {'time': self.clock()}
        try:
            values = [int(v) for v in (self.proc / 'stat').read_text().splitlines()[0].split()[1:9]]
            require(len(values) >= 5)
            current['cpu'] = (sum(values), values[3] + values[4])
            if self.previous and 'cpu' in self.previous:
                total = current['cpu'][0] - self.previous['cpu'][0]
                idle = current['cpu'][1] - self.previous['cpu'][1]
                if total > 0 and 0 <= idle <= total:
                    result['cpu_percent'] = round(100 * (total - idle) / total, 2)
        except Exception:
            pass
        try:
            memory = {row.split(':')[0]: int(row.split()[1]) for row in (self.proc / 'meminfo').read_text().splitlines()}
            total, available = memory['MemTotal'], memory['MemAvailable']
            if total > 0 and 0 <= available <= total:
                result['memory_percent'] = round(100 * (total - available) / total, 2)
        except Exception:
            pass
        try:
            usage = self.disk('/')
            if usage.total > 0:
                result['disk_percent'] = round(100 * usage.used / usage.total, 2)
        except Exception:
            pass
        try:
            received = transmitted = 0
            for row in (self.proc / 'net/dev').read_text().splitlines()[2:]:
                name, values = row.split(':', 1)
                if name.strip() == 'lo':
                    continue
                columns = values.split()
                received += int(columns[0])
                transmitted += int(columns[8])
            current['network'] = (received, transmitted)
            if self.previous and 'network' in self.previous and current['time'] > self.previous['time']:
                for index, key in enumerate(('network_receive_bytes_per_second', 'network_transmit_bytes_per_second')):
                    difference = current['network'][index] - self.previous['network'][index]
                    if difference >= 0:
                        result[key] = round(difference / (current['time'] - self.previous['time']), 2)
        except Exception:
            pass
        self.previous = current
        return result


def daemon():
    config = json.loads(read_private(CONFIG / 'client.json'))
    private_directory(STATE)
    metrics = HostMetrics()
    fd = os.open(STATE / 'daemon.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                response = api(config['api_url'], '/api/v1/targets/' + config['target_id'] + '/heartbeats',
                               config['client_token'], {'generation': config['generation'], 'client_version': VERSION,
                               'project_id': config['project_id'], 'checks': checks(config), 'capabilities': CAPABILITIES,
                               'metrics': metrics.collect()}, test_allow_http=config.get('test_allow_http') is True)
                if response.get('command'):
                    handle_command(config, response['command'])
            except Exception as exc:
                # Deliberately omit HTTP bodies, commands, credentials and exception messages.
                print('RailShot client operation failed: ' + type(exc).__name__, file=sys.stderr, flush=True)
            time.sleep(30)
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('enroll')
    install.add_argument('--api-url', required=True)
    install.add_argument('--enrollment-id', required=True)
    install.add_argument('--profile-id')
    install.add_argument('--project-id')
    install.add_argument('--test-allow-http', action='store_true', help='명시적으로 승인된 시험에서만 HTTP 주소 허용')
    install.add_argument('--runtime-config', help='선택 사항: 별도 VM 선택 및 SSH 입력을 담은 root 전용 JSON 파일')
    commands.add_parser('daemon')
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, 'ROOT_REQUIRED')
        enroll(args) if args.command == 'enroll' else daemon()
    except Exception as exc:
        print('RailShot 작업 실패: ' + type(exc).__name__ + '. 비밀정보를 보호하기 위해 원문 오류를 숨깁니다.', file=sys.stderr)
        if isinstance(exc, ValueError) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', str(exc)):
            print('확인할 상태 코드: ' + str(exc), file=sys.stderr)
        if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 410) and args.command == 'enroll':
            print('등록 자격의 만료·사용 여부를 확인하십시오. 미등록 상태라면 화면에서 새 일회용 등록 토큰을 발급받아 재실행하십시오. 저장된 설치 파일과 인증정보는 유지됩니다.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
