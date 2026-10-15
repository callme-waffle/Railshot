mock_provider "aws" {}
run "scope_csi_to_control_and_owned_filesystem" {
  command = plan
  variables {
    account_id    = "123456789012"
    role_name     = "env-test-control"
    filesystem_id = "fs-0123456789abcdef0"
    cluster_id    = "env-test"
  }
  assert {
    condition     = aws_iam_role_policy.efs.role == "env-test-control" && jsondecode(aws_iam_role_policy.efs.policy).Statement[1].Resource == "arn:aws:elasticfilesystem:ap-northeast-2:123456789012:file-system/fs-0123456789abcdef0"
    error_message = "Only the control role may provision access points on the exact EFS filesystem."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.efs.policy).Statement[3].Condition.StringEquals["aws:ResourceTag/railshot-cluster"] == "env-test"
    error_message = "Access-point deletion must retain the environment tag boundary."
  }
}
