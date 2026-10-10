#!/usr/bin/env python3
"""Reconcile one registered runtime from an operator-pinned source release.

The caller authenticates the source archive/commit before invoking this program.
This executor binds approved runtime policy, existing ownership, and actual health
to a durable release receipt. It never provisions infrastructure, changes DNS, or retries
an incomplete mutation. Run each target in a separate process.
"""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import environment as env
import runtime_upgrade as upgrade

ROOT = Path(__file__).resolve().parents[2]


def node_only(value):
    return value.get('version') == 2 and value.get('scope') == 'node-only'


def load_node_configuration(registry, target_id, config, binding_file=None):
    """An explicit app-free contract; legacy v1 never falls back to this path."""
    env.argo.require(node_only(config) and set(config) == {'version', 'scope', 'registration', 'management'}
                     and binding_file is None, 'explicit node-only config v2 required')
    env.argo.require(registry.get('version') == 1 and isinstance(registry.get('targets'), dict), 'registry v1 required')
    selected = registry['targets'][target_id]
    env.argo.require(selected['purpose'] == 'runtime', 'registered runtime required')
    request, descriptor = env.registered_node(selected, target_id, 'registration.' + target_id)
    settings = config['registration']
    env.argo.require(set(settings) == {'state_dir', 'observability_config_file'}, 'node-only registration settings differ')
    for value in settings.values():
        path = Path(value)
        env.argo.require(path.is_absolute() and path.resolve() == path and str(path) != '/', 'canonical operator path required')
    env.read_private(settings['observability_config_file'])
    management = {'server': descriptor.get('management_endpoint', 'https://' + descriptor['addresses']['private'] + ':6443')}
    if descriptor.get('management_endpoint'):
        management['tls_server_name'] = descriptor['addresses']['private']
    env.argo.require(config['management'] == management and request['target']['architecture'] == 'amd64',
                     'node management endpoint/TLS binding differs')
    identity = {'version': 2, 'scope': 'node-only', 'descriptor': descriptor, 'settings': settings, 'management': management,
                'ssh': selected['ssh'],
                'ssh_sha256': {key: hashlib.sha256(Path(selected['ssh'][key]).read_bytes()).hexdigest()
                               for key in ('identity_file', 'known_hosts_file')}}
    return request, None, settings, None, None, identity


def node_identity(identity, observed, management):
    env.argo.require(observed['node_ip'] == identity['descriptor']['addresses']['private']
                     and observed['architecture'] == 'amd64' and isinstance(observed['node_uid'], str)
                     and bool(observed['node_uid']), 'registered node identity differs')
    ca = base64.b64decode(management['ca_data'], validate=True)
    env.argo.require(bool(ca) and hashlib.sha256(ca).hexdigest() == management['ca_sha256']
                     and {k: v for k, v in management.items() if k not in ('ca_data', 'ca_sha256')} == identity['management'],
                     'registered management CA/endpoint differs')
    return {**identity, 'node': {key: observed[key] for key in ('node_uid', 'node_ip', 'architecture')},
            'management': management}


def node_management_health(registration, observed):
    management = registration['management']
    ca = base64.b64decode(observed['management_ca_data'], validate=True)
    env.argo.require(observed['node_uid'] == registration['node_uid'] and observed.get('api_ready') is True
                     and observed['node_ip'] == registration['node_ip'] and observed['architecture'] == registration['architecture']
                     and observed.get('cilium_ready') is True
                     and observed['management_ca_data'] == management['ca_data']
                     and hashlib.sha256(ca).hexdigest() == management['ca_sha256'], 'node UID, health or management CA drifted')
    endpoint = urlsplit(management['server'])
    context = ssl.create_default_context(cadata=ca.decode('ascii'))
    client = env.credentials.RegisteredHTTPSConnection(endpoint.hostname, endpoint.port,
        server_name=management.get('tls_server_name', endpoint.hostname), context=context, timeout=15)
    try:
        client.connect()  # No application service account or fabricated API credential.
    finally:
        client.close()
    return {'server': management['server'], 'tls_verified': True, 'api_ready_via_ssh': True,
            'ca_sha256': management['ca_sha256']}


def runtime_readback(prefix, registration):
    observed = node_call(prefix, {'action': 'inspect', **({'include_ca': True} if node_only(registration) else {})})
    if node_only(registration):
        node_management_health(registration, observed)
    return observed


def public_runtime(observed):
    return {key: value for key, value in observed.items() if key != 'management_ca_data'}


