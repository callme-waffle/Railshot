# Cinder CSI for the OpenStack runtime

Use the official `openstack-cinder-csi` Helm chart **2.34.0**, driver **v1.34.0**, on K3s **v1.34.11+k3s1**. The values select `railshot-openstack-runtime-01` and create one default StorageClass, `railshot-cinder`: Cinder type `lvmdriver-1`, ext4, `Delete`, expansion enabled, and `WaitForFirstConsumer`. The chart's topology-enabled provisioner supports delayed binding. No local-path provisioner is installed.

Before installation, select the runtime's private kubeconfig and verify its node identity, `/var/lib/kubelet` directory, OpenStack metadata access, Cinder/Nova/Keystone connectivity, volume type and quota. Check that no other Cinder driver or default StorageClass is already installed. Resolve existing ownership before installing; Helm must not adopt another release's cluster-scoped objects.

The operator supplies the existing `kube-system/railshot-cinder-csi-cloud-config` Secret with a `cloud.conf` key and the CSI identity's credentials. This chart reads the Secret and never creates it. Keep credentials out of Git, Helm values, command arguments and rendered manifests. For a private OpenStack CA, include `ca.pem` in that Secret and reference `/etc/config/ca.pem` from `cloud.conf`; the values remove the chart's `/etc/cacert` host mount. Keep TLS verification enabled.

## Snapshot API prerequisite

