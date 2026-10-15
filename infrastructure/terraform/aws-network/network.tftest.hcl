mock_provider "aws" {}
variables {
  management_source_cidrs = ["8.8.8.8/32"]
  observer_source_cidrs = ["8.8.4.4/32"]
  metrics_ports = [30910, 30081]
}
run "new_isolated_network" {
  command = plan
  variables {
    account_id     = "123456789012"
    environment_id = "env-0123456789abcdef01234567"
    owner_id       = "account:owner"
    cidr           = "10.72.0.0/16"
    zones          = ["ap-northeast-2a", "ap-northeast-2c"]
  }
  assert {
    condition     = length(aws_subnet.environment) == 2 && alltrue([for s in aws_subnet.environment : s.map_public_ip_on_launch])
    error_message = "Exactly two explicitly public egress subnets must be planned."
  }
  assert {
    condition     = length(aws_iam_role.node) == 2 && length(aws_security_group.targets.ingress) == 0
    error_message = "Separate control/worker roles and a rule-only edge target group are required."
  }
  assert {
    condition     = alltrue([for r in aws_security_group.cluster.ingress : r.self && length(r.cidr_blocks) == 0])
    error_message = "Cluster ingress must never be open to public CIDRs."
  }
  assert {
    condition = alltrue([for r in aws_security_group.control_api.ingress : r.from_port == 6443 && r.to_port == 6443 && r.cidr_blocks == ["8.8.8.8/32"]]) && alltrue([for r in aws_security_group.metrics.ingress : contains([30910, 30081], r.from_port) && r.from_port == r.to_port && r.cidr_blocks == ["8.8.4.4/32"]])
    error_message = "Control API and exporter ingress must use their separately registered sources."
  }
}
run "database_backup_role_is_not_a_worker_role" {
  command = plan
  variables {
    account_id       = "123456789012"
    environment_id   = "env-0123456789abcdef01234567"
    owner_id         = "account:owner"
    cidr             = "10.72.0.0/16"
    zones            = ["ap-northeast-2a", "ap-northeast-2c"]
    database_enabled = true
    database_backup  = { bucket = "owned-backup-bucket", prefix = "postgres/env-0123456789abcdef01234567" }
  }
  assert {
    condition     = length(aws_iam_role.node) == 3 && aws_iam_role_policy.database_backup[0].role == aws_iam_role.node["database"].id
    error_message = "Backup object permissions must attach only to the database role."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.database_backup[0].policy).Statement[2].Resource == "arn:aws:s3:::owned-backup-bucket/postgres/env-0123456789abcdef01234567/*"
    error_message = "Database S3 permissions must be confined to the approved environment prefix."
  }
}
