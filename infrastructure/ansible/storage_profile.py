"""Shared filesystem contract and isolated backend execution for kubeadm clusters.

The caller admits/locks the entire cluster. This module additionally locks the
backend's private state across request IDs. Never interpret a plan as readiness.
"""
from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
NAMESPACE = 'railshot-storage'
CLASS = 'railshot-persistent'
BACKENDS = {'aws': 'efs', 'gcp': 'filestore', 'openstack': 'cephfs'}


class StorageError(ValueError):
    """A storage request or its observed backend cannot be safely used."""

    def __init__(self, message, *, unknown=False):
        super().__init__(message)
        self.unknown = unknown


def _object(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise StorageError(f'{label} has missing or unsupported fields')
    return value


def _text(value, label, pattern=r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,254}'):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise StorageError(f'Invalid {label}')
    return value


def _path(value, label):
    if not isinstance(value, str) or not value.startswith('/') or any(c in value for c in '\n\r\x00') or '..' in Path(value).parts:
        raise StorageError(f'{label} must be an absolute file reference')
    return value


def _private_file(value):
    path = Path(value)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StorageError('Credential/state file must be private and owned by the executor')
    return path


def _directory(value):
    path = Path(value)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise StorageError('Storage state directory must be private and owned by the executor')
    return path


def _write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def _worker_resource(provider, node):
    resource = node['resource_id']
    if provider == 'aws':
        match = re.fullmatch(r'(?:arn:aws:ec2:[a-z0-9-]+:[0-9]{12}:instance/)?(i-[a-f0-9]{8,17})', resource)
        if not match:
            raise StorageError('AWS worker requires an EC2 instance ID or ARN')
        return match.group(1)
    if provider == 'gcp':
        match = re.fullmatch(r'(?:https://www.googleapis.com/compute/v1/)?projects/([^/]+)/zones/([^/]+)/instances/([^/]+)', resource)
        if not match or match.group(2) != node['availability_zone']:
            raise StorageError('GCP worker requires a canonical instance reference in its AZ')
        return match.group(3)
    return resource


def validate(request):
    """Validate only; performs no file, SSH, cloud, or Kubernetes observation."""
    try:
        target = request['target']
        provider = target['provider']
        cluster = target['id']
        region = target['placement']
        scope = target['account_id']
        storage = request['runtime']['storage']
        workers = request['inventory']['workers']
        if provider not in BACKENDS or len(workers) != 2:
            raise StorageError('Shared storage requires one supported provider and two workers')
        _text(cluster, 'cluster ID', r'[a-z0-9][a-z0-9-]{0,62}')
        _text(region, 'region')
        _text(scope, 'account/project')
        _object(storage, ('backend', 'mode', 'binding', 'state_dir', 'credentials_file', 'aws', 'gcp', 'cephfs'),
                ('backend', 'mode', 'binding'), 'storage')
        if storage['backend'] != BACKENDS[provider]:
            raise StorageError('Storage backend does not match provider')
        binding = storage['binding']
        if binding != {'cluster_id': cluster, 'provider': provider, 'region': region, 'account_id': scope}:
            raise StorageError('Storage binding must match the admitted cluster/provider/region/account')
        for node in workers:
            _text(node['id'], 'worker ID', r'[a-z0-9][a-z0-9-]{0,62}')
            zone = _text(node['availability_zone'], 'worker AZ')
            address = ipaddress.IPv4Address(node['private_ipv4'])
            if not address.is_private or address.is_loopback or address.is_unspecified:
                raise StorageError('Worker storage addresses must be private unicast IPv4')
            _worker_resource(provider, node)
            if provider == 'aws' and node['resource_id'].startswith('arn:') and not node['resource_id'].startswith(f'arn:aws:ec2:{region}:{scope}:instance/'):
                raise StorageError('AWS worker ARN must match cluster account and region')
            if provider == 'aws' and not zone.startswith(region):
                raise StorageError('AWS worker AZ must belong to the cluster region')
            if provider == 'gcp' and (zone.rsplit('-', 1)[0] != region or f'projects/{scope}/' not in node['resource_id']):
                raise StorageError('GCP worker must belong to the cluster region and project')
        if 'state_dir' in storage:
            _path(storage['state_dir'], 'storage state_dir')
        if provider != 'openstack':
            if storage['mode'] not in ('create', 'reuse'):
                raise StorageError('Cloud storage mode must be create or reuse')
            _path(storage['state_dir'], 'storage state_dir')
            _path(storage['credentials_file'], 'storage credentials_file')
        elif storage['mode'] != 'reuse':
            raise StorageError('OpenStack requires an existing CephFS backend')
        if provider == 'aws':
            if not re.fullmatch(r'[0-9]{12}', scope):
                raise StorageError('AWS account_id must contain 12 digits')
            cfg = _object(storage['aws'], ('vpc_id', 'worker_subnet_ids', 'filesystem_id', 'controller_credentials_secret', 'controller_credentials_file'),
                          ('vpc_id', 'worker_subnet_ids', 'controller_credentials_secret'), 'AWS storage')
            _text(cfg['vpc_id'], 'VPC ID', r'vpc-[a-f0-9]{8,17}')
            subnets = cfg['worker_subnet_ids']
            if not isinstance(subnets, dict) or set(subnets) != {n['id'] for n in workers}:
                raise StorageError('AWS worker_subnet_ids must name both workers')
            for subnet in subnets.values():
                _text(subnet, 'subnet ID', r'subnet-[a-f0-9]{8,17}')
            _text(cfg['controller_credentials_secret'], 'CSI credential Secret', r'[a-z0-9][a-z0-9-]{0,62}')
            if 'controller_credentials_file' in cfg:
                _path(cfg['controller_credentials_file'], 'remote CSI credentials_file')
            if storage['mode'] == 'reuse':
                _text(cfg['filesystem_id'], 'EFS ID', r'fs-[a-f0-9]{8,17}')
            elif cfg.get('filesystem_id'):
                raise StorageError('create mode cannot supply an existing EFS ID')
        elif provider == 'gcp':
            cfg = _object(storage['gcp'], ('network', 'instance_name', 'share_name', 'capacity_gib', 'reserved_ip_range'),
                          ('network', 'instance_name', 'share_name', 'capacity_gib'), 'GCP storage')
            _text(cfg['network'], 'GCP network', rf'projects/{re.escape(scope)}/global/networks/[a-z][a-z0-9-]{{0,62}}')
            _text(cfg['instance_name'], 'Filestore name', r'[a-z][a-z0-9-]{0,62}')
            _text(cfg['share_name'], 'Filestore share', r'[a-zA-Z][a-zA-Z0-9_]{0,15}')
            if type(cfg['capacity_gib']) is not int or not 1024 <= cfg['capacity_gib'] <= 10240:
                raise StorageError('Regional Filestore capacity must be 1024..10240 GiB in this profile')
            if storage['mode'] == 'create':
                network = ipaddress.IPv4Network(cfg['reserved_ip_range'], strict=True)
                if not any(network.subnet_of(ipaddress.IPv4Network(r)) for r in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')) or network.prefixlen != 26:
                    raise StorageError('Regional Filestore requires a private, unused /26 reserved range')
                if any(ipaddress.IPv4Address(n['private_ipv4']) in network for n in workers):
                    raise StorageError('Filestore reserved range overlaps a worker address')
        else:
            cfg = _object(storage['cephfs'], ('cluster_id', 'monitors', 'fs_name', 'subvolume_group', 'provisioner_secret', 'node_secret', 'secret_files'),
                          ('cluster_id', 'monitors', 'fs_name', 'subvolume_group', 'provisioner_secret', 'node_secret'), 'CephFS storage')
            _text(cfg['cluster_id'], 'Ceph FSID', r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}')
            for field in ('fs_name', 'subvolume_group', 'provisioner_secret', 'node_secret'):
                _text(cfg[field], field, r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}')
            if not isinstance(cfg['monitors'], list) or not cfg['monitors'] or len(set(cfg['monitors'])) != len(cfg['monitors']):
                raise StorageError('Ceph monitors must be a nonempty unique list')
            for monitor in cfg['monitors']:
                _text(monitor, 'Ceph MON', r'[0-9.]+:(?:3300|6789)')
                if not ipaddress.IPv4Address(monitor.split(':')[0]).is_private:
                    raise StorageError('Ceph MONs must use private IPv4 addresses')
            files = cfg.get('secret_files', {})
            if not isinstance(files, dict) or set(files) - {cfg['provisioner_secret'], cfg['node_secret']}:
                raise StorageError('Ceph secret_files can only reference the configured CSI Secrets')
            for value in files.values():
                _path(value, 'remote Ceph credential file')
        if set(storage) & {'aws', 'gcp', 'cephfs'} != { {'aws': 'aws', 'gcp': 'gcp', 'openstack': 'cephfs'}[provider] }:
            raise StorageError('Only the selected provider storage configuration is allowed')
        return request
    except (KeyError, TypeError, ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
        raise StorageError('Incomplete or invalid shared storage configuration') from exc


def variables(request):
    validate(request)
    storage = request['runtime']['storage']
    config = {k: v for k, v in storage.items() if k not in ('credentials_file', 'state_dir')}
    return {'railshot_storage': {**config, 'workers': [n['id'] for n in request['inventory']['workers']]}}


def execute(argv, *, env, timeout, cwd=None):
    """Capture output privately and bound descendants; no cloud output in errors."""
    import tempfile
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True, umask=0o077)
        try:
            result = process.wait(timeout=timeout)
            if result:
                raise StorageError('Storage backend command failed; inspect private state before retry')
            stdout.seek(0)
            data = stdout.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise StorageError('Storage backend response exceeded the output limit')
            return data.decode()
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def _cloud_env(storage, provider, original):
    env = {key: original[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'TF_PLUGIN_CACHE_DIR') if key in original}
    credential = _private_file(storage['credentials_file'])
    data = json.loads(credential.read_text())
    if provider == 'aws':
        _object(data, ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'),
                ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'), 'AWS credentials')
        if any(not isinstance(value, str) or not value or '\x00' in value for value in data.values()):
            raise StorageError('Invalid AWS credential file')
        env.update(data)
        env.update(AWS_REGION=storage['binding']['region'], AWS_DEFAULT_REGION=storage['binding']['region'],
                   AWS_PAGER='', AWS_CONFIG_FILE='/dev/null', AWS_SHARED_CREDENTIALS_FILE='/dev/null',
                   AWS_EC2_METADATA_DISABLED='true', TF_IN_AUTOMATION='1')
    else:
        if data.get('type') != 'service_account' or data.get('project_id') != storage['binding']['account_id']:
            raise StorageError('GCP credential project must match cluster project')
        # ADC is read by Terraform; REST observations use a short-lived token from
        # the same service account via gcloud with its isolated configuration.
        env.update(GOOGLE_APPLICATION_CREDENTIALS=str(credential),
                   CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=str(credential),
                   CLOUDSDK_CORE_PROJECT=storage['binding']['account_id'], TF_IN_AUTOMATION='1')
    return env


def _terraform_vars(request):
    storage = request['runtime']['storage']
    target = request['target']
    common = {'cluster_id': target['id'], 'region': target['placement']}
    if target['provider'] == 'aws':
        cfg = storage['aws']
        return {**common, 'account_id': target['account_id'], 'vpc_id': cfg['vpc_id'],
                'workers': {n['id']: {'instance_id': _worker_resource('aws', n), 'private_ip': n['private_ipv4'],
                                     'az': n['availability_zone'], 'subnet_id': cfg['worker_subnet_ids'][n['id']]}
                            for n in request['inventory']['workers']}}
    cfg = storage['gcp']
    return {**common, 'project_id': target['account_id'], **cfg,
            'workers': {n['id']: {'instance_name': _worker_resource('gcp', n), 'private_ip': n['private_ipv4'], 'zone': n['availability_zone']}
                        for n in request['inventory']['workers']}}


def _observe(request, call, resource):
    """Bind observed resource/worker identity, access and ownership before use."""
    target, storage = request['target'], request['runtime']['storage']
    cluster, region, scope = target['id'], target['placement'], target['account_id']
    workers = request['inventory']['workers']
    if target['provider'] == 'aws':
        identity = call(['aws', 'sts', 'get-caller-identity', '--output', 'json'])
        if identity.get('Account') != scope:
            raise StorageError('AWS credentials do not belong to the admitted account')
        fs = call(['aws', 'efs', 'describe-file-systems', '--file-system-id', resource, '--region', region, '--output', 'json'])['FileSystems']
        if len(fs) != 1:
            raise StorageError('Expected exactly one EFS filesystem')
        fs = fs[0]
        tags = {item['Key']: item['Value'] for item in fs.get('Tags', [])}
        if (fs.get('OwnerId') != scope or fs.get('FileSystemId') != resource or fs.get('LifeCycleState') != 'available'
                or not fs.get('Encrypted') or fs.get('AvailabilityZoneName') or tags.get('railshot-cluster') != cluster
                or tags.get('managed-by') != 'railshot'):
            raise StorageError('EFS must be available, encrypted, Regional, and owned by this cluster')
        instance_ids = [_worker_resource('aws', n) for n in workers]
        instances = call(['aws', 'ec2', 'describe-instances', '--instance-ids', *instance_ids, '--region', region, '--output', 'json'])
        rows = {i['InstanceId']: i for r in instances.get('Reservations', []) for i in r.get('Instances', [])}
        targets = call(['aws', 'efs', 'describe-mount-targets', '--file-system-id', resource, '--region', region, '--output', 'json'])['MountTargets']
        subnets = storage['aws']['worker_subnet_ids']
        for node in workers:
            actual = rows.get(_worker_resource('aws', node), {})
            if (actual.get('State', {}).get('Name') != 'running' or actual.get('VpcId') != storage['aws']['vpc_id']
                    or actual.get('SubnetId') != subnets[node['id']] or actual.get('PrivateIpAddress') != node['private_ipv4']
                    or actual.get('Placement', {}).get('AvailabilityZone') != node['availability_zone']):
                raise StorageError('Observed AWS worker differs from the admitted storage binding')
            matched = [m for m in targets if m.get('AvailabilityZoneName') == node['availability_zone'] and m.get('LifeCycleState') == 'available']
            if len(matched) != 1 or matched[0].get('VpcId') != storage['aws']['vpc_id']:
                raise StorageError('An available EFS mount target in each worker AZ/VPC is required')
            groups = call(['aws', 'efs', 'describe-mount-target-security-groups', '--mount-target-id', matched[0]['MountTargetId'], '--region', region, '--output', 'json'])['SecurityGroups']
            rules = call(['aws', 'ec2', 'describe-security-groups', '--group-ids', *groups, '--region', region, '--output', 'json'])['SecurityGroups']
            allowed = False
            for group in rules:
                if group.get('VpcId') != storage['aws']['vpc_id']:
                    raise StorageError('EFS mount target security group is outside the cluster VPC')
                for rule in group.get('IpPermissions', []):
                    if rule.get('IpProtocol') == '-1' or (rule.get('IpProtocol') == 'tcp' and rule.get('FromPort', 0) <= 2049 <= rule.get('ToPort', 65535)):
                        for cidr in rule.get('IpRanges', []):
                            net = ipaddress.IPv4Network(cidr['CidrIp'])
                            if str(net) not in {n['private_ipv4'] + '/32' for n in workers}:
                                raise StorageError('EFS NFS ingress must be restricted to the two worker addresses')
                            allowed |= ipaddress.IPv4Address(node['private_ipv4']) in net
                        if rule.get('Ipv6Ranges') or rule.get('PrefixListIds') or rule.get('UserIdGroupPairs'):
                            # Exact private /32s are used rather than trusting broad group membership.
                            raise StorageError('EFS NFS ingress must use approved worker /32s only')
            if not allowed:
                raise StorageError('EFS does not allow NFS from both workers')
        return {'provider': 'aws', 'backend': 'efs', 'filesystem_id': resource, 'region': region, 'account_id': scope, 'cluster_id': cluster}
    cfg = storage['gcp']
    instance = call(['gcloud', 'filestore', 'instances', 'describe', resource, '--project', scope, '--region', region, '--format=json'])
    expected_name = f'projects/{scope}/locations/{region}/instances/{cfg["instance_name"]}'
    shares = instance.get('fileShares', [])
    networks = instance.get('networks', [])
    if (instance.get('name') != expected_name or instance.get('tier') != 'REGIONAL' or instance.get('state') != 'READY' or instance.get('protocol', 'NFS_V3') != 'NFS_V3'
            or instance.get('labels', {}).get('railshot-cluster') != cluster or instance.get('labels', {}).get('managed-by') != 'railshot'
            or len(shares) != 1 or shares[0].get('name') != cfg['share_name'] or int(shares[0].get('capacityGb', 0)) != cfg['capacity_gib']
            or len(networks) != 1 or networks[0].get('network', '').removeprefix('https://www.googleapis.com/compute/v1/') != cfg['network']):
        raise StorageError('Filestore must be READY, Regional, and match owned project/network/share/capacity')
    exports = shares[0].get('nfsExportOptions', [])
    allowed = {n['private_ipv4'] for n in workers}
    observed = set()
    for export in exports:
        ips = export.get('ipRanges', [])
        if export.get('accessMode') != 'READ_WRITE' or export.get('squashMode') != 'NO_ROOT_SQUASH':
            raise StorageError('Filestore needs approved read/write directory provisioning permissions')
        for ip in ips:
            parsed = ipaddress.IPv4Network(ip, strict=False)
            if parsed.prefixlen != 32 or str(parsed.network_address) not in allowed:
                raise StorageError('Filestore NFS export must be restricted to the two worker addresses')
            observed.add(str(parsed.network_address))
    if observed != allowed:
        raise StorageError('Filestore export must allow both workers')
    for node in workers:
        worker = call(['gcloud', 'compute', 'instances', 'describe', _worker_resource('gcp', node), '--project', scope, '--zone', node['availability_zone'], '--format=json'])
        nics = worker.get('networkInterfaces', [])
        if worker.get('status') != 'RUNNING' or len(nics) != 1 or nics[0].get('networkIP') != node['private_ipv4'] or nics[0].get('network', '').removeprefix('https://www.googleapis.com/compute/v1/') != cfg['network']:
            raise StorageError('Observed GCP worker differs from the admitted storage binding')
    ip, = networks[0]['ipAddresses']
    if not ipaddress.IPv4Address(ip).is_private:
        raise StorageError('Filestore requires a private NFS server address')
    return {'provider': 'gcp', 'backend': 'filestore', 'server': ip, 'share': '/' + cfg['share_name'],
            'resource_id': expected_name, 'region': region, 'account_id': scope, 'cluster_id': cluster}


def _prepare(request, *, work, env, deadline, runner=None):
    """Prepare/reuse a real backend; never claims CSI or PVC readiness.

    A failed/unknown apply remains journaled. Subsequent attempts stop until an
    operator inspects Terraform/cloud state and removes only the attempt marker.
    The durable Terraform state and binding must always be retained.
    """
    validate(request)
    safe = variables(request)
    provider = request['target']['provider']
    storage = request['runtime']['storage']
    if provider == 'openstack':
        safe['railshot_storage']['backend_descriptor'] = {'provider': provider, 'backend': 'cephfs', **storage['binding']}
        # Remote CSI/PVC observation proves connectivity; configuration is no proof.
        return {'storage_backend_ready': False, 'storage_pvc_ready': False, 'variables': safe}
    directory = _directory(storage['state_dir'])
    digest = hashlib.sha256(json.dumps(storage, sort_keys=True).encode()).hexdigest()
    fd = os.open(directory / 'backend.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StorageError('Storage backend is busy') from exc
        binding = directory / 'binding.json'
        if binding.exists() or binding.is_symlink():
            old = json.loads(_private_file(binding).read_text())
            if old != {'sha256': digest, 'binding': storage['binding']}:
                raise StorageError('Storage state is bound to different inputs; automatic replacement is refused')
        else:
            if any(directory.glob('*.tfstate*')):
                raise StorageError('Unbound existing Terraform state requires operator inspection')
            _write(binding, {'sha256': digest, 'binding': storage['binding']})
        marker = directory / 'apply-unknown.json'
        if marker.exists() or marker.is_symlink():
            raise StorageError('Previous storage apply outcome is unresolved; inspect state and backend before retry', unknown=True)
        cloud = _cloud_env(storage, provider, env)
        cloud.update(TF_DATA_DIR=str(directory / 'terraform-data'))
        if provider == 'gcp':
            cloud['CLOUDSDK_CONFIG'] = str(_directory(str(directory / 'gcloud')))
        execute_backend = runner or execute

        def raw(argv, cwd=None):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StorageError('Storage backend deadline exceeded')
            return execute_backend(argv, env=cloud, timeout=remaining, cwd=cwd)

        def call(argv):
            try:
                value = json.loads(raw(argv))
                if not isinstance(value, dict):
                    raise StorageError('Storage observation must be an object')
                return value
            except (ValueError, KeyError, TypeError) as exc:
                raise StorageError('Storage observation was unreadable; readiness is not established') from exc

        if storage['mode'] == 'create':
            terraform_vars = directory / 'backend.tfvars.json'
            _write(terraform_vars, _terraform_vars(request))
            module = ROOT / 'infrastructure' / 'terraform' / f'{provider}-storage'
            prefix = ['terraform', f'-chdir={module}']
            state = str(directory / 'backend.tfstate')
            if provider == 'aws' and not Path(state).exists():
                identity = call(['aws', 'sts', 'get-caller-identity', '--output', 'json'])
                if identity.get('Account') != request['target']['account_id']:
                    raise StorageError('AWS credential identity differs from cluster account')
                existing = call(['aws', 'efs', 'describe-file-systems', '--creation-token', 'railshot-' + request['target']['id'], '--region', request['target']['placement'], '--output', 'json'])
                if existing.get('FileSystems'):
                    raise StorageError('Existing EFS creation token without owned state requires operator inspection')
            if Path(state).exists() or Path(state).is_symlink():
                _private_file(state)
            raw([*prefix, 'init', '-input=false', '-no-color'])
            planfile = directory / 'backend.plan'
            raw([*prefix, 'plan', '-input=false', '-no-color', f'-state={state}', f'-var-file={terraform_vars}', f'-out={planfile}'])
            plan = call([*prefix, 'show', '-json', str(planfile)])
            # An automatic rerun may update access rules, but must never delete or
            # replace a filesystem or anything else in the backend module state.
            if any('delete' in row.get('change', {}).get('actions', []) for row in plan.get('resource_changes', [])):
                raise StorageError('Storage plan contains a destructive change; operator review is required')
            _write(marker, {'binding': storage['binding'], 'state': state})
            raw([*prefix, 'apply', '-input=false', '-no-color', f'-state={state}', str(planfile)])
            outputs = call([*prefix, 'output', '-json', f'-state={state}'])
            resource = outputs['backend_id']['value']
            # Readiness and ownership are observed separately from Terraform output.
            descriptor = _observe(request, call, resource)
            marker.unlink()
        else:
            resource = storage['aws']['filesystem_id'] if provider == 'aws' else storage['gcp']['instance_name']
            descriptor = _observe(request, call, resource)
        safe['railshot_storage']['backend_descriptor'] = descriptor
        _write(directory / 'observed-backend.json', descriptor)
        return {'storage_backend_ready': True, 'storage_pvc_ready': False, 'variables': safe}


def prepare(request, *, work, env, deadline, runner=None):
    """Public bounded-error adapter: never expose credential/cloud response data."""
    try:
        return _prepare(request, work=work, env=env, deadline=deadline, runner=runner)
    except StorageError as exc:
        exc.unknown = exc.unknown or _apply_unknown(request)
        raise
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        raise StorageError('Storage preparation failed; inspect private state, readiness is unproven',
                           unknown=_apply_unknown(request)) from exc


def _apply_unknown(request):
    try:
        marker = Path(request['runtime']['storage']['state_dir']) / 'apply-unknown.json'
        return marker.exists() or marker.is_symlink()
    except (OSError, KeyError, TypeError):
        return False
