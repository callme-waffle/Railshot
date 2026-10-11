#!/usr/bin/env python3
"""Verify registry access, then bind unchanged evidence to one CI producer."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent / 'gate'))
from bundle import spec_name


DNS_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
TARGET_ID = r'[a-z][a-z0-9-]{0,62}'


def validate_target(env):
    """Workflow targets come only from the operator allowlist, never app input."""
    bindings = json.loads(env.get('CONFIGURED_BINDINGS') or '{}')
    if not isinstance(bindings, dict):
        raise ValueError('operator target bindings must be an object')
    for target, binding in bindings.items():
        if (not re.fullmatch(TARGET_ID, target) or not isinstance(binding, dict)
                or set(binding) != {'app', 'tenant', 'image_pull_secret'}
                or not isinstance(binding['app'], str)
                or not re.fullmatch(r'[a-z][a-z0-9-]{1,28}[a-z0-9]', binding['app'])
                or not isinstance(binding['tenant'], str)
                or not re.fullmatch(r'[a-z0-9]{1,20}', binding['tenant'])
                or not isinstance(binding['image_pull_secret'], dict)
                or set(binding['image_pull_secret']) != {'namespace', 'name'}
                or not all(isinstance(v, str) and re.fullmatch(DNS_LABEL, v)
                           for v in binding['image_pull_secret'].values())):
            raise ValueError('invalid operator target binding')
    binding = bindings.get(env.get('TARGET_ID'))
    if binding is not None:
        if any(env.get(k.upper()) != binding[k] for k in ('app', 'tenant')):
            raise ValueError('application differs from registered target binding')
        return binding
    raw = env.get('CONFIGURED_TARGETS', '')
    targets = json.loads(raw) if raw else [env.get('CONFIGURED_TARGET', '')]
    if not isinstance(targets, list) or not targets or not all(
            isinstance(target, str) and re.fullmatch(TARGET_ID, target) for target in targets):
        raise ValueError('operator target allowlist must contain valid target IDs')
    if len(targets) != len(set(targets)) or env.get('TARGET_ID') not in targets:
        raise ValueError('target is not in the operator-configured allowlist')


def validate_registry(registry, images_sha256):
    if not isinstance(registry, dict) or set(registry) != {
            'visibility', 'verification', 'images_sha256', 'image_pull_secret'}:
        raise ValueError('explicit registry access contract required')
    if registry['images_sha256'] != images_sha256 or not re.fullmatch(r'[a-f0-9]{64}', images_sha256):
        raise ValueError('registry image evidence hash mismatch')
    private = registry['visibility'] == 'private'
    if registry['visibility'] not in ('public', 'private') or registry['verification'] != (
            'authenticated_manifest_read' if private else 'anonymous_manifest_read'):
        raise ValueError('unsupported registry verification')
    secret = registry['image_pull_secret']
    if private:
        if not isinstance(secret, dict) or set(secret) != {'namespace', 'name'} or not all(
                isinstance(v, str) and re.fullmatch(DNS_LABEL, v) for v in secret.values()):
            raise ValueError('private registry requires namespace and image pull Secret name')
    elif secret is not None:
        raise ValueError('public registry cannot carry a pull Secret reference')


def verify_registry(images_bytes, env):
    if env.get('CONFIGURED_BINDINGS'):
        binding = validate_target(env)
        if binding:
            env = {**env, 'PULL_SECRET_NAMESPACE': binding['image_pull_secret']['namespace'],
                   'PULL_SECRET_NAME': binding['image_pull_secret']['name']}
    images = json.loads(images_bytes)
    prefix = env.get('REGISTRY_PREFIX', '')
    if not re.fullmatch(r'ghcr\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*', prefix):
        raise ValueError('operator GHCR prefix required')
    if not isinstance(images, dict) or not images or not all(
            isinstance(ref, str) and ref.startswith(prefix + '/') and re.fullmatch(
                r'ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}', ref) for ref in images.values()):
        raise ValueError('bound immutable GHCR digests required')
    visibility = env.get('REGISTRY_VISIBILITY', 'private')
    private = visibility == 'private'
    registry = {'visibility': visibility,
                'verification': 'authenticated_manifest_read' if private else 'anonymous_manifest_read',
                'images_sha256': hashlib.sha256(images_bytes).hexdigest(),
                'image_pull_secret': {'namespace': env.get('PULL_SECRET_NAMESPACE', ''),
                                      'name': env.get('PULL_SECRET_NAME', '')} if private else None}
    validate_registry(registry, registry['images_sha256'])
    auth = {}
    if private:
        username, token = env.get('GHCR_PULL_USERNAME', ''), env.get('GHCR_PULL_TOKEN', '')
        if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,38})', username) or not token.strip():
            raise ValueError('dedicated GHCR pull username and token required')
        auth['auth'] = base64.b64encode((username + ':' + token).encode()).decode()
    # A new config with an explicit GHCR entry never inherits the publisher's login/helper.
    with tempfile.TemporaryDirectory(prefix='railshot-pull-', dir=env.get('RUNNER_TEMP')) as directory:
        config = Path(directory) / 'config.json'
        config.touch(mode=0o600)
        config.write_text(json.dumps({'auths': {'ghcr.io': auth}}))
        docker_env = {k: v for k, v in env.items() if k not in (
            'GHCR_PULL_TOKEN', 'GHCR_TOKEN', 'GITHUB_TOKEN', 'DOCKER_AUTH_CONFIG')}
        docker_env['DOCKER_CONFIG'] = directory
        for ref in images.values():
            try:
                result = subprocess.run(['docker', 'manifest', 'inspect', ref], env=docker_env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError('registry manifest verification could not complete') from exc
            if result.returncode:
                raise ValueError('published digest is not readable with the configured pull access')
    return registry


def prepare(bundle, images, output, env):
    bundle, images, output = Path(bundle), Path(images), Path(output)
    fields = {'source_commit': env['SOURCE_COMMIT'], 'target_id': env['TARGET_ID'],
              'tenant': env['TENANT'], 'app': env['APP']}
    for key, pattern in {'source_commit': r'[a-f0-9]{40}', 'target_id': TARGET_ID,
                         'tenant': r'[a-z0-9]{1,20}', 'app': r'[a-z][a-z0-9-]{1,28}[a-z0-9]'}.items():
        if not re.fullmatch(pattern, fields[key]):
            raise ValueError('invalid publication identity: ' + key)
    if env['GITHUB_SHA'] != fields['source_commit']:
        raise ValueError('workflow/source commit mismatch')
    receipt = {'version': 2, 'status': 'published', **fields,
               'run_id': int(env['GITHUB_RUN_ID']), 'producer_attempt': int(env['GITHUB_RUN_ATTEMPT']),
               'bundle_artifact_id': int(env['BUNDLE_ARTIFACT_ID'])}
    if any(receipt[key] < 1 for key in ('run_id', 'producer_attempt', 'bundle_artifact_id')):
        raise ValueError('positive producer identifiers required')
    spec = spec_name(path.name for path in bundle.iterdir())
    files = {'images.json': images, **{name: bundle / name for name in (spec, 'verdict.json', 'manifest.json')}}
    if any(path.is_symlink() or not path.is_file() for path in files.values()):
        raise ValueError('publication inputs must be regular files')
    contents = {name: path.read_bytes() for name, path in files.items()}
    receipt['files'] = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    if output.exists():
        raise FileExistsError('publication output already exists')
    receipt['registry'] = verify_registry(contents['images.json'], env)
    output.mkdir()  # Refuse to mix a previous publication into this attempt.
    for name, data in contents.items():
        (output / name).write_bytes(data)
    (output / 'handoff.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle')
    parser.add_argument('images')
    parser.add_argument('output')
    args = parser.parse_args()
    prepare(args.bundle, args.images, args.output, os.environ)
