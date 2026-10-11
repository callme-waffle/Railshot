#!/usr/bin/env python3
"""Observe exact node roster and retain the kube-system UID across re-execution."""
import json
from pathlib import Path
import subprocess
import sys
import yaml


def check_nodes(observed, expected):
    wanted = {n['id']: n for n in expected['nodes']}
    items = observed['items']
    if len(items) != 3 or {n['metadata']['name'] for n in items} != set(wanted):
        raise ValueError('observed node roster differs')
    for node in items:
        name, info = node['metadata']['name'], node['status']['nodeInfo']
        if info['architecture'] != 'amd64' or info['kubeletVersion'] != expected['version']:
            raise ValueError('node architecture or version differs')
        addresses = [x['address'] for x in node['status']['addresses'] if x['type'] == 'InternalIP']
        if addresses != [wanted[name]['private_ipv4']]:
            raise ValueError('node address differs')
        if not any(c['type'] == 'Ready' and c['status'] == 'True' for c in node['status']['conditions']):
            raise ValueError('node is not ready')
        control = name not in expected['workers']
        if ('node-role.kubernetes.io/control-plane' in node['metadata']['labels']) != control:
            raise ValueError('node role differs')
        taints = node.get('spec', {}).get('taints', [])
        if control and not any(t['key'] == 'node-role.kubernetes.io/control-plane' and t['effect'] == 'NoSchedule' for t in taints):
            raise ValueError('control-plane scheduling isolation missing')
        if not control and any(t['effect'] in ('NoSchedule', 'NoExecute') for t in taints):
            raise ValueError('compute node is not schedulable')
    return True


def main():
    expected = json.loads(Path(sys.argv[1]).read_text())
    def kube(*args):
        value = subprocess.run(['/usr/bin/kubectl', '--kubeconfig=/etc/kubernetes/admin.conf',
                                '--request-timeout=20s', *args, '-o', 'json'], capture_output=True, check=True, timeout=30)
        return json.loads(value.stdout)
    check_nodes(kube('get', 'nodes'), expected)
    config = yaml.safe_load(kube('get', 'configmap', 'kubeadm-config', '-n', 'kube-system')['data']['ClusterConfiguration'])
    if (config.get('kubernetesVersion') != expected['version']
            or config.get('controlPlaneEndpoint') != expected['api_address'] + ':6443'
            or config.get('networking', {}).get('podSubnet') != expected['pod_cidr']
            or config.get('networking', {}).get('serviceSubnet') != expected['service_cidr']):
        raise ValueError('observed kubeadm configuration differs')
    namespace = kube('get', 'namespace', 'kube-system')
    identity = {'target_id': expected['target_id'], 'kube_system_uid': namespace['metadata']['uid']}
    marker = Path('/etc/railshot/kubeadm-cluster-uid.json')
    if marker.exists():
        if json.loads(marker.read_text()) != identity:
            raise ValueError('cluster UID changed; reconciliation required')
    else:
        with marker.open('x') as stream:
            stream.write(json.dumps(identity))
        marker.chmod(0o600)


if __name__ == '__main__':
    main()
