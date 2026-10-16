mock_provider "aws" {}
variables {
  account_id                       = "123456789012"
  environment_id                   = "env-0123456789abcdef01234567"
  owner_id                         = "account:owner"
  cidr                             = "10.72.0.0/16"
  zones                            = ["ap-northeast-2a", "ap-northeast-2c"]
  management_source_cidrs          = ["8.8.8.8/32"]
  observer_source_cidrs            = ["8.8.4.4/32"]
  metrics_ports                    = [30910, 30081]
  runtime_permissions_boundary_arn = "arn:aws:iam::123456789012:policy/railshot-environment-runtime-boundary"
  database_enabled                 = true
}
run "every_runtime_role_is_bounded" {
  command = plan
  assert {
    condition     = length(aws_iam_role.node) == 3 && alltrue([for role in aws_iam_role.node : role.permissions_boundary == var.runtime_permissions_boundary_arn])
    error_message = "Control, worker and database roles must carry the fixed boundary."
  }
}
run "reject_foreign_boundary" {
  command = plan
  variables {
    runtime_permissions_boundary_arn = "arn:aws:iam::999999999999:policy/railshot-environment-runtime-boundary"
  }
  expect_failures = [aws_iam_role.node]
}
run "reject_different_boundary" {
  command = plan
  variables {
    runtime_permissions_boundary_arn = "arn:aws:iam::123456789012:policy/unapproved"
  }
  expect_failures = [var.runtime_permissions_boundary_arn]
}