@contextmanager
def connection(request):
    node = request['inventory']['control_plane'][0]
    with env.ansible.forwarded_port(node['ssh'].get('transport_ref'), time.monotonic() + 1800) as port:
        host = next(iter(env.ansible.build_inventory(request, port)['all']['children']['k3s_server']['hosts'].values()))
        yield ['ssh', *shlex.split(host['ansible_ssh_common_args']), '-i', host['ansible_ssh_private_key_file'],
               '-p', str(host['ansible_port']), '-o', 'ConnectTimeout=15', host['ansible_user'] + '@' + host['ansible_host']]


def node_call(prefix, payload):
    # Code comes from the authenticated source release, not from operator JSON.
    script = base64.b64encode((ROOT / 'deployment/scripts/runtime_upgrade.py').read_bytes()).decode()
    command = 'import base64;exec(compile(base64.b64decode(' + repr(script) + '),"runtime_upgrade.py","exec"))'
    result = subprocess.run([*prefix, shlex.join(['sudo', '-n', 'python3', '-c', command])],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=1800)
    env.argo.require(result.returncode == 0, 'node operation incomplete; inspect recovery receipt before retrying')
    return json.loads(result.stdout)


def stage_source(prefix, source_sha):
    home = '/var/lib/railshot/runtime-release-sources/' + source_sha
    names = ('airgap/versions.json', 'cilium/preflight.py')
    files = {name: base64.b64encode((ROOT / 'deployment' / name).read_bytes()).decode() for name in names}
    script = '''import base64,json,os,pathlib,sys
os.umask(0o077)
p=json.load(sys.stdin); root=pathlib.Path(p['home'])
root.mkdir(parents=True,exist_ok=True,mode=0o700)
for parent in [root,root.parent]:
    assert parent.resolve()==parent and parent.stat().st_uid==0 and not parent.stat().st_mode&0o077
for name,data in p['files'].items():
    path=root/name; data=base64.b64decode(data,validate=True)
    assert path.resolve()==path
    path.parent.mkdir(mode=0o700,exist_ok=True)
    if path.exists(): assert path.read_bytes()==data
    else:
        with path.open('xb') as stream: stream.write(data)
print(json.dumps({'source':str(root)}))
'''
    observed = json.loads(env.argo.native([*prefix, shlex.join(['sudo', '-n', 'python3', '-c', script])],
                                        document={'home': home, 'files': files}))
    env.argo.require(observed == {'source': home}, 'staged release source differs')
    return home


def load(args):
    release = env.read_private(args.release)
    upgrade.validate(release, json.loads((ROOT / 'deployment/airgap/versions.json').read_text()))
    record = env.read_private(Path(args.registration_dir) / 'registration.json')
    configuration = env.read_private(args.config)
    if node_only(configuration) or node_only(record):
        env.argo.require(node_only(configuration) and node_only(record) and record['status'] == 'succeeded'
                         and record['target_id'] == args.target_id
                         and set(record) == {'version', 'scope', 'status', 'input_sha256', 'target_id', 'environment_id',
                             'provider_kind', 'resource_id', 'steps', 'adoption_plan_sha256', 'node_uid', 'node_ip',
                             'architecture', 'management', 'baseline_policy_sha256', 'observability'},
                         'explicit node-only registration receipt required')
        request, cd, settings, pull, binding, identity = load_node_configuration(
            env.read_private(args.registry), args.target_id, configuration, args.binding)
        identity['environment_id'] = Path(args.registration_dir).name
        identity = node_identity(identity, record, record['management'])
        env.argo.require(record['provider_kind'] == request['target']['provider']
                         and record['resource_id'] == identity['descriptor']['resource_id']
                         and record['environment_id'] == identity['environment_id']
                         and record['input_sha256'] == env.argo.document_hash(identity)
                         and re.fullmatch('[a-f0-9]{64}', record['baseline_policy_sha256']), 'node-only registration binding differs')
        claim = env.read_private(Path(settings['state_dir']) / (args.target_id + '.json'))
        env.argo.require(claim == {'input_sha256': record['input_sha256'], 'resource_id': record['resource_id'],
                                 'environment_id': record['environment_id']}, 'registered node owner differs')
        adoption = env.read_private(Path(args.registration_dir) / 'adoption-receipt.json')
        env.argo.require(adoption.get('status') == 'verified' and adoption.get('scope') == 'node-only'
                         and adoption.get('plan_sha256') == record['adoption_plan_sha256']
                         and adoption.get('target_id') == args.target_id and adoption.get('provider') == record['provider_kind'],
                         'node adoption is incomplete; operator recovery required')
        return release, record, request, cd, settings, pull, binding, identity
    cd_bytes = env.read_private(Path(args.registration_dir) / 'cd.json', raw=True)
    env.argo.require(record['status'] == 'succeeded' and record['target_id'] == args.target_id
                     and record['cd_sha256'] == hashlib.sha256(cd_bytes).hexdigest(), 'existing registration receipt differs')
    cd = json.loads(cd_bytes); registered = cd['targets'][args.target_id]
    # Use the saved allocated URL/NodePort. Updating common policies never reallocates an edge.
    configuration['cd'] = cd
    registered.pop('edge', None)
    configuration['registration'].pop('edge_config_file', None)
    env.argo.require(configuration['registration'].get('observability_config_file'), 'registered observer required for common release')
    request, cd, settings, pull, binding, identity = env.load_configuration(
        env.read_private(args.registry), args.target_id, configuration, args.binding)
    env.argo.require(request['target']['provider'] == record['provider_kind']
                     and record['namespace'] == registered['target']['namespace']
                     and record['cluster_server'] == registered['target']['cluster_server'], 'registered target binding differs')
    # Bind the logical registration to its registry resource via the original claim.
    claim = env.read_private(Path(settings['state_dir']) / (args.target_id + '.json'))
    env.argo.require(claim['input_sha256'] == record['input_sha256']
                     and claim['resource_id'] == identity['descriptor']['resource_id'], 'registered resource owner differs')
    return release, record, request, cd, settings, pull, binding, identity


