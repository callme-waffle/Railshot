#!/usr/bin/env python3
"""Prepare app CD and a pending route request from a verified CI publication, locally only."""
import argparse
import base64
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

import applications
import environment as runtime
import handoff
import yaml


def require(condition, code='APPLICATION_PUBLICATION_INVALID'):
    applications.require(condition, code)


def file_sha(path):
    return hashlib.sha256(runtime.read_private(path, raw=True)).hexdigest()


def request_file(path):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and path.resolve() == path and stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid() and not info.st_mode & 0o077
            and 0 < info.st_size <= 14_000_000, 'APPLICATION_REQUEST_INVALID')
    return json.loads(path.read_bytes(), object_pairs_hook=runtime.ansible.unique_pairs)


def _registration(config, request):
    require(applications.exact(request, ('deployment_id', 'application_id', 'environment_id', 'publication', 'files')),
            'APPLICATION_REQUEST_INVALID')
    deployment_id = request['deployment_id']
    require(isinstance(deployment_id, str) and str(uuid.UUID(deployment_id)) == deployment_id, 'APPLICATION_REQUEST_INVALID')
    publication = request['publication']
    require(isinstance(publication, dict), 'APPLICATION_PUBLICATION_INVALID')
    registration_request = {key: request[key] for key in ('application_id', 'environment_id')}
    registration_request['app'] = publication.get('app')
    profile, _, descriptor, endpoint, _, authority, _, fingerprint = applications._inputs(config, registration_request)
    app_id = request['application_id']
    home = Path(config['state_dir']) / app_id
    applications.assert_deployable(home)
    record = runtime.read_private(home / 'registration.json')
    require(record.get('status') == 'succeeded', 'APPLICATION_NOT_REGISTERED')
    require(all(record.get(key) == value for key, value in registration_request.items())
            and record.get('target_id') == app_id and record.get('provider') == profile['provider']
            and record.get('input_sha256') == fingerprint
            and record.get('environment_sha256') == applications.digest(authority), 'APPLICATION_BINDING_CHANGED')
    binding_raw = runtime.read_private(home / 'binding.json', raw=True)
    require(record.get('binding_sha256') == hashlib.sha256(binding_raw).hexdigest(), 'APPLICATION_BINDING_CHANGED')
    binding = json.loads(binding_raw, object_pairs_hook=runtime.ansible.unique_pairs)
    require(applications.exact(binding, ('version', 'environment_id', 'application_id', 'provider', 'cd', 'registered', 'ingress', 'hostname'))
            and type(binding['version']) is int and binding['version'] == 1
            and binding['environment_id'] == request['environment_id'] and binding['application_id'] == app_id
            and binding['provider'] == profile['provider'] and binding['cd'] == config['cd']
            and binding['ingress'] == profile['ingress'], 'APPLICATION_BINDING_CHANGED')
    registered = binding['registered']
    require(applications.exact(registered, ('app', 'tenant', 'target')) and registered['app'] == publication.get('app')
            and registered['tenant'] == profile['tenant'], 'APPLICATION_BINDING_CHANGED')
    target = {**copy.deepcopy(profile['target']), 'id': app_id, 'namespace': app_id, 'project': app_id,
              'argocd_namespace': 'argocd', 'cluster_server': endpoint, 'node_port': record.get('node_port'),
              'path': 'gitops/applications/' + registered['app'] + '/' + app_id,
              'image_pull_secret': {'namespace': app_id, 'name': 'ghcr-pull'}}
    require(type(target['node_port']) is int and 30000 <= target['node_port'] <= 32767
            and registered['target'] == target and record.get('target') == target, 'APPLICATION_BINDING_CHANGED')
    hostname = applications.service_name(registered['app'], profile['tenant'], request['environment_id'],
                                         profile['ingress']['base_domain'])['hostname']
    require(binding['hostname'] == record.get('hostname') == hostname, 'APPLICATION_BINDING_CHANGED')
    require(publication.get('target_id') == app_id and publication.get('tenant') == profile['tenant']
            and isinstance(publication.get('source_commit'), str) and re.fullmatch(r'[a-f0-9]{40}', publication['source_commit']),
            'APPLICATION_PUBLICATION_MISMATCH')
    require(isinstance(publication.get('images'), dict) and publication['images'] and all(
        isinstance(image, str) and re.fullmatch(r'ghcr\.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}', image)
        for image in publication['images'].values()), 'APPLICATION_PUBLICATION_MISMATCH')
    return home, binding, record['binding_sha256'], descriptor, authority


