#!/usr/bin/env python3
"""Connected Linux builder using an existing K3s containerd, isolated image namespace."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from uuid import uuid4
from urllib.parse import quote
from bundle import ROOT, BundleError, architecture, approved, canonical, pinned, policy, sha256, verify
sys.path.insert(0, str(ROOT/'scripts'))
from input_adapter import parse_input


def main():
    parser = argparse.ArgumentParser(description='Prepare immutable bundle on connected Linux with existing K3s/containerd')
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--bundle-version', required=True)
    parser.add_argument('--image', action='append', default=[], help='Optional additional tagged/digest workload image')
    args = parser.parse_args()
    work = None
    namespace = None
    try:
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise BundleError('BUILDER_PREREQUISITE', 'Requires root on connected Linux with running K3s/containerd')
        spec = parse_input(json.loads(Path(args.input).read_text())).spec
        approved(spec)
        config = policy()
        arch = architecture()
        platform_name = 'linux/' + arch
        output = Path(args.output).resolve()
        if output.exists():
            existing = verify(output, spec)
            refs = {i['reference'] for i in existing['manifest']['images']}
            if existing['manifest']['bundle_version'] != args.bundle_version or {canonical(i) for i in args.image} - refs:
                raise BundleError('BUNDLE_IMMUTABLE', 'Use a new output directory/version to add images or refresh bundle')
            print(json.dumps({'status': 'reused', 'path': str(output), 'manifest_sha256': existing['manifest_sha256']}))
            return 0
        output.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix='.railshot-bundle-', dir=output.parent))
        (work/'artifacts').mkdir(); (work/'images').mkdir()
        files, images = {}, []

        def call(argv, timeout=600):
            p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
            if p.returncode:
                raise BundleError('BUNDLE_PREPARE_FAILED', p.stderr.strip()[-2000:] or p.stdout[-2000:])
            return p.stdout

        def fetch(url, target):
            print('[BUNDLE] download ' + target.name, file=sys.stderr, flush=True)
            call(['curl', '--fail', '--silent', '--show-error', '--location', '--retry', '2', '--connect-timeout', '5',
                  '--max-time', '180', url, '-o', str(target)])

        def record(key, file, source=None):
            files[key] = {'path': str(file.relative_to(work)), 'sha256': sha256(file), 'size': file.stat().st_size}
            if source:
                files[key]['source'] = source

        version = quote(spec.runtime.k3s_version, safe='')
        release = 'https://github.com/k3s-io/k3s/releases/download/' + version + '/'
        checks = work/'artifacts/upstream-k3s-sha256.txt'
        fetch(release + f'sha256sum-{arch}.txt', checks)
        known = {line.split()[-1].lstrip('*'): line.split()[0] for line in checks.read_text().splitlines() if len(line.split()) >= 2}
        for key, name in (('k3s', 'k3s-arm64' if arch == 'arm64' else 'k3s'), ('k3s_images', f'k3s-airgap-images-{arch}.tar.zst')):
            file = work/'artifacts'/name
            fetch(release+name, file)
            if known.get(name) != sha256(file):
                raise BundleError('UPSTREAM_CHECKSUM_MISMATCH', name)
            record(key, file, release+name)
        installer_url = f'https://raw.githubusercontent.com/k3s-io/k3s/{version}/install.sh'
        installer = work/'artifacts/install-k3s.sh'; fetch(installer_url, installer); record('installer', installer, installer_url)
        cli_name = f'cilium-linux-{arch}.tar.gz'
        cli_url = f'https://github.com/cilium/cilium-cli/releases/download/{spec.runtime.cilium_cli_version}/{cli_name}'
        cli_archive = work/'artifacts'/cli_name; fetch(cli_url, cli_archive)
        cli_checksum = work/'artifacts/cilium-upstream.sha256'; fetch(cli_url+'.sha256sum', cli_checksum)
        if cli_checksum.read_text().split()[0] != sha256(cli_archive):
            raise BundleError('UPSTREAM_CHECKSUM_MISMATCH', cli_name)
        with tarfile.open(cli_archive) as tar:
            member = tar.getmember('cilium')
            if not member.isfile():
                raise BundleError('BUNDLE_ARCHIVE_UNSAFE', 'Cilium binary is not a regular file')
            with (work/'artifacts/cilium').open('wb') as target:
                shutil.copyfileobj(tar.extractfile(member), target)
        record('cilium_cli', work/'artifacts/cilium', cli_url)
        cli_archive.unlink(); cli_checksum.unlink(); checks.unlink()
        chart_url = f'https://helm.cilium.io/cilium-{spec.runtime.cilium_version}.tgz'
        chart = work/'artifacts/cilium-chart.tgz'; fetch(chart_url, chart); record('cilium_chart', chart, chart_url)

        def ctr(*parts):
            return call(['/usr/local/bin/k3s', 'ctr', '-n', namespace, *parts])

        namespace = 'railshot-bundle-' + uuid4().hex[:12]
        call(['/usr/local/bin/k3s', 'ctr', 'namespaces', 'create', namespace])
        ctr('images', 'import', '--platform', platform_name, str(work/files['k3s_images']['path']))
        for line in ctr('images', 'list').splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3 and not parts[0].startswith('sha256:'):
                images.append({'reference': parts[0], 'digest': parts[2], 'archive': 'k3s_images', 'role': 'k3s_system'})
        requested = [(r, 'cilium_' + k) for k, r in config['cilium_images'].items()]
        requested += [(config['health_image'], 'health'), (spec.workload.image, 'workload'), (config['sample_image'], 'sample')]
        requested += [(i, 'extra') for i in args.image]
        seen = {i['reference'] for i in images}
        for raw, role in requested:
            # Additional images pass the same injection/tag validation as workload input.
            extra = json.loads(Path(args.input).read_text())
            extra['workload'] = {'image': raw, 'namespace': spec.workload.namespace}
            parse_input(extra)
            ref = canonical(raw)
            if ref in seen:
                continue
            print('[BUNDLE] resolve/export ' + ref, file=sys.stderr, flush=True)
            ctr('images', 'pull', '--platform', platform_name, ref)
            inventory = {p[0]: p[2] for line in ctr('images', 'list').splitlines()[1:] if len(p := line.split()) >= 3}
            digest = inventory[ref]
            immutable = pinned(ref, digest)
            if immutable != ref:
                ctr('images', 'tag', '--force', ref, immutable)
            aliases = list(dict.fromkeys([ref, immutable]))
            key = 'image_' + str(len(files))
            # Different tags may resolve to the same digest; do not overwrite an earlier alias index.
            file = work/'images'/f'{digest.split(":")[1]}-{key}.tar'
            ctr('images', 'export', '--platform', platform_name, '--skip-manifest-json', str(file), *aliases)
            record(key, file)
            for alias in aliases:
                images.append({'reference': alias, 'digest': digest, 'archive': key, 'role': role})
                seen.add(alias)
        data = {'schema_version': '1', 'bundle_version': args.bundle_version,
                'created_at': datetime.now(timezone.utc).isoformat(), 'platform': platform_name,
                'runtime': config['runtime'], 'version_policy': config['policy_version'], 'files': files, 'images': images}
        manifest = work/'bundle-manifest.json'
        manifest.write_text(json.dumps(data, indent=2)+'\n')
        (work/'manifest.sha256').write_text(sha256(manifest)+'\n')
        verified = verify(work, spec)
        os.chmod(work, 0o755)
        os.replace(work, output); work = None
        print(json.dumps({'status': 'prepared', 'path': str(output), 'bundle_version': data['bundle_version'],
                          'manifest_sha256': verified['manifest_sha256'], 'platform': platform_name, 'images': len(images)}))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(json.dumps({'status': 'failed', 'error': {'code': getattr(exc, 'code', 'BUNDLE_PREPARE_FAILED'), 'message': str(exc)}}))
        return 1
    finally:
        if work and work.exists():
            shutil.rmtree(work)
        if namespace:
            # Only this invocation's namespace; never remove the builder cluster's k8s.io images.
            try:
                names = subprocess.check_output(['/usr/local/bin/k3s', 'ctr', '-n', namespace, 'images', 'list', '-q'], text=True, timeout=30).splitlines()
                if names:
                    subprocess.run(['/usr/local/bin/k3s', 'ctr', '-n', namespace, 'images', 'remove', *names], capture_output=True, timeout=60, check=True)
                for attempt in range(5):
                    removal = subprocess.run(['/usr/local/bin/k3s', 'ctr', 'namespaces', 'remove', namespace], capture_output=True, text=True, timeout=30)
                    if removal.returncode == 0:
                        break
                    if attempt == 4:
                        raise RuntimeError(removal.stderr.strip())
                    # Image removal/transfer lease garbage collection is asynchronous.
                    time.sleep(1)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                print('[WARN] Builder namespace cleanup requires manual retry: ' + namespace + ': ' + str(exc), file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