def app_health(kube, registered, target_id):
    target = registered['target']; namespace = target['namespace']; app = registered['app']
    obj = kube(namespace, 'get', 'deployment', app, '-o', 'json')
    env.argo.require(obj['spec']['template']['metadata']['labels'].get('railshot.io/target') == target_id,
                     'application target label differs')
    status = obj.get('status', {}); count = obj['spec'].get('replicas', 1)
    env.argo.require(count > 0 and status.get('observedGeneration', 0) >= obj['metadata']['generation']
                     and status.get('updatedReplicas', 0) == count and status.get('availableReplicas', 0) == count,
                     'application rollout is not ready')
    return {'uid': obj['metadata']['uid'], 'generation': obj['metadata']['generation'], 'ready_replicas': count,
            'images': [c['image'] for c in obj['spec']['template']['spec']['containers']]}


def management_health(cd, target_id):
    config = env.argo.kubectl(cd['context'], 'argocd', 'get', 'configmap', 'railshot-credentials', '-o', 'json')
    policy = env.credentials.validate_policy(json.loads(config['data']['policy.json']))
    rows = [t for t in policy['targets'] if t['target_id'] == target_id]
    env.argo.require(len(rows) == 1, 'one registered renewal target required')
    row = rows[0]; target = cd['targets'][target_id]['target']
    env.argo.require(row['server'] == target['cluster_server'] and target['namespace'] in row['namespaces'], 'management endpoint binding differs')
    secret = env.argo.kubectl(cd['context'], 'argocd', 'get', 'secret', row['secret'], '-o', 'json')
    credential, ca = env.credentials.registration(secret, row, time.time())
    pods = env.credentials.customer(row['server'], ca, credential['bearerToken'],
        '/api/v1/namespaces/' + target['namespace'] + '/pods?limit=1',
        **({'server_name': row['tls_server_name']} if 'tls_server_name' in row else {}))
    env.argo.require(pods.get('kind') == 'PodList' and isinstance(pods.get('items'), list), 'registered management credential read failed')
    return {'server': row['server'], 'namespace': target['namespace'], 'tls_verified': True, 'read_verified': True}


def observer_health(kube):
    deadline = time.monotonic() + 180
    while True:
        ready = True
        for kind, name, desired, actual in (('deployment', 'cluster-metrics', 'replicas', 'availableReplicas'),
                                            ('daemonset', 'node-metrics', 'desiredNumberScheduled', 'numberReady')):
            obj = kube('railshot-observability', 'get', kind, name, '-o', 'json')
            status = obj.get('status', {})
            count = obj['spec'].get(desired, 1) if kind == 'deployment' else status.get(desired, 0)
            ready &= (count > 0 and status.get('observedGeneration', 0) >= obj['metadata']['generation']
                      and status.get(actual, 0) == count)
        if ready:
            return {'exporters_ready': True}
        env.argo.require(time.monotonic() < deadline, 'observer rollout not ready')
        time.sleep(3)


