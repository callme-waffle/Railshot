#!/usr/bin/env python3
"""Validate pinned Helm chart bytes/rendering locally; never contacts a cluster."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SPEC = importlib.util.spec_from_file_location('shared_storage_install', HERE / 'install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helm', default='helm')
    parser.add_argument('--kubernetes-version', default='1.36.5')
    args = parser.parse_args()
    for provider in ('aws', 'gcp', 'openstack'):
        config = json.loads((ROOT / 'infrastructure/ansible/examples/storage' / (provider + '.json')).read_text())['runtime']['storage']
        config['workers'] = ['compute-a', 'compute-b']
        descriptor = {**config['binding'], 'backend': config['backend']}
        if provider == 'aws':
            descriptor['filesystem_id'] = 'fs-0123456789abcdef0'
        elif provider == 'gcp':
            descriptor.update(server='10.0.8.2', share='/apps')
        config['backend_descriptor'] = descriptor
        values, storage_class = installer.render(config)
        with tempfile.TemporaryDirectory(prefix='railshot-chart-render-') as work:
            directory = Path(work)
            archive, value_file = directory / 'chart.tgz', directory / 'values.json'
            installer.download_chart(config['backend'], archive)
            value_file.write_text(json.dumps(values))
            output = subprocess.run([args.helm, 'template', installer.RELEASE, str(archive), '--namespace', installer.NAMESPACE,
                                     '--kube-version', args.kubernetes_version, '--values', str(value_file)],
                                    check=True, capture_output=True, text=True).stdout
            documents = [document for document in yaml.safe_load_all(output) if document]
            workloads = [doc for doc in documents if doc['kind'] in ('Deployment', 'DaemonSet')]
            if not {'Deployment', 'DaemonSet'} <= {workload['kind'] for workload in workloads}:
                raise ValueError('Chart did not render both CSI controller and node workloads')
            for workload in workloads:
                spec = workload['spec']['template']['spec']
                actual = spec['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms'][0]['matchExpressions'][0]['values']
                if actual != config['workers']:
                    raise ValueError('Chart worker placement differs from reviewed inputs')
                if provider in ('gcp', 'openstack') and spec.get('hostNetwork') is not True:
                    raise ValueError('CSI must use worker host addresses for private backend access')
                if workload['kind'] == 'Deployment' and provider in ('gcp', 'openstack'):
                    if not spec['affinity'].get('podAntiAffinity', {}).get('requiredDuringSchedulingIgnoredDuringExecution'):
                        raise ValueError('Host-network CSI controller replicas need distinct workers')
            if any(doc['kind'] == 'StorageClass' for doc in documents):
                raise ValueError('The CSI chart must not own the application StorageClass')
            if storage_class['metadata']['name'] != installer.CLASS or storage_class['reclaimPolicy'] != 'Retain':
                raise ValueError('App StorageClass contract differs')
        print(json.dumps({'provider': provider, 'chart_version': installer.CHARTS[config['backend']]['version'],
                          'chart_sha256_verified': True, 'rendered': True, 'storage_ready': False}))


if __name__ == '__main__':
    main()
