#!/usr/bin/env python3
"""Add one owned ingress to an existing, operator-bound Named Tunnel deployment."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
_paths = sys.path[:]
try:
    sys.path.insert(0, str(ROOT / 'deployment/scripts'))
    import environment as runtime
    from service_name import service_name
finally:
    sys.path[:] = _paths

# Several deployment helpers are named render.py; bind this one by its path.
_spec = importlib.util.spec_from_file_location('railshot_tunnel_render', Path(__file__).with_name('render.py'))
renderer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(renderer)
OWNERS = 'railshot.io/application-owners'
HASH = 'railshot.io/config-sha256'
CONFIG_FIELDS = {'version', 'state_dir', 'registry_file', 'environment_id', 'resource_id',
                 'runtime_private_address', 'base_domain', 'namespace', 'name', 'tunnel_id',
                 'credentials_secret', 'origin_vip', 'ca_configmap', 'configmap_uid', 'deployment_uid'}
RENDER_FIELDS = ('namespace', 'name', 'tunnel_id', 'credentials_secret', 'origin_vip', 'ca_configmap')


class RegistrationError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise RegistrationError(code)


def rendered(config, hosts):
    items = renderer.render(**{key: config[key] for key in RENDER_FIELDS}, hostnames=hosts)['items']
    return items[1], items[2]


def inputs(config_path, request):
    config = runtime.read_private(config_path)
    require(isinstance(config, dict) and set(config) == CONFIG_FIELDS
            and type(config['version']) is int and config['version'] == 1, 'TUNNEL_CONFIGURATION_INVALID')
    for key in ('state_dir', 'registry_file'):
        value = config[key]
        require(isinstance(value, str) and Path(value).is_absolute() and Path(value).resolve() == Path(value)
                and '..' not in Path(value).parts, 'TUNNEL_CONFIGURATION_INVALID')
    require(isinstance(request, dict) and set(request) == {'environment_id', 'application_id', 'app', 'tenant', 'hostname'}
            and all(isinstance(value, str) for value in request.values()), 'TUNNEL_REQUEST_INVALID')
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', request['environment_id'])
            and re.fullmatch(r'[a-z][a-z0-9-]{1,28}[a-z0-9]', request['app'])
            and re.fullmatch(r'[a-z0-9]{1,20}', request['tenant'])
            and request['environment_id'] == config['environment_id'], 'TUNNEL_REQUEST_INVALID')
    identity = json.dumps([request['environment_id'], request['tenant'], request['app']],
                          ensure_ascii=False, separators=(',', ':')).encode()
    require(request['application_id'] == 'app-' + hashlib.sha256(identity).hexdigest()[:24], 'TUNNEL_APPLICATION_MISMATCH')
    require(request['hostname'] == service_name(request['app'], request['tenant'], request['environment_id'],
                                                config['base_domain'])['hostname'], 'TUNNEL_HOSTNAME_MISMATCH')
    for key in ('resource_id', 'runtime_private_address', 'configmap_uid', 'deployment_uid'):
        require(isinstance(config[key], str) and 0 < len(config[key]) <= 256, 'TUNNEL_CONFIGURATION_INVALID')
    rendered(config, [request['hostname']])  # Reuse all renderer reference/TLS input checks before connecting.
    registry = runtime.read_private(config['registry_file'])
    require(isinstance(registry, dict) and type(registry.get('version')) is int and registry['version'] == 1
            and isinstance(registry.get('targets'), dict), 'TUNNEL_REGISTRY_INVALID')
    selected = registry['targets'][config['environment_id']]
    require(selected.get('purpose') == 'runtime' and 'server_file' in selected, 'TUNNEL_RUNTIME_MISMATCH')
    native, descriptor = runtime.registered_node(selected, config['environment_id'], 'tunnel.' + request['application_id'])
    require(native['target']['provider'] == descriptor['provider_kind'] == 'openstack'
            and descriptor['resource_id'] == config['resource_id']
            and descriptor['addresses']['private'] == config['runtime_private_address'], 'TUNNEL_RUNTIME_MISMATCH')
    return config, native


def matches(expected, observed):
    """Allow API-defaulted object fields, while retaining exact list membership/order."""
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(key in observed and matches(value, observed[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(observed, list) and len(expected) == len(observed) and all(matches(a, b) for a, b in zip(expected, observed))
    return type(expected) is type(observed) and expected == observed


def bound_object(value, expected, uid):
    meta = value['metadata']
    require(value['kind'] == expected['kind'] and value['apiVersion'] == expected['apiVersion']
            and matches(expected['metadata'], meta) and meta['uid'] == uid
            and isinstance(meta['resourceVersion'], str) and bool(meta['resourceVersion'])
            and not meta.get('ownerReferences') and not meta.get('deletionTimestamp'), 'TUNNEL_RESOURCE_BINDING_MISMATCH')


def inspect(config, cm, deployment):
    owners = json.loads(cm['metadata']['annotations'][OWNERS], object_pairs_hook=runtime.ansible.unique_pairs)
    require(isinstance(owners, dict) and all(isinstance(owner, str) and re.fullmatch(r'app-[a-f0-9]{24}', owner)
                                                      for owner in owners.values()), 'TUNNEL_OWNERS_INVALID')
    expected_cm, expected_deployment = rendered(config, list(owners))
    bound_object(cm, expected_cm, config['configmap_uid'])
    bound_object(deployment, expected_deployment, config['deployment_uid'])
    require(set(cm['data']) == {'config.json'} and not cm.get('binaryData')
            and json.loads(cm['data']['config.json'], object_pairs_hook=runtime.ansible.unique_pairs)
            == json.loads(expected_cm['data']['config.json']), 'TUNNEL_INGRESS_DRIFT')
    # The ConfigMap is the authority. A prior CM patch may have succeeded before the
    # template-hash patch failed; every other rendered Deployment field must match.
    del expected_deployment['spec']['template']['metadata']['annotations'][HASH]
    pod = deployment['spec']['template']['spec']
    expected_pod = expected_deployment['spec']['template']['spec']
    # These PodSpec bool fields have false defaults and JSON omitempty. Other
    # security settings (including pointer bools) must remain explicitly present.
    for key in ('hostNetwork', 'hostPID', 'hostIPC'):
        if key not in pod and expected_pod[key] is False:
            del expected_pod[key]
    require(matches(expected_deployment['spec'], deployment['spec']) and not pod.get('initContainers')
            and not any(container.get('env') or container.get('envFrom') for container in pod['containers'])
            and re.fullmatch(r'[a-f0-9]{64}', deployment['spec']['template']['metadata']['annotations'].get(HASH, '')),
            'TUNNEL_DEPLOYMENT_DRIFT')
    return owners


def patch(kube, namespace, value, replacements):
    meta = value['metadata']
    operations = [{'op': 'test', 'path': '/metadata/' + key, 'value': meta[key]} for key in ('uid', 'resourceVersion')]
    operations += [{'op': 'replace', 'path': path, 'value': new} for path, new in replacements]
    return kube(namespace, 'patch', value['kind'], meta['name'], '--type=json', '--patch-file=/dev/stdin',
                '-o', 'json', document=operations)


def rollout(kube, config, cm, expected_hash):
    deadline = time.monotonic() + 120
    # runtime_kubectl's native command is bounded at 30s. Reserve that budget for
    # each read, rather than starting a final read that can exceed this deadline.
    while time.monotonic() + 30 <= deadline:
        deployment = kube(config['namespace'], 'get', 'deployment', config['name'], '-o', 'json')
        inspect(config, cm, deployment)
        require(deployment['spec']['template']['metadata']['annotations'][HASH] == expected_hash, 'TUNNEL_DEPLOYMENT_DRIFT')
        status = deployment.get('status', {})
        if (time.monotonic() <= deadline and status.get('observedGeneration', 0) >= deployment['metadata']['generation']
                and all(status.get(key) == 1 for key in ('replicas', 'updatedReplicas', 'readyReplicas', 'availableReplicas'))):
            return
        time.sleep(2)
    raise RegistrationError('TUNNEL_ROLLOUT_UNVERIFIED')


def failure(error, changed=False):
    return {'status': 'unknown' if changed else 'blocked', 'https_verified': False,
            'error': {'code': str(error) if isinstance(error, RegistrationError) else 'TUNNEL_OPERATION_UNVERIFIED',
                      'outcome_unknown': changed}}


def ensure(private_config_path, request):
    changed = False
    try:
        config, native = inputs(private_config_path, request)
        root = runtime.bridge.private_directory(config['state_dir'])
        with os.fdopen(os.open(root / 'ingress.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
            info = os.fstat(lock.fileno())
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid(),
                    'TUNNEL_LOCK_INVALID')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RegistrationError('TUNNEL_BUSY') from None
            for path in root.glob('application-lifecycle-*/intent.json'):
                require(runtime.read_private(path).get('phase') in ('planned', 'succeeded'), 'TUNNEL_LIFECYCLE_RECONCILE_REQUIRED')
            with runtime.runtime_kubectl(native) as kube:
                namespace, name = config['namespace'], config['name']
                cm = kube(namespace, 'get', 'configmap', name + '-config', '-o', 'json')
                deployment = kube(namespace, 'get', 'deployment', name, '-o', 'json')
                owners = inspect(config, cm, deployment)
                require(owners.get(request['hostname'], request['application_id']) == request['application_id'], 'TUNNEL_HOSTNAME_OWNED')
                owners[request['hostname']] = request['application_id']
                desired_cm, desired_deployment = rendered(config, list(owners))
                desired_hash = desired_deployment['spec']['template']['metadata']['annotations'][HASH]
                if (cm['data'] != desired_cm['data'] or
                        json.loads(cm['metadata']['annotations'][OWNERS]) != owners):
                    changed = True  # A failed response can follow a successful remote patch.
                    patch(kube, namespace, cm, [('/data/config.json', desired_cm['data']['config.json']),
                          ('/metadata/annotations/railshot.io~1application-owners', runtime.bridge.encoded(owners).decode())])
                if deployment['spec']['template']['metadata']['annotations'][HASH] != desired_hash:
                    changed = True
                    patch(kube, namespace, deployment, [('/spec/template/metadata/annotations/railshot.io~1config-sha256', desired_hash)])
                cm = kube(namespace, 'get', 'configmap', name + '-config', '-o', 'json')
                require(cm['data'] == desired_cm['data'] and
                        json.loads(cm['metadata']['annotations'][OWNERS], object_pairs_hook=runtime.ansible.unique_pairs) == owners,
                        'TUNNEL_READBACK_MISMATCH')
                rollout(kube, config, cm, desired_hash)
            result = {'status': 'succeeded', 'phase': 'tunnel_configured', 'https_verified': False,
                      'hostname': request['hostname'], 'tunnel_id': config['tunnel_id'],
                      'dns': {'type': 'CNAME', 'content': config['tunnel_id'] + '.cfargotunnel.com', 'proxied': True}}
            runtime.save(root / (request['application_id'] + '.json'), result)
            return result
    except Exception as error:
        return failure(error, changed)


@contextmanager
def lifecycle_session(config_path, request):
    config, native = inputs(config_path, request)
    root = runtime.bridge.private_directory(config['state_dir'])
    with os.fdopen(os.open(root / 'ingress.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        info = os.fstat(lock.fileno())
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid(), 'TUNNEL_LOCK_INVALID')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RegistrationError('TUNNEL_BUSY') from None
        with runtime.runtime_kubectl(native) as kube:
            yield config, kube


def lifecycle_snapshot(config, kube, request):
    cm = kube(config['namespace'], 'get', 'configmap', config['name'] + '-config', '-o', 'json')
    deployment = kube(config['namespace'], 'get', 'deployment', config['name'], '-o', 'json')
    owners = inspect(config, cm, deployment)
    require(owners.get(request['hostname'], request['application_id']) == request['application_id'], 'TUNNEL_HOSTNAME_OWNED')
    expected_hash = rendered(config, list(owners))[1]['spec']['template']['metadata']['annotations'][HASH]
    require(deployment['spec']['template']['metadata']['annotations'][HASH] == expected_hash, 'TUNNEL_ROLLOUT_UNVERIFIED')
    # UID/resourceVersion are CAS preconditions; runtime status is not ownership.
    cm.pop('status', None); deployment.pop('status', None)
    return {'config': config, 'configmap': cm, 'deployment': deployment, 'owners': owners}


def lifecycle_plan(config_path, request, action):
    require(action in ('start', 'stop', 'delete'), 'TUNNEL_ACTION_INVALID')
    with lifecycle_session(config_path, request) as (config, kube):
        return lifecycle_snapshot(config, kube, request)


def lifecycle_validate(config_path, request, action, expected):
    require(action in ('start', 'stop', 'delete'), 'TUNNEL_ACTION_INVALID')
    with lifecycle_session(config_path, request) as (config, kube):
        require(lifecycle_snapshot(config, kube, request) == expected, 'TUNNEL_LIFECYCLE_PLAN_STALE')


def lifecycle_execute(config_path, request, action, expected):
    """The caller's durable lifecycle intent must precede this exact CAS update."""
    require(action in ('start', 'stop', 'delete'), 'TUNNEL_ACTION_INVALID')
    with lifecycle_session(config_path, request) as (config, kube):
        require(lifecycle_snapshot(config, kube, request) == expected, 'TUNNEL_LIFECYCLE_PLAN_STALE')
        owners = dict(expected['owners'])
        if action == 'start':
            owners[request['hostname']] = request['application_id']
        else:
            owners.pop(request['hostname'], None)
        cm, deployment = expected['configmap'], expected['deployment']
        desired_cm, desired_deployment = rendered(config, list(owners))
        desired_hash = desired_deployment['spec']['template']['metadata']['annotations'][HASH]
        if owners != expected['owners']:
            patch(kube, config['namespace'], cm, [('/data/config.json', desired_cm['data']['config.json']),
                  ('/metadata/annotations/railshot.io~1application-owners', runtime.bridge.encoded(owners).decode())])
            patch(kube, config['namespace'], deployment, [('/spec/template/metadata/annotations/railshot.io~1config-sha256', desired_hash)])
        cm = kube(config['namespace'], 'get', 'configmap', config['name'] + '-config', '-o', 'json')
        require(cm['data'] == desired_cm['data'] and json.loads(cm['metadata']['annotations'][OWNERS]) == owners,
                'TUNNEL_LIFECYCLE_READBACK_MISMATCH')
        rollout(kube, config, cm, desired_hash)
        return {'status': 'succeeded', 'https_verified': False, 'hostname': request['hostname'],
                'configmap_uid': cm['metadata']['uid'], 'tunnel_id': config['tunnel_id'],
                'route_present': request['hostname'] in owners}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True, help='private JSON request file')
    args = parser.parse_args()
    try:
        result = ensure(args.config, runtime.read_private(args.request))
    except Exception as error:
        result = failure(error)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'succeeded' else 1


if __name__ == '__main__':
    sys.exit(main())
