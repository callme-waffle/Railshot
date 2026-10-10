"""Prepare VM SSH prerequisites without creating a VM or changing routing."""
import ipaddress
import json
import os
import stat
import tempfile
from pathlib import Path
import re
import subprocess
from .cli import ProviderError


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise ValueError('Invalid resource identifier')
    return value


def _id(row):
    return row.get('id', row.get('ID'))


def _safe_directory(path):
    path = Path(path).absolute()
    for parent in reversed([path, *path.parents]):
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ValueError('Unsafe directory path')
        else:
            parent.mkdir(mode=0o700)
    if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o022:
        raise ValueError('Directory must be owned by current user and not writable by others')
    return path


def _private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
        raise ValueError('Unsafe credential file')
    if info.st_mode & 0o077:
        raise ValueError('Private credential file permissions required')


def _trusted_input_file(value):
    path = Path(value).absolute()
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
            raise ValueError('Untrusted SSH file parent')
        # A root-owned sticky /tmp ancestor cannot replace an owned child directory.
        sticky_root_ancestor = parent != path.parent and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_mode & 0o022 and not sticky_root_ancestor:
            raise ValueError('SSH file parent is writable by others')
    _private_file(path)
    return path


def _verify_group_rules(cli, group_id, source):
    rows = cli.run(['security','group','rule','list',group_id])
    ingress = []
    for row in rows:
        rule = cli.run(['security','group','rule','show',str(_id(row))])
        if rule.get('direction') == 'ingress':
            ingress.append(rule)
    if len(ingress) != 1:
        raise ProviderError('security_group_rules_changed')
    rule = ingress[0]
    if (rule.get('protocol') != 'tcp' or str(rule.get('port_range_min')) != '22'
            or str(rule.get('port_range_max')) != '22'
            or rule.get('remote_ip_prefix') != str(source)
            or rule.get('remote_group_id') not in (None, '')
            or rule.get('ethertype') != ('IPv4' if source.version == 4 else 'IPv6')):
        raise ProviderError('security_group_rules_changed')


def prepare_vm_access(cli, config, state_dir, on_resource=lambda resource: None, *, runner=subprocess.run):
    network_id = _identifier(config['network_id'])
    username = _identifier(config['ssh_username'])
    if username == 'root':
        raise ValueError('Use a non-root SSH account')
    prefix = _identifier(config.get('resource_prefix', 'jasmin'))
    source = ipaddress.ip_network(config['ssh_source_cidr'], strict=True)
    if source.prefixlen == 0:
        raise ValueError('SSH access must be restricted to a management CIDR')
    cli.run(['network','show', network_id])
    state_dir = _safe_directory(state_dir)
    directory = state_dir / 'ssh'
    if directory.is_symlink():
        raise ValueError('SSH directory cannot be a symlink')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe_directory(directory)
    directory.chmod(0o700)
    key = directory / 'id_ed25519'
    if key.is_symlink() or key.with_suffix('.pub').is_symlink():
        raise ValueError('SSH key cannot be a symlink')
    if not key.exists():
        try:
            result = runner(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(key)], shell=False, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            raise ProviderError('ssh_key_generation_failed') from None
        if result.returncode:
            raise ProviderError('ssh_key_generation_failed')
    _private_file(key)
    public_path = key.with_suffix('.pub')
    if not public_path.is_file():
        raise ProviderError('public_key_missing', 'Restore the matching public key before retrying')
    if public_path.stat().st_nlink != 1 or public_path.stat().st_uid != os.geteuid():
        raise ValueError('Unsafe public key file')
    public = public_path.read_text().strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', public):
        raise ValueError('Expected a single Ed25519 public key')
    resources = config.get('existing_resources', [])
    key_name = prefix + '-ssh'
    recorded_key = next((r for r in resources if r.get('type') == 'keypair' and r.get('name') == key_name), None)
    if recorded_key:
        found = cli.run(['keypair','show',key_name])
        remote = found.get('public_key', found.get('public key',''))
        if remote.split()[:2] != public.split()[:2]:
            raise ProviderError('keypair_mismatch')
    else:
        # Creation fails on an existing name; never adopt another user's resource.
        cli.run(['keypair','create','--public-key',str(public_path),key_name])
        on_resource({'type':'keypair','id':key_name,'name':key_name})
    group_name = prefix + '-ssh'
    recorded_group = next((r for r in resources if r.get('type') == 'security_group' and r.get('name') == group_name), None)
    if recorded_group:
        group_id = _identifier(recorded_group['id'])
        group = cli.run(['security','group','show',group_id])
        _verify_group_rules(cli, group_id, source)
        # Existing rules must be inspected by a human if ownership state cannot establish completion.
        if not any(r.get('type')=='security_group_rule' and r.get('security_group_id')==group_id for r in resources):
            raise ProviderError('incomplete_security_group', 'Reconcile recorded group before resuming access setup')
    else:
        existing = cli.run(['security','group','list'])
        if any(r.get('Name',r.get('name'))==group_name for r in existing):
            raise ProviderError('existing_security_group')
        group = cli.run(['security','group','create','--description','Railshot managed SSH access',group_name])
        group_id = _id(group)
        on_resource({'type':'security_group','id':group_id,'name':group_name})
        rule = cli.run(['security','group','rule','create','--ingress','--ethertype','IPv4' if source.version==4 else 'IPv6','--protocol','tcp','--dst-port','22','--remote-ip',str(source),group_id])
        on_resource({'type':'security_group_rule','id':_id(rule),'security_group_id':group_id})
        _verify_group_rules(cli, group_id, source)
    cloud_init = '#cloud-config\n' + json.dumps({'users':[{'name':username,'lock_passwd':True,'ssh_authorized_keys':[public]}],'ssh_pwauth':False,'disable_root':True}, indent=2) + '\n'
    cloud_init_path = Path(state_dir) / 'vm-cloud-init.yaml'
    if cloud_init_path.exists() or cloud_init_path.is_symlink():
        _private_file(cloud_init_path)
    fd, temporary = tempfile.mkstemp(prefix='.cloud-init-', dir=state_dir)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(cloud_init)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, cloud_init_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {'network_id':network_id,'keypair_name':key_name,'security_group_id':group_id,
            'ssh_username':username,'private_key_path':str(key),'cloud_init_path':str(cloud_init_path),
            'ssh_source_cidr':str(source),'route_status':'unverified','vm_access_status':'unverified',
            'note':'Apply this profile when creating a VM; no VM or route was created.'}


def verify_vm_access(profile, host_key_file, *, runner=subprocess.run):
    host = str(ipaddress.ip_address(profile['host']))
    username = _identifier(profile['ssh_username'])
    known_hosts = _trusted_input_file(host_key_file)
    if not known_hosts.read_text().strip():
        raise ValueError('An independently verified known_hosts file is required')
    key = _trusted_input_file(profile['private_key_path'])
    command = ['ssh','-F','/dev/null','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
               '-o','StrictHostKeyChecking=yes','-o','UserKnownHostsFile='+str(known_hosts),
               '-o','GlobalKnownHostsFile=/dev/null','-o','IdentityAgent=none','-o','ConnectTimeout=10','-i',str(key),username+'@'+host,'true']
    try:
        result = runner(command, shell=False, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        raise ProviderError('ssh_connection_failed') from None
    if result.returncode:
        raise ProviderError('ssh_verification_failed')
    return {'vm_access_status':'verified','host':host,'ssh_username':username}
