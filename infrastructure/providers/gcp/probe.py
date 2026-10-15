#!/usr/bin/env python3
"""Read-only cluster observation over authenticated IAP and pinned SSH."""
import json
import importlib.util
import os
from contextlib import ExitStack
from pathlib import Path
import sys
import time
ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'infrastructure/ansible'), str(ROOT)]
import kubeadm
from transport import forwarded_port
from infrastructure.providers.gcp.native import private_json


def observe(registry, *, remote, expected_uid=None):
    request = kubeadm.from_registry(registry, request_id=registry['cluster_id'] + '.observe')
    expected = {n['id']: n for n in kubeadm.nodes(request)}
    nodes = remote('kube-system', 'get', 'nodes', '-o', 'json')['items']
    cluster = len(nodes) == 3 and {n['metadata']['name'] for n in nodes} == set(expected)
    for node in nodes:
        name = node['metadata']['name']
        if name not in expected: cluster = False; continue
        internal = [r['address'] for r in node.get('status', {}).get('addresses', []) if r['type'] == 'InternalIP']
        ready = any(c['type'] == 'Ready' and c['status'] == 'True' for c in node.get('status', {}).get('conditions', []))
        cluster = cluster and ready and internal == [expected[name]['private_ipv4']]
        if name == request['inventory']['control_plane'][0]['id']:
            cluster = cluster and any(t['key'] == 'node-role.kubernetes.io/control-plane' and t['effect'] == 'NoSchedule' for t in node.get('spec', {}).get('taints', []))
    uid = remote('kube-system', 'get', 'namespace', 'kube-system', '-o', 'json')['metadata']['uid']
    cluster = cluster and (expected_uid is None or expected_uid == uid)
    workloads = remote('railshot-storage', 'get', 'deployment,daemonset', '-o', 'json')['items']
    owned = [w for w in workloads if w['metadata'].get('annotations', {}).get('meta.helm.sh/release-name') == 'railshot-storage-driver']
    deployments = [w for w in owned if w['kind'] == 'Deployment']
    daemons = [w for w in owned if w['kind'] == 'DaemonSet']
    storage = bool(deployments and daemons)
    for row in deployments:
        storage = storage and row.get('status', {}).get('availableReplicas', 0) >= row['spec'].get('replicas', 1) and row.get('status', {}).get('observedGeneration', 0) >= row['metadata']['generation']
    for row in daemons:
        storage = storage and row.get('status', {}).get('desiredNumberScheduled', 0) >= 2 and row['status'].get('numberReady') == row['status']['desiredNumberScheduled']
    # Keep the original PVC/PV identity and backend proof; controller availability
    # alone cannot establish storage readiness.
    try:
        cfg = kubeadm._addon('storage_profile').variables(request)['railshot_storage']
        cfg['backend_descriptor'] = private_json(Path(registry['runtime']['storage']['state_dir']) / 'observed-backend.json')
        spec = importlib.util.spec_from_file_location('gcp_storage_install', ROOT / 'deployment/shared-storage/install.py')
        installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)
        pvc = remote('railshot-storage-proof', 'get', 'pvc', 'shared-filesystem-proof', '-o', 'json')
        pv = remote('railshot-storage-proof', 'get', 'pv', pvc['spec']['volumeName'], '-o', 'json')
        journal = remote('railshot-storage-proof', 'get', 'configmap', 'proof-binding', '-o', 'json')
        fingerprint = installer._proof_fingerprint(cfg)
        installer._proof_owned(pvc, registry['cluster_id'], fingerprint)
        installer._owned(journal, registry['cluster_id'])
        installer._verify_proof_volume(cfg, pvc, pv)
        actual_class = remote('railshot-storage', 'get', 'storageclass', installer.CLASS, '-o', 'json')
        _, desired_class = installer.render(cfg)
        storage = storage and all(actual_class.get(k) == desired_class[k] for k in ('provisioner', 'parameters', 'reclaimPolicy'))
        storage = storage and journal.get('data', {}).get('pvc_uid') == pvc['metadata']['uid'] and journal['data'].get('pv_uid') == pv['metadata']['uid'] and journal['data'].get('backend_sha256') == fingerprint
    except (ValueError, KeyError, OSError): storage = False
    return {'cluster': bool(cluster), 'storage': bool(storage), 'cluster_uid': uid}


def main():
    registry = private_json(sys.argv[1]); expected = sys.argv[2] if len(sys.argv) > 2 else None
    request = kubeadm.from_registry(registry, request_id=registry['cluster_id'] + '.observe')
    deadline = time.monotonic() + 180
    with ExitStack() as stack:
        ports = {node['id']: stack.enter_context(forwarded_port(node['ssh']['transport_ref'], deadline)) for node in kubeadm.nodes(request)}
        result = observe(registry, remote=kubeadm.kubectl(request, ports, dict(os.environ), deadline), expected_uid=expected)
    print(json.dumps(result))


if __name__ == '__main__': main()
