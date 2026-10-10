#!/usr/bin/env python3
"""Build a deterministic, credential-free personal client artifact; never publish."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

RUNTIME_FILES = (
    'deployment/bootstrap/uninstall.sh', 'deployment/bootstrap/install_payload.py',
    'deployment/bootstrap/revoke_personal_identity.py',
    'deployment/bootstrap/requirements.lock',
    'deployment/bootstrap/templates/agent-authorized-keys.README',
    'infrastructure/providers/openstack/templates/cloud-init.yaml.tmpl',
    'infrastructure/ansible/run.py', 'infrastructure/ansible/transport.py',
    'infrastructure/ansible/runtime.yml', 'infrastructure/ansible/guest.yml',
    'infrastructure/ansible/ansible.cfg', 'infrastructure/ansible/tasks/guest-checks.yml',
    'ci/scripts/storage.py', 'contracts/ansible-request.schema.json',
    'deployment/scripts/common.sh', 'deployment/scripts/lifecycle_runtime.py', 'deployment/bootstrap/preflight.sh',
    'deployment/cloudflared/render.py',
    'deployment/bootstrap/install-k3s.sh', 'deployment/bootstrap/health.sh',
    'deployment/bootstrap/runtime-healthz.py', 'deployment/cilium/install.sh',
    'deployment/cilium/preflight.py', 'deployment/cilium/health.sh', 'deployment/airgap/versions.json',
)


def package(source, output):
    source, output = Path(source), Path(output)
    files = []
    for subtree in ('apps/agent', 'deployment/bootstrap/client_setup', 'infrastructure/providers/openstack'):
        for path in (source / subtree).rglob('*.py'):
            if any(part in ('tests', '__pycache__', '.venv') for part in path.relative_to(source).parts):
                continue
            if path.is_symlink():
                raise ValueError('Artifact symlink rejected')
            if path.is_file():
                files.append(path)
    files.extend(source / name for name in RUNTIME_FILES)
    files.extend(source / name for name in (
        'deployment/bootstrap/install.sh',
        'deployment/bootstrap/personal-install.sh',
        'deployment/bootstrap/personal-registration.sh',
    ))
    if any(not path.is_file() or path.is_symlink() for path in files):
        raise ValueError('Required runtime file missing or unsafe')
    required = {'apps/agent/personal.py', 'apps/agent/personal_remove.py', 'apps/agent/openstack_control.py',
                'deployment/bootstrap/install.sh', 'deployment/bootstrap/personal-registration.sh',
                'deployment/cloudflared/render.py',
                'deployment/bootstrap/client_setup/state.py', 'infrastructure/providers/openstack/cli.py'}
    if not required <= {str(path.relative_to(source)) for path in files}:
        raise ValueError('Required client files missing')
    # Exclusive output creation avoids replacing a reviewed release artifact.
    with output.open('xb') as destination:
        with gzip.GzipFile(filename='', mode='wb', fileobj=destination, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for path in sorted(files):
                    data = path.read_bytes()
                    record = tarfile.TarInfo(str(path.relative_to(source)))
                    record.size, record.mode, record.mtime = len(data), 0o644, 0
                    archive.addfile(record, io.BytesIO(data))
    return {'artifact_path': str(output.resolve()), 'artifact_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.source, args.output)))


if __name__ == '__main__':
    main()
