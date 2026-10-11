"""Render structured objects; never interpolate JSON into shell or YAML."""
import json
from pathlib import Path

NAME = 'railshot-workload'
ROOT = Path(__file__).resolve().parents[1]


def labels(spec):
    return {'app.kubernetes.io/managed-by': 'railshot-runtime', 'railshot.io/environment': spec.environment_id}


def render_policy(spec):
    policy = json.loads((ROOT/'cilium/network-policy.json.template').read_text())
    policy['metadata']['namespace'] = spec.workload.namespace
    policy['metadata']['labels'] = labels(spec)
    policy['spec']['ingress'][0]['ports'][0]['port'] = spec.workload.container_port
    return policy


def render(spec, image_override=None, pull_policy=None):
    values = {'name': NAME, 'namespace': spec.workload.namespace, 'replicas': spec.workload.replicas,
              'image': image_override or spec.workload.image, 'container_port': spec.workload.container_port,
              'health_path': spec.workload.health_path, 'node_port': spec.exposure.node_port}

    def resolve(value):
        if isinstance(value, dict):
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        if isinstance(value, str) and value.startswith('$'):
            return values[value[1:]]
        return value

    items = [resolve(json.loads((ROOT/'manifests'/f'{kind}.json.template').read_text()))
             for kind in ('namespace', 'workload', 'service')]
    for item in items:
        item['metadata']['labels'] = labels(spec)
    pod = items[1]['spec']['template']
    pod['metadata']['labels'].update(labels(spec))
    if pull_policy:
        pod['spec']['containers'][0]['imagePullPolicy'] = pull_policy
    if spec.workload.sample_content:
        items.append({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {
            'name': NAME, 'namespace': spec.workload.namespace, 'labels': labels(spec)},
            'data': {'index.html': 'Railshot Runtime OK\n'}})
        pod['spec']['volumes'] = [{'name': 'sample-html', 'configMap': {'name': NAME}}]
        pod['spec']['containers'][0]['volumeMounts'] = [{'name': 'sample-html',
                                                       'mountPath': '/usr/share/nginx/html', 'readOnly': True}]
    return {'apiVersion': 'v1', 'kind': 'List', 'items': items}
