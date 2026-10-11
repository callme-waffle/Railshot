# AWS kubeadm shared storage

Prepared EC2 workers remain in their existing VM state. This module creates an encrypted Regional EFS filesystem, one mount target per worker AZ, and an NFS security group restricted to both worker private /32 addresses. EFS is protected with `prevent_destroy` and a stable creation token.

The [storage executor](../../ansible/storage_profile.py) supplies reviewed inputs and private credentials, retains dedicated state, rejects destructive plans, and observes filesystem ownership/access after apply. Do not point this module at VM state or use it to adopt an existing filesystem. Reuse is read-only and verified by the executor. Existing resources require `managed-by=railshot` and `railshot-cluster=CLUSTER`; identity, encrypted Regional status, mount targets, and NFS access are also observed.

See [shared storage operations](../../../docs/kubeadm/shared-storage.md) for input examples, credentials, recovery and live acceptance. Local `terraform test` uses mock providers and never contacts AWS.
