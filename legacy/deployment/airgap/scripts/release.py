#!/usr/bin/env python3
"""Local release packaging and explicit draft-only GitHub distribution; no runtime downloads."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
try:
    from .bundle import BundleError, local_file, sha256, verify
except ImportError:  # Standalone CLI.
    from bundle import BundleError, local_file, sha256, verify

VERSION = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+')


def call(args, timeout=600):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise BundleError('ARTIFACT_TIMEOUT', Path(args[0]).name + ' exceeded time limit') from exc
    if p.returncode:
        raise BundleError('ARTIFACT_COMMAND_FAILED', p.stderr.strip()[-1500:])
    return p.stdout


def names(arch, version):
    if arch not in ('arm64', 'amd64') or not VERSION.fullmatch(version):
        raise BundleError('ARTIFACT_INPUT_INVALID', 'Expected arm64/amd64 and vMAJOR.MINOR.PATCH')
    return [f'railshot-airgap-{arch}-{version}.tar.zst', f'bundle-manifest-{arch}.json',
            f'artifact-metadata-{arch}.json', f'checksums-{arch}.txt']


def validate_assets(directory, arch, version):
    directory = Path(directory)
    archive, manifest, metadata, checksums = names(arch, version)
    data = json.loads(local_file(directory, metadata).read_text())
    if data['architecture'] != arch or data['release_version'] != version:
        raise BundleError('ARTIFACT_METADATA_MISMATCH', 'Version/architecture mismatch')
    required = {archive, manifest}
    if set(data['assets']) != required:
        raise BundleError('ARTIFACT_METADATA_MISMATCH', 'Expected exactly archive and manifest assets')
    for name, info in data['assets'].items():
        path = local_file(directory, name)
        if path.stat().st_size != info['size'] or sha256(path) != info['sha256']:
            raise BundleError('ARTIFACT_CHECKSUM_MISMATCH', name)
    expected = ''.join(f'{sha256(local_file(directory, n))}  {n}\n' for n in (archive, manifest, metadata))
    if local_file(directory, checksums).read_text() != expected:
        raise BundleError('ARTIFACT_CHECKSUM_MISMATCH', 'checksums file differs from assets')
    return data


def pack(bundle_path, output, version, trusted_manifest=None):
    bundle = verify(bundle_path, expected_sha256=trusted_manifest)
    arch = bundle['manifest']['platform'].split('/')[1]
    asset_names = names(arch, version)
    output = Path(output).resolve()
    if output.exists():
        data = validate_assets(output, arch, version)
        if data['manifest_sha256'] != bundle['manifest_sha256']:
            raise BundleError('ARTIFACT_IMMUTABLE', 'Existing release directory belongs to another bundle')
        return {'status': 'reused', 'directory': str(output), **data}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.railshot-release-', dir=output.parent) as temp:
        work = Path(temp); archive, manifest, metadata, checksums = asset_names
        process = subprocess.Popen(['zstd', '-q', '-T2', '-3', '-o', str(work/archive)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=process.stdin, mode='w|') as tar:
                # Only verified declared files, not arbitrary contents in the source directory.
                refs = {'bundle-manifest.json', 'manifest.sha256'} | {i['path'] for i in bundle['manifest']['files'].values()}
                for ref in sorted(refs):
                    tar.add(local_file(Path(bundle['root']), ref), arcname=ref, recursive=False)
            process.stdin.close()
            if process.wait(timeout=300):
                raise BundleError('ARTIFACT_COMPRESSION_FAILED', process.stderr.read().decode()[-1500:])
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
            process.stderr.close()
        shutil.copyfile(Path(bundle['root'])/'bundle-manifest.json', work/manifest)
        data = {'schema_version': '1', 'release_version': version, 'release_tag': 'airgap-bundle-'+version,
                'architecture': arch, 'bundle_version': bundle['manifest']['bundle_version'],
                'manifest_sha256': bundle['manifest_sha256'], 'runtime': bundle['manifest']['runtime'],
                'created_at': datetime.now(timezone.utc).isoformat(), 'publication_status': 'local_prepared',
                'assets': {n: {'sha256': sha256(work/n), 'size': (work/n).stat().st_size} for n in (archive, manifest)}}
        (work/metadata).write_text(json.dumps(data, indent=2)+'\n')
        (work/checksums).write_text(''.join(f'{sha256(work/n)}  {n}\n' for n in (archive, manifest, metadata)))
        validate_assets(work, arch, version)
        os.replace(work, output)
        return {'status': 'prepared', 'directory': str(output), **data}


def auth(repo, write=False):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise BundleError('ARTIFACT_INPUT_INVALID', 'Expected owner/repository')
    call(['gh', 'auth', 'status'], 30)
    info = json.loads(call(['gh', 'api', 'repos/'+repo], 30))
    if write and not info.get('permissions', {}).get('push'):
        raise BundleError('RELEASE_PERMISSION_DENIED', 'GitHub write permission is required')


def upload(args):
    directory = Path(args.directory).resolve()
    data = validate_assets(directory, args.architecture, args.version)
    auth(args.repo, write=True)
    tag = 'airgap-bundle-'+args.version
    p = subprocess.run(['gh', 'release', 'view', tag, '--repo', args.repo, '--json', 'isDraft,assets'], capture_output=True, text=True, timeout=30)
    if p.returncode:
        if not args.create_draft:
            raise BundleError('RELEASE_NOT_FOUND', 'Create an approved draft first or explicitly pass --create-draft')
        if not args.target or not re.fullmatch(r'[a-f0-9]{40}', args.target) or not args.notes_file:
            raise BundleError('ARTIFACT_INPUT_INVALID', 'Draft creation requires exact --target commit SHA and --notes-file')
        # Do not turn a transient API error into a create: check the tag explicitly via GitHub API.
        check = subprocess.run(['gh', 'api', f'repos/{args.repo}/releases/tags/{tag}'], capture_output=True, text=True, timeout=30)
        if check.returncode == 0 or '404' not in check.stderr:
            raise BundleError('RELEASE_LOOKUP_FAILED', check.stderr.strip()[-1000:])
        call(['gh', 'release', 'create', tag, '--repo', args.repo, '--draft', '--target', args.target,
              '--title', tag, '--notes-file', str(Path(args.notes_file).resolve())], 60)
        state = {'isDraft': True, 'assets': []}
    else:
        state = json.loads(p.stdout)
    if not state['isDraft']:
        raise BundleError('RELEASE_IMMUTABLE', 'Helper uploads only to draft releases; never changes a published release')
    assets = names(args.architecture, args.version)
    if {a['name'] for a in state['assets']} & set(assets):
        raise BundleError('RELEASE_ASSET_EXISTS', 'Refusing to overwrite existing assets; select a new release version')
    call(['gh', 'release', 'upload', tag, '--repo', args.repo, *[str(directory/n) for n in assets]], 1800)
    return {'status': 'uploaded_to_draft', 'repository': args.repo, 'tag': tag, 'assets': assets, 'published': False}


def unpack(archive, destination):
    destination = Path(destination)
    destination.mkdir()
    process = subprocess.Popen(['zstd', '-d', '-q', '-c', str(archive)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    seen, total = set(), 0
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as tar:
            for member in tar:
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or not path.parts or not (member.isfile() or member.isdir()) or member.name in seen:
                    raise BundleError('BUNDLE_ARCHIVE_UNSAFE', member.name)
                seen.add(member.name); total += member.size
                if total > 10*1024**3:
                    raise BundleError('BUNDLE_ARCHIVE_UNSAFE', 'Uncompressed bundle exceeds 10 GiB')
                target = destination/path
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open('xb') as stream:
                        shutil.copyfileobj(tar.extractfile(member), stream)
        if process.wait(timeout=300):
            raise BundleError('ARTIFACT_DECOMPRESSION_FAILED', process.stderr.read().decode()[-1500:])
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdout.close(); process.stderr.close()


def download(args):
    # No "latest" selection. Trust pins come from onboarding, not from downloaded metadata.
    asset_names = names(args.architecture, args.version)
    for pin in (args.archive_sha256, args.manifest_sha256):
        if not re.fullmatch(r'[a-f0-9]{64}', pin):
            raise BundleError('ARTIFACT_INPUT_INVALID', 'Trusted SHA256 pins are required')
    output = Path(args.output).resolve()
    if output.exists():
        bundle = verify(output, expected_sha256=args.manifest_sha256)
        return {'status': 'reused', 'path': str(output), 'manifest_sha256': bundle['manifest_sha256']}
    auth(args.repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.railshot-download-', dir=output.parent) as temp:
        work = Path(temp); assets = work/'assets'; assets.mkdir()
        command = ['gh', 'release', 'download', 'airgap-bundle-'+args.version, '--repo', args.repo, '--dir', str(assets)]
        for name in asset_names: command += ['--pattern', name]
        call(command, 1800)
        data = validate_assets(assets, args.architecture, args.version)
        if data['assets'][asset_names[0]]['sha256'] != args.archive_sha256 or data['manifest_sha256'] != args.manifest_sha256:
            raise BundleError('ARTIFACT_CHECKSUM_MISMATCH', 'Release differs from trusted onboarding pins')
        extracted = work/'bundle'; unpack(assets/asset_names[0], extracted)
        bundle = verify(extracted, expected_sha256=args.manifest_sha256)
        os.replace(extracted, output)
    return {'status': 'downloaded_and_verified', 'path': str(output), 'manifest_sha256': bundle['manifest_sha256']}


def main():
    parser = argparse.ArgumentParser(description='Explicit, checksummed GitHub Release distribution helpers')
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('pack'); p.add_argument('--bundle', required=True); p.add_argument('--output', required=True)
    p.add_argument('--version', required=True); p.add_argument('--manifest-sha256')
    for action in ('upload', 'download'):
        p = sub.add_parser(action); p.add_argument('--repo', required=True); p.add_argument('--version', required=True)
        p.add_argument('--architecture', choices=('amd64', 'arm64'), required=True)
        if action == 'upload':
            p.add_argument('--directory', required=True); p.add_argument('--create-draft', action='store_true')
            p.add_argument('--target'); p.add_argument('--notes-file')
        else:
            p.add_argument('--output', required=True); p.add_argument('--archive-sha256', required=True); p.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    try:
        result = pack(args.bundle, args.output, args.version, args.manifest_sha256) if args.action == 'pack' else upload(args) if args.action == 'upload' else download(args)
        print(json.dumps(result)); return 0
    except (BundleError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(json.dumps({'status': 'failed', 'error': {'code': getattr(exc, 'code', 'ARTIFACT_ERROR'), 'message': str(exc)}})); return 1


if __name__ == '__main__': sys.exit(main())
