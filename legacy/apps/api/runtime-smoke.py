#!/usr/bin/env python3
"""API container readiness checks; versions/imports only, never cloud or cluster calls."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PIN_FILES = {'ansible-core': 'ci/requirements-dev.txt', 'PyYAML': 'ci/requirements-test.txt',
             'jsonschema': 'ci/requirements-test.txt'}


def requirements():
    result = {}
    for package, filename in PIN_FILES.items():
        matches = re.findall(r'^' + re.escape(package) + r'==([0-9]+(?:\.[0-9]+)+)$',
                             (ROOT / filename).read_text(), re.M)
        if len(matches) != 1:
            raise ValueError('One existing exact dependency pin required: ' + package)
        result[package] = matches[0]
    return result


def tools():
    manifest = json.loads((ROOT / 'apps/api/runtime-tools.json').read_text())
    rows = manifest['tools']
    if manifest['platform'] != 'linux/amd64' or set(rows) != {'terraform', 'kubectl', 'aws', 'ssm', 'gcloud'}:
        raise ValueError('Exact Linux amd64 controller tool set required')
    hosts = {'terraform': 'releases.hashicorp.com', 'kubectl': 'dl.k8s.io', 'aws': 'awscli.amazonaws.com',
             'ssm': 's3.amazonaws.com', 'gcloud': 'storage.googleapis.com'}
    names = set()
    for name, row in rows.items():
        parsed = urlsplit(row['url'])
        if (not re.fullmatch(r'v?\d+(?:\.\d+){2,3}', row['version']) or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])
                or not re.fullmatch(r'[a-z][a-z0-9.]+', row['filename']) or row['filename'] in names
                or parsed.scheme != 'https' or parsed.hostname != hosts[name] or parsed.username or parsed.password
                or parsed.query or parsed.fragment or row['version'] not in parsed.path):
            raise ValueError('Pinned official tool URL/checksum required: ' + name)
        names.add(row['filename'])
    runtime = json.loads((ROOT / 'deployment/airgap/versions.json').read_text())['runtime']
    if rows['kubectl']['version'] != runtime['k3s_version'].split('+')[0]:
        raise ValueError('kubectl must match the existing K3s version policy')
    return rows


def verify_artifacts(directory, rows):
    for row in rows.values():
        with (Path(directory) / row['filename']).open('rb') as stream:
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != row['sha256']:
                raise ValueError('Downloaded tool checksum differs: ' + row['filename'])


def installed(rows):
    if sys.version_info < (3, 11):
        raise ValueError('Python 3.11 or newer is required by the native controller and Ansible')
    for package, version in requirements().items():
        if importlib.metadata.version(package) != version:
            raise ValueError('Python dependency version differs: ' + package)
    # All probes are local. Isolated config prevents discovery of host/operator credentials.
    with tempfile.TemporaryDirectory(prefix='railshot-runtime-smoke-') as temporary:
        env = {**os.environ, 'HOME': temporary, 'CLOUDSDK_CONFIG': temporary,
               'ANSIBLE_CONFIG': str(ROOT / 'infrastructure/ansible/ansible.cfg'),
               'CLOUDSDK_CORE_DISABLE_PROMPTS': '1', 'CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK': 'true',
               'AWS_EC2_METADATA_DISABLED': 'true', 'AWS_PAGER': '', 'PYTHONDONTWRITEBYTECODE': '1', 'CHECKPOINT_DISABLE': '1'}
        def run(*args):
            return subprocess.run(args, env=env, cwd=ROOT, text=True, capture_output=True,
                                  check=True, timeout=30).stdout
        if json.loads(run('terraform', 'version', '-json'))['terraform_version'] != rows['terraform']['version']:
            raise ValueError('Terraform version differs')
        if json.loads(run('kubectl', 'version', '--client', '-o', 'json'))['clientVersion']['gitVersion'] != rows['kubectl']['version']:
            raise ValueError('kubectl version differs')
        if not run('aws', '--version').startswith('aws-cli/' + rows['aws']['version'] + ' '):
            raise ValueError('AWS CLI version differs')
        if run('session-manager-plugin', '--version').strip() != rows['ssm']['version']:
            raise ValueError('SSM plugin version differs')
        if json.loads(run('gcloud', 'version', '--format=json'))['Google Cloud SDK'] != rows['gcloud']['version']:
            raise ValueError('gcloud version differs')
        run('git', '--version')
        subprocess.run(['ssh', '-V'], env=env, check=True, capture_output=True, timeout=10)
        run('ansible-playbook', '--version')
        run('ansible-vault', '--version')
        run('openssl', 'version')
        for script in ['observability/register.py', 'observability/bootstrap.py', 'gitops/bridge.py', 'gitops/edge.py',
                       'gitops/credentials.py', 'deployment/scripts/environment.py', 'ci/scripts/runner/replenish.py',
                       'deployment/scripts/applications.py', 'deployment/scripts/application_routes.py',
                       'deployment/scripts/application_lifecycle.py',
                       'deployment/cloudflared/register.py',
                       'infrastructure/providers/terraform_tools/provision.py', 'infrastructure/ansible/run.py',
                       'infrastructure/providers/terraform_tools/budget.py',
                       'infrastructure/ansible/cluster.py', 'infrastructure/providers/terraform_tools/access.py']:
            run(sys.executable, script, '--help')
        # Ansible builtin task imports and all runtime copy sources must actually be packaged.
        for script in ['guest.yml', 'runtime.yml', 'database.yml', 'application-database.yml']:
            run('ansible-playbook', '-i', 'localhost,', str(ROOT / 'infrastructure/ansible' / script), '--syntax-check')
        for relative in ['ci/scripts/schemas/railshot.schema.json', 'contracts/ansible-request.schema.json',
                         'infrastructure/ansible/ansible.cfg', 'infrastructure/ansible/group_vars/all.yml',
                         'infrastructure/ansible/tasks/guest-checks.yml',
                         'deployment/scripts/common.sh', 'deployment/bootstrap/preflight.sh', 'deployment/bootstrap/install-k3s.sh',
                         'deployment/bootstrap/health.sh', 'deployment/bootstrap/runtime-healthz.py', 'observability/runtime_health.py',
                         'deployment/cilium/install.sh', 'deployment/cilium/preflight.py', 'deployment/cilium/health.sh']:
            if not (ROOT / relative).is_file():
                raise ValueError('Missing native runtime source: ' + relative)
        for provider in ['aws', 'gcp']:
            if not (ROOT / 'infrastructure/terraform' / provider / '.terraform.lock.hcl').is_file():
                raise ValueError('Missing reviewed Terraform provider lock: ' + provider)
    print('API native runtime smoke passed (no cloud calls).')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--requirements', action='store_true')
    mode.add_argument('--downloads', action='store_true')
    mode.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    if args.requirements:
        print('\n'.join(f'{package}=={version}' for package, version in requirements().items()))
        return
    rows = tools()
    if args.downloads:
        for row in rows.values():
            print('\t'.join(row[key] for key in ['filename', 'url', 'sha256']))
    elif args.artifacts:
        verify_artifacts(args.artifacts, rows)
        print('All five official tool downloads match the committed checksums.')
    else:
        installed(rows)


if __name__ == '__main__':
    main()
