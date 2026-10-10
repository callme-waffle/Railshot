#!/usr/bin/env python3
"""Register an operator-bound runtime with one existing shared observer."""
import argparse
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(ROOT / 'deployment/scripts'), str(ROOT / 'infrastructure/ansible'),
               str(ROOT / 'gitops'), str(ROOT / 'ci/scripts')]
# Other deployment helpers also have a render.py; bind this renderer by path.
_renderer_spec = importlib.util.spec_from_file_location('railshot_observer_render', ROOT / 'observability/render.py')
_renderer = importlib.util.module_from_spec(_renderer_spec)
_renderer_spec.loader.exec_module(_renderer)
cluster, prometheus, NAMESPACE, validate = _renderer.cluster, _renderer.prometheus, _renderer.NAMESPACE, _renderer.validate
blackbox = _renderer.blackbox
from storage import durable_write


def require(ok):
    if not ok:
        raise ValueError('Observer registration binding or configuration differs')


def settings(config):
    require(set(config) - {'observer_transport', 'observer_source_cidr', 'api_registrar'} == {'version', 'owner', 'lifecycle', 'expires_at', 'state_dir', 'prometheus_url', 'observer_ip',
                           'observer_registry_file', 'observer_target_id', 'observer_directory',
                           'node_metrics_port', 'cluster_metrics_port'} and config['version'] == 1)
    require(config.get('observer_transport', 'registered') in ('registered', 'direct'))
    require(config['lifecycle'] in ('acceptance', 'shared'))
    require(datetime.fromisoformat(config['expires_at'].replace('Z', '+00:00')) > datetime.now(timezone.utc))
    require(isinstance(config['owner'], str) and re.fullmatch(r'[a-z][a-z0-9-]{2,39}', config['owner']))
    require(str(ipaddress.IPv4Address(config['observer_ip'])) == config['observer_ip'])
    require(config['prometheus_url'] == 'http://' + config['observer_ip'] + ':9090')
    source = ipaddress.IPv4Network(config.get('observer_source_cidr', config['observer_ip'] + '/32'), strict=True)
    require(source.prefixlen == 32 and not (source.is_unspecified or source.is_loopback or source.is_multicast or source.is_link_local))
    if 'observer_source_cidr' in config:
        address = source.network_address
        private = any(address in ipaddress.IPv4Network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        require(str(source) == config['observer_source_cidr'] and (private or address.is_global and not address.is_multicast))
    for key in ('state_dir', 'observer_registry_file', 'observer_directory'):
        path = Path(config[key])
        require(path.is_absolute() and '..' not in path.parts and str(path) != '/')
    require(re.fullmatch(r'/[a-zA-Z0-9_./-]+', config['observer_directory']))
    require(type(config['node_metrics_port']) is int and type(config['cluster_metrics_port']) is int)
    require(30000 <= config['node_metrics_port'] <= 32767 and 30000 <= config['cluster_metrics_port'] <= 32767
            and config['node_metrics_port'] != config['cluster_metrics_port'])
    if 'api_registrar' in config:
        api_binding(config['api_registrar'])
    return config


def metrics_host(descriptor, node_ip):
    address = descriptor['addresses'].get('metrics', node_ip)
    if descriptor.get('provider_kind') == 'openstack':
        return address
    if descriptor.get('provider_kind') == 'gcp' and ('metrics' in descriptor['addresses'] or descriptor.get('management_endpoint')):
        public = ipaddress.IPv4Address(descriptor['addresses'].get('public', ''))
        require(public.is_global and not public.is_multicast and address == str(public)
                and descriptor.get('management_endpoint') == f'https://{public}:6443')
        return address
    return node_ip


def registration_row(config, request, descriptor):
    fields = {'version', 'target_id', 'environment_id', 'node_ip', 'registry_file'}
    app_fields = {'app', 'namespace', 'probe_url'} if request.get('app') is not None else set()
    require(set(request) - {'context', 'traffic_port', 'application_id'} == fields | app_fields and request['version'] == 1)
    require(request['target_id'] == descriptor['target_id'] and request['node_ip'] == descriptor['addresses']['private'])
    for key in ('target_id', *(['app', 'namespace'] if app_fields else [])):
        require(isinstance(request[key], str) and re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', request[key]))
    require(isinstance(request['environment_id'], str) and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', request['environment_id']))
    rendered = validate({'name': config['owner'], 'node_ip': request['node_ip'],
        'observer_source_cidr': config.get('observer_source_cidr', config['observer_ip'] + '/32'), 'node_metrics_port': config['node_metrics_port'],
        'cluster_metrics_port': config['cluster_metrics_port'], 'probe_urls': [request['probe_url']] if app_fields else [], 'argocd_metrics': None})
    metrics_address = metrics_host(descriptor, request['node_ip'])
    # Keep runtime ownership intact; product applications carry their own identity.
    application_id = request.get('application_id')
    require(application_id is None or app_fields and isinstance(application_id, str)
            and re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', application_id))
    traffic_port = request.get('traffic_port')
    require(traffic_port is None or app_fields and type(traffic_port) is int and 30000 <= traffic_port <= 32767)
    return {'target_id': request['target_id'], **{key: request[key] for key in app_fields},
            **({'application_id': application_id} if application_id else {}),
            **({'node_ip': request['node_ip']} if metrics_address != request['node_ip'] else {}),
            'prometheus_url': config['prometheus_url'], 'node_instance': f"{metrics_address}:{config['node_metrics_port']}",
            **({'traffic_instance': f'{metrics_address}:{traffic_port}'} if traffic_port else {}),
            'cluster_instance': f"{metrics_address}:{config['cluster_metrics_port']}",
            'environment_id': request['environment_id'], 'resource_id': descriptor['resource_id']}, rendered


def merge_rows(rows, row):
    require(isinstance(rows, list) and len(rows) <= 100)
    target = [item for item in rows if item['target_id'] == row['target_id']]
    physical = ('resource_id', 'prometheus_url', 'node_instance', 'cluster_instance', 'node_ip')
    require(all(all(item.get(key) == row.get(key) for key in physical) for item in target))
    existing = [item for item in target if item.get('app') == row.get('app')]
    if existing:
        require(len(existing) == 1)
        previous = existing[0]
        # Add optional observations without changing the original binding identity.
        require({k: v for k, v in previous.items() if k not in ('healthz_url', 'traffic_instance', 'application_id')} ==
                {k: v for k, v in row.items() if k not in ('healthz_url', 'traffic_instance', 'application_id')})
        require(not (previous.get('healthz_url') and row.get('healthz_url')) or
                previous['healthz_url'] == row['healthz_url'])
        require(not (previous.get('application_id') and row.get('application_id')) or
                previous['application_id'] == row['application_id'])
        require(not (previous.get('traffic_instance') and row.get('traffic_instance')) or
                previous['traffic_instance'] == row['traffic_instance'])
        return [{**previous, **row} if item is previous else item for item in rows]
    require(len(rows) < 100 and not any(item['target_id'] != row['target_id'] and
                                      item['node_instance'] == row['node_instance'] for item in rows))
    return [*rows, row]


def scrape_config(rows):
    require(bool(rows))
    first = rows[0]
    cluster_address = first.get('cluster_instance', first['node_instance'])
    result = prometheus({'node_ip': first['node_instance'].split(':')[0],
        'node_metrics_port': int(first['node_instance'].split(':')[1]),
        'cluster_metrics_port': int(cluster_address.split(':')[1]),
        'probe_urls': sorted({row['probe_url'] for row in rows if row.get('probe_url')}), 'argocd_metrics': None})
    jobs = []
    for job in result['scrape_configs']:
        key = {'node': 'node_instance', 'cluster': 'cluster_instance', 'http': 'probe_url'}[job['job_name']]
        addresses = sorted({row[key] for row in rows if row.get(key)})
        if addresses:
            job['static_configs'] = [{'targets': addresses}]
            jobs.append(job)
    result['scrape_configs'] = jobs
    # Optional operator-owned app binding; no auto-discovery or pipeline changes.
    traffic = []
    for row in rows:
        if row.get('traffic_instance'):
            require(row.get('app') and re.fullmatch(r'[a-zA-Z0-9.-]+:[0-9]{1,5}', row['traffic_instance']))
            traffic.append({'targets': [row['traffic_instance']],
                            'labels': {'app': row['app'], 'target_id': row.get('application_id', row['target_id'])}})
    if traffic:
        jobs.append({'job_name': 'app_traffic', 'metrics_path': '/metrics', 'static_configs': traffic,
                     'metric_relabel_configs': [{'source_labels': ['__name__'],
                         'regex': 'railshot_http_requests_total|railshot_http_request_duration_seconds_(bucket|sum|count)',
                         'action': 'keep'}]})
    health = {row['target_id']: row['healthz_url'] for row in rows if row.get('healthz_url')}
    if health:
        jobs.append({'job_name': 'runtime_healthz', 'metrics_path': '/probe',
            'scrape_interval': '30s', 'scrape_timeout': '10s',
            'static_configs': [{'targets': [url], 'labels': {'module': 'runtime_healthz_' + target}}
                               for target, url in sorted(health.items())],
            'relabel_configs': [
                {'source_labels': ['module'], 'target_label': '__param_module'},
                {'source_labels': ['__address__'], 'target_label': '__param_target'},
                {'source_labels': ['__param_target'], 'target_label': 'instance'},
                {'target_label': '__address__', 'replacement': 'blackbox:9115'}]})
    return result


def node_request(registry_file, target_id, read_private=None, ansible=None):
    # Retain the existing observer bootstrap's four-argument call contract.
    from environment import read_private as read_registered, registered_node
    registry = (read_private or read_registered)(registry_file)
    require(registry.get('version') == 1 and target_id in registry.get('targets', {}))
    return registered_node(registry['targets'][target_id], target_id, 'observation.' + target_id,
                           timeout_seconds=300)


@contextmanager
def observer_ssh(config, request, ansible):
    node = request['inventory']['control_plane'][0]
    require(node['private_ipv4'] == config['observer_ip'])
    direct = config.get('observer_transport', 'registered') == 'direct'
    if direct:
        # Operator opt-in changes only the route to the already registered observer.
        request = copy.deepcopy(request)
        node = request['inventory']['control_plane'][0]
        node['ssh'].pop('transport_ref', None)
        require(node['ssh'].get('connect_host', node['private_ipv4']) == node['private_ipv4'])
    with ansible.forwarded_port(node['ssh'].get('transport_ref'), time.monotonic() + 360) as port:
        host = next(iter(ansible.build_inventory(request, port)['all']['children']['k3s_server']['hosts'].values()))
        if direct:
            require(port is None and host['ansible_host'] == config['observer_ip'])
        prefix = ['ssh', *shlex.split(host['ansible_ssh_common_args']), '-i', host['ansible_ssh_private_key_file'],
                  '-p', str(host['ansible_port']), '-o', 'ConnectTimeout=15',
                  *(['-o', 'HostKeyAlias=' + node['private_ipv4']] if direct else []),
                  host['ansible_user'] + '@' + host['ansible_host']]
        yield prefix


SYNC_RUNTIME = '''import json,pathlib,subprocess,sys,time
p=json.load(sys.stdin); out=pathlib.Path(p['directory'])
assert out.is_dir() and not out.is_symlink() and (out/'owner').read_text().strip()==p['owner']
assert (out/'compose.yaml').read_text() in (p['compose'],p['previous_compose'])
ca=out/'runtime-ca'
assert not ca.is_symlink()
ca.mkdir(mode=0o755,exist_ok=True)
files={'compose.yaml':p['compose'],'prometheus.json':json.dumps(p['prometheus']),
       'blackbox.json':json.dumps(p['blackbox']),**{'runtime-ca/'+k+'.crt':v for k,v in p['certificates'].items()}}
before={}
for name in files:
    path=out/name
    assert not path.is_symlink() and (not path.exists() or path.is_file())
    before[name]=path.read_bytes() if path.exists() else None
existing=json.loads(before['blackbox.json'])
assert set(existing)=={'modules'} and isinstance(existing['modules'],dict)
modules={**p['blackbox']['modules'],**existing['modules']}
modules.update({name:value for name,value in p['blackbox']['modules'].items() if name.startswith('runtime_healthz_')})
files['blackbox.json']=json.dumps({'modules':modules})
def command(*args):
    subprocess.run(['sudo','docker','compose',*args],cwd=out,check=True,
                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=15)
def reload_blackbox():
    command('up','-d','--no-deps','blackbox')
    for attempt in range(5):
        try:
            command('exec','-T','prometheus','wget','-q','-O-','--post-data=',
                    'http://blackbox:9115/-/reload')
            return
        except subprocess.CalledProcessError:
            if attempt==4: raise
            time.sleep(0.5)
try:
    for name,data in files.items():
        path=out/name
        path.write_text(data); path.chmod(0o644)
    command('exec','-T','prometheus','promtool','check','config','/etc/prometheus/prometheus.json')
    command('run','--rm','--no-deps','blackbox','--config.file=/etc/blackbox/blackbox.json','--config.check')
    reload_blackbox()
    command('kill','-s','SIGHUP','prometheus')
    assert all((out/name).read_text()==data for name,data in files.items())
except Exception:
    for name,data in before.items():
        path=out/name
        if data is None: path.unlink(missing_ok=True)
        else: path.write_bytes(data)
    reload_blackbox()
    command('kill','-s','SIGHUP','prometheus')
    raise
print(json.dumps({'synced':True}))
'''


def sync_observer(config, request, document, ansible, native, bindings=None):
    with observer_ssh(config, request, ansible) as prefix:
        if bindings:
            compose = (ROOT / 'observability/compose.yaml').read_text().replace('127.0.0.1:9090', config['observer_ip'] + ':9090')
            previous_compose = compose.replace('    volumes:\n      - ./blackbox.json:/etc/blackbox/blackbox.json:ro\n'
                '      - ./runtime-ca:/etc/blackbox/runtime-ca:ro',
                '    volumes: ["./blackbox.json:/etc/blackbox/blackbox.json:ro"]')
            payload = {'directory': config['observer_directory'], 'owner': config['owner'] + ':' + config['lifecycle'],
                'compose': compose, 'previous_compose': previous_compose, 'prometheus': document,
                'blackbox': blackbox(bindings), 'certificates': {key: value['ca_pem'] for key, value in bindings.items()}}
            require(json.loads(native([*prefix, 'python3 -c ' + shlex.quote(SYNC_RUNTIME)],
                                      document=payload, timeout=300)) == {'synced': True})
            return
        directory = shlex.quote(config['observer_directory'])
        # Bootstrap owns this exact directory and marker. Registration cannot adopt another stack.
        command = f'cd {directory} && test "$(cat owner)" = {shlex.quote(config["owner"] + ":" + config["lifecycle"])} && cat > prometheus.next.json && '
        command += 'sudo docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.json >/dev/null && '
        command += 'cp prometheus.json prometheus.previous.json && cat prometheus.next.json > prometheus.json && '
        command += '(sudo docker compose exec -T prometheus promtool check config /etc/prometheus/prometheus.json >/dev/null || '
        command += '{ cat prometheus.previous.json > prometheus.json; exit 1; }) && sudo docker compose kill -s SIGHUP prometheus >/dev/null'
        native([*prefix, command], document=document)
        require(json.loads(native([*prefix, 'cat ' + directory + '/prometheus.json'])) == document)


def wait_network_policy(kube, pod, document):
    observed = kube('default', 'get', document['kind'], document['metadata']['name'], '-o', 'json')
    require(observed and observed.get('spec') == document['spec'])
    uid = observed['metadata']['uid']
    binding = document['spec']['labels'][0]['value']
    command = ('exec', pod['metadata']['name'], '-c', 'cilium-agent', '--')
    deadline = time.monotonic() + 60
    while True:
        repository = kube('kube-system', *command, 'cilium-dbg', 'policy', 'get', '-o', 'json')
        imported = []
        for rule in json.loads(repository['policy']):
            labels = {(v['source'], v['key']): v['value'] for v in rule.get('Labels', [])}
            if labels.get(('k8s', 'io.cilium.k8s.policy.uid')) == uid:
                imported.append(labels)
        if len(imported) == 1 and imported[0].get(('unspec', 'railshot.io/observer-policy-sha256')) == binding:
            break
        require(time.monotonic() < deadline)
        time.sleep(1)
    revision = repository['revision']
    require(type(revision) is int and revision > 0)
    # Same pinned native policy-import/revision gate as bootstrap-platform.metadata_policy.
    kube('kube-system', *command, 'sh', '-c',
         'cilium-dbg policy wait "$1" --max-wait-time 15 --fail-wait-time 10 >/dev/null && printf null', 'sh', str(revision))


def collector(config):
    return {'id': config['owner'], 'lifecycle': config['lifecycle'], 'expires_at': config['expires_at']}


def collector_route(config):
    return {key: config[key] for key in ('owner', 'lifecycle', 'expires_at', 'prometheus_url', 'observer_ip',
                                         'observer_directory', 'node_metrics_port', 'cluster_metrics_port')}


def local_product(config):
    from environment import read_private
    path = Path(config['state_dir']) / 'product.json'
    bound = os.environ.get('RAILSHOT_OBSERVER_PRODUCT_FILE')
    require(not bound or str(path) == bound)
    product = read_private(path)
    require(product.get('version') == 1 and product.get('collector') == collector(config))
    return product


def commit_row(config, row, before_commit):
    """Both API registrations and operator deltas commit under this one PVC lock."""
    from environment import read_private
    import run as ansible
    from argo import native
    require('api_registrar' not in config)
    state = Path(config['state_dir'])
    bound = os.environ.get('RAILSHOT_OBSERVER_PRODUCT_FILE')
    require(not bound or str(state / 'product.json') == bound)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(state.resolve() == state and state.stat().st_uid == os.geteuid() and not state.stat().st_mode & 0o077)
    lock = os.open(state / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077)
        fcntl.flock(lock, fcntl.LOCK_EX)
        product = local_product(config) if (state / 'product.json').exists() else None
        desired = state / 'desired.json'
        rows = read_private(desired)['targets'] if desired.exists() else product['targets'] if product else []
        # A pending desired file may add rows, but must never erase committed registrations.
        for previous in product['targets'] if product else []:
            rows = merge_rows(rows, previous)
        rows = merge_rows(rows, row)
        # Keep runtime health independent of any application registered on this node.
        node_row = next((dict(item) for item in rows if item['target_id'] == row['target_id'] and not item.get('app')), None)
        if node_row is None:
            node_row = {key: value for key, value in row.items() if key not in ('app', 'namespace', 'probe_url', 'traffic_instance', 'application_id')}
        rows = merge_rows(rows, node_row)
        durable_write(desired, json.dumps({'version': 1, 'targets': rows}).encode())
        binding = before_commit()
        blackbox({row['target_id']: binding})
        require(binding['server_name'] == row.get('node_ip', row['node_instance'].split(':')[0]))
        bindings_path = state / 'runtime-healthz.json'
        bindings = read_private(bindings_path)['targets'] if bindings_path.exists() else {}
        bindings[row['target_id']] = binding
        node_row['healthz_url'] = binding['healthz_url']
        rows = merge_rows(rows, node_row)
        durable_write(bindings_path, json.dumps({'version': 1, 'targets': bindings}).encode())
        durable_write(desired, json.dumps({'version': 1, 'targets': rows}).encode())
        active_bindings = {}
        for item in rows:
            if item.get('healthz_url'):
                bound = bindings.get(item['target_id'])
                require(bound and bound['healthz_url'] == item['healthz_url'])
                active_bindings[item['target_id']] = bound
        blackbox(active_bindings)
        observer, _ = node_request(config['observer_registry_file'], config['observer_target_id'])
        sync_observer(config, observer, scrape_config(rows), ansible, native, bindings=active_bindings)
        product = {'version': 1, 'targets': rows, 'collector': collector(config)}
        durable_write(state / 'product.json', json.dumps(product).encode())
        require(local_product(config) == product)
        return product
    finally:
        os.close(lock)


def api_binding(binding):
    require(isinstance(binding, dict) and set(binding) == {'context', 'deployment_uid', 'pvc_uid', 'config_file'})
    require(isinstance(binding['context'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,200}', binding['context']))
    for key in ('deployment_uid', 'pvc_uid'):
        require(isinstance(binding[key], str) and re.fullmatch(r'[a-f0-9-]{36}', binding[key]))
    path = Path(binding['config_file'])
    require(str(path).startswith('/var/lib/railshot/config/') and '..' not in path.parts and str(path) == binding['config_file'])
    return binding


def api_pod(binding, kube):
    def owned(obj, kind, uid):
        return any(o.get('controller') is True and o.get('kind') == kind and o.get('uid') == uid
                   for o in obj['metadata'].get('ownerReferences', []))
    deployment = kube('get', 'deployment', 'railshot-api', '-o', 'json')
    pvc = kube('get', 'pvc', 'railshot-api', '-o', 'json')
    require(deployment['metadata']['uid'] == binding['deployment_uid'] and pvc['metadata']['uid'] == binding['pvc_uid'])
    containers = deployment['spec']['template']['spec']['containers']
    require(len(containers) == 1 and containers[0]['name'] == 'api')
    image = containers[0]['image']
    require(re.fullmatch(r'ghcr\.io/jasmin-softbank/railshot-api@sha256:[a-f0-9]{64}', image))
    expected = os.environ.get('RAILSHOT_RELEASE_API_IMAGE')
    require(not expected or image == expected)
    owned_sets = {r['metadata']['uid'] for r in kube('get', 'replicasets', '-o', 'json')['items']
                  if owned(r, 'Deployment', binding['deployment_uid'])}
    pods = [p for p in kube('get', 'pods', '-l', 'app=railshot-api', '-o', 'json')['items']
            if not p['metadata'].get('deletionTimestamp')
            and any(owned(p, 'ReplicaSet', uid) for uid in owned_sets)
            and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in p.get('status', {}).get('conditions', []))]
    require(len(pods) == 1)
    pod = pods[0]; spec = pod['spec']; containers = spec['containers']
    require(spec.get('serviceAccountName') == 'railshot-product' and not spec.get('hostNetwork')
            and len(containers) == 1 and containers[0]['name'] == 'api' and containers[0]['image'] == image
            and {'name': 'state', 'persistentVolumeClaim': {'claimName': 'railshot-api'}} in spec['volumes']
            and any(m.get('name') == 'state' and m.get('mountPath') == '/var/lib/railshot'
                    and not m.get('subPath') and not m.get('subPathExpr') and not m.get('readOnly') for m in containers[0]['volumeMounts']))
    return {'name': pod['metadata']['name'], 'uid': pod['metadata']['uid'], 'image': image}


def api_exchange(config, *, request=None, descriptor=None, health_binding=None):
    from argo import kubectl
    binding = api_binding(config['api_registrar'])
    kube = lambda *args: kubectl(binding['context'], 'railshot-system', *args)
    before = api_pod(binding, kube)
    payload = {'version': 1, 'action': 'read' if request is None else 'commit',
               'config_file': binding['config_file'], 'collector': collector_route(config)}
    if request is not None:
        blackbox({request['target_id']: health_binding})
        require(health_binding['server_name'] == request['node_ip'])
        payload.update(request=request, descriptor=descriptor, health_binding=health_binding)
    command = ['kubectl', '--context', binding['context'], '--request-timeout=420s', '-n', 'railshot-system',
               'exec', '-i', before['name'], '-c', 'api', '--', '/opt/railshot-python/bin/python3',
               '/app/observability/register.py', '--api-registrar']
    # Send once: a timeout or Pod replacement is unknown, never an automatic retry.
    result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, timeout=420)
    require(result.returncode == 0 and len(result.stdout) <= 131072)
    response = json.loads(result.stdout)
    require(api_pod(binding, kube) == before and response.get('status') == 'succeeded')
    product = response['product']
    require(product.get('version') == 1 and product.get('collector') == collector(config))
    if request is not None:
        row, _ = registration_row(config, request, descriptor)
        require(merge_rows(product['targets'], row) == product['targets'])
        nodes = [item for item in product['targets'] if item['target_id'] == row['target_id'] and not item.get('app')]
        require(len(nodes) == 1 and nodes[0].get('healthz_url') == health_binding['healthz_url'])
    return product


def product(config, *, require_api=False):
    config = settings(config)
    require(not require_api or 'api_registrar' in config)
    return api_exchange(config) if 'api_registrar' in config else local_product(config)


def api_request(payload):
    from environment import read_private
    require(payload.get('version') == 1 and payload.get('action') in ('read', 'commit'))
    require(set(payload) == {'version', 'action', 'config_file', 'collector'} |
            ({'request', 'descriptor', 'health_binding'} if payload['action'] == 'commit' else set()))
    path = Path(payload['config_file'])
    require(str(path).startswith('/var/lib/railshot/config/') and '..' not in path.parts and str(path) == payload['config_file'])
    config = settings(read_private(path))
    require('api_registrar' not in config and collector_route(config) == payload['collector']
            and os.environ.get('RAILSHOT_OBSERVER_PRODUCT_FILE') == str(Path(config['state_dir']) / 'product.json'))
    if payload['action'] == 'read':
        result = local_product(config)
    else:
        row, _ = registration_row(config, payload['request'], payload['descriptor'])
        binding = payload['health_binding']
        blackbox({row['target_id']: binding})
        require(binding['server_name'] == payload['request']['node_ip'])
        result = commit_row(config, row, lambda: binding)
    return {'status': 'succeeded', 'product': result}


def register(config, request, output):
    from environment import runtime_kubectl
    import run as ansible
    from argo import native
    config = settings(config)
    runtime, descriptor = node_request(request['registry_file'], request['target_id'])
    row, rendering = registration_row(config, request, descriptor)
    receipt = {'status': 'unknown', 'target_id': row['target_id'], 'app': row.get('app'), 'registered': False,
               'collection_state': 'pending', 'steps': []}
    def save():
        durable_write(output, json.dumps(receipt).encode())
    def apply_exporters():
        save()  # Record an uncertain outcome before the first mutation.
        from runtime_health import configure_runtime_healthz
        binding = configure_runtime_healthz(runtime, descriptor, ansible, native)
        blackbox({row['target_id']: binding})
        require(binding['server_name'] == request['node_ip'])
        receipt['steps'].append('runtime_healthz_configured'); save()
        labels = {'app.kubernetes.io/managed-by': 'railshot-observer', 'railshot.io/observer': config['owner']}
        with runtime_kubectl(runtime) as kube:
            cilium = kube('kube-system', 'get', 'pods', '-l', 'k8s-app=cilium', '-o', 'json')['items']
            cilium = [pod for pod in cilium if not pod['metadata'].get('deletionTimestamp')]
            require(len(cilium) == 1 and any(condition.get('type') == 'Ready' and condition.get('status') == 'True'
                                           for condition in cilium[0].get('status', {}).get('conditions', [])))
            services = kube('default', 'get', 'services', '-A', '-o', 'json')['items']
            for service in services:
                if any(port.get('nodePort') in (config['node_metrics_port'], config['cluster_metrics_port']) for port in service['spec']['ports']):
                    require(service['metadata'].get('namespace') == NAMESPACE and
                            all(service['metadata'].get('labels', {}).get(k) == v for k, v in labels.items()))
            for document in cluster(rendering)['items']:
                meta = document['metadata']; meta.setdefault('labels', {}).update(labels)
                namespace = meta.get('namespace', 'default')
                existing = kube(namespace, 'get', document['kind'], meta['name'], '--ignore-not-found', '-o', 'json')
                if existing:
                    require(not existing['metadata'].get('ownerReferences') and all(existing['metadata'].get('labels', {}).get(k) == v for k, v in labels.items()))
                kube(namespace, 'apply', '--server-side', '--field-manager=railshot-observer', '-f', '-', '-o', 'json', document=document)
                if document['kind'] == 'CiliumClusterwideNetworkPolicy':
                    wait_network_policy(kube, cilium[0], document)
        receipt['steps'].append('exporters_applied'); save()
        return binding
    if 'api_registrar' in config:
        # Validate the live canonical binding before touching any exporter.
        merge_rows(product(config, require_api=True)['targets'], row)
        binding = apply_exporters()
        api_exchange(config, request=request, descriptor=descriptor, health_binding=binding)
    else:
        commit_row(config, row, apply_exporters)
    receipt.update(status='succeeded', registered=True)
    receipt['steps'].extend(['collector_registered', 'product_registered']); save()
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-registrar', action='store_true')
    parser.add_argument('--config')
    parser.add_argument('--request')
    parser.add_argument('--out')
    args = parser.parse_args()
    from environment import read_private
    try:
        if args.api_registrar:
            require(not any((args.config, args.request, args.out)))
            raw = sys.stdin.read(131073); require(len(raw) <= 131072)
            result = api_request(json.loads(raw))
        else:
            require(all((args.config, args.request, args.out)))
            result = register(read_private(args.config), read_private(args.request), Path(args.out))
    except Exception as error:
        # Internal paths, SSH diagnostics, descriptors and credentials stay private.
        print(json.dumps({'status': 'unknown', 'registered': False, 'collection_state': 'pending', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
