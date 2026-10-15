mock_provider "aws" {}
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
}
