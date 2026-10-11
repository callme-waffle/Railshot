#!/usr/bin/env python3
"""Install the reviewed GCP application StorageClass on its retained data disk."""
import argparse
import json
from pathlib import Path
import subprocess

import yaml


def run(*args, document=None):
    result = subprocess.run(args, input=None if document is None else json.dumps(document),
                            text=True, capture_output=True, check=True, timeout=45)
    return json.loads(result.stdout) if result.stdout.strip() else None


def install(manifest):
    mount, = run('findmnt', '-J', '--target', '/var/lib/rancher', '--output', 'TARGET,SOURCE,FSTYPE')['filesystems']
    if mount['target'] != '/var/lib/rancher' or mount['fstype'] != 'ext4' or not mount['source'].startswith('/dev/'):
        raise ValueError('retained data disk must be mounted at /var/lib/rancher')
    nodes = run('k3s', 'kubectl', 'get', 'nodes', '-o', 'json')['items']
    if len(nodes) != 1 or nodes[0]['metadata']['name'] != 'railshot-gcp-poc':
        raise ValueError('reviewed single GCP runtime required')
    documents = list(yaml.safe_load_all(Path(manifest).read_text()))
    for item in documents:
        meta = item['metadata']
        current = run('k3s', 'kubectl', '-n', meta.get('namespace', 'default'), 'get',
                      item['kind'], meta['name'], '--ignore-not-found', '-o', 'json')
        if current and (current['metadata'].get('ownerReferences') or
                        current['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') != 'railshot'):
            raise ValueError('existing storage object is not owned by Railshot')
    # Namespace precedes namespaced objects; the checked-in manifest pins both images.
    for item in documents:
        run('k3s', 'kubectl', 'apply', '--server-side', '--field-manager=railshot-app-storage',
            '-f', '-', '-o', 'json', document=item)
    print(json.dumps({'status': 'applied', 'storage_class': 'railshot-persistent',
                      'node': nodes[0]['metadata']['name'], 'mount': mount,
                      'verification': 'PVC write and Pod replacement still required'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    install(parser.parse_args().manifest)
