# AWS application storage

The AWS customer runtime uses the same `railshot-persistent` class and Local Path Provisioner v0.0.37 as GCP. The checked-in manifest pins BusyBox 1.37.0, sets the renamed ConfigMap and service account explicitly, uses local PVs with `WaitForFirstConsumer`, and reclaims a disposable volume on PVC deletion.

This installation targets node `ip-172-31-13-147` on instance `i-0050c052a1fcf41fe` in `ap-northeast-2a`. Its existing encrypted 20 GiB gp3 volume `vol-02595d81ba31fae63` is mounted as ext4 at `/var/lib/rancher`, with `DeleteOnTermination=false`. The storage root is `/var/lib/rancher/railshot-volumes`. The installer checks the mount, EBS device serial, single-node identity, and ownership of every existing Kubernetes object before applying the manifest. It does not format a disk or modify K3s/Cilium.

Run on the reviewed runtime:

```sh
sudo python3 deployment/scripts/install-aws-app-storage.py \
  --manifest deployment/manifests/aws-app-storage.yaml
sudo k3s kubectl -n railshot-storage rollout status deployment/railshot-local-path --timeout=60s
```

Verify a disposable `app-<24 hex>` namespace with a 1 GiB PVC and a non-root UID/GID 65532 consumer. Write a file, delete the writer Pod, mount the same claim in a replacement Pod, and read the original file. Then delete only that test namespace and confirm its PV and exact data directory disappear. The provisioner needs a consumer before delayed binding creates the volume.

New applications can use the common one-volume, one-replica storage contract. Files survive Pod replacement and VM reboot; stop/start preserves the claim. Permanent application deletion removes its PVC and data. Existing apps are not migrated by this change. This is a local volume on retained EBS, not the AWS EBS CSI driver: recovery after instance replacement requires operator reattachment. Local Path does not enforce requested capacity as a filesystem quota or provide replication/backups. Monitor the 20 GiB disk shared with K3s; 17 GiB was available at installation.

The reviewed upstream implementation is [Rancher Local Path Provisioner v0.0.37](https://github.com/rancher/local-path-provisioner/tree/v0.0.37). Application schema, rendering and runtime lifecycle support are delivered separately in PR #164.
