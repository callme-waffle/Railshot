#!/usr/bin/env python3
"""Local checksummed bundle format; runtime operations never download artifacts."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
from input_adapter import parse_input

DIGEST = re.compile(r'sha256:[a-f0-9]{64}')


class BundleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            result.update(block)
    return result.hexdigest()


def architecture():
    arch = {'aarch64': 'arm64', 'arm64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
    if not arch:
        raise BundleError('ARCH_UNSUPPORTED', platform.machine())
    return arch


def policy():
    return json.loads((ROOT/'airgap/versions.json').read_text())


def approved(spec):
    expected = policy()['runtime']
    if any(getattr(spec.runtime, key) != value for key, value in expected.items()):
        raise BundleError('VERSION_NOT_APPROVED', 'Requested runtime differs from airgap/versions.json; test and promote a profile explicitly')


def canonical(image):
    if '/' not in image:
        return 'docker.io/library/' + image
    part = image.split('/')[0]
    if '.' not in part and ':' not in part and part != 'localhost':
        return 'docker.io/' + image
    if image.startswith('docker.io/') and '/' not in image[len('docker.io/'):]:
        return 'docker.io/library/' + image[len('docker.io/'):]
    return image


def pinned(reference, digest):
    repository = reference.split('@')[0]
    if ':' in repository.rsplit('/', 1)[-1]:
        repository = repository.rsplit(':', 1)[0]
    return repository + '@' + digest


def local_file(root, name):
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise BundleError('BUNDLE_PATH_UNSAFE', str(name))
    for ancestor in (path, *path.parents):
        if (root/ancestor).is_symlink():
            raise BundleError('BUNDLE_PATH_UNSAFE', f'Symlink: {name}')
    actual = root/path
    if not actual.is_file():
        raise BundleError('BUNDLE_FILE_MISSING', str(name))
    return actual


def safe_archive(archive):
    for member in archive.getmembers():
        if member.name.startswith('/') or '..' in Path(member.name).parts or not (member.isfile() or member.isdir()):
            raise BundleError('BUNDLE_ARCHIVE_UNSAFE', member.name)


def verify(path, spec=None, expected_sha256=None):
    root = Path(path)
    for managed in ('/var/lib/rancher/k3s', '/etc/rancher/k3s'):
        if root.resolve().is_relative_to(Path(managed).resolve()):
            raise BundleError('BUNDLE_PATH_UNSAFE', 'Bundle must be outside K3s directories removed by full cleanup')
    if not root.is_dir():
        raise BundleError('BUNDLE_MISSING', 'Local bundle directory is absent')
    manifest_path = local_file(root, 'bundle-manifest.json')
    digest = sha256(manifest_path)
    checksum = local_file(root, 'manifest.sha256').read_text().strip()
    if checksum != digest or (expected_sha256 and expected_sha256 != digest):
        raise BundleError('BUNDLE_CHECKSUM_MISMATCH', 'Manifest checksum differs from checksum file or pinned onboarding digest')
    try:
        data = json.loads(manifest_path.read_text())
        if data['schema_version'] != '1' or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', data['bundle_version']):
            raise ValueError('Unsupported manifest version/name')
        if data['platform'] != 'linux/' + architecture():
            raise BundleError('BUNDLE_PLATFORM_MISMATCH', f'Bundle={data["platform"]}, node=linux/{architecture()}')
        if data['runtime'] != policy()['runtime']:
            raise BundleError('VERSION_NOT_APPROVED', 'Bundle runtime versions are not approved')
        if spec:
            approved(spec)
        for required in ('k3s', 'installer', 'k3s_images', 'cilium_cli', 'cilium_chart'):
            if required not in data['files']:
                raise BundleError('BUNDLE_FILE_MISSING', required)
        for key, item in data['files'].items():
            file = local_file(root, item['path'])
            if not re.fullmatch(r'[a-f0-9]{64}', item['sha256']) or file.stat().st_size != item['size'] or sha256(file) != item['sha256']:
                raise BundleError('BUNDLE_CHECKSUM_MISMATCH', f'Artifact checksum/size mismatch: {key}')
        images = data['images']
        if not isinstance(images, list) or not images:
            raise ValueError('images must be a nonempty list')
        references = set()
        for image in images:
            ref = image['reference']
            if ref in references or not DIGEST.fullmatch(image['digest']):
                raise ValueError('Duplicate reference or malformed image digest')
            references.add(ref)
            if image['archive'] not in data['files']:
                raise BundleError('BUNDLE_IMAGE_MISSING', f'Archive absent for {ref}')
        for role, ref in policy()['cilium_images'].items():
            if not any(i['reference'] == ref and i['digest'] == ref.split('@')[1] and i['role'] == 'cilium_' + role for i in images):
                raise BundleError('BUNDLE_IMAGE_MISSING', 'Approved Cilium image absent: ' + role)
        required_refs = {canonical(policy()['health_image']), canonical(policy()['sample_image'])}
        if spec:
            required_refs.add(canonical(spec.workload.image))
        if required_refs - references:
            raise BundleError('BUNDLE_IMAGE_MISSING', 'Images absent: ' + ', '.join(sorted(required_refs - references)))
        for marker in ('rancher/mirrored-coredns-coredns:', 'rancher/mirrored-pause:'):
            if not any(marker in i['reference'] and i['archive'] == 'k3s_images' for i in images):
                raise BundleError('BUNDLE_IMAGE_MISSING', 'K3s system image absent: ' + marker)
        # OCI index must agree with declared image digests; no platform content is pulled during verify.
        groups = defaultdict(list)
        for image in images:
            if image['archive'] != 'k3s_images':
                groups[image['archive']].append(image)
        for key, group in groups.items():
            with tarfile.open(local_file(root, data['files'][key]['path'])) as archive:
                safe_archive(archive)
                for member in archive.getmembers():
                    if member.isfile() and member.name.startswith('blobs/sha256/'):
                        expected_blob = member.name.rsplit('/', 1)[-1]
                        hashed = hashlib.sha256()
                        stream = archive.extractfile(member)
                        for block in iter(lambda: stream.read(1024*1024), b''):
                            hashed.update(block)
                        if hashed.hexdigest() != expected_blob:
                            raise BundleError('BUNDLE_DIGEST_MISMATCH', 'OCI blob content differs from digest: ' + member.name)
                index = json.load(archive.extractfile('index.json'))
                actual = {m.get('annotations', {}).get('io.containerd.image.name', m.get('annotations', {}).get('org.opencontainers.image.ref.name')): m['digest'] for m in index['manifests']}
                for image in group:
                    if actual.get(image['reference']) != image['digest']:
                        raise BundleError('BUNDLE_IMAGE_MISSING', 'OCI index/reference digest mismatch: ' + image['reference'])
                    if not archive.getmember('blobs/sha256/' + image['digest'].split(':')[1]).isfile():
                        raise BundleError('BUNDLE_IMAGE_MISSING', 'Image root blob absent: ' + image['reference'])
        with tarfile.open(local_file(root, data['files']['cilium_chart']['path'])) as chart:
            safe_archive(chart)
            chart.getmember('cilium/Chart.yaml')
        return {'root': str(root.resolve()), 'manifest': data, 'manifest_sha256': digest}
    except BundleError:
        raise
    except (KeyError, TypeError, ValueError, tarfile.TarError, OSError) as exc:
        raise BundleError('BUNDLE_INVALID', f'Invalid bundle manifest/archive: {exc}') from exc


def artifact(bundle, key):
    return Path(bundle['root'])/bundle['manifest']['files'][key]['path']


def copy_if_changed(source, target, mode=0o644):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and sha256(target) == sha256(source):
        return False
    fd, temp = tempfile.mkstemp(prefix='.railshot-', dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, temp)
        os.chmod(temp, mode)
        os.replace(temp, target)
    finally:
        if Path(temp).exists():
            Path(temp).unlink()
    return True


def stage_k3s(bundle):
    marker = Path('/etc/rancher/k3s/config.yaml')
    if not marker.is_file() or '# Managed by Railshot deployment runtime.' not in marker.read_text():
        raise BundleError('OWNERSHIP_CONFLICT', 'K3s staging requires the managed dedicated-node configuration')
    binary = Path('/usr/local/bin/k3s')
    if binary.exists() and sha256(binary) != bundle['manifest']['files']['k3s']['sha256']:
        raise BundleError('BINARY_DRIFT', 'Existing K3s differs from the verified binary; no automatic replacement')
    copy_if_changed(artifact(bundle, 'k3s'), binary, 0o755)
    copy_if_changed(artifact(bundle, 'k3s_images'), '/var/lib/rancher/k3s/agent/images/railshot-system.tar.zst')
    copy_if_changed(artifact(bundle, 'cilium_cli'), '/usr/local/lib/railshot-deployment/cilium', 0o755)


def ctr(*args):
    try:
        result = subprocess.run(['/usr/local/bin/k3s', 'ctr', '-n', 'k8s.io', *args], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise BundleError('IMAGE_PRELOAD_TIMEOUT', 'Local containerd operation exceeded 300 seconds') from exc
    if result.returncode:
        raise BundleError('IMAGE_PRELOAD_FAILED', result.stderr.strip()[-2000:])
    return result.stdout


def preload(bundle):
    current = {p[0]: p[2] for line in ctr('images', 'list').splitlines()[1:] if len(p := line.split()) >= 3}
    groups = defaultdict(list)
    for image in bundle['manifest']['images']:
        groups[image['archive']].append(image)
    imported, reused = [], []
    for key, group in groups.items():
        if all(current.get(i['reference']) == i['digest'] for i in group):
            reused.extend(i['reference'] for i in group)
            continue
        ctr('images', 'import', '--platform', bundle['manifest']['platform'], '--digests',
            '--label', 'io.cri-containerd.image=managed', str(artifact(bundle, key)))
        imported.append(key)
    current = {p[0]: p[2] for line in ctr('images', 'list').splitlines()[1:] if len(p := line.split()) >= 3}
    missing = [i['reference'] for i in bundle['manifest']['images'] if current.get(i['reference']) != i['digest']]
    if missing:
        raise BundleError('IMAGE_DIGEST_MISMATCH', 'Imported references differ from bundle: ' + ', '.join(missing))
    # Verify platform layers actually exist, not only image-name metadata. ctr performs no registry access here.
    # Bundled ctr v2.2.7 checks the native platform; its `check` subcommand has no --platform flag.
    checks = ctr('images', 'check')
    names = {i['reference'] for i in bundle['manifest']['images']}
    for line in checks.splitlines()[1:]:
        if line.split() and line.split()[0] in names and ('incomplete' in line or 'complete' not in line):
            raise BundleError('IMAGE_CONTENT_MISSING', line)
    return {'imported_archives': imported, 'reused_images': reused, 'digest_verified': True}


def cilium_values(bundle, offline):
    values = {}
    keys = {'agent': 'image', 'operator': 'operator.image', 'envoy': 'envoy.image'}
    for role, ref in policy()['cilium_images'].items():
        branch = values
        for part in keys[role].split('.'):
            branch = branch.setdefault(part, {})
        branch.update(override=ref, pullPolicy='Never' if offline else 'IfNotPresent')
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('verify', 'stage-k3s', 'preload'))
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--input')
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    try:
        spec = parse_input(json.loads(Path(args.input).read_text())).spec if args.input else None
        bundle = verify(args.bundle, spec, args.manifest_sha256)
        output = {'status': 'verified', 'bundle_version': bundle['manifest']['bundle_version'], 'manifest_sha256': bundle['manifest_sha256']}
        if args.action == 'stage-k3s':
            stage_k3s(bundle)
        elif args.action == 'preload':
            output['preload'] = preload(bundle)
        print(json.dumps(output))
        return 0
    except (BundleError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'failed', 'error': {'code': getattr(exc, 'code', 'BUNDLE_ERROR'), 'message': str(exc)}}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