def collection_query(row):
    # Match the product observer's target/app scopes and 90-second freshness rule.
    def selector(job, instance):
        return '{job=' + json.dumps(job) + ',instance=' + json.dumps(instance) + '}'
    node = selector('node', row['node_instance']); cluster = selector('cluster', row['cluster_instance'])
    app_fields = {'app', 'namespace', 'probe_url'} & set(row)
    env.argo.require(not app_fields or app_fields == {'app', 'namespace', 'probe_url'}, 'partial observer app binding')
    expressions = {**{job + '_up': 'up' + sel for job, sel in (('node', node), ('cluster', cluster))},
        **{job + '_observed': 'timestamp(up' + sel + ')' for job, sel in (('node', node), ('cluster', cluster))},
        'cpu_percent': '100 * (1 - avg(rate(node_cpu_seconds_total' + node[:-1] + ',mode="idle"}[2m])))',
        'cpu_observed': 'min(timestamp(node_cpu_seconds_total' + node + '))',
        'memory_percent': '100 * (1 - node_memory_MemAvailable_bytes' + node + ' / node_memory_MemTotal_bytes' + node + ')',
        'memory_observed': 'min(timestamp(node_memory_MemAvailable_bytes' + node + ') or timestamp(node_memory_MemTotal_bytes' + node + '))'}
    if app_fields:
        http = selector('http', row['probe_url'])
        pod = ('kube_pod_status_phase' + cluster[:-1] + ',namespace=' + json.dumps(row['namespace']) + ',phase="Running"}')
        labels = ('kube_pod_labels' + cluster[:-1] + ',namespace=' + json.dumps(row['namespace'])
                  + ',label_app_kubernetes_io_name=' + json.dumps(row['app'])
                  + ',label_railshot_io_target=' + json.dumps(row['target_id']) + '}')
        expressions.update(http_up='up' + http, http_observed='timestamp(up' + http + ')',
            pods='sum(' + pod + ' and on(namespace,pod,uid) ' + labels + ')',
            pods_observed='min(timestamp(' + pod + ') and on(namespace,pod,uid) ' + labels + ')',
            http='probe_success' + http, probe_observed='timestamp(probe_success' + http + ')')
    return ' or '.join('label_replace((' + expression + '), "railshot_metric", "' + name + '", "", "")'
                       for name, expression in expressions.items())


def collection_values(document, now, *, application=True):
    env.argo.require(document.get('status') == 'success' and document['data']['resultType'] == 'vector', 'observer query failed')
    values = {}
    for item in document['data']['result']:
        name = item['metric']['railshot_metric']; value = float(item['value'][1])
        env.argo.require(name not in values and math.isfinite(value), 'ambiguous or nonfinite observer sample')
        values[name] = value
    clocks = ('node_observed', 'cluster_observed', 'cpu_observed', 'memory_observed')
    required_up = ('node_up', 'cluster_up')
    if application:
        clocks += ('http_observed', 'pods_observed', 'probe_observed')
        required_up += ('http_up', 'http')
    env.argo.require(all(values.get(name) == 1 for name in required_up)
                     and all(name in values and -5 <= now - values[name] <= 90 for name in clocks)
                     and all(name in values and 0 <= values[name] <= 100 for name in ('cpu_percent', 'memory_percent'))
                     and (not application or values.get('pods', 0) >= 1), 'observer samples missing, stale, or unhealthy')
    return {'collection_state': 'ready', 'cpu_percent': values['cpu_percent'], 'memory_percent': values['memory_percent'],
            **({'running_app_pods': values['pods'], 'http_probe_success': True} if application else {}),
            'oldest_observed_at': datetime.fromtimestamp(min(values[name] for name in clocks), timezone.utc).isoformat()}


