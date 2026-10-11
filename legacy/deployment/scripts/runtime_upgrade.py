"""Explicit, single-node runtime patch upgrades; installation guards stay unchanged.

K3s binary replacement and SQLite backup follow docs.k3s.io/upgrades/manual and
docs.k3s.io/datastore/backup-restore. Cilium uses explicit values and retains its
Helm revision for operator recovery (docs.cilium.io/en/stable/operations/upgrade/).
"""
import base64
import fcntl
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
from urllib.request import urlopen

K3S = '/usr/local/bin/k3s'
CILIUM = '/usr/local/lib/railshot-deployment/cilium'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:\+k3s(\d+))?', value)
    require(match, 'stable runtime version required')
    return tuple(int(x or 0) for x in match.groups())


def validate(release, policy=None):
    require(set(release) <= {'version', 'source_sha', 'from_policy', 'to_policy', 'from_policy_sha256', 'to_policy_sha256', 'upgrade'}
            and release.get('version') == 1 and re.fullmatch('[a-f0-9]{40}', release.get('source_sha', '')), 'release identity invalid')
    for side in ('from', 'to'):
        value = release[side + '_policy']
        require(digest(value) == release[side + '_policy_sha256'], 'runtime policy digest differs')
        require(set(value['runtime']) == {'k3s_version', 'cilium_version', 'cilium_cli_version'}, 'runtime fields differ')
        for item in value['runtime'].values():
            version(item)
        require(set(value['cilium_images']) == {'agent', 'operator', 'envoy'}, 'Cilium image set differs')
        for item in value['cilium_images'].values():
            require(re.fullmatch(r'quay\.io/cilium/[a-z0-9-]+@sha256:[a-f0-9]{64}', item), 'pinned Cilium image required')
    if policy is not None:
        require(release['to_policy'] == policy, 'release differs from checked-out approved policy')
    before, after = release['from_policy'], release['to_policy']
    changed = before['runtime'] != after['runtime'] or before['cilium_images'] != after['cilium_images']
    if changed:
        options = release.get('upgrade', {})
        require(set(options) <= {'recovery_ack', 'k3s_binary_sha256', 'cilium_cli_sha256'}, 'upgrade fields differ')
        require(options.get('recovery_ack') is True, 'explicit downtime/backup/recovery acknowledgement required')
        # ponytail: patch upgrades only; minor upgrades need a reviewed compatibility and migration procedure.
        for key in before['runtime']:
            old, new = version(before['runtime'][key]), version(after['runtime'][key])
            require(old[:2] == new[:2] and new >= old, 'only forward patches within the same minor are supported')
        for key, artifact in (('k3s_version', 'k3s_binary_sha256'), ('cilium_cli_version', 'cilium_cli_sha256')):
            if before['runtime'][key] != after['runtime'][key]:
                require(re.fullmatch('[a-f0-9]{64}', options.get(artifact, '')), 'approved download checksum required')
    return changed


def run(*args, timeout=360, input_text=None):
    result = subprocess.run(args, text=True, input=input_text, capture_output=True, timeout=timeout,
                            env={**os.environ, 'KUBECONFIG': '/etc/rancher/k3s/k3s.yaml'})
    require(result.returncode == 0, 'runtime command failed: ' + Path(args[0]).name)
    return result.stdout


def kube(*args):
    return json.loads(run(K3S, 'kubectl', '--request-timeout=20s', *args))


def inspect(*, include_ca=False):
    nodes = kube('get', 'nodes', '-o', 'json')['items']
    require(len(nodes) == 1, 'single-node runtime required')
    node = nodes[0]
    require(any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']), 'node is not Ready')
    binary = run(K3S, '--version').split()[2]
    require(binary == node['status']['nodeInfo']['kubeletVersion'], 'K3s binary/kubelet versions differ')
    releases = kube('-n', 'kube-system', 'get', 'secret', '-l', 'owner=helm,name=cilium,status=deployed', '-o', 'json')['items']
    require(len(releases) == 1, 'one deployed Cilium Helm revision required')
    release = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(releases[0]['data']['release']))))
    images = {}
    for key, kind, name, container in (('agent', 'daemonset', 'cilium', 'cilium-agent'),
                                      ('operator', 'deployment', 'cilium-operator', 'cilium-operator'),
                                      ('envoy', 'daemonset', 'cilium-envoy', 'cilium-envoy')):
        obj = kube('-n', 'kube-system', 'get', kind, name, '-o', 'json')
        rows = [c['image'] for c in obj['spec']['template']['spec']['containers'] if c['name'] == container]
        require(len(rows) == 1, 'Cilium container identity differs')
        images[key] = rows[0]
    cli = re.search(r'cilium-cli:\s*(v\d+\.\d+\.\d+)', run(CILIUM, 'version', '--client'))
    require(cli, 'installed Cilium CLI version unavailable')
    require(run(K3S, 'kubectl', '--request-timeout=20s', 'get', '--raw=/readyz').strip() == 'ok', 'K3s API is not ready')
    run(CILIUM, 'status', '--wait', '--wait-duration', '60s', timeout=75)
    result = {'runtime': {'k3s_version': binary, 'cilium_version': release['chart']['metadata']['version'],
                        'cilium_cli_version': cli[1]}, 'cilium_images': images,
            'node_uid': node['metadata']['uid'], 'node_name': node['metadata']['name'],
            'node_ip': next(a['address'] for a in node['status']['addresses'] if a['type'] == 'InternalIP'),
            'architecture': node['status']['nodeInfo']['architecture'], 'helm_revision': release['version'],
            'ready': True, 'api_ready': True, 'cilium_ready': True}
    if include_ca:
        # Export only the public server CA, never the kubeconfig/client key.
        result['management_ca_data'] = run(K3S, 'kubectl', 'config', 'view', '--raw', '--minify',
            '-o', 'jsonpath={.clusters[0].cluster.certificate-authority-data}').strip()
        require(bool(base64.b64decode(result['management_ca_data'], validate=True)), 'runtime management CA missing')
    return result


