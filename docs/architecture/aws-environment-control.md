# AWS environment control

## Work specification

Provide an AWS implementation of the shared environment control-plane port in
Seoul (`ap-northeast-2`). Both customer-owned AssumeRole connections and explicitly
configured Railshot managed credentials bind to an exact AWS account. An environment
owns its VPC, subnets, security groups and three EC2 kubeadm nodes, with an optional
separate PostgreSQL node. Existing kubeadm, EFS and ALB/Route53/ACM executors remain
the runtime and public-access owners. EKS and Patroni are excluded.

Completion requires executable native provisioning, authenticated resource
observation, bounded operations, persistent operation evidence, unknown-outcome
reconciliation and application registration through the shared facade. No resource
creation receipt alone establishes cluster, storage, application or public readiness.

## Implementation plan

1. Add explicit account-bound AWS credentials with renewable STS sessions; keep
   credential bytes out of durable operation bindings and public results.
2. Add a dedicated Terraform network module and wrap the existing VM module with
   a private saved-plan executor. Preserve one Terraform writer per resource.
3. Observe owned EC2 resources and generate native kubeadm, EFS and edge inputs;
   enroll host keys through authenticated SSM before runtime installation.
4. Implement start/stop/reboot and ownership-checked retaining deletion; preserve
   data-volume references, stop on uncertain execution, and require reconciliation.
5. Run adapter and native-command contract tests plus available Terraform checks.
   Live acceptance requires an approved test account, quota/budget and delegated DNS.

## Evidence boundaries

Local mocks and Terraform validation are not live AWS acceptance. Provisioning,
Kubernetes health, PVC mobility, public HTTPS, app operations and cleanup must each
have observed evidence before their corresponding readiness flag is true.

## Approved identity and lifecycle decisions

Customer and managed accounts both assume an exact role. Each native stage gets
fresh-enough STS credentials; the durable binding records the role/account and
policy rather than credential bytes. The EFS controller uses the dedicated
control-plane instance role through host networking; workers never receive EFS
API permissions. Existing secret-based CSI remains supported for existing callers.

The new network uses public-address egress without public node ingress. Retaining
teardown records EBS root/data volumes and EFS filesystem identities before removing
their compute/network attachments. Missing, expired or insufficient budget approval
and the existing authoritative billing ledger block creation.

The common facade owns application registration verification, attachment guards,
normalized HTTP resources. AWS creates the fourth DB
VM, grants only its separate role scoped backup access, and invokes the shared PostgreSQL executor through a fresh STS child. It does not claim database
readiness from provisioning alone. See the provider README for all private inputs
and explicit live-acceptance boundaries.