def finalize(config_path, request):
    config = applications.load_config(config_path)
    # This same lock serializes application ownership and registration receipt changes.
    root = applications.private_directory(config['state_dir'])
    with os.fdopen(os.open(root / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                'APPLICATION_STORAGE_INVALID')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise applications.RegistrationError('REGISTRATION_BUSY') from exc
        home, binding, binding_sha, descriptor, authority = _registration(config, request)
        fingerprint = applications.digest({'request': request, 'binding_sha256': binding_sha})
        run = applications.private_directory(applications.private_directory(home / 'deployments') / request['deployment_id'])
        journal = run / 'release.json'
        if journal.exists():
            saved = runtime.read_private(journal)
            require(saved.get('input_sha256') == fingerprint, 'APPLICATION_RELEASE_CONFLICT')
            require(saved.get('status') == 'succeeded', 'APPLICATION_PREPARATION_INCOMPLETE')
            require(applications.exact(saved.get('files_sha256'), ('cd.json', 'route-request.json'))
                    and all(file_sha(run / name) == sha for name, sha in saved['files_sha256'].items()), 'APPLICATION_RELEASE_CHANGED')
            return saved['result']
        require(not any((run / name).exists() or (run / name).is_symlink()
                        for name in ('cd.json', 'route-request.json')), 'APPLICATION_RELEASE_CONFLICT')
        files = request['files']
        require(isinstance(files, dict), 'APPLICATION_PUBLICATION_INVALID')
        spec_name = handoff.spec_name(files)
        spec64 = files[spec_name]
        require(isinstance(spec64, str) and len(spec64) < 2_700_000)
        spec = yaml.safe_load(base64.b64decode(spec64, validate=True))
        require(isinstance(spec, dict) and isinstance(spec.get('services'), list) and len(spec['services']) == 1)
        health = handoff.http_path(spec['services'][0]['health'])
        registered = {**copy.deepcopy(binding['registered']),
                      'public_http': {'url': 'https://' + binding['hostname'] + health, 'expected_status': 200}}
        runtime.bridge.validate_public_http(registered['public_http'], health)
        cd = {**binding['cd'], 'targets': {request['application_id']: registered}}
        cd_bytes = runtime.bridge.encoded(cd)
        bridge_request = {'action': 'apply', 'deployment_id': request['deployment_id'], 'target_id': request['application_id'],
                          'config_sha256': hashlib.sha256(cd_bytes).hexdigest(),
                          'publication': request['publication'], 'files': files}
        _, contents, _ = runtime.bridge.validate_request({**cd, '_sha256': bridge_request['config_sha256']}, bridge_request)
        # Validate original bytes through the existing release contract. No build, image pull,
        # registry request, Git fetch/push, Kubernetes action or public HTTP probe occurs here.
        with tempfile.TemporaryDirectory(prefix='.publication-', dir=run) as temporary:
            for name, raw in contents.items():
                runtime.durable_write(Path(temporary) / name, raw)
            _, images, publication = handoff.read_artifact(temporary, request['application_id'])
            revision = runtime.bridge.git(cd, 'rev-parse', '--verify', 'HEAD')
            require(re.fullmatch(r'[a-f0-9]{40}', revision), 'GITOPS_CHECKOUT_INVALID')
            rendered = handoff.render(temporary, {**registered['target'], 'revision': revision})
        require(rendered['app'] == registered['app'] and rendered['tenant'] == registered['tenant']
                and rendered['source_commit'] == request['publication']['source_commit'], 'APPLICATION_PUBLICATION_MISMATCH')
        http = rendered['http']
        route_request = {'version': 1, 'deployment_id': request['deployment_id'], 'application_id': request['application_id'],
            'environment_id': request['environment_id'], 'target_id': request['application_id'], 'app': registered['app'],
            'tenant': registered['tenant'], 'namespace': registered['target']['namespace'], 'provider': binding['provider'],
            'resource': {'resource_id': descriptor['resource_id'], 'cluster_server': registered['target']['cluster_server'],
                'private_address': descriptor['addresses']['private'], 'registry_file': config['registry_file'],
                'registry_target_id': request['environment_id'], 'authority_sha256': applications.digest(authority)},
            'hostname': binding['hostname'], 'node_port': http['node_port'], 'port': http['container_port'],
            'health_path': http['health_path'], 'route': http['route'], 'source_commit': rendered['source_commit'],
            'images': images, 'source_repository': config['environments'][request['environment_id']]['source_repository'],
            'publication_id': {key: request['publication'][key] for key in ('run_id', 'producer_attempt', 'bundle_artifact_id', 'artifact_id')},
            'publication_sha256': applications.digest(request['publication']),
            'files_sha256': {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()},
            'binding_sha256': binding_sha, 'ingress': binding['ingress'], 'public_route_state': 'pending'}
        result = {'status': 'succeeded', 'phase': 'cd_prepared', 'application_id': request['application_id'],
                  'environment_id': request['environment_id'], 'target_id': request['application_id'], 'app': registered['app'],
                  'public_route_state': 'pending'}
        intent = {'status': 'preparing', 'input_sha256': fingerprint, 'binding_sha256': binding_sha,
                  'render_revision': revision, 'result': result}
        runtime.save(journal, intent)
        # Completion is last; a partial local write cannot be mistaken for deployability.
        runtime.durable_write(run / 'cd.json', cd_bytes)
        runtime.save(run / 'route-request.json', route_request)
        runtime.save(journal, {**intent, 'status': 'succeeded',
                              'files_sha256': {name: file_sha(run / name) for name in ('cd.json', 'route-request.json')}})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = finalize(args.config, request_file(args.request))
    except Exception as exc:
        result = {'status': 'blocked', 'public_route_state': 'pending',
                  'error': {'code': exc.code if isinstance(exc, applications.RegistrationError) else 'APPLICATION_PREPARATION_FAILED',
                            'retryable': False, 'outcome_unknown': False}}
    print(json.dumps(result))
    return 0 if result['status'] == 'succeeded' else 3


if __name__ == '__main__':
    raise SystemExit(main())
