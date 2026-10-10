#!/usr/bin/env python3
"""Fixed SSM entry: fetch the exact current trusted source, then execute its release."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

REPOSITORY = 'https://github.com/Jasmin-Softbank/Railshot.git'


def target_edge_kind(target):
    node_only = target.get('scope') == 'node-only'
    if target.get('scope') not in (None, 'node-only') or node_only and ('edge_config_file' in target or 'binding_file' in target):
        raise ValueError('TARGET_SCOPE_INVALID')
    # Read private configuration only under its existing operator identity.
    result = subprocess.run(['sudo', '-n', '-u', 'railshot-operator', 'python3', '-c',
        'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))',
        target['config_file'] if node_only else target['edge_config_file']],
        capture_output=True, text=True, timeout=10, check=True)
    config = json.loads(result.stdout)
    if node_only:
        if config.get('version') != 2 or config.get('scope') != 'node-only':
            raise ValueError('RUNTIME_SCOPE_MISMATCH')
        return 'none'
    return config.get('edge_kind', 'native')


def execute(trusted_ref):
    scope = os.environ.get('SSM_Scope', 'multicloud')
    if scope not in {'ci-runtime', 'multicloud'}:
        raise ValueError('RELEASE_SCOPE_INVALID')
    names = ('SourceSha', 'Revision', 'DashboardDigest', 'ApiDigest', 'RunnerDigest')
    values = {name: os.environ.get('SSM_' + name, '') for name in names}
    if trusted_ref not in ('refs/heads/main', 'refs/heads/integration/team-assembly-20261002'):
        raise ValueError('TRUSTED_REF_INVALID')
    if any(not re.fullmatch('[a-f0-9]{' + ('40' if n in ('SourceSha', 'Revision') else '64') + '}', v) for n, v in values.items()):
        raise ValueError('RELEASE_INPUT_INVALID')
    for name in ('PublicationRunId', 'PublicationRunAttempt'):
        values[name] = os.environ.get('SSM_' + name, '')
        if not re.fullmatch('[1-9][0-9]{0,19}', values[name]):
            raise ValueError('PUBLICATION_RUN_ID_INVALID')
    source_home = Path('/opt/railshot-release/sources')
    source_home.mkdir(mode=0o755, parents=True, exist_ok=True)
    if source_home.resolve() != source_home or source_home.stat().st_uid != 0 or source_home.stat().st_mode & 0o022:
        raise ValueError('TRUSTED_SOURCE_DIRECTORY_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='railshot-release-', dir=source_home) as directory:
        os.chmod(directory, 0o755)
        old_umask = os.umask(0o022)
        def git(*args):
            result = subprocess.run(['git', *args], cwd=directory, capture_output=True, text=True, timeout=120,
                                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'})
            if result.returncode: raise ValueError('RELEASE_SOURCE_FETCH_FAILED')
            return result.stdout.strip()
        git('init', '-q'); git('remote', 'add', 'origin', REPOSITORY)
        git('fetch', '--depth=1', '--no-tags', 'origin', trusted_ref)
        if git('rev-parse', 'FETCH_HEAD') != values['SourceSha']:
            raise ValueError('SUPERSEDED_RELEASE_SOURCE')
        git('checkout', '--detach', '-q', values['SourceSha'])
        os.umask(old_umask)
        root = Path(directory)
        sys.path.insert(0, str(root / 'deployment/scripts'))
        import release_admission
        import multicloud_release
        import edge_update
        from platform_workers import github_token
        images = {name: f'ghcr.io/jasmin-softbank/railshot-{name}@sha256:' + values[key]
                  for name, key in (('dashboard', 'DashboardDigest'), ('api', 'ApiDigest'), ('ci-runner', 'RunnerDigest'))}
        secret = subprocess.run(['/usr/local/bin/k3s', 'kubectl', '--request-timeout=10s', '-n', 'railshot-system',
                                 'get', 'secret', 'railshot-github', '-o', 'json'], capture_output=True, timeout=20, check=True)
        os.environ['GITHUB_TOKEN'] = github_token(base64.b64decode(json.loads(secret.stdout)['data']['token'], validate=True).decode())
        try:
            release_admission.admit(values['SourceSha'], trusted_ref)
            publication = release_admission.publication(values['SourceSha'], trusted_ref,
                values['PublicationRunId'], values['PublicationRunAttempt'], images)
            if not Path('/etc/railshot/release.json').is_file():
                raise ValueError('RELEASE_CONFIG_UNAVAILABLE')
            config = multicloud_release.private('/etc/railshot/release.json')
            if scope == 'ci-runtime':
                manifest = {'version': 1, 'source_sha': values['SourceSha'], 'platform_revision': values['Revision'], 'images': images}
                return {**multicloud_release.execute(config, manifest, scope=scope), 'publication': publication}
            edge_kinds = {target['provider']: target_edge_kind(target) for target in config['targets']}
            manifest = {'version': 1, 'source_sha': values['SourceSha'], 'platform_revision': values['Revision'], 'images': images,
                        'runtime_policy': json.loads((root / 'deployment/airgap/versions.json').read_bytes()),
                        'provider_targets': {t['provider']: t['target_id'] for t in config['targets']},
                        'edge_kinds': edge_kinds,
                        'edge_modules': {provider: None if edge_kinds[provider] == 'none' else edge_update.module_digest(root, provider, edge_kinds[provider]) for provider in ('aws', 'gcp', 'openstack')}}
            return {**multicloud_release.execute(config, manifest), 'publication': publication}
        finally:
            os.environ.pop('GITHUB_TOKEN', None)


if __name__ == '__main__':
    try:
        result = execute(sys.argv[1])
    except Exception as error:
        result = {'status': 'blocked', 'code': str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_]+', str(error)) else 'RELEASE_HOST_READBACK_REQUIRED'}
    # Detailed receipts remain private on the control host. SSM has a bounded output channel.
    if len(json.dumps(result)) > 20000:
        result = {key: result[key] for key in ('status', 'scope', 'source_sha', 'stage', 'code', 'publication', 'workers', 'apps') if key in result} | {
            'targets': [{k: t[k] for k in ('provider', 'target_id', 'source_sha', 'status', 'code') if k in t} for t in result.get('targets', [])]}
    print(json.dumps(result))
    sys.exit(0 if result['status'] == 'verified' else 1)