def collection_health(settings, registration, registered, identity, *, wait_seconds=180):
    spec = importlib.util.spec_from_file_location('release_observer_validation', ROOT / 'observability/register.py')
    observer = importlib.util.module_from_spec(spec); spec.loader.exec_module(observer)
    config = observer.settings(env.read_private(settings['observability_config_file']))
    product = observer.product(config, require_api=True)
    env.argo.require(product.get('version') == 1 and product.get('collector') ==
        {'id': config['owner'], 'lifecycle': config['lifecycle'], 'expires_at': config['expires_at']}, 'observer collector binding differs')
    application = not node_only(registration)
    target_id = registered['target']['id'] if application else registration['target_id']
    rows = [row for row in product['targets'] if row['target_id'] == target_id
            and (row.get('app') == registered['app'] if application else not {'app', 'namespace', 'probe_url'} & set(row))]
    env.argo.require(len(rows) == 1, 'one exactly bound observation required')
    row = rows[0]; descriptor = identity['descriptor']
    address = observer.metrics_host(descriptor, descriptor['addresses']['private'])
    env.argo.require(row['resource_id'] == descriptor['resource_id'] and row['environment_id'] == registration['environment_id']
                     and row['prometheus_url'] == config['prometheus_url']
                     and row['node_instance'] == address + ':' + str(config['node_metrics_port'])
                     and row['cluster_instance'] == address + ':' + str(config['cluster_metrics_port']), 'observer runtime binding differs')
    if application:
        env.argo.require(row['namespace'] == registered['target']['namespace'] and row['probe_url'] == registered['public_http']['url'],
                         'observer application binding differs')
    url = config['prometheus_url'] + '/api/v1/query?' + urlencode({'query': collection_query(row), 'timeout': '4s'})
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            with build_opener(ProxyHandler({}), env.credentials.NoRedirect()).open(Request(url), timeout=5) as response:
                raw = response.read(65537)
                env.argo.require(response.status == 200 and len(raw) <= 65536, 'observer response invalid')
            return collection_values(json.loads(raw), time.time(), application=application)
        except (OSError, ValueError, KeyError, TypeError):
            env.argo.require(time.monotonic() < deadline, 'actual observer collection unverified')
            time.sleep(3)


def health_proof(args, loaded, node_uid):
    release, registration, request, cd, settings, pull, binding, identity = loaded
    fingerprint = upgrade.digest({'release': release, 'identity': identity, 'registration': registration})
    with connection(request) as prefix:
        observed = runtime_readback(prefix, registration)
    env.argo.require(observed['node_uid'] == node_uid
                     and observed['node_ip'] == identity['descriptor']['addresses']['private']
                     and observed['architecture'] == 'amd64' and upgrade.matches(observed, release['to_policy']),
                     'live runtime drifted from the verified to-policy')
    empty = node_only(registration)
    registered = None if empty else cd['targets'][args.target_id]
    application = probe = {'status': 'not_applicable', 'reason': 'node-only'}
    with env.runtime_kubectl(request) as kube:
        if not empty:
            namespace = kube('default', 'get', 'namespace', registered['target']['namespace'], '-o', 'json')
            env.argo.require(namespace['metadata'].get('labels', {}).get('railshot.io/registration') == registration['input_sha256'][:32],
                             'registered namespace ownership drifted')
            application = app_health(kube, registered, args.target_id)
        observer = observer_health(kube)
    observer.update(collection_health(settings, registration, registered, identity, wait_seconds=0))
    management = node_management_health(registration, observed) if empty else management_health(cd, args.target_id)
    if not empty:
        public = registered['public_http']
        probe = env.bridge.public_probe(public, urlsplit(public['url']).path)
        env.argo.require(probe['state'] == 'succeeded', 'public HTTPS health drifted')
    return {'version': 1, 'status': 'verified', 'verify_only': True, 'source_sha': release['source_sha'],
            **({'scope': 'node-only'} if empty else {}),
            'provider': request['target']['provider'], 'target_id': args.target_id,
            'input_sha256': fingerprint, 'to_policy_sha256': release['to_policy_sha256'],
            'after': public_runtime(observed), 'application': application, 'observability': observer,
            'management': management, 'public_http': probe, 'checked_at': datetime.now(timezone.utc).isoformat()}


def verify(args):
    release, registration, request, cd, settings, pull, binding, identity = load(args)
    path = Path(args.state_dir) / 'receipt.json'
    registration_home = Path(args.registration_dir)
    # Reuse the existing target lock read-only; never create state for verification.
    with os.fdopen(os.open(registration_home / 'runtime-release.lock', os.O_RDONLY | os.O_NOFOLLOW), 'r') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        previous = env.read_private(path)
        marker = env.read_private(registration_home / 'runtime-release.json')
        fingerprint = upgrade.digest({'release': release, 'identity': identity, 'registration': registration})
        env.argo.require(previous.get('status') == marker.get('status') == 'verified'
                         and previous.get('source_sha') == marker.get('source_sha') == release['source_sha']
                         and previous.get('target_id') == args.target_id
                         and previous.get('provider') == request['target']['provider']
                         and previous.get('input_sha256') == fingerprint
                         and marker.get('receipt') == str(path), 'verified release/target binding differs')
        return health_proof(args, (release, registration, request, cd, settings, pull, binding, identity), previous['after']['node_uid'])