Chart 2.34.0 always deploys the `csi-snapshotter:v8.3.0` sidecar; it has no disable switch and installs neither snapshot CRDs nor the common snapshot controller. The sidecar watches `VolumeSnapshotContent` and `VolumeSnapshotClass`; without their APIs its caches cannot sync. Pod readiness alone does not prove snapshot support. [Upstream sidecar source](https://github.com/kubernetes-csi/external-snapshotter/blob/v8.3.0/pkg/sidecar-controller/snapshot_controller_base.go)

Reuse existing compatible snapshot CRDs. If absent, the cluster operator installs these three pinned, cluster-scoped CRDs once:

```sh
kubectl apply --server-side --field-manager=railshot-cinder -f https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/v8.3.0/client/config/crd/snapshot.storage.k8s.io_volumesnapshots.yaml
kubectl apply --server-side --field-manager=railshot-cinder -f https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/v8.3.0/client/config/crd/snapshot.storage.k8s.io_volumesnapshotcontents.yaml
kubectl apply --server-side --field-manager=railshot-cinder -f https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/v8.3.0/client/config/crd/snapshot.storage.k8s.io_volumesnapshotclasses.yaml
kubectl wait --for=condition=Established --timeout=60s \
  crd/volumesnapshots.snapshot.storage.k8s.io \
  crd/volumesnapshotcontents.snapshot.storage.k8s.io \
  crd/volumesnapshotclasses.snapshot.storage.k8s.io
```

This is a volume provisioning setup. Snapshot create/restore additionally needs the shared snapshot controller and a reviewed VolumeSnapshotClass; those are not installed here. [Upstream snapshot deployment requirements](https://github.com/kubernetes-csi/external-snapshotter/blob/v8.3.0/README.md#usage)

## Install and verify

Run from the repository root with the runtime kubeconfig selected. Use K3s's existing Helm controller; the runtime does not need a Helm binary. On the runtime itself, `sudo k3s kubectl` is the equivalent of `kubectl` below. [K3s HelmChart configuration](https://docs.k3s.io/helm/)

```sh
kubectl config current-context
kubectl get node railshot-openstack-runtime-01
kubectl get storageclass
kubectl get crd helmcharts.helm.cattle.io
kubectl -n kube-system get secret railshot-cinder-csi-cloud-config -o name
```

Use the reviewed official `openstack-cinder-csi-2.34.0.tgz` archive. Its SHA-256 is `a5e2238ef925dbc8ce47f2a9f29dd3c79aa8fc6cb79f466a5134cff14bdb3361`. The following creates a credential-free HelmChart from that exact archive and the checked-in values; replace only the archive path. `chartContent` supplies the pinned bytes and takes precedence over `chart`/`version`. `failurePolicy: abort` leaves a failed install for inspection instead of uninstalling and reinstalling it automatically.

```sh
python3 - /private/path/openstack-cinder-csi-2.34.0.tgz <<'PY' > /tmp/railshot-cinder-helmchart.json
import base64, hashlib, json, pathlib, sys
chart = pathlib.Path(sys.argv[1]).read_bytes()
assert hashlib.sha256(chart).hexdigest() == 'a5e2238ef925dbc8ce47f2a9f29dd3c79aa8fc6cb79f466a5134cff14bdb3361'
json.dump({
    'apiVersion': 'helm.cattle.io/v1', 'kind': 'HelmChart',
    'metadata': {'name': 'railshot-cinder-csi', 'namespace': 'kube-system'},
    'spec': {
        'chart': 'openstack-cinder-csi', 'version': '2.34.0',
        'targetNamespace': 'kube-system', 'failurePolicy': 'abort', 'timeout': '10m',
        'chartContent': base64.b64encode(chart).decode(),
        'valuesContent': pathlib.Path('deployment/cinder/values.yaml').read_text(),
    },
}, sys.stdout)
PY
kubectl apply -f /tmp/railshot-cinder-helmchart.json
kubectl -n kube-system get helmchart railshot-cinder-csi -o yaml
kubectl -n kube-system wait --for=condition=Complete job/helm-install-railshot-cinder-csi --timeout=10m
kubectl -n kube-system rollout status deployment/openstack-cinder-csi-controllerplugin --timeout=5m
kubectl -n kube-system rollout status daemonset/openstack-cinder-csi-nodeplugin --timeout=5m
kubectl get csidriver cinder.csi.openstack.org
kubectl get csinode railshot-openstack-runtime-01 -o yaml
kubectl get storageclass railshot-cinder -o yaml
kubectl -n kube-system logs deployment/openstack-cinder-csi-controllerplugin -c csi-snapshotter --tail=30
```

If the install Job has not been created yet, read the HelmChart's `status.jobName` and wait for that Job. Inspect its logs on failure; do not delete the HelmChart to retry. `CSINode` must advertise `cinder.csi.openstack.org` with the correct OpenStack server ID and topology. `railshot-cinder` must be the only default class.

For a fresh chart download and local render review on an operator workstation that already has Helm, isolate its repository files so unrelated repositories are not refreshed:

```sh
cinder_helm_home="$(mktemp -d)"
export HELM_REPOSITORY_CONFIG="$cinder_helm_home/repositories.yaml"
export HELM_REPOSITORY_CACHE="$cinder_helm_home/cache"
mkdir -p "$HELM_REPOSITORY_CACHE"
helm repo add cpo https://kubernetes.github.io/cloud-provider-openstack
helm pull cpo/openstack-cinder-csi --version 2.34.0 --destination "$cinder_helm_home"
cinder_chart="$cinder_helm_home/openstack-cinder-csi-2.34.0.tgz"
helm lint "$cinder_chart" --values deployment/cinder/values.yaml
helm template railshot-cinder-csi "$cinder_chart" --namespace kube-system \
  --kube-version 1.34.11 --values deployment/cinder/values.yaml > "$cinder_helm_home/rendered.yaml"
```

Inspect the rendered file before applying the HelmChart: exactly one StorageClass, no Secret, driver v1.34.0, and both Pods mounting the named Secret. Keep release ownership with the HelmChart controller; do not also manage the same release with direct `helm upgrade` commands.

Acceptance requires a disposable RWO PVC using `railshot-cinder` **and a consuming Pod**; delayed binding leaves an unused PVC Pending. Use the scheduler with a node selector, not `spec.nodeName`, so delayed provisioning can run. Verify Bound, attach/mount, a write surviving Pod recreation, and expansion if claimed. Record the PV's `spec.csi.volumeHandle` and confirm the same Cinder volume is attached to the intended server. Delete only the disposable Pod/PVC and confirm the VolumeAttachment, PV and that exact Cinder volume disappear. Helm readiness alone is not storage acceptance. [Upstream topology example](https://github.com/kubernetes/cloud-provider-openstack/blob/v1.34.0/examples/cinder-csi-plugin/topology/example.yaml)

## Uninstall impact

Only after migrating or deliberately deleting all Cinder-backed claims and verifying their volume cleanup:

```sh
kubectl -n kube-system delete helmchart railshot-cinder-csi --wait=true --timeout=5m
kubectl -n kube-system get jobs,pods -l helmcharts.helm.cattle.io/chart=railshot-cinder-csi
```

Deleting the HelmChart asks the K3s controller to uninstall the release. Confirm its uninstall Job completes and the release's controller, node plugin, RBAC, CSIDriver and StorageClass are absent. It does not delete workload PVCs/PVs, Cinder volumes, the externally supplied Secret or the separately installed snapshot CRDs. Removing the driver while claims remain prevents normal provisioning, attachment and deletion. A PVC/PV with `Delete` reclaim policy deletes its backing volume when cleanup completes; preserve required data before deleting claims. Keep shared snapshot CRDs and controllers owned by the cluster operator. If the HelmChart was also saved in K3s's manifests directory, remove that source first to prevent re-creation.
