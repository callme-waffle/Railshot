#!/usr/bin/env python3
"""Bootstrap the local-only Argo control plane from reviewed repository inputs."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

import yaml

ROOT = Path(__file__).resolve().parents[3]
ADMIN = Path('/etc/rancher/k3s/k3s.yaml')
STATE = Path('/var/lib/railshot-control')
PROVENANCE_FIELDS = {'source_sha256', 'oci_archive_sha256', 'manifest_digest', 'image_id'}
SHA = re.compile(r'[a-f0-9]{64}')
DIGEST = re.compile(r'sha256:[a-f0-9]{64}')


def require(value, code='LOCAL_CONTROL_BOOTSTRAP_INVALID'):
    if not value:
        raise ValueError(code)


def private(path, *, secret=False):
    path = Path(path)
    require(path.is_absolute() and not path.is_symlink() and path.is_file())
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and (not secret or not info.st_mode & 0o077))
    return path


def load(path, *, secret=False):
    path = private(path, secret=secret)
    require(path.stat().st_size <= 1024 * 1024)
    return json.loads(path.read_bytes())


def atomic(path, value, mode=0o600, uid=None):
    path = Path(path); path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.control-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n'); stream.flush(); os.fchmod(stream.fileno(), mode)
            if uid is not None:
                os.fchown(stream.fileno(), uid, uid)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_text(path, value, *, uid, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.control-secret-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value.rstrip('\n') + '\n'); stream.flush()
            os.fchmod(stream.fileno(), mode); os.fchown(stream.fileno(), uid, uid); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(args, *, input=None, timeout=180, binary=False):
    result = subprocess.run(args, input=input, capture_output=True, timeout=timeout, check=False,
                            text=not binary)
    require(result.returncode == 0, 'LOCAL_CONTROL_COMMAND_FAILED')
    return result.stdout


def step(args, code, **kwargs):
    try:
        return run(args, **kwargs)
    except Exception:
        raise ValueError(code) from None


def hash_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_digest(root=ROOT):
    root = Path(root).resolve()
    raw = run(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], binary=True)
    names = sorted(name.decode() for name in raw.split(b'\0') if name)
    require(names and len(names) == len(set(names)))
    digest = hashlib.sha256(b'railshot-local-source-v1\0')
    for name in names:
        path = root / name
        require(path.is_file() and not path.is_symlink())
        info = path.stat()
        header = json.dumps([name, stat.S_IMODE(info.st_mode), info.st_size], separators=(',', ':')).encode()
        digest.update(len(header).to_bytes(8, 'big')); digest.update(header)
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def oci_identity(archive):
    archive = Path(archive)
    require(archive.is_file() and not archive.is_symlink())
    with tarfile.open(archive, 'r:') as bundle:
        members = bundle.getmembers()
        require(len(members) <= 10000 and all(member.isfile() or member.isdir() for member in members))
        names = [member.name for member in members]
        require(len(names) == len(set(names)))
        def read(name):
            matches = [member for member in members if member.name == name]
            require(len(matches) == 1 and matches[0].isfile() and matches[0].size <= 8 * 1024 * 1024)
            return bundle.extractfile(matches[0]).read()
        def blob(digest):
            require(isinstance(digest, str) and DIGEST.fullmatch(digest))
            value = read('blobs/sha256/' + digest.split(':', 1)[1])
            require('sha256:' + hashlib.sha256(value).hexdigest() == digest)
            return value
        index = json.loads(read('index.json'))
        require(index.get('schemaVersion') == 2 and len(index.get('manifests', [])) == 1)
        descriptor = index['manifests'][0]; manifest_digest = descriptor.get('digest')
        require(DIGEST.fullmatch(manifest_digest or ''))
        manifest = json.loads(blob(manifest_digest))
        require(manifest.get('schemaVersion') == 2 and isinstance(manifest.get('config'), dict))
        image_id = manifest['config'].get('digest'); config = json.loads(blob(image_id))
        require(config.get('os') == 'linux' and config.get('architecture') == 'arm64')
        return manifest_digest, image_id


def prepare_image(args):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', args.control_container),
            'LOCAL_CONTROL_IMAGE_OUTPUT_INVALID')
    output = args.output.resolve(); root = ROOT.resolve()
    require(not output.is_relative_to(root) and not output.exists() and not output.is_symlink(),
            'LOCAL_CONTROL_IMAGE_OUTPUT_INVALID')
    output.mkdir(mode=0o700, parents=True)
    archive, metadata = output / 'railshot-api.oci.tar', output / 'build-metadata.json'
    before = source_digest()
    step(['docker', 'buildx', 'build', '--platform=linux/arm64', '--target=local-renewal',
         '--file', str(ROOT / 'apps/api/Dockerfile'), '--tag=localhost/railshot-api:local',
         '--provenance=false',
         '--output=type=oci,dest=' + str(archive), '--metadata-file=' + str(metadata), str(ROOT)],
         'LOCAL_CONTROL_IMAGE_BUILD_FAILED', timeout=1800)
    require(source_digest() == before, 'LOCAL_CONTROL_IMAGE_SOURCE_CHANGED')
    try:
        manifest_digest, image_id = oci_identity(archive)
    except Exception:
        raise ValueError('LOCAL_CONTROL_IMAGE_OCI_INVALID') from None
    proof = {'source_sha256': before, 'oci_archive_sha256': hash_file(archive),
             'manifest_digest': manifest_digest, 'image_id': image_id}
    image_directory = '/var/lib/rancher/k3s/agent/images'
    step(['docker', 'exec', args.control_container, '/bin/mkdir', '-p', image_directory],
         'LOCAL_CONTROL_IMAGE_COPY_FAILED', timeout=30)
    step(['docker', 'exec', args.control_container, '/bin/chmod', '0700', image_directory],
         'LOCAL_CONTROL_IMAGE_COPY_FAILED', timeout=30)
    destination = args.control_container + ':/var/lib/rancher/k3s/agent/images/railshot-local-api.oci.tar'
    step(['docker', 'cp', str(archive), destination], 'LOCAL_CONTROL_IMAGE_COPY_FAILED', timeout=300)
    step(['docker', 'exec', args.control_container, '/bin/ctr', '-n', 'k8s.io', 'images', 'import',
          image_directory + '/railshot-local-api.oci.tar'],
         'LOCAL_CONTROL_IMAGE_IMPORT_FAILED', timeout=600)
    images = step(['docker', 'exec', args.control_container, '/bin/crictl', 'images', '-o', 'json'],
                  'LOCAL_CONTROL_IMAGE_READBACK_FAILED', timeout=60)
    try:
        observed = json.loads(images)
    except (TypeError, ValueError):
        raise ValueError('LOCAL_CONTROL_IMAGE_READBACK_FAILED') from None
    require(any(manifest_digest in row.get('repoDigests', []) or any(manifest_digest in value for value in row.get('repoDigests', []))
                for row in observed.get('images', [])), 'LOCAL_CONTROL_IMAGE_READBACK_FAILED')
    atomic(output / 'local-image-provenance.json', proof)
    print(json.dumps({'status': 'verified', **proof}))


def kube(*args, document=None, timeout=60):
    command = ['kubectl', '--kubeconfig', str(ADMIN), '--request-timeout=20s', *args]
    raw = run(command, input=None if document is None else json.dumps(document), timeout=timeout)
    return json.loads(raw) if raw.strip() else None


def get(namespace, kind, name):
    return kube('-n', namespace, 'get', kind, name, '--ignore-not-found', '-o', 'json')


def create(document):
    namespace = document.get('metadata', {}).get('namespace')
    prefix = ['-n', namespace] if namespace else []
    return kube(*prefix, 'create', '-f', '-', '-o', 'json', document=document)


def labels(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', value)


def validate_registration_role(role):
    require(role['apiVersion'] == 'rbac.authorization.k8s.io/v1' and role['kind'] == 'Role')
    rules = role.get('rules')
    if rules is None:
        rules = []
    require(isinstance(rules, list))
    for rule in rules:
        require(isinstance(rule, dict) and set(rule) == {'apiGroups', 'resources', 'resourceNames', 'verbs'}
                and len(rule['apiGroups']) == len(rule['resources']) == 1
                and isinstance(rule['resourceNames'], list) and rule['resourceNames']
                and all(labels(name) for name in rule['resourceNames']))
        group, resource = rule['apiGroups'][0], rule['resources'][0]
        require((group, resource) in {('argoproj.io', 'appprojects'), ('argoproj.io', 'applications'), ('', 'secrets')}
                and rule['verbs'] in (['get', 'patch'], ['delete']))


def stable(value):
    result = {key: value[key] for key in value if key not in ('status',)}
    meta = result.get('metadata')
    if isinstance(meta, dict):
        result['metadata'] = {key: meta[key] for key in ('name', 'namespace', 'labels', 'annotations') if key in meta}
    return result


def contains(expected, observed):
    """Require every declared field while tolerating Kubernetes default fields."""
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            key in observed and contains(value, observed[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(observed, list) and len(expected) == len(observed) and all(
            contains(left, right) for left, right in zip(expected, observed))
    return expected == observed


def ensure_registration():
    document = yaml.safe_load((ROOT / 'deployment/manifests/runtime-registration-access.yaml').read_bytes())
    require(document.get('apiVersion') == 'v1' and document.get('kind') == 'List'
            and isinstance(document.get('items'), list) and len(document['items']) == 5)
    for desired in document['items']:
        meta = desired['metadata']; namespace = meta['namespace']; kind, name = desired['kind'], meta['name']
        existing = get(namespace, kind, name)
        if not existing:
            create(desired)
            existing = get(namespace, kind, name)
        if kind == 'Role' and name == 'railshot-product-registrations':
            validate_registration_role(existing)
        else:
            require(contains(stable(desired), stable(existing)), 'LOCAL_CONTROL_OWNERSHIP_CONFLICT')


def apply(document, manager='railshot-local-control'):
    namespace = document.get('metadata', {}).get('namespace')
    prefix = ['-n', namespace] if namespace else []
    return kube(*prefix, 'apply', '--server-side', '--field-manager=' + manager, '-f', '-', '-o', 'json', document=document)


def install_argo():
    for namespace in ('argocd', 'railshot-system'):
        desired = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}}
        if not kube('get', 'namespace', namespace, '--ignore-not-found', '-o', 'json'):
            create(desired)
    rendered = run(['kubectl', 'kustomize', str(ROOT / 'gitops/argo')], timeout=180)
    run(['kubectl', '--kubeconfig', str(ADMIN), 'apply', '--server-side',
         '--field-manager=railshot-local-argo', '-f', '-'], input=rendered, timeout=300)
    patch = [
        {'op': 'add', 'path': '/spec/template/spec/hostNetwork', 'value': True},
        {'op': 'add', 'path': '/spec/template/spec/dnsPolicy', 'value': 'ClusterFirstWithHostNet'},
    ]
    kube('-n', 'argocd', 'patch', 'statefulset', 'argocd-application-controller', '--type=json',
         '--patch-file=/dev/stdin', '-o', 'json', document=patch)
    for kind, name in (('deployment', 'argocd-repo-server'), ('deployment', 'argocd-server'),
                       ('statefulset', 'argocd-application-controller')):
        run(['kubectl', '--kubeconfig', str(ADMIN), '-n', 'argocd', 'rollout', 'status', kind + '/' + name,
             '--timeout=600s'], timeout=620)
    controller = get('argocd', 'statefulset', 'argocd-application-controller')
    pod = controller['spec']['template']['spec']
    require(pod.get('hostNetwork') is True and pod.get('dnsPolicy') == 'ClusterFirstWithHostNet')


def repo_credential(token_path, username, url, auth_output):
    require(isinstance(username, str) and re.fullmatch(r'[A-Za-z0-9-]{1,39}', username)
            and url == 'https://github.com/Jasmin-Softbank')
    token = private(token_path, secret=True).read_text().strip()
    require(20 <= len(token) <= 512 and not re.search(r'\s', token))
    desired = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque', 'metadata': {
        'name': 'railshot-local-github', 'namespace': 'argocd', 'labels': {
            'argocd.argoproj.io/secret-type': 'repo-creds', 'app.kubernetes.io/managed-by': 'railshot'}},
        'stringData': {'url': url, 'username': username, 'password': token}}
    existing = get('argocd', 'secret', desired['metadata']['name'])
    if existing:
        require(existing.get('metadata', {}).get('labels') == desired['metadata']['labels']
                and not existing['metadata'].get('ownerReferences'), 'LOCAL_CONTROL_OWNERSHIP_CONFLICT')
    applied = apply(desired)
    expected = {key: base64.b64encode(value.encode()).decode() for key, value in desired['stringData'].items()}
    require(applied.get('data') == expected)
    auth_output = Path(auth_output)
    auth_output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(auth_output, 1000, 1000); auth_output.chmod(0o700)
    atomic_text(auth_output / 'github-token', token, uid=1000)


def credentials_module():
    path = ROOT / 'gitops/credentials.py'
    module_path = str(path.parent)
    sys.path.insert(0, module_path)
    try:
        spec = importlib.util.spec_from_file_location('railshot_local_credentials', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    finally:
        sys.path.remove(module_path)
    return module


def provenance(path):
    value = load(path)
    require(set(value) == PROVENANCE_FIELDS
            and all(isinstance(value[key], str) and SHA.fullmatch(value[key])
                    for key in ('source_sha256', 'oci_archive_sha256'))
            and all(isinstance(value[key], str) and DIGEST.fullmatch(value[key])
                    for key in ('manifest_digest', 'image_id')))
    return value


def ensure_credentials(policy_path, provenance_path):
    module = credentials_module(); requested = load(policy_path)
    module.validate_policy(requested)
    live = get('argocd', 'configmap', 'railshot-credentials')
    if live:
        current = json.loads(live['data']['policy.json'])
        module.validate_policy(current)
        require(current == requested, 'LOCAL_RENEWAL_POLICY_RECONCILE_REQUIRED')
    proof = provenance(provenance_path)
    image = 'localhost/railshot-api@' + proof['manifest_digest']
    declaration = module.render(requested, image, platform_arch='arm64', local_provenance=proof)
    for item in declaration['items']:
        apply(item, manager='railshot-local-renewal')
    cron = get('argocd', 'cronjob', 'railshot-credentials')
    template = cron['spec']['jobTemplate']['spec']['template']
    annotations = template['metadata']['annotations']; pod = template['spec']
    require(pod['hostNetwork'] is True and pod['dnsPolicy'] == 'ClusterFirstWithHostNet'
            and pod['nodeSelector'] == {'kubernetes.io/arch': 'arm64', 'railshot.io/node-role': 'platform'}
            and annotations['railshot.io/local-manifest-digest'] == proof['manifest_digest'])
    return proof, image


def verify_local_job(proof, image):
    name = 'railshot-credentials-local-' + proof['manifest_digest'].split(':')[1][:20]
    job = get('argocd', 'job', name)
    if not job:
        raw = run(['kubectl', '--kubeconfig', str(ADMIN), '-n', 'argocd', 'create', 'job',
                   '--from=cronjob/railshot-credentials', name, '-o', 'json'])
        job = json.loads(raw)
    run(['kubectl', '--kubeconfig', str(ADMIN), '-n', 'argocd', 'wait', '--for=condition=complete',
         'job/' + name, '--timeout=300s'], timeout=320)
    pods = kube('-n', 'argocd', 'get', 'pods', '-l', 'job-name=' + name, '-o', 'json')['items']
    require(len(pods) == 1)
    pod = pods[0]; statuses = pod.get('status', {}).get('containerStatuses', [])
    terminated = statuses[0].get('state', {}).get('terminated', {}) if len(statuses) == 1 else {}
    containers = pod.get('spec', {}).get('containers', [])
    require(len(statuses) == 1 and terminated.get('exitCode') == 0
            and len(containers) == 1 and containers[0].get('image') == image
            and statuses[0].get('imageID') == image)
    node = kube('get', 'node', pod['spec']['nodeName'], '-o', 'json')
    require(node['metadata'].get('uid') and node['metadata'].get('labels', {}).get('kubernetes.io/arch') == 'arm64'
            and node['metadata']['labels'].get('railshot.io/node-role') == 'platform')
    return {'job_uid': job['metadata']['uid'], 'pod_uid': pod['metadata']['uid'],
            'pod_image_id': statuses[0]['imageID'], 'node_uid': node['metadata']['uid']}


def bootstrap(args):
    require(os.geteuid() == 0)
    private(ADMIN, secret=True)
    gateway = str(private(args.gateway_config, secret=True))
    restored = run(['python3', str(ROOT / 'deployment/scripts/personal_wireguard.py'),
        '--config', gateway, '--restore'], timeout=30)
    require(json.loads(restored).get('status') == 'succeeded', 'LOCAL_CONTROL_GATEWAY_GUARD_FAILED')
    isolated = run(['python3', str(ROOT / 'deployment/scripts/personal_wireguard.py'),
        '--config', gateway, '--verify-forwarding'], timeout=30)
    require(json.loads(isolated).get('forwarding_isolated') is True, 'LOCAL_CONTROL_GATEWAY_GUARD_FAILED')
    install_argo()
    ensure_registration()
    repo_credential(args.github_token, args.github_user, args.repo_prefix, args.auth_output)
    proof, image = ensure_credentials(args.policy, args.provenance)
    runtime = verify_local_job(proof, image)
    isolated = run(['python3', str(ROOT / 'deployment/scripts/personal_wireguard.py'),
        '--config', gateway, '--verify-forwarding'], timeout=30)
    require(json.loads(isolated).get('forwarding_isolated') is True, 'LOCAL_CONTROL_GATEWAY_GUARD_FAILED')
    receipt = {'version': 1, 'status': 'verified', 'platform': 'linux/arm64',
               'image': image, 'provenance': proof, **runtime}
    atomic(Path(args.state) / 'bootstrap-receipt.json', receipt)
    print(json.dumps({'status': 'verified', 'image': image, 'node_uid': runtime['node_uid']}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    image = commands.add_parser('image', help='build, bind and import the local arm64 API image')
    image.add_argument('--output', type=Path, required=True)
    image.add_argument('--control-container', default='railshot-personal-control')
    apply_parser = commands.add_parser('apply', help='apply and verify the local control declarations')
    apply_parser.add_argument('--policy', type=Path, required=True)
    apply_parser.add_argument('--provenance', type=Path, required=True)
    apply_parser.add_argument('--github-token', type=Path, required=True)
    apply_parser.add_argument('--github-user', required=True)
    apply_parser.add_argument('--repo-prefix', default='https://github.com/Jasmin-Softbank')
    apply_parser.add_argument('--gateway-config', type=Path, default=Path('/etc/railshot-personal-gateway/config.json'))
    apply_parser.add_argument('--auth-output', type=Path, default=Path('/var/lib/railshot-control-auth'))
    apply_parser.add_argument('--state', type=Path, default=STATE)
    args = parser.parse_args()
    try:
        prepare_image(args) if args.command == 'image' else bootstrap(args)
    except Exception as error:
        code = str(error)
        if not re.fullmatch(r'LOCAL_CONTROL_[A-Z0-9_]+', code):
            code = 'LOCAL_CONTROL_BOOTSTRAP_FAILED'
        print(json.dumps({'status': 'blocked', 'code': code}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
