#!/usr/bin/env python3
"""Install one owned CSI release and prove same-PVC mobility on two computes.

Run on the kubeadm control plane with root-owned /etc/kubernetes/admin.conf.
Cloud credentials never enter this process; CSI credentials come from existing
owned Kubernetes Secrets or root-only files prepositioned on this control node.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import urllib.request

NAMESPACE = 'railshot-storage'
RELEASE = 'railshot-storage-driver'
CLASS = 'railshot-persistent'
OWNER = 'railshot.io/cluster'
CHARTS = {
    'efs': {'version': '4.5.1', 'driver': 'efs.csi.aws.com', 'sha256': 'ce948ef21ce273ba28596a480b4b2a4deccdefab3635f6cfd37ef8a15565e295',
            'url': 'https://github.com/kubernetes-sigs/aws-efs-csi-driver/releases/download/helm-chart-aws-efs-csi-driver-4.5.1/aws-efs-csi-driver-4.5.1.tgz'},
    'filestore': {'version': '4.13.5', 'driver': 'nfs.csi.k8s.io', 'sha256': 'aace8754451ecbd87e090ec29e6cbffcf88dbd9980cecb165da8b9d0880f7fac',
                  'url': 'https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts/v4.13.5/csi-driver-nfs-4.13.5.tgz'},
    'cephfs': {'version': '3.18.1', 'driver': 'cephfs.csi.ceph.com', 'sha256': '01fac42009678ec37b7d3c2f2b313f7d6f4caff1a18e62fb8e763976e26b656d',
                'url': 'https://ceph.github.io/csi-charts/cephfs/ceph-csi-cephfs-3.18.1.tgz'},
}


class InstallError(ValueError):
    pass


def command(argv, *, document=None, json_output=False):
    completed = subprocess.run(argv, input=None if document is None else json.dumps(document),
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               check=False, timeout=360, umask=0o077)
    if completed.returncode:
        # Kubernetes Secret/Helm errors can contain supplied values. Do not echo.
        raise InstallError('Storage command failed; private target state requires inspection')
    return json.loads(completed.stdout) if json_output and completed.stdout.strip() else completed.stdout


class Kubernetes:
    def __init__(self, kubeconfig='/etc/kubernetes/admin.conf', runner=command):
        self.prefix = ['/usr/bin/kubectl', '--kubeconfig', kubeconfig]
        self.runner = runner

    def get(self, kind, name=None, namespace=None):
        args = [*self.prefix]
        if namespace:
            args += ['-n', namespace]
        args += ['get', kind]
        if name:
            args += [name, '--ignore-not-found']
        result = self.runner([*args, '-o', 'json'], json_output=True)
        return result or None

    def apply(self, document):
        return self.runner([*self.prefix, 'apply', '--server-side', '--field-manager=railshot-shared-storage', '-f', '-', '-o', 'json'],
                           document=document, json_output=True)

    def run(self, *args):
        return self.runner([*self.prefix, *args])


def _metadata(name, cluster, *, namespace=None):
    meta = {'name': name, 'labels': {'app.kubernetes.io/managed-by': 'railshot', OWNER: cluster}}
    if namespace:
        meta['namespace'] = namespace
    return meta


def affinity(workers, controller_labels=None, *, required=False):
    result = {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [
        {'matchExpressions': [{'key': 'kubernetes.io/hostname', 'operator': 'In', 'values': workers}]}]}}}
    if controller_labels:
        term = {'labelSelector': {'matchLabels': controller_labels}, 'topologyKey': 'kubernetes.io/hostname'}
        result['podAntiAffinity'] = ({'requiredDuringSchedulingIgnoredDuringExecution': [term]} if required else
                                     {'preferredDuringSchedulingIgnoredDuringExecution': [{'weight': 100, 'podAffinityTerm': term}]})
    return result


def render(config):
    """Render non-secret Helm values and class; usable offline and in tests."""
    backend = config['backend']
    cluster = config['binding']['cluster_id']
    descriptor = config['backend_descriptor']
    workers = config['workers']
    if backend not in CHARTS or len(workers) != 2 or len(set(workers)) != 2:
        raise InstallError('One supported backend and two unique workers are required')
    if any(descriptor.get(key) != config['binding'].get(key) for key in ('provider', 'region', 'account_id', 'cluster_id')):
        raise InstallError('Backend descriptor differs from admitted cluster binding')
    values = {}
    params = {}
    mount_options = []
    if backend == 'efs':
        secret = config['aws'].get('controller_credentials_secret')
        values = {'controller': {'replicaCount': 2, 'affinity': affinity(workers, {'app': 'efs-csi-controller'}),
                                 'env': [{'name': key, 'valueFrom': {'secretKeyRef': {'name': secret, 'key': key, 'optional': key == 'AWS_SESSION_TOKEN'}}}
                                         for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')] +
                                        [{'name': 'AWS_REGION', 'value': config['binding']['region']},
                                         {'name': 'AWS_EC2_METADATA_DISABLED', 'value': 'true'}]},
                  'node': {'affinity': affinity(workers)}, 'storageClasses': []}
        if config['aws'].get('identity_mode') == 'instance_profile':
            node = config['aws'].get('controller_node')
            if not isinstance(node, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', node) or node in workers:
                raise InstallError('Instance-profile controller requires the dedicated control node')
            values['controller'] = {'replicaCount': 1, 'hostNetwork': True,
                'dnsPolicy': 'ClusterFirstWithHostNet', 'nodeSelector': {'kubernetes.io/hostname': node},
                'tolerations': [{'key': 'node-role.kubernetes.io/control-plane', 'operator': 'Exists', 'effect': 'NoSchedule'}],
                'tags': {'railshot-cluster': cluster},
                'env': [{'name': 'AWS_REGION', 'value': config['binding']['region']},
                        {'name': 'AWS_EC2_METADATA_DISABLED', 'value': 'false'}]}
        params = {'provisioningMode': 'efs-ap', 'fileSystemId': descriptor['filesystem_id'], 'directoryPerms': '0770',
                  'gidRangeStart': '10000', 'gidRangeEnd': '20000', 'basePath': '/' + cluster,
                  'subPathPattern': '${.PVC.namespace}/${.PVC.name}', 'ensureUniqueDirectory': 'true', 'reuseAccessPoint': 'false'}
        mount_options = ['tls']
    elif backend == 'filestore':
        values = {'controller': {'replicas': 2, 'affinity': affinity(workers, {'app': 'csi-nfs-controller'}, required=True), 'enableSnapshotter': False,
                                 'defaultOnDeletePolicy': 'retain'},
                  'node': {'affinity': affinity(workers)}, 'storageClass': {'create': False}}
        params = {'server': descriptor['server'], 'share': descriptor['share'],
                  'subDir': '${pvc.metadata.namespace}/${pvc.metadata.name}/${pv.metadata.name}',
                  'mountPermissions': '0770', 'onDelete': 'retain'}
        # REGIONAL Filestore exports NFSv3 unless an explicit supported v4.1
        # instance was selected; first profile deliberately uses portable v3.
        mount_options = ['nfsvers=3', 'hard', 'timeo=600', 'retrans=2']
    else:
        cfg = config['cephfs']
        values = {'csiConfig': [{'clusterID': cfg['cluster_id'], 'monitors': cfg['monitors'],
                                 'cephFS': {'subvolumeGroup': cfg['subvolume_group']}}],
                  'provisioner': {'replicaCount': 2, 'affinity': affinity(workers, {'component': 'provisioner', 'release': RELEASE}, required=True), 'enableHostNetwork': True},
                  'nodeplugin': {'affinity': affinity(workers)}, 'storageClass': {'create': False},
                  'secret': {'create': False}, 'volumeSnapshotClass': {'create': False}}
        params = {'clusterID': cfg['cluster_id'], 'fsName': cfg['fs_name'], 'subvolumeGroup': cfg['subvolume_group']}
        for stage, secret in [('provisioner', cfg['provisioner_secret']), ('controller-expand', cfg['provisioner_secret']),
                              ('node-stage', cfg['node_secret'])]:
            params[f'csi.storage.k8s.io/{stage}-secret-name'] = secret
            params[f'csi.storage.k8s.io/{stage}-secret-namespace'] = NAMESPACE
    storage_class = {'apiVersion': 'storage.k8s.io/v1', 'kind': 'StorageClass',
                     'metadata': {**_metadata(CLASS, cluster), 'annotations': {'storageclass.kubernetes.io/is-default-class': 'false'}},
                     'provisioner': CHARTS[backend]['driver'], 'parameters': params, 'reclaimPolicy': 'Retain',
                     'volumeBindingMode': 'Immediate', 'allowVolumeExpansion': backend != 'efs'}
    if mount_options:
        storage_class['mountOptions'] = mount_options
    return values, storage_class


def _owned(item, cluster):
    if not item or item.get('metadata', {}).get('labels', {}).get(OWNER) != cluster or item.get('metadata', {}).get('ownerReferences'):
        raise InstallError('Existing storage object is not owned by this cluster')


def preflight(kube, config, desired):
    cluster = config['binding']['cluster_id']
    existing_namespace = kube.get('namespace', NAMESPACE)
    if existing_namespace:
        _owned(existing_namespace, cluster)
    classes = kube.get('storageclasses') or {'items': []}
    for item in classes.get('items', []):
        annotations = item['metadata'].get('annotations', {})
        if any(annotations.get(key) == 'true' for key in ('storageclass.kubernetes.io/is-default-class', 'storageclass.beta.kubernetes.io/is-default-class')):
            raise InstallError('An existing default StorageClass requires explicit operator review')
    current = kube.get('storageclass', CLASS)
    if current:
        _owned(current, cluster)
        for field in ('provisioner', 'parameters', 'reclaimPolicy', 'volumeBindingMode', 'allowVolumeExpansion', 'mountOptions'):
            if current.get(field) != desired.get(field):
                raise InstallError('Existing StorageClass differs; replacement is refused')
    driver = kube.get('csidriver', CHARTS[config['backend']]['driver'])
    if driver:
        annotations = driver['metadata'].get('annotations', {})
        if annotations.get('meta.helm.sh/release-name') != RELEASE or annotations.get('meta.helm.sh/release-namespace') != NAMESPACE:
            raise InstallError('Existing CSI driver belongs to another installer')
    return current


def _secret_data(path, required, allowed):
    file = Path(path)
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise InstallError('Remote CSI credential file must be private and owned by the installer')
    data = json.loads(file.read_text())
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data) or any(not isinstance(v, str) or not v for v in data.values()):
        raise InstallError('CSI credential file has unsupported or missing keys')
    return {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}


def ensure_secrets(kube, config):
    cluster = config['binding']['cluster_id']
    refs = []
    if config['backend'] == 'efs' and config['aws'].get('identity_mode', 'secret') == 'secret':
        cfg = config['aws']
        refs = [(cfg['controller_credentials_secret'], cfg.get('controller_credentials_file'),
                 ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'), ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'))]
    elif config['backend'] == 'cephfs':
        cfg = config['cephfs']
        refs = [(name, cfg.get('secret_files', {}).get(name), ('userID', 'userKey'), ('userID', 'userKey'))
                for name in sorted({cfg['provisioner_secret'], cfg['node_secret']})]
    for name, path, required, allowed in refs:
        current = kube.get('secret', name, NAMESPACE)
        if current:
            _owned(current, cluster)
            if set(required) - set(current.get('data', {})):
                raise InstallError('Existing CSI Secret is missing required keys')
            if path and current['data'] != _secret_data(path, required, allowed):
                raise InstallError('CSI credential rotation requires explicit operator action')
        elif path:
            kube.apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': _metadata(name, cluster, namespace=NAMESPACE),
                        'type': 'Opaque', 'data': _secret_data(path, required, allowed)})
        else:
            raise InstallError('Preposition a private CSI credential file or an owned Secret')


def download_chart(backend, destination):
    chart = CHARTS[backend]
    with urllib.request.urlopen(chart['url'], timeout=45) as response:
        raw = response.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != chart['sha256']:
        raise InstallError('Pinned CSI chart SHA256 verification failed')
    destination.write_bytes(raw)
    destination.chmod(0o600)


def _proof_fingerprint(config):
    _, desired = render(config)
    return hashlib.sha256(json.dumps({'backend': config['backend_descriptor'], 'storage_class': desired}, sort_keys=True).encode()).hexdigest()


def _proof_objects(config, request_id, token):
    cluster = config['binding']['cluster_id']
    namespace = 'railshot-storage-proof'
    name = 'shared-filesystem-proof'
    meta = _metadata(namespace, cluster)
    meta['labels']['railshot.io/purpose'] = 'shared-storage-proof'
    meta['annotations'] = {'railshot.io/backend-sha256': _proof_fingerprint(config)}
    ns = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': meta}
    pvc_meta = _metadata(name, cluster, namespace=namespace)
    pvc_meta['labels']['railshot.io/purpose'] = 'shared-storage-proof'
    pvc_meta['annotations'] = meta['annotations']
    pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': pvc_meta,
           'spec': {'storageClassName': CLASS, 'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '1Gi'}}}}
    pods = []
    for mode, worker in zip(('write', 'read'), config['workers']):
        action = f'printf %s {token} > /data/proof && sync' if mode == 'write' else f'test "$(cat /data/proof)" = {token}'
        pod_meta = _metadata('proof-' + token[:20] + '-' + mode, cluster, namespace=namespace)
        pod_meta['annotations'] = {'railshot.io/request-sha256': hashlib.sha256(request_id.encode()).hexdigest(), 'railshot.io/nonce': token}
        pods.append({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': pod_meta,
                     'spec': {'nodeSelector': {'kubernetes.io/hostname': worker}, 'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                              'securityContext': {'runAsUser': 10000, 'runAsGroup': 10000, 'fsGroup': 10000, 'runAsNonRoot': True, 'seccompProfile': {'type': 'RuntimeDefault'}},
                              'containers': [{'name': 'proof', 'image': 'docker.io/library/busybox:1.37.0', 'command': ['sh', '-c', action],
                                              'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'}, 'limits': {'cpu': '100m', 'memory': '64Mi'}},
                                              'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                                              'volumeMounts': [{'name': 'data', 'mountPath': '/data'}]}],
                              'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': name}}]}})
    return ns, pvc, pods


def _proof_owned(item, cluster, fingerprint):
    _owned(item, cluster)
    metadata = item['metadata']
    if (metadata.get('labels', {}).get('railshot.io/purpose') != 'shared-storage-proof'
            or metadata.get('annotations', {}).get('railshot.io/backend-sha256') != fingerprint):
        raise InstallError('Existing proof object is bound to different storage')


def _verify_proof_volume(config, pvc, pv):
    claim = pv.get('spec', {}).get('claimRef', {})
    csi = pv.get('spec', {}).get('csi', {})
    if (pvc.get('status', {}).get('phase') != 'Bound' or pvc['spec'].get('storageClassName') != CLASS
            or pvc['spec'].get('accessModes') != ['ReadWriteOnce']
            or claim.get('namespace') != pvc['metadata']['namespace'] or claim.get('name') != pvc['metadata']['name']
            or claim.get('uid') != pvc['metadata'].get('uid') or not claim.get('uid')
            or csi.get('driver') != CHARTS[config['backend']]['driver']
            or pv['spec'].get('storageClassName') != CLASS or pv['spec'].get('persistentVolumeReclaimPolicy') != 'Retain'):
        raise InstallError('Proof PVC/PV ownership, class or driver does not match')
    descriptor = config['backend_descriptor']
    attrs = csi.get('volumeAttributes', {})
    if config['backend'] == 'efs' and not csi.get('volumeHandle', '').startswith(descriptor['filesystem_id'] + '::'):
        raise InstallError('Proof PV belongs to a different EFS filesystem')
    if config['backend'] == 'filestore' and (attrs.get('server') != descriptor['server'] or attrs.get('share') != descriptor['share']):
        raise InstallError('Proof PV belongs to a different Filestore export')
    if config['backend'] == 'cephfs' and (attrs.get('clusterID') != config['cephfs']['cluster_id'] or attrs.get('fsName') != config['cephfs']['fs_name']):
        raise InstallError('Proof PV belongs to a different Ceph filesystem')


def prove_mobility(kube, config, request_id, token, wait_seconds):
    """Reuse exactly one owned proof PVC; delete only current nonce-bound Pods.

    The retained test PVC is isolated from application namespaces. A failed Pod
    blocks retries until inspected and removed. No backend deletion is automated.
    Successful reruns allocate no additional PVC, PV, access point or subvolume.
    """
    if not re.fullmatch(r'[a-f0-9]{32}', token):
        raise InstallError('Invalid mobility proof nonce')
    ns, desired_pvc, pods = _proof_objects(config, request_id, token)
    namespace, cluster = ns['metadata']['name'], config['binding']['cluster_id']
    fingerprint = _proof_fingerprint(config)
    existing_ns = kube.get('namespace', namespace)
    if existing_ns:
        _proof_owned(existing_ns, cluster, fingerprint)
        if (kube.get('pods', namespace=namespace) or {}).get('items'):
            raise InstallError('Prior proof Pods remain; inspect them before retry')
    else:
        kube.apply(ns)
    existing_pvc = kube.get('pvc', desired_pvc['metadata']['name'], namespace)
    if existing_pvc:
        _proof_owned(existing_pvc, cluster, fingerprint)
        if existing_pvc['spec'].get('resources', {}).get('requests', {}).get('storage') != '1Gi':
            raise InstallError('Existing proof PVC differs from the isolated test contract')
    else:
        kube.apply(desired_pvc)
    kube.run('-n', namespace, 'wait', 'pvc/' + desired_pvc['metadata']['name'], '--for=jsonpath={.status.phase}=Bound', f'--timeout={wait_seconds}s')
    observed = kube.get('pvc', desired_pvc['metadata']['name'], namespace)
    pv_name = observed['spec']['volumeName']
    pv = kube.get('pv', pv_name)
    _verify_proof_volume(config, observed, pv or {})
    current_journal = kube.get('configmap', 'proof-binding', namespace)
    binding = {'pvc_uid': observed['metadata']['uid'], 'pv_uid': pv['metadata']['uid'], 'backend_sha256': fingerprint}
    if current_journal:
        _owned(current_journal, cluster)
        if any(current_journal.get('data', {}).get(k) != value for k, value in binding.items()):
            raise InstallError('Observed proof volume identity changed')
    journal = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': _metadata('proof-binding', cluster, namespace=namespace),
               'data': {**binding, 'request_id': request_id, 'nonce': token, 'pvc': desired_pvc['metadata']['name'], 'pv': pv_name}}
    kube.apply(journal)
    for pod in pods:
        kube.apply(pod)
        pod_name = pod['metadata']['name']
        kube.run('-n', namespace, 'wait', 'pod/' + pod_name, '--for=jsonpath={.status.phase}=Succeeded', f'--timeout={wait_seconds}s')
        actual = kube.get('pod', pod_name, namespace)
        _owned(actual, cluster)
        if (actual.get('spec', {}).get('nodeName') != pod['spec']['nodeSelector']['kubernetes.io/hostname']
                or actual.get('metadata', {}).get('annotations', {}).get('railshot.io/nonce') != token
                or actual.get('spec', {}).get('volumes') != pod['spec']['volumes']):
            raise InstallError('Storage proof did not use the requested worker/PVC/nonce')
        statuses = actual.get('status', {}).get('containerStatuses', [])
        if (actual.get('status', {}).get('phase') != 'Succeeded' or len(statuses) != 1
                or statuses[0].get('state', {}).get('terminated', {}).get('exitCode') != 0):
            raise InstallError('Storage proof container did not succeed')
        # Wait for removal and confirm absence before the next worker mounts it.
        kube.run('-n', namespace, 'delete', 'pod', pod_name, '--wait=true', f'--timeout={wait_seconds}s')
        if kube.get('pod', pod_name, namespace):
            raise InstallError('Writer proof Pod removal was not established')
    return {'proof_pvc': namespace + '/' + desired_pvc['metadata']['name'], 'proof_pv': pv_name,
            'proof_binding': namespace + '/proof-binding'}


def install(config, *, request_id, nonce, wait_seconds=300, kube=None, helm_runner=command, downloader=download_chart, mobility=prove_mobility):
    if not re.fullmatch(r'[a-f0-9]{32}', nonce) or not 30 <= wait_seconds <= 1800:
        raise InstallError('Invalid proof nonce or wait timeout')
    kube = kube or Kubernetes()
    values, storage_class = render(config)
    preflight(kube, config, storage_class)
    cluster = config['binding']['cluster_id']
    kube.apply({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': _metadata(NAMESPACE, cluster)})
    ensure_secrets(kube, config)
    with tempfile.TemporaryDirectory(prefix='railshot-csi-') as work:
        directory = Path(work)
        chart_file = directory / 'driver.tgz'
        values_file = directory / 'values.json'
        downloader(config['backend'], chart_file)
        values_file.write_text(json.dumps(values))
        values_file.chmod(0o600)
        # Deliberately no --atomic/--force/take-ownership/destructive rollback.
        helm_runner(['/usr/local/bin/helm', '--kubeconfig', '/etc/kubernetes/admin.conf', 'upgrade', '--install', RELEASE,
                     str(chart_file), '--namespace', NAMESPACE, '--values', str(values_file), '--wait', f'--timeout={wait_seconds}s'])
    workloads = kube.get('deployment,daemonset', namespace=NAMESPACE) or {'items': []}
    deployments, daemons = 0, 0
    for workload in workloads.get('items', []):
        meta = workload['metadata']
        annotations = meta.get('annotations', {})
        if annotations.get('meta.helm.sh/release-name') == RELEASE:
            deployments += workload['kind'] == 'Deployment'
            daemons += workload['kind'] == 'DaemonSet'
            kube.run('-n', NAMESPACE, 'rollout', 'status', workload['kind'].lower() + '/' + meta['name'], f'--timeout={wait_seconds}s')
    if not deployments or not daemons:
        raise InstallError('CSI controller and node workload readiness was not established')
    kube.apply(storage_class)
    proof = mobility(kube, config, request_id, nonce, wait_seconds)
    return {'storage_backend_ready': True, 'storage_csi_ready': True, 'storage_pvc_ready': True, 'storage_ready': True, **proof}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    # A failed execution must leave no receipt from an earlier attempt.
    receipt = Path(args.receipt)
    if receipt.exists() or receipt.is_symlink():
        raise InstallError('Receipt path already exists')
    data = json.loads(Path(args.config).read_text())
    result = install(data['railshot_storage'], request_id=data['request_id'], nonce=data['nonce'], wait_seconds=data['wait_seconds'])
    result.update(request_id=data['request_id'], target_id=data['target_id'], nonce=data['nonce'], stage='storage')
    fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(result, stream)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        raise SystemExit('Shared storage failed; readiness was not established. Inspect private target state.')