def check_noop_failure(previous, target_id, provider, node_uid, policy):
    runtime = previous.get('runtime', {})
    env.argo.require(previous.get('version') == 1 and previous.get('status') == 'recovery_required' and previous.get('scope') == 'node-only'
                     and previous.get('stage') in {'observability', 'health'}
                     and previous.get('target_id') == target_id and previous.get('provider') == provider
                     and runtime.get('status') == 'verified' and runtime.get('changed') is False
                     and previous.get('from_policy_sha256') == previous.get('to_policy_sha256') == upgrade.digest(policy)
                     and all(runtime.get(side, {}).get('node_uid') == node_uid
                             and upgrade.matches(runtime[side], policy) for side in ('before', 'after')),
                     'only a bound node-only no-op observation failure can be reconciled')


def reconciled_baseline(marker, registration, identity, target_id, provider):
    """Validate a separate acknowledgement without rewriting the failed release."""
    path = Path(marker['receipt'])
    env.argo.require(node_only(registration) and marker.get('status') == 'reconciled'
                     and marker.get('reconciliation') == str(path.with_name('reconciliation.json')),
                     'reconciled target binding differs')
    failed = env.read_private(path, raw=True)
    previous = json.loads(failed)
    proof = env.read_private(marker['reconciliation'])
    expected = {'version': 1, 'status': 'reconciled', 'scope': 'node-only', 'source_sha': marker['source_sha'],
                'target_id': target_id, 'provider': provider, 'failed_receipt': str(path),
                'failed_receipt_sha256': hashlib.sha256(failed).hexdigest(),
                'input_sha256': previous['input_sha256'], 'identity_sha256': upgrade.digest(identity),
                'registration_sha256': upgrade.digest(registration)}
    env.argo.require(all(proof.get(key) == value for key, value in expected.items())
                     and previous['source_sha'] == marker['source_sha']
                     and proof['to_policy_sha256'] == upgrade.digest(proof['to_policy'])
                     and proof['after']['node_uid'] == registration['node_uid'], 'reconciliation evidence binding differs')
    check_noop_failure(previous, target_id, provider, registration['node_uid'], proof['to_policy'])
    return proof


