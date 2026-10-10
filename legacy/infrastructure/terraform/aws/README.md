# AWS customer app host

This module prepares one Ubuntu 24.04 amd64 host and retained encrypted data disk. It does not install K3s, Cilium, Argo CD, WireGuard or applications. The platform CI/CD host uses the separate `terraform/ci` module. Use the [administrator executor](../../providers/terraform_tools/README.md) for a reviewed, account-bound saved plan.

For a new customer node behind the separately managed ALB, set:

| Input | Required selection |
| --- | --- |
| `account_id`, `region`, `ami_id` | Registered account, region and exact Canonical Ubuntu image |
| `product_environment` | `true` for a new product target: creation tags `ProjectOwner=railshot-product` and `Target=<target_id>`. Default `false` preserves legacy tags. |
| `vpc_id`, `subnet_id` | Both explicit existing IDs; subnet must belong to the VPC |
| `http_enabled`, `https_enabled` | Both `false`; no direct public web ingress |
| `additional_security_group_ids` | Reviewed groups for ALB NodePort and management/cluster traffic; same VPC |
| `create_ci_plan_role` | `false`; avoid recreating the account-level GitHub OIDC identity on each customer host |
| `existing_instance_profile` | Optional reviewed EC2 instance profile name; reuse skips node IAM role, policies and instance profile creation. The executor needs PassRole for its exact role. |
| `allocate_eip` | `false` when a subnet-assigned public IP provides egress; routes and public-IP policy remain operator prerequisites |
| `operator_ssh_public_key` | One OpenSSH public key; creates `railshot-operator` with locked password and noninteractive sudo at first boot |
| `initialize_empty_data_disk` | `true` only for a reviewed new blank module-created disk |

`node_security_group_id`, `vpc_id`, `subnet_id` and `instance_id` outputs support the separately owned edge rules. `node_descriptor.transport_ref` remains `ssm:<region>:<instance-id>`. It does not open TCP22; the operator uses authenticated SSM forwarding and a separately verified SSH host key. The managed node SSM role retains its explicit Parameter Store deny. When reusing an existing instance profile, its owner must verify the account, SSM permissions and equivalent Parameter Store deny; this module does not modify its IAM policies. Private SSH credentials and WireGuard keys are never Terraform inputs.

`existing_instance_profile=null` preserves the managed IAM defaults through moved blocks. Setting it on an existing managed target would remove that target's IAM resources from this configuration and requires separate lifecycle review. Use reuse for new targets; do not switch existing targets merely to adopt this option.

`product_environment=true` merges the two product tags into provider `default_tags`, retaining `Project=railshot` and `ManagedBy=terraform`. This covers the module-created instance, root EBS, separate data EBS, security group and optional EIP at creation; resource-specific name/retention tags remain. Existing VPCs, subnets, additional security groups and reused IAM profiles are read or referenced without retagging. Implicit EC2 network interfaces and inline security group rules are outside this tag contract. Leave this option false on legacy targets; enabling it on an existing target would plan tag changes.

The pinned [AWS provider 6.66.0 creation implementation](https://github.com/hashicorp/terraform-provider-aws/blob/v6.66.0/internal/service/ec2/ec2_instance.go#L1152-L1195) sends provider defaults in both instance and volume `RunInstances.TagSpecifications`. This supplies the root EBS tags during creation. We omit `root_block_device.tags`, which the [provider documentation](https://github.com/hashicorp/terraform-provider-aws/blob/v6.66.0/website/docs/r/instance.html.markdown#ebs-ephemeral-and-root-block-devices) says applies through a separate post-creation API, and `volume_tags`, which conflicts with separately managed volume tags. Provider-version upgrades must preserve this creation-time behavior before using tag-restricted IAM policies. Source and offline checks establish the configuration contract; the new resources' actual tags still require readback after an approved apply.

Legacy defaults remain: default VPC/subnet selection, public HTTP, EIP and read-only GitHub CI role. Moved blocks preserve their Terraform resource identities when these defaults stay enabled. Disabling resources on an existing target can destroy them and must be reviewed; this configuration is intended for a new customer target. Public IP addresses are egress references, not proof that an application URL exists. The legacy `app_domain` output is null when no EIP is requested.

Bootstrap mounts only the expected data disk and records host preparation. Existing ext4 is reused; unknown or conflicting disk state fails closed. Existing guests do not re-run cloud-init merely because the key/configuration input changed. Rotate credentials through the separate management path. Terraform-created resources and descriptor output do not establish guest or Kubernetes readiness.

Offline template and validation checks:

```sh
uv run --python 3.13 --with pyyaml python infrastructure/terraform/aws/test_bootstrap.py
```

For a DB host, set `purpose: database` under an approved `database_cluster` target. The data disk mounts at `/var/lib/postgresql`, while the default runtime purpose retains `/var/lib/rancher`. Public HTTP/HTTPS ingress is suppressed for DB hosts. Explicit `database_ingress` / `database_egress` rules accept only TCP 5432/2379/2380/8008 with RFC1918 /16-/32 CIDRs. Register only actual application/proxy/cluster peers and use the same existing VPC/subnet. The common executor's `access.py` can bind a new SSH host key through STS/EC2/SSM without SSH trust-on-first-use. These additions configure a host and access rules; database installation and readiness remain Ansible responsibilities.

`max_run_duration_seconds` is optional (default null), from 1800 to 604800 whole seconds. For a new instance, cloud-init starts an enabled systemd timer before disk preparation; each timer activation triggers `/sbin/poweroff` after that interval. The default EC2 guest shutdown behavior stops the instance and preserves disks. This is a per-start guest timer, not an absolute provider-enforced deadline, and depends on cloud-init reaching runcmd and systemd continuing to run. It does not remove retained volumes or replace operator cleanup. Existing guests are not reconfigured by changing Terraform input.
