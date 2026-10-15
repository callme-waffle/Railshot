# OpenStack environment provisioning

## Specification

A project-bound environment creates an isolated Neutron network, subnet, router,
security groups and ports, then one kubeadm control node and two workers. An
optional fourth VM belongs to the standalone PostgreSQL runtime. Shared and
dedicated environments use the same infrastructure ownership boundaries; the
application service enforces the dedicated application binding.

The existing customer vault and WireGuard gateway boundaries remain in force.
The structured forced SSH grammar adds only project-bound network configuration,
fixed guest bootstrap, host-key readback and owned-server start/stop operations. The executor runs where the selected OpenStack project and guest
management network are reachable. Operator credentials are selected only by the
account configuration. Assisted-vault execution requires the installed vault and
an operator-only unwrap reference; it never changes permissions to read a vault.

Completion requires live observations for infrastructure, kubeadm, CephFS CSI/PVC,
Octavia/TLS/Named Tunnel and application registration. PostgreSQL completion is
reported independently by its runtime. A validated plan is never ready.

## Plan

1. Bind environment/account/project/region and private operator files.
2. Check image, flavors, external network, management route and templates.
3. Journal each native mutation before execution; persist its resource ID before
   starting the next mutation. An interrupted operation is not retried blindly.
4. Create the Neutron dependency graph and Nova guests. Authenticate guest host
   keys using owned-server Nova console evidence or an independently trusted
   operator known_hosts file.
5. Render private node snapshots and reuse the existing kubeadm, CephFS, Octavia,
   Barbican and Named Tunnel implementation.
6. Supply a private PostgreSQL node descriptor to the standalone database runtime,
   then expose the generated application registration configuration.
7. Observe readiness again and permit deletion only after applications, public
   routes and database data have been explicitly removed or retained.

The native adapter accepts no general shell command or executable path from
browser requests. Existing operator templates must
contain only approved CephFS, ingress, DNS, tunnel and GitOps authority. Missing
configuration blocks before infrastructure creation.

## Validation boundary

Local tests exercise native command generation, project/owner binding, failure
journaling, cross-environment isolation and readiness handling. Live provisioning
requires an authorized project, reachable management route, verified host keys,
CephFS credentials, Octavia/Barbican, DNS/tunnel authority, and PostgreSQL backup
storage. No live result is inferred from mocks or syntax checks.