def matches(observed, policy):
    # Helm may retain a tag alongside the immutable digest; keep repository and digest bound.
    images = {key: re.sub(r':[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}(?=@sha256:[a-f0-9]{64}$)', '', image)
              for key, image in observed['cilium_images'].items()}
    return observed['runtime'] == policy['runtime'] and images == policy['cilium_images']


def download(url, destination, checksum):
    with urlopen(url, timeout=30) as response, destination.open('xb') as stream:
        shutil.copyfileobj(response, stream)
    require(file_hash(destination) == checksum, 'download checksum differs')


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, value):
    temporary = path.with_suffix('.next')
    temporary.write_text(json.dumps(value)); temporary.chmod(0o600)
    os.replace(temporary, path)


def wait_api():
    deadline = time.monotonic() + 300
    while True:
        try:
            run(K3S, 'kubectl', '--request-timeout=10s', 'get', '--raw=/readyz', timeout=15)
            return
        except (ValueError, subprocess.TimeoutExpired):
            require(time.monotonic() < deadline, 'K3s API did not recover; use the saved backup')
            time.sleep(3)


def load_guard(source):
    spec = importlib.util.spec_from_file_location('upgrade_cilium_preflight', source / 'cilium/preflight.py')
    guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
    return guard


def apply(release, expected, source, base=Path('/var/lib/railshot/runtime-updates'), root=Path('/')):
    changed = validate(release, json.loads((source / 'airgap/versions.json').read_text()))
    observed = inspect()
    require(observed['node_uid'] == expected['node_uid'] and observed['node_ip'] == expected['node_ip']
            and observed['architecture'] == 'amd64', 'runtime node binding differs')
    require(matches(observed, release['from_policy']), 'live runtime differs from approved from-policy')
    guard = load_guard(source)
    pod, service = guard.check_host('customer')
    guard.check_cluster(pod, service, expected_agent=release['from_policy']['cilium_images']['agent'])
    if not changed:
        return {'status': 'verified', 'before': observed, 'after': observed, 'changed': False}
    identity_path = root / 'etc/railshot/runtime-identity.json'
    require(identity_path.is_file(), 'explicit upgrade requires runtime ownership marker')
    identity = json.loads(identity_path.read_text())
    require(identity['target_id'] == expected['target_id'] and identity['resource_id'] == expected['resource_id']
            and identity['node_ip'] == observed['node_ip'] and identity['k3s_version'] == observed['runtime']['k3s_version']
            and identity['cilium_version'] == observed['runtime']['cilium_version'], 'runtime ownership/version differs')
    db = root / 'var/lib/rancher/k3s/server/db'
    require((db / 'state.db').is_file() and not (db / 'etcd').exists(), 'only managed SQLite datastore is supported')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(base.resolve() == base and base.stat().st_uid == os.geteuid() and not base.stat().st_mode & 0o077, 'private recovery directory required')
    for previous in base.glob('*/receipt.json'):
        require(json.loads(previous.read_text())['status'] == 'verified', 'previous runtime upgrade requires operator recovery')
    home = base / release['source_sha']; home.mkdir(mode=0o700)  # Existing attempt is never automatically replayed.
    receipt = {'status': 'preparing', 'source_sha': release['source_sha'], 'before': observed,
               'recovery_directory': str(home), 'automatic_rollback': False}
    save(home / 'receipt.json', receipt)
    before, after = release['from_policy']['runtime'], release['to_policy']['runtime']
    try:
        if before['k3s_version'] != after['k3s_version']:
            download('https://github.com/k3s-io/k3s/releases/download/' + after['k3s_version'] + '/k3s',
                     home / 'k3s.next', release['upgrade']['k3s_binary_sha256'])
        if before['cilium_cli_version'] != after['cilium_cli_version']:
            archive = home / 'cilium.tar.gz'
            download('https://github.com/cilium/cilium-cli/releases/download/' + after['cilium_cli_version'] + '/cilium-linux-amd64.tar.gz',
                     archive, release['upgrade']['cilium_cli_sha256'])
            with tarfile.open(archive) as bundle:
                member = bundle.getmember('cilium'); require(member.isfile(), 'Cilium CLI archive differs')
                with (home / 'cilium.next').open('xb') as stream:
                    shutil.copyfileobj(bundle.extractfile(member), stream)
        helm = kube('-n', 'kube-system', 'get', 'secret', '-l', 'owner=helm,name=cilium', '-o', 'json')
        (home / 'helm-release.json').write_text(json.dumps(helm))
        deployed = [r for r in helm['items'] if r['metadata']['labels'].get('status') == 'deployed']
        require(len(deployed) == 1, 'deployed Helm revision changed before backup')
        helm_values = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(deployed[0]['data']['release']))))['config']
        (home / 'helm-values.json').write_text(json.dumps(helm_values))
        receipt['status'] = 'backup_started'; save(home / 'receipt.json', receipt)
        run('systemctl', 'stop', 'k3s')
        # Stop SQLite before copying its database, WAL, token and configuration.
        with tarfile.open(home / 'k3s-backup.tar', 'x') as backup:
            for path in ('etc/rancher/k3s', 'var/lib/rancher/k3s/server', 'etc/railshot/runtime-identity.json',
                         'usr/local/bin/k3s', 'usr/local/lib/railshot-deployment/cilium'):
                backup.add(root / path, arcname=path)
        receipt.update(status='backup_complete', backup_sha256=file_hash(home / 'k3s-backup.tar'))
        save(home / 'receipt.json', receipt)
        if (home / 'k3s.next').exists():
            run('install', '-m', '0755', str(home / 'k3s.next'), K3S)
        run('systemctl', 'start', 'k3s')
        wait_api()
        run(K3S, 'kubectl', 'wait', '--for=condition=Ready', 'nodes', '--all', '--timeout=300s')
        if (home / 'cilium.next').exists():
            run('install', '-m', '0755', str(home / 'cilium.next'), CILIUM)
        options = ['--version', after['cilium_version'], '--namespace', 'kube-system', '--reset-values',
                   '--values', str(home / 'helm-values.json')]
        for value in ('kubeProxyReplacement=false', 'ipam.mode=cluster-pool',
                      'ipam.operator.clusterPoolIPv4PodCIDRList=' + pod, 'operator.replicas=1',
                      'routingMode=tunnel', 'tunnelProtocol=vxlan', 'hubble.enabled=false'):
            options += ['--set', value]
        for key, value in release['to_policy']['cilium_images'].items():
            options += ['--set-string', {'agent': 'image.override', 'operator': 'operator.image.override', 'envoy': 'envoy.image.override'}[key] + '=' + value]
        run(CILIUM, 'upgrade', *options)
        run(CILIUM, 'status', '--wait', '--wait-duration', '300s')
        run(K3S, 'kubectl', '-n', 'kube-system', 'rollout', 'status', 'deployment/coredns', '--timeout=300s')
        current = inspect(); require(matches(current, release['to_policy']), 'upgraded runtime readback differs')
        guard.check_cluster(pod, service, expected_agent=release['to_policy']['cilium_images']['agent'])
        identity.update(k3s_version=after['k3s_version'], cilium_version=after['cilium_version'])
        save(identity_path, identity)
        receipt.update(status='verified', after=current, changed=True)
        save(home / 'receipt.json', receipt)
        return receipt
    except Exception:
        receipt['status'] = 'recovery_required'; save(home / 'receipt.json', receipt)
        raise


def main():
    os.umask(0o077)
    require(os.geteuid() == 0 and sys.platform == 'linux', 'root Linux executor required')
    payload = json.load(sys.stdin)
    require(payload['action'] in ('inspect', 'apply'), 'unsupported runtime action')
    if payload['action'] == 'inspect':
        require(set(payload) <= {'action', 'include_ca'} and type(payload.get('include_ca', False)) is bool, 'inspect fields differ')
        result = inspect(include_ca=payload.get('include_ca', False))
    else:
        with open('/run/railshot-deployment.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = apply(payload['release'], payload['expected'], Path(payload['source']))
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'error_type': type(error).__name__}), file=sys.stderr)
        sys.exit(1)
