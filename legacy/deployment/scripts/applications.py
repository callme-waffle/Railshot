#!/usr/bin/env python3
"""Register an app on an existing runtime; never provision, install or publish a URL."""
import argparse
import base64
import copy
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat

import environment as runtime
from service_name import service_name

APP = r'[a-z][a-z0-9-]{1,28}[a-z0-9]'
ENVIRONMENT = r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}'


class RegistrationError(ValueError):
    def __init__(self, code, *, unknown=False):
        super().__init__(code)
        self.code, self.unknown = code, unknown


def require(condition, code='APPLICATION_CONFIGURATION_INVALID'):
    if not condition:
        raise RegistrationError(code)


def exact(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def digest(value):
    return hashlib.sha256(runtime.bridge.encoded(value)).hexdigest()


def application_id(environment_id, tenant, app):
    raw = json.dumps([environment_id, tenant, app], separators=(',', ':'), ensure_ascii=False).encode()
    return 'app-' + hashlib.sha256(raw).hexdigest()[:24]


def absolute(value):
    require(isinstance(value, str) and Path(value).is_absolute() and '..' not in Path(value).parts)
    require(Path(value).resolve() == Path(value))
    return value


def private_directory(path):
    absolute(str(path))
    return runtime.bridge.private_directory(path)


def load_config(config_path):
    """Only an administrator-owned file selects endpoints, tools and credential references."""
    config = runtime.read_private(config_path)
    require(exact(config, ('version', 'state_dir', 'registry_file', 'cd', 'environments')) and type(config['version']) is int and config['version'] == 1)
    for key in ('state_dir', 'registry_file'):
        absolute(config[key])
    require(not Path(config['state_dir']).is_relative_to(runtime.ROOT), 'PRIVATE_STATE_OUTSIDE_REPOSITORY_REQUIRED')
    cd = config['cd']
    require(exact(cd, ('version', 'state_dir', 'repository', 'branch', 'context')) and type(cd['version']) is int and cd['version'] == 1)
    for key in ('state_dir', 'repository'):
        absolute(cd[key])
    require(isinstance(cd['branch'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/._-]{0,127}', cd['branch'])
            and '..' not in cd['branch'] and not cd['branch'].endswith('/'))
    require(isinstance(cd['context'], str) and re.fullmatch(ENVIRONMENT, cd['context']))
    require(isinstance(config['environments'], dict) and 0 < len(config['environments']) <= 100)
    for name, profile in config['environments'].items():
        require(isinstance(name, str) and re.fullmatch(ENVIRONMENT, name))
        require(exact(profile, ('provider', 'tenant', 'source_repository', 'pull_secret_file', 'target', 'ingress')))
        require(profile['provider'] in ('aws', 'gcp', 'openstack') and isinstance(profile['tenant'], str)
                and re.fullmatch(r'[a-z0-9]{1,20}', profile['tenant']))
        require(isinstance(profile['source_repository'], str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', profile['source_repository']))
        absolute(profile['pull_secret_file'])
        target = profile['target']
        require(exact(target, ('repo_url', 'architecture', 'ingress_cidrs', 'resources')) and target['architecture'] == 'amd64')
        runtime.argo.https_url(target['repo_url'])
        require(isinstance(target['ingress_cidrs'], list) and 0 < len(target['ingress_cidrs']) <= 32)
        for cidr in target['ingress_cidrs']:
            require(isinstance(cidr, str) and ipaddress.ip_network(cidr).prefixlen > 0)
        resources = target['resources']
        require(exact(resources, ('requests', 'limits')) and all(exact(v, ('cpu', 'memory')) for v in resources.values()))
        for values in resources.values():
            require(all(isinstance(values[key], str) and re.fullmatch(pattern, values[key])
                        for key, pattern in (('cpu', r'[1-9][0-9]*m'), ('memory', r'[1-9][0-9]*Mi'))))
        for key, unit in (('cpu', 'm'), ('memory', 'Mi')):
            require(int(resources['requests'][key].removesuffix(unit)) <= int(resources['limits'][key].removesuffix(unit)))
        # Additional ingress fields are opaque operator metadata for the separate route finalizer.
        require(isinstance(profile['ingress'], dict) and len(runtime.bridge.encoded(profile['ingress'])) <= 16384)
        service_name('validation-app', profile['tenant'], name, profile['ingress'].get('base_domain'))
    return config


def _inputs(config, request):
    require(exact(request, ('environment_id', 'app', 'application_id')), 'APPLICATION_REQUEST_INVALID')
    env_id, app, app_id = (request[key] for key in ('environment_id', 'app', 'application_id'))
    require(isinstance(env_id, str) and re.fullmatch(ENVIRONMENT, env_id)
            and env_id in config['environments'] and isinstance(app, str) and re.fullmatch(APP, app), 'APPLICATION_REQUEST_INVALID')
    profile = config['environments'][env_id]
    require(app_id == application_id(env_id, profile['tenant'], app), 'APPLICATION_ID_MISMATCH')
    registry = runtime.read_private(config['registry_file'])
    require(exact(registry, ('version', 'targets')) and type(registry['version']) is int and registry['version'] == 1
            and isinstance(registry['targets'], dict) and env_id in registry['targets'], 'ENVIRONMENT_NOT_REGISTERED')
    selected = registry['targets'][env_id]
    require(isinstance(selected, dict) and selected.get('purpose') == 'runtime', 'ENVIRONMENT_NOT_REGISTERED')
    native, descriptor = runtime.registered_node(selected, env_id, 'registration.' + app_id)
    require(native['target']['provider'] == descriptor['provider_kind'] == profile['provider']
            and native['target']['architecture'] == 'amd64', 'ENVIRONMENT_IDENTITY_MISMATCH')
    endpoint = descriptor.get('management_endpoint', 'https://' + descriptor['addresses']['private'] + ':6443')
    # registered_node already verifies any override against the real provider/SSH connection.
    runtime.argo.https_url(endpoint)
    # The validated SSM reference joins AWS ARN/raw instance-ID aliases without a cloud lookup.
    resource = native['inventory']['control_plane'][0]['ssh']['transport_ref'] if profile['provider'] == 'aws' else descriptor['resource_id']
    physical = {'provider': profile['provider'], 'resource_id': resource}
    if profile['provider'] == 'openstack':
        physical['project_id'] = selected['project_id']
    authority = {**physical, 'cluster_server': endpoint, 'private_address': descriptor['addresses']['private'],
                 'ssh': native['inventory']['control_plane'][0]['ssh']}
    # Key contents are not persisted or hashed into ownership: a key rotation with the same
    # reviewed reference does not transfer the environment to a different application.
    pull = runtime.read_private(profile['pull_secret_file'])
    require(exact(pull, ('auths',)) and exact(pull['auths'], ('ghcr.io',))
            and exact(pull['auths']['ghcr.io'], ('auth',)), 'PULL_CONFIGURATION_INVALID')
    decoded = base64.b64decode(pull['auths']['ghcr.io']['auth'], validate=True).decode()
    require(':' in decoded and all(decoded.split(':', 1)), 'PULL_CONFIGURATION_INVALID')
    identity = {'environment_id': env_id, 'authority': authority, 'cd': config['cd'], 'profile': profile,
                'pull_sha256': digest(pull), 'app': app, 'application_id': app_id}
    return profile, native, descriptor, endpoint, physical, authority, pull, digest(identity)


def _claim(path, value):
    if path.exists():
        require(runtime.read_private(path) == value, 'ENVIRONMENT_OWNERSHIP_CONFLICT')
    else:
        runtime.save(path, value)


def assert_deployable(home):
    """Shared guard for registration and publication, including direct CLI callers."""
    path = Path(home) / 'lifecycle.json'
    if path.exists():
        require(runtime.read_private(path).get('status') == 'ready', 'APPLICATION_LIFECYCLE_BLOCKED')


def register(config_path, request):
    config = load_config(config_path)
    profile, native, descriptor, endpoint, physical, authority, pull, fingerprint = _inputs(config, request)
    env_id, app, app_id = (request[key] for key in ('environment_id', 'app', 'application_id'))
    root = private_directory(config['state_dir'])
    with os.fdopen(os.open(root / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'APPLICATION_STORAGE_INVALID')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RegistrationError('REGISTRATION_BUSY') from exc
        environments = private_directory(root / 'environments')
        identities = private_directory(root / 'environment-ids')
        environment_claim = {'environment_id': env_id, 'physical_sha256': digest(physical), 'authority_sha256': digest(authority)}
        claims = [environments / (digest(physical) + '.json'), identities / (digest(env_id) + '.json')]
        for path in claims:
            require(not path.exists() or runtime.read_private(path) == environment_claim, 'ENVIRONMENT_OWNERSHIP_CONFLICT')
        for path in claims:
            _claim(path, environment_claim)
        home = private_directory(root / app_id)
        assert_deployable(home)
        receipt = home / 'registration.json'
        if receipt.exists():
            record = runtime.read_private(receipt)
            require(record.get('input_sha256') == fingerprint, 'APPLICATION_BINDING_CHANGED')
            if record.get('status') == 'succeeded':
                require(record.get('binding_sha256') == hashlib.sha256(runtime.read_private(home / 'binding.json', raw=True)).hexdigest(), 'APPLICATION_BINDING_CHANGED')
                if 'cluster_registration' not in record or record.get('runtime_permissions_version') != 2:
                    # Upgrade an already registered app without rebuilding, rebinding or
                    # replaying its original registration/CD side effects.
                    binding = runtime.read_private(home / 'binding.json')
                    registered = binding['registered']; cd = {**config['cd'], 'targets': {app_id: registered}}
                    runtime.shared_cluster_policy(cd, registered, env_id)
                    record.update(status='running', stage='cluster')
                    runtime.save(receipt, record)
                    try:
                        runtime.grant_control_objects(cd, registered, app_id, environment_id=env_id)
                        with runtime.runtime_kubectl(native) as kube:
                            role = next(d for d in runtime.runtime_documents(registered['target'], app_id, pull, None) if d['kind'] == 'Role')
                            runtime.owned_apply(kube, role)
                            control = lambda ns, *args, **kwargs: runtime.argo.kubectl(cd['context'], ns, *args, **kwargs)
                            runtime.owned_apply(control, runtime.application_project(registered, app_id))
                            record['runtime_permissions_version'] = 2
                            record['cluster_registration'] = runtime.share_application_cluster(kube, cd, registered, env_id)
                        record['steps'].append('cluster')
                        record.update(status='succeeded', stage='registered')
                    except Exception:
                        record.update(status='unknown', error={'code': 'APPLICATION_RECONCILE_REQUIRED',
                                      'retryable': False, 'outcome_unknown': True})
                    runtime.save(receipt, record)
                return record
            # A started write or an uncertain outcome requires operator reconciliation.
            record.update(status='unknown', error={'code': 'APPLICATION_RECONCILE_REQUIRED', 'retryable': False, 'outcome_unknown': True})
            runtime.save(receipt, record)
            return record
        mutation_started = False
        try:
            # Resolve existing runtime networking before granting permissions or dispatching CI.
            if profile['provider'] == 'aws' and profile['ingress'].get('edge_config_file'):
                try:
                    runtime.aws_security_group(descriptor, profile['ingress'].get('target_security_group_id'))
                except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                    raise RegistrationError('APPLICATION_AWS_ROUTE_PREFLIGHT_FAILED') from exc
            with runtime.runtime_kubectl(native) as kube:
                services = kube('default', 'get', 'services', '-A', '-o', 'json')
                require(isinstance(services, dict) and isinstance(services.get('items'), list), 'RUNTIME_OBSERVATION_INVALID')
                used = {port['nodePort'] for svc in services['items'] for port in svc.get('spec', {}).get('ports', []) if 'nodePort' in port}
                for prior_path in root.glob('app-*/registration.json'):
                    prior = runtime.read_private(prior_path)
                    lifecycle = prior_path.parent / 'lifecycle.json'
                    if lifecycle.exists() and runtime.read_private(lifecycle).get('status') == 'deleted':
                        continue  # Allocation is released only after verified complete cleanup.
                    if prior.get('environment_id') == env_id:
                        require(type(prior.get('node_port')) is int, 'APPLICATION_STORAGE_INVALID')
                        used.add(prior['node_port'])
                start = int(app_id[4:], 16) % 2768
                port = next((30000 + (start + n) % 2768 for n in range(2768) if 30000 + (start + n) % 2768 not in used), None)
                require(port is not None, 'NODEPORT_CAPACITY_EXCEEDED')
                target = {**copy.deepcopy(profile['target']), 'id': app_id, 'namespace': app_id, 'project': app_id,
                          'argocd_namespace': 'argocd', 'cluster_server': endpoint, 'node_port': port,
                          'path': 'gitops/applications/' + app + '/' + app_id,
                          'image_pull_secret': {'namespace': app_id, 'name': 'ghcr-pull'}}
                registered = {'app': app, 'tenant': profile['tenant'], 'target': target}
                cd = {**config['cd'], 'targets': {app_id: registered}}
                runtime.preflight_renewal(cd, registered, app_id)
                runtime.shared_cluster_policy(cd, registered, env_id)
                binding = {'version': 1, 'environment_id': env_id, 'application_id': app_id, 'provider': profile['provider'],
                           'cd': copy.deepcopy(config['cd']), 'registered': registered, 'ingress': copy.deepcopy(profile['ingress']),
                           'hostname': service_name(app, profile['tenant'], env_id, profile['ingress']['base_domain'])['hostname']}
                record = {'status': 'running', **request, 'target_id': app_id, 'namespace': app_id, 'node_port': port,
                          'hostname': binding['hostname'], 'provider': profile['provider'], 'input_sha256': fingerprint,
                          'environment_sha256': digest(authority), 'target': target, 'stage': 'reserved', 'steps': [],
                          'deployment_supported': False}
                # Read-only ownership check precedes the mutation intent and any permission grants.
                documents = runtime.runtime_documents(target, app_id, pull, None)
                existing = kube(app_id, 'get', 'Namespace', app_id, '--ignore-not-found', '-o', 'json')
                require(not existing or (not existing['metadata'].get('ownerReferences') and all(
                    existing['metadata'].get('labels', {}).get(key) == value for key, value in documents[0]['metadata']['labels'].items())), 'APPLICATION_NAMESPACE_CONFLICT')
                runtime.save(receipt, record)  # Reserve the port and durable intent before the first external write.
                def step(name, action):
                    record['stage'] = name
                    runtime.save(receipt, record)
                    value = action()
                    record['steps'].append(name)
                    runtime.save(receipt, record)
                    return value
                mutation_started = True
                try:
                    step('namespace', lambda: [runtime.owned_apply(kube, document) for document in documents])
                    step('permissions', lambda: runtime.grant_control_objects(cd, registered, app_id, environment_id=env_id))
                    tls = {'tls_server_name': descriptor['addresses']['private']} if (
                        profile['provider'] == 'openstack' and native['inventory']['control_plane'][0]['ssh'].get('connect_host')
                        or profile['provider'] == 'gcp' and descriptor.get('management_endpoint')) else {}
                    renewal, expiry = step('argo', lambda: runtime.register_argo(kube, cd, registered, app_id, app_id, None, **tls))
                    step('credentials', lambda: runtime.install_renewal(cd, renewal))
                    record['cluster_registration'] = step('cluster', lambda: runtime.share_application_cluster(kube, cd, registered, env_id))
                    step('ci', lambda: runtime.bind_ci(profile, registered, app_id))
                    runtime.save(home / 'binding.json', binding)
                    record.update(status='succeeded', stage='registered', runtime_permissions_version=2, credentials={'renewal': 'configured', 'expires_at': expiry},
                                  binding_sha256=hashlib.sha256(runtime.read_private(home / 'binding.json', raw=True)).hexdigest())
                    runtime.save(receipt, record)
                except Exception:
                    record.update(status='unknown', deployment_supported=False,
                                  error={'code': 'APPLICATION_RECONCILE_REQUIRED', 'retryable': False, 'outcome_unknown': True})
                    try:
                        runtime.save(receipt, record)
                    except Exception as storage_error:
                        raise RegistrationError('APPLICATION_RECONCILE_REQUIRED', unknown=True) from storage_error
                return record
        except Exception as exc:
            if mutation_started:
                record.update(status='unknown', deployment_supported=False,
                              error={'code': 'APPLICATION_RECONCILE_REQUIRED', 'retryable': False, 'outcome_unknown': True})
                try:
                    runtime.save(receipt, record)
                except Exception:
                    pass  # The durable started intent still prevents another write attempt.
                raise RegistrationError('APPLICATION_RECONCILE_REQUIRED', unknown=True) from exc
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = register(args.config, runtime.read_private(args.request))
    except Exception as exc:
        unknown = isinstance(exc, RegistrationError) and exc.unknown
        result = {'status': 'unknown' if unknown else 'blocked', 'deployment_supported': False,
                  'error': {'code': exc.code if isinstance(exc, (RegistrationError, runtime.credentials.PolicyCapacityError)) else 'APPLICATION_INPUT_INVALID',
                            'retryable': False, 'outcome_unknown': unknown}}
    print(json.dumps(result))
    return 0 if result['status'] == 'succeeded' else 3


if __name__ == '__main__':
    raise SystemExit(main())
