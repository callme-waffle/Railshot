# GCP kubeadm shared storage

Prepared GCE workers remain in their existing VM state. This module creates Regional Filestore in the admitted project/network with a private /26 range and worker /32 NFS export access. A dedicated retained state and `prevent_destroy` protect the backend. The first profile uses 1024..10240 GiB, DIRECT_PEERING and NFSv3. Confirm regional availability, quota and nonoverlapping network reservations before execution.

The [storage executor](../../ansible/storage_profile.py) supplies reviewed inputs and private service-account credentials, rejects destructive plans and independently observes ownership/access/readiness. Reuse is read-only and requires matching project/region/network/share/capacity and `managed-by=railshot`, `railshot-cluster=CLUSTER` labels.

Generic NFS CSI works on self-managed kubeadm; GKE is not required. PVC directory capacity is advisory, not an enforced per-PVC quota. See [shared storage operations](../../../docs/kubeadm/shared-storage.md) for credentials, recovery and live acceptance. Local `terraform test` uses mock providers and never contacts GCP.
