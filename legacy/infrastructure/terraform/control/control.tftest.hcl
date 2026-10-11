# Terraform >= 1.7; every run is a mock-provider plan, with no AWS API calls.
# Run after init: terraform test -filter=control.tftest.hcl
mock_provider "aws" {
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-085e5a8268cf2b206" }
  }
  mock_data "aws_ami" {
    defaults = { id = "ami-0123456789abcdef0" }
  }
}

variables {
  account_id = "721622471953"
  region     = "ap-northeast-2"
  vpc_id     = "vpc-085e5a8268cf2b206"
  subnet_id  = "subnet-0123456789abcdef0"
  ami_id     = "ami-0123456789abcdef0"
}

run "default_has_no_registered_runtime_permission" {
  command = plan
  assert {
    condition     = length(aws_iam_role_policy.registered_runtimes) == 0
    error_message = "Default settings must not grant access to registered runtime instances."
  }
}

run "instance_ids_do_not_enable_executor" {
  command = plan
  variables {
    enable_product_executor         = false
    product_metadata_hop_limit      = 3
    registered_runtime_instance_ids = ["i-0123456789abcdef0"]
  }
  assert {
    condition     = length(aws_iam_role_policy.registered_runtimes) == 0
    error_message = "Explicit instance IDs must not bypass the executor opt-in."
  }
  assert {
    condition     = aws_instance.control.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "An opted-out executor must retain hop limit 1 even when the product setting is 3."
  }
}

run "enabled_executor_without_instances_has_no_permission" {
  command = plan
  variables { enable_product_executor = true }
  assert {
    condition     = length(aws_iam_role_policy.registered_runtimes) == 0
    error_message = "An empty registration must not expand to wildcard runtime access."
  }
  assert {
    condition     = aws_instance.control.metadata_options[0].http_put_response_hop_limit == 2
    error_message = "An enabled executor must default to hop limit 2."
  }
}

run "enabled_executor_can_select_three_hops" {
  command = plan
  variables {
    enable_product_executor    = true
    product_metadata_hop_limit = 3
  }
  assert {
    condition     = aws_instance.control.metadata_options[0].http_put_response_hop_limit == 3
    error_message = "An enabled executor must honor the explicit three-hop setting."
  }
}

run "reject_unreviewed_metadata_hop_limit" {
  command = plan
  variables {
    enable_product_executor    = true
    product_metadata_hop_limit = 4
  }
  expect_failures = [var.product_metadata_hop_limit]
}

run "enabled_executor_grants_exact_instances_and_document_check" {
  command = plan
  variables {
    enable_product_executor         = true
    registered_runtime_instance_ids = ["i-11111111111111111", "i-0123456789abcdef0"]
  }
  assert {
    condition = jsondecode(aws_iam_role_policy.registered_runtimes[0].policy) == {
      Version = "2012-10-17"
      Statement = [{
        Effect = "Allow"
        Action = "ssm:StartSession"
        Resource = [
          "arn:aws:ec2:ap-northeast-2:721622471953:instance/i-0123456789abcdef0",
          "arn:aws:ec2:ap-northeast-2:721622471953:instance/i-11111111111111111",
        ]
        Condition = {
          StringEquals = { "aws:RequestedRegion" = "ap-northeast-2" }
          BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" }
        }
      }]
    }
    error_message = "Registered runtimes may grant only StartSession on exact regional/account EC2 ARNs, subject to the document-access check."
  }
  assert {
    condition = [for statement in jsondecode(aws_iam_role_policy.product_executor[0].policy).Statement : statement
      if statement.Sid == "UseFixedForwardingDocument"] == [{
        Sid      = "UseFixedForwardingDocument"
        Effect   = "Allow"
        Action   = "ssm:StartSession"
        Resource = "arn:aws:ssm:ap-northeast-2::document/AWS-StartPortForwardingSession"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = "ap-northeast-2" }
        }
    }]
    error_message = "The executor's document grant must remain the fixed port-forwarding document in the registered region."
  }
  assert {
    condition = length([for statement in jsondecode(aws_iam_role_policy.product_executor[0].policy).Statement : statement
      if try(statement.Sid == "StartForwardingOnOwnedInstances" &&
      statement.Condition.BoolIfExists["ssm:SessionDocumentAccessCheck"] == "true" && !contains(keys(statement.Condition), "Bool"), false)
    ]) == 1
    error_message = "Owned-instance forwarding must tolerate absent document-check context while rejecting an explicit false value."
  }
  assert {
    condition = length([for statement in jsondecode(aws_iam_policy.product_edge[0].policy).Statement : statement
      if try(statement.Sid == "ReadEdgeRegion" && contains(statement.Action, "elasticloadbalancing:DescribeListenerAttributes") &&
      statement.Condition.StringEquals["aws:RequestedRegion"] == "ap-northeast-2", false)
    ]) == 1
    error_message = "The registered-region edge read grant must support the provider's listener-attribute refresh."
  }
}

run "reject_wildcard_instance_id" {
  command = plan
  variables {
    enable_product_executor         = true
    registered_runtime_instance_ids = ["i-*"]
  }
  expect_failures = [var.registered_runtime_instance_ids]
}

run "reject_more_than_twenty_instances" {
  command = plan
  variables {
    enable_product_executor         = true
    registered_runtime_instance_ids = [for n in range(21) : format("i-%017x", n)]
  }
  expect_failures = [var.registered_runtime_instance_ids]
}
