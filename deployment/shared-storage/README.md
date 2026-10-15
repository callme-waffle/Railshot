# Shared-filesystem CSI assets

`install.py` is copied to the kubeadm control plane by the fixed Ansible `shared_storage` role. It installs exactly one owned, digest-verified CSI Helm release and preserves the `railshot-persistent` app StorageClass with `Retain`. Its render helper emits only nonsecret Helm values and StorageClass parameters. Existing namespace/class/driver or credential drift stops the installation.

Pinned chart packages and SHA256 digests are in `CHARTS`: AWS EFS CSI 4.5.1 (driver 3.5.1), generic NFS CSI 4.13.5, CephFS CSI 3.18.1. The published chart bytes are verified before Helm execution. Kubernetes Secret values come from owned Secrets or root-only JSON file references on the control plane; they are never Helm values or command arguments.

The storage receipt requires actual CSI rollout and same-PVC write/delete/read on the two computes. An owned isolated proof PVC is reused across successful runs. Offline rendering cannot generate this receipt. See [operations and acceptance](../../docs/kubeadm/shared-storage.md).
