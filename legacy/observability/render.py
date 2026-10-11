#!/usr/bin/env python3
"""Render one observer VM and one single-node cluster. No network or apply calls."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import ssl
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
NAMESPACE = 'railshot-observability'
KSM_METRICS = [
    'kube_node_status_condition', 'kube_pod_status_phase', 'kube_pod_labels', 'kube_deployment_spec_replicas',
    'kube_deployment_status_replicas_available',
    'kube_pod_container_status_restarts_total',
    'kube_pod_container_status_waiting_reason',
]


def validate(config):
    required = {'name', 'node_ip', 'observer_source_cidr', 'node_metrics_port',
                'cluster_metrics_port', 'probe_urls', 'argocd_metrics'}
    if set(config) != required:
        raise ValueError('Use exactly the fields in target.example.json')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', config['name']):
        raise ValueError('name must be a short lowercase identifier')
    node = ipaddress.IPv4Address(config['node_ip'])
    source = ipaddress.IPv4Network(config['observer_source_cidr'], strict=True)
    if node.is_unspecified or node.is_loopback or node.is_multicast or node.is_link_local:
        raise ValueError('node_ip must be a routed node IPv4 address')
    if source.prefixlen != 32 or any((source.network_address.is_loopback,
                                     source.network_address.is_unspecified,
                                     source.network_address.is_multicast,
                                     source.network_address.is_link_local)):
        raise ValueError('observer_source_cidr must identify the observer egress IPv4 /32')
    ports = [config['node_metrics_port'], config['cluster_metrics_port']]
    if any(type(p) is not int or not 30000 <= p <= 32767 for p in ports) or len(set(ports)) != 2:
        raise ValueError('Use two different NodePorts in 30000..32767')
    urls = config['probe_urls']
    if not isinstance(urls, list) or not 0 <= len(urls) <= 10 or len(set(urls)) != len(urls):
        raise ValueError('Provide 0..10 unique operator-approved HTTP probe URLs')
    for value in urls:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or '$' in value
                or any(c.isspace() for c in value)):
            raise ValueError('Probe URL must be HTTP(S), without credentials, query or fragment')
        _ = parsed.port
    argo = config['argocd_metrics']
    if argo is not None:
        if not isinstance(argo, str) or not re.fullmatch(r'[0-9.]+:[0-9]+', argo):
            raise ValueError('argocd_metrics must be a private/VPN IPv4:port or null')
        host, port = argo.split(':')
        ip = ipaddress.IPv4Address(host)
        if not ip.is_private or ip.is_loopback or ip.is_unspecified or not 1 <= int(port) <= 65535:
            raise ValueError('Argo metrics must use a private/VPN endpoint')
    return config


def prometheus(config):
    jobs = [
        {'job_name': 'node', 'static_configs': [{'targets': [f"{config['node_ip']}:{config['node_metrics_port']}"]}]},
        {'job_name': 'cluster', 'static_configs': [{'targets': [f"{config['node_ip']}:{config['cluster_metrics_port']}"]}]},
        {'job_name': 'http', 'metrics_path': '/probe', 'params': {'module': ['http_2xx']},
         'static_configs': [{'targets': config['probe_urls']}],
         'relabel_configs': [
             {'source_labels': ['__address__'], 'target_label': '__param_target'},
             {'source_labels': ['__param_target'], 'target_label': 'instance'},
             {'target_label': '__address__', 'replacement': 'blackbox:9115'}]},
    ]
    if not config['probe_urls']:
        jobs = [job for job in jobs if job['job_name'] != 'http']
    if config['argocd_metrics']:
        jobs.append({'job_name': 'argocd', 'static_configs': [{'targets': [config['argocd_metrics']]}],
                     'metric_relabel_configs': [{'source_labels': ['__name__'], 'regex': 'argocd_app_info', 'action': 'keep'}]})
    return {'global': {'scrape_interval': '30s', 'scrape_timeout': '10s'}, 'scrape_configs': jobs}


def obj(kind, name, spec=None, api='v1', namespaced=True, **extra):
    value = {'apiVersion': api, 'kind': kind, 'metadata': {'name': name}}
    if namespaced:
        value['metadata']['namespace'] = NAMESPACE
    if spec is not None:
        value['spec'] = spec
    value.update(extra)
    return value


def network_policy(config):
    spec = {'endpointSelector': {}, 'enableDefaultDeny': {'ingress': False, 'egress': False},
            'egressDeny': [{'toEntities': ['host', 'remote-node'], 'toPorts': [{'ports': [
                {'port': str(port), 'protocol': 'TCP'} for port in (9100, config['node_metrics_port'])]}]}]}
    binding = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    spec['labels'] = [{'key': 'railshot.io/observer-policy-sha256', 'value': binding, 'source': 'unspec'}]
    return obj('CiliumClusterwideNetworkPolicy', 'railshot-observer-host-metrics', spec,
               api='cilium.io/v2', namespaced=False)


def cluster(config):
    security = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                'runAsNonRoot': True, 'runAsUser': 65534,
                'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}}
    ksm = {'name': 'metrics', 'image': 'registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.18.0',
           'args': ['--resources=nodes,pods,deployments',
                    '--metric-labels-allowlist=pods=[app.kubernetes.io/name,railshot.io/target]', '--metric-allowlist=' + ','.join(KSM_METRICS)],
           'ports': [{'containerPort': 8080}], 'securityContext': security,
           'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'}, 'limits': {'cpu': '200m', 'memory': '128Mi'}},
           'readinessProbe': {'httpGet': {'path': '/readyz', 'port': 8081}, 'periodSeconds': 10}}
    node = {'name': 'metrics', 'image': 'quay.io/prometheus/node-exporter:v1.12.1',
            'args': ['--web.listen-address=$(HOST_IP):9100',
                     '--path.procfs=/host/proc', '--path.sysfs=/host/sys', '--path.rootfs=/host/root',
                     '--collector.disable-defaults', '--collector.cpu', '--collector.meminfo', '--collector.filesystem', '--collector.netdev',
                     '--collector.filesystem.mount-points-exclude=^/(dev|proc|sys|var/lib/(docker|containerd|kubelet|rancher))($|/)'],
            'env': [{'name': 'HOST_IP', 'valueFrom': {'fieldRef': {'fieldPath': 'status.hostIP'}}}],
            'ports': [{'containerPort': 9100}], 'securityContext': security,
            'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'}, 'limits': {'cpu': '200m', 'memory': '128Mi'}},
            'readinessProbe': {'httpGet': {'path': '/', 'port': 9100}, 'periodSeconds': 10},
            'volumeMounts': [{'name': n, 'mountPath': p, 'readOnly': True, 'mountPropagation': 'HostToContainer'}
                             for n, p in [('proc', '/host/proc'), ('sys', '/host/sys'), ('root', '/host/root')]]}
    items = [network_policy(config), obj('Namespace', NAMESPACE, namespaced=False),
             obj('ServiceAccount', 'cluster-metrics'),
             obj('ClusterRole', 'railshot-observer', api='rbac.authorization.k8s.io/v1', namespaced=False,
                 rules=[{'apiGroups': [''], 'resources': ['nodes', 'pods'], 'verbs': ['list', 'watch']},
                        {'apiGroups': ['apps'], 'resources': ['deployments'], 'verbs': ['list', 'watch']}]),
             obj('ClusterRoleBinding', 'railshot-observer', api='rbac.authorization.k8s.io/v1', namespaced=False,
                 roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'railshot-observer'},
                 subjects=[{'kind': 'ServiceAccount', 'name': 'cluster-metrics', 'namespace': NAMESPACE}])]
    for name, container, kind in [('cluster-metrics', ksm, 'Deployment'), ('node-metrics', node, 'DaemonSet')]:
        labels = {'app': name}
        pod = {'containers': [container], 'nodeSelector': {'kubernetes.io/os': 'linux'}}
        if kind == 'Deployment':
            pod['serviceAccountName'] = 'cluster-metrics'
        else:
            # netdev reads this network namespace; a Pod namespace would report the exporter itself.
            # The operator must restrict native 9100 as well as the NodePort before applying.
            pod.update(hostNetwork=True, dnsPolicy='ClusterFirstWithHostNet')
            pod['automountServiceAccountToken'] = False
            pod['volumes'] = [{'name': n, 'hostPath': {'path': p, 'type': 'Directory'}}
                              for n, p in [('proc', '/proc'), ('sys', '/sys'), ('root', '/')]]
        spec = {'selector': {'matchLabels': labels}, 'template': {'metadata': {'labels': labels}, 'spec': pod}}
        if kind == 'Deployment':
            spec['replicas'] = 1
        items.append(obj(kind, name, spec, api='apps/v1'))
        port = 8080 if kind == 'Deployment' else 9100
        node_port = config['cluster_metrics_port'] if kind == 'Deployment' else config['node_metrics_port']
        items.append(obj('Service', name, {'type': 'NodePort', 'externalTrafficPolicy': 'Local',
                                         'selector': labels, 'ports': [{'port': port, 'targetPort': port, 'nodePort': node_port}]}))
    items.append(obj('NetworkPolicy', 'observer-only', {
        'podSelector': {}, 'policyTypes': ['Ingress'],
        'ingress': [{'from': [{'ipBlock': {'cidr': config['observer_source_cidr']}}],
                     'ports': [{'protocol': 'TCP', 'port': 8080}, {'protocol': 'TCP', 'port': 9100}]}]},
        api='networking.k8s.io/v1'))
    return {'apiVersion': 'v1', 'kind': 'List', 'items': items}


def dashboard(config):
    def from_job(expr, job):
        # Do not leave a previous good sample visible when its collector is down.
        return f'({expr}) and on(job,instance) (up{{job="{job}"}} == 1)'
    panels = []
    definitions = [
        ('Collector reachability', 'up', 'table', 'short'),
        ('Node Ready (1 = ready)', from_job('kube_node_status_condition{condition="Ready",status="true"}', 'cluster'), 'table', 'short'),
        ('Deployment replicas: desired / available', from_job('kube_deployment_spec_replicas or kube_deployment_status_replicas_available', 'cluster'), 'timeseries', 'short'),
        ('Pod waiting reasons', from_job('kube_pod_container_status_waiting_reason == 1', 'cluster'), 'table', 'short'),
        ('Container restarts / 15m', from_job('increase(kube_pod_container_status_restarts_total[15m])', 'cluster'), 'table', 'short'),
        ('Node resources used (%)', from_job('100 * (1 - avg by(job,instance)(rate(node_cpu_seconds_total{mode="idle"}[2m])))', 'node'), 'timeseries', 'percent'),
        ('HTTP check (1 = passed)', from_job('probe_success', 'http'), 'table', 'short'),
        ('HTTP probe duration', from_job('probe_duration_seconds', 'http'), 'timeseries', 's'),
    ]
    if config['argocd_metrics']:
        definitions.append(('Argo application Sync / Health', from_job('argocd_app_info', 'argocd'), 'table', 'short'))
    for index, (title, expr, kind, unit) in enumerate(definitions):
        panels.append({'id': index + 1, 'title': title, 'type': kind,
                       'gridPos': {'x': index % 2 * 12, 'y': index // 2 * 7, 'w': 12, 'h': 7},
                       'datasource': {'type': 'prometheus', 'uid': 'railshot-prometheus'},
                       'targets': [{'refId': 'A', 'expr': expr, 'instant': kind == 'table',
                                    'range': kind != 'table', 'format': 'table' if kind == 'table' else 'time_series'}],
                       'fieldConfig': {'defaults': {'unit': unit, 'noValue': 'Unknown / no data'}, 'overrides': []},
                       'options': {'showHeader': True} if kind == 'table' else {}})
        if title.startswith('Deployment replicas'):
            panels[-1]['targets'] = [
                {'refId': 'A', 'expr': from_job('kube_deployment_spec_replicas', 'cluster'),
                 'legendFormat': '{{namespace}}/{{deployment}} desired', 'range': True},
                {'refId': 'B', 'expr': from_job('kube_deployment_status_replicas_available', 'cluster'),
                 'legendFormat': '{{namespace}}/{{deployment}} available', 'range': True}]
        if title.startswith('Node resources'):
            panels[-1]['targets'][0]['legendFormat'] = 'CPU'
            for ref, legend, expr in [
                ('B', 'Memory', '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'),
                ('C', 'Root disk', '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})')]:
                panels[-1]['targets'].append({'refId': ref, 'expr': from_job(expr, 'node'),
                                              'legendFormat': legend, 'range': True})
    return {'uid': 'railshot-deployment', 'title': f"Railshot deployment - {config['name']}",
            'schemaVersion': 39, 'version': 1, 'editable': False, 'timezone': 'browser',
            'refresh': '30s', 'time': {'from': 'now-30m', 'to': 'now'}, 'panels': panels}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    path.chmod(0o644)


def blackbox(bindings=None):
    http = {'method': 'GET', 'follow_redirects': False, 'preferred_ip_protocol': 'ip4',
            'ip_protocol_fallback': False}
    modules = {'http_2xx': {'prober': 'http', 'timeout': '5s', 'http': http}}
    for target, binding in (bindings or {}).items():
        if (not re.fullmatch(r'[a-z][a-z0-9-]{1,61}[a-z0-9]', target)
                or set(binding) != {'healthz_url', 'server_name', 'ca_pem'}):
            raise ValueError('Runtime health binding must contain only endpoint, server name and public CA')
        url = urlsplit(binding['healthz_url'])
        address = ipaddress.IPv4Address(binding['server_name'])
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.path != '/healthz' or url.query or url.fragment
                or any(c.isspace() for c in binding['healthz_url'])
                or address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local):
            raise ValueError('Runtime health binding requires a verified HTTPS healthz endpoint and node IPv4')
        _ = url.port
        if not re.fullmatch(r'(?:-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*)+', binding['ca_pem']):
            raise ValueError('Runtime health trust must contain only public PEM certificates')
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=binding['ca_pem'])
        modules['runtime_healthz_' + target] = {'prober': 'http', 'timeout': '5s', 'http': {
            **http, 'valid_status_codes': [200], 'tls_config': {
                'ca_file': '/etc/blackbox/runtime-ca/' + target + '.crt',
                'server_name': binding['server_name'], 'insecure_skip_verify': False}}}
    return {'modules': modules}


def render_observer(config, output, scrape_config, bind_ip="127.0.0.1"):
    ipaddress.IPv4Address(bind_ip)
    output = Path(output)
    # Refuse existing paths so rerendering never silently rotates credentials or changes a running stack.
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    compose = (HERE / 'compose.yaml').read_text().replace('127.0.0.1:9090', bind_ip + ':9090')
    (output / 'compose.yaml').write_text(compose)
    write_json(output / 'prometheus.json', scrape_config)
    write_json(output / 'blackbox.json', blackbox())
    (output / 'runtime-ca').mkdir(mode=0o755)
    write_json(output / 'dashboards/deployment.json', dashboard(config))
    write_json(output / 'provisioning/datasources/prometheus.yaml', {
        'apiVersion': 1, 'datasources': [{'name': 'Prometheus', 'uid': 'railshot-prometheus',
            'type': 'prometheus', 'access': 'proxy', 'url': 'http://prometheus:9090',
            'isDefault': True, 'editable': False}]})
    write_json(output / 'provisioning/dashboards/default.yaml', {
        'apiVersion': 1, 'providers': [{'name': 'Railshot', 'type': 'file', 'disableDeletion': True,
            'allowUiUpdates': False, 'options': {'path': '/var/lib/grafana/dashboards'}}]})
    # Compose bind secrets retain host mode; readable by the non-root Grafana UID inside
    # its container. Host access is gated by this 0700 parent directory.
    secret_dir = output / 'secrets'
    secret_dir.mkdir(mode=0o700)
    password = secret_dir / 'grafana_password'
    password.write_text(secrets.token_urlsafe(32) + '\n', encoding='ascii')
    password.chmod(0o444)


def render(config, output):
    validate(config)
    render_observer(config, output, prometheus(config))
    write_json(Path(output) / 'cluster.json', cluster(config))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        render(json.loads(args.target.read_text(encoding='utf-8')), args.output)
    except (ValueError, TypeError, OSError, KeyError) as exc:
        parser.exit(2, f'Render failed: {exc}\n')
    print(f'Rendered configuration in {args.output}. Nothing was installed or deployed.')


if __name__ == '__main__':
    main()