def reconcile(args):
    loaded = load(args)
    release, registration, request, cd, settings, pull, binding, identity = loaded
    env.argo.require(node_only(registration) and release['from_policy'] == release['to_policy'],
                     'only an unchanged node-only runtime can be reconciled')
    path = Path(args.state_dir) / 'receipt.json'
    acknowledgement = path.with_name('reconciliation.json')
    target_marker = Path(args.registration_dir) / 'runtime-release.json'
    fingerprint = upgrade.digest({'release': release, 'identity': identity, 'registration': registration})
    # Existing lock only: reconciliation never creates or repairs registration state.
    with os.fdopen(os.open(target_marker.with_name('runtime-release.lock'), os.O_RDWR | os.O_NOFOLLOW), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        failed = env.read_private(path, raw=True); previous = json.loads(failed)
        marker_bytes = env.read_private(target_marker, raw=True); marker = json.loads(marker_bytes)
        expected_marker = {'status': 'running', 'source_sha': release['source_sha'], 'receipt': str(path)}
        recovered_marker = {**expected_marker, 'status': 'reconciled', 'reconciliation': str(acknowledgement)}
        env.argo.require(marker in (expected_marker, recovered_marker)
                         and previous.get('source_sha') == release['source_sha']
                         and previous.get('input_sha256') == fingerprint, 'failed release/target binding differs')
        check_noop_failure(previous, args.target_id, request['target']['provider'], registration['node_uid'], release['to_policy'])
        saved = (reconciled_baseline(recovered_marker, registration, identity, args.target_id, request['target']['provider'])
                 if acknowledgement.exists() else None)
        env.argo.require(marker != recovered_marker or saved is not None, 'reconciliation acknowledgement missing')
        current = health_proof(args, loaded, registration['node_uid'])
        fresh = load(args)
        env.argo.require(upgrade.digest({'release': fresh[0], 'identity': fresh[7], 'registration': fresh[1]}) == fingerprint
                         and env.read_private(target_marker, raw=True) == marker_bytes
                         and env.read_private(path, raw=True) == failed, 'reconciliation inputs changed during readback')
        proof = {**current, 'status': 'reconciled', 'failed_receipt': str(path),
                 'failed_receipt_sha256': hashlib.sha256(failed).hexdigest(),
                 'identity_sha256': upgrade.digest(identity), 'registration_sha256': upgrade.digest(registration),
                 'to_policy': release['to_policy']}
        proof.pop('verify_only')
        if saved is None:
            env.save(acknowledgement, proof)
        if marker != recovered_marker:
            env.save(target_marker, recovered_marker)
        return proof  # Repeated calls recheck live health without rewriting any evidence.


def execute(args):
    release, registration, request, cd, settings, pull, binding, identity = load(args)
    empty = node_only(registration)
    home = env.bridge.private_directory(args.state_dir)
    registration_home = env.bridge.private_directory(args.registration_dir)
    target_marker = registration_home / 'runtime-release.json'
    path = home / 'receipt.json'
    fingerprint = upgrade.digest({'release': release, 'identity': identity, 'registration': registration})
    with os.fdopen(os.open(registration_home / 'runtime-release.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            previous = env.read_private(path)
            env.argo.require(previous.get('input_sha256') == fingerprint, 'release receipt input changed')
            env.argo.require(previous.get('status') == 'verified', 'incomplete release requires operator reconciliation; automatic retry forbidden')
            if target_marker.exists():
                marker = env.read_private(target_marker)
                env.argo.require(marker.get('source_sha') == release['source_sha'] and marker.get('receipt') == str(path),
                                 'a different release owns the target; stale success cannot be replayed')
            env.save(target_marker, {'status': 'verified', 'source_sha': release['source_sha'], 'receipt': str(path)})
            return {**previous, 'replayed': True}
        baseline = None
        if target_marker.exists():
            marker = env.read_private(target_marker)
            if marker.get('status') == 'reconciled':
                baseline = reconciled_baseline(marker, registration, identity, args.target_id, request['target']['provider'])
                env.argo.require(marker['source_sha'] != release['source_sha'], 'reconciled failure cannot be replayed')
            else:
                env.argo.require(marker['status'] == 'verified', 'previous target release requires operator reconciliation')
        receipt = {'version': 1, 'source_sha': release['source_sha'], 'target_id': args.target_id,
                   **({'scope': 'node-only'} if empty else {}),
                   'provider': request['target']['provider'], 'input_sha256': fingerprint, 'status': 'checking',
                   'steps': [], 'mutation_started': False, 'from_policy_sha256': release['from_policy_sha256'],
                   'to_policy_sha256': release['to_policy_sha256'], 'automatic_rollback': False}
        if upgrade.validate(release):
            receipt['recovery_directory'] = '/var/lib/railshot/runtime-updates/' + release['source_sha']
        def stage(name, *, mutation=False):
            receipt['stage'] = name
            receipt['mutation_started'] |= mutation
            if mutation:
                env.save(target_marker, {'status': 'running', 'source_sha': release['source_sha'], 'receipt': str(path)})
            env.save(path, receipt)
        def done(name):
            receipt['steps'].append(name); env.save(path, receipt)
        try:
            stage('observer_preflight')
            spec = importlib.util.spec_from_file_location('runtime_release_observer', ROOT / 'observability/register.py')
            observer = importlib.util.module_from_spec(spec); spec.loader.exec_module(observer)
            observer_config = env.read_private(settings['observability_config_file'])
            observer.product(observer_config, require_api=True)
            done('observer_preflight')
            stage('runtime_readback')
            if empty:
                previous_policy = registration['baseline_policy_sha256']
                if baseline is not None:
                    previous_policy = baseline['to_policy_sha256']
                elif target_marker.exists():
                    marker = env.read_private(target_marker)
                    previous = env.read_private(marker['receipt'])
                    env.argo.require(previous['status'] == 'verified' and previous['target_id'] == args.target_id
                                     and previous['source_sha'] == marker['source_sha']
                                     and previous['provider'] == request['target']['provider']
                                     and previous['scope'] == 'node-only'
                                     and previous['after']['node_uid'] == registration['node_uid'], 'previous runtime binding differs')
                    previous_policy = previous['to_policy_sha256']
                env.argo.require(previous_policy == release['from_policy_sha256'], 'release from-policy differs from approved node baseline')
            with connection(request) as prefix:
                observed = runtime_readback(prefix, registration)
                expected = {'node_uid': observed['node_uid'], 'node_ip': identity['descriptor']['addresses']['private'],
                            'target_id': args.target_id, 'resource_id': identity['descriptor']['resource_id']}
                env.argo.require(observed['node_ip'] == expected['node_ip'] and observed['architecture'] == 'amd64'
                                 and upgrade.matches(observed, release['from_policy']), 'live runtime differs from registered from-policy')
                receipt['before'] = public_runtime(observed); done('runtime_readback')
                stage('runtime_apply', mutation=True)
                source = stage_source(prefix, release['source_sha'])
                receipt['runtime'] = node_call(prefix, {'action': 'apply', 'release': release, 'expected': expected, 'source': source})
                env.argo.require(receipt['runtime']['status'] == 'verified', 'runtime apply was not verified')
                done('runtime_apply')
            registered = None if empty else cd['targets'][args.target_id]
            if not empty:
                stage('common_policy', mutation=True)
                with env.runtime_kubectl(request) as kube:
                    namespace = kube('default', 'get', 'namespace', registered['target']['namespace'], '-o', 'json')
                    env.argo.require(namespace['metadata'].get('labels', {}).get('railshot.io/registration') == registration['input_sha256'][:32],
                                     'registered namespace owner missing; refusing adoption')
                    for document in env.runtime_documents(registered['target'], registration['input_sha256'][:32], pull, binding):
                        env.owned_apply(kube, document)
                done('common_policy')
            stage('observability', mutation=True)
            env.argo.require(settings.get('observability_config_file'), 'registered observer required for common release')
            observation = {'version': 1, 'target_id': args.target_id, 'environment_id': registration['environment_id'],
                           'node_ip': expected['node_ip'], 'registry_file': str(args.registry)}
            if not empty:
                observation.update(app=registered['app'], namespace=registered['target']['namespace'],
                                   probe_url=registered['public_http']['url'], context=cd['context'])
            result = observer.register(observer_config, observation, home / 'observability.json')
            env.argo.require(result.get('registered') is True and result.get('status') == 'succeeded', 'observer reconciliation unverified')
            done('observability')
            stage('health')
            with env.runtime_kubectl(request) as kube:
                receipt['application'] = ({'status': 'not_applicable', 'reason': 'node-only'} if empty
                                          else app_health(kube, registered, args.target_id))
                receipt['observability'] = observer_health(kube)
            receipt['observability'].update(collection_health(settings, registration, registered, identity))
            if empty:
                receipt['public_http'] = {'status': 'not_applicable', 'reason': 'node-only'}
            else:
                receipt['management'] = management_health(cd, args.target_id)
                public = registered['public_http']
                receipt['public_http'] = env.bridge.public_probe(public, urlsplit(public['url']).path)
                env.argo.require(receipt['public_http']['state'] == 'succeeded', 'public HTTPS health unverified')
            with connection(request) as prefix:
                after = runtime_readback(prefix, registration)
            if empty:
                receipt['management'] = node_management_health(registration, after)
            env.argo.require(after['node_uid'] == expected['node_uid'] and upgrade.matches(after, release['to_policy']), 'final runtime readback differs')
            receipt.update(status='verified', after=public_runtime(after), verified_at=datetime.now(timezone.utc).isoformat())
            done('health')
            env.save(target_marker, {'status': 'verified', 'source_sha': release['source_sha'], 'receipt': str(path)})
        except Exception as error:
            receipt.update(status='recovery_required' if receipt['mutation_started'] else 'blocked',
                           error_type=type(error).__name__)
            env.save(path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('registry', 'target-id', 'config', 'registration-dir', 'state-dir', 'release'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--binding')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify-only', action='store_true', help='read current health without applying or writing release state')
    mode.add_argument('--reconcile', action='store_true', help='acknowledge a no-op node observation failure after fresh health readback')
    args = parser.parse_args(); os.umask(0o077)
    try:
        receipt = reconcile(args) if args.reconcile else verify(args) if args.verify_only else execute(args)
    except Exception as error:
        receipt = {'status': 'blocked', 'error_type': type(error).__name__}
    print(json.dumps(receipt))
    return 0 if receipt['status'] in {'verified', 'reconciled'} else 1


if __name__ == '__main__':
    sys.exit(main())
