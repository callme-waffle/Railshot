# Isolated bootstrap state: no control instance, customer host, existing role or OIDC-provider mutation.
terraform {
  required_version = ">= 1.5.7"
  backend "local" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.66.0" }
  }
}

provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["721622471953"]
}

variable "trusted_ref" {
  type    = string
  default = "refs/heads/integration/team-assembly-20261002"
  validation {
    condition     = contains(["refs/heads/integration/team-assembly-20261002", "refs/heads/main"], var.trusted_ref)
    error_message = "Select exactly one reviewed platform release ref, never a wildcard."
  }
}

data "aws_iam_openid_connect_provider" "github" {
  arn = "arn:aws:iam::721622471953:oidc-provider/token.actions.githubusercontent.com"
}

locals {
  verifier = file("${path.module}/../../../deployment/scripts/verify-platform.py")
  document = jsonencode({
    schemaVersion = "2.2"
    description   = "Fixed read-only Railshot platform verification; source SHA256 ${sha256(local.verifier)}"
    parameters = {
      Revision        = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[a-f0-9]{40}$" }
      DashboardDigest = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[a-f0-9]{64}$" }
      ApiDigest       = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[a-f0-9]{64}$" }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "verifyPlatform"
      inputs = {
        timeoutSeconds = "90"
        runCommand = concat([
          "set -eu",
          "python3 - local --revision \"$SSM_Revision\" --dashboard-digest \"$SSM_DashboardDigest\" --api-digest \"$SSM_ApiDigest\" <<'RAILSHOT_VERIFY_PY'"
        ], split("\n", local.verifier), ["RAILSHOT_VERIFY_PY"])
      }
    }]
  })
}

resource "aws_ssm_document" "verify" {
  name            = "Railshot-VerifyPlatform"
  document_type   = "Command"
  document_format = "JSON"
  content         = local.document
  lifecycle {
    postcondition {
      condition     = self.hash_type == "Sha256"
      error_message = "Workflow verification requires the AWS document SHA256 hash."
    }
  }
}

resource "aws_iam_role" "verify" {
  name                 = "railshot-platform-verifier"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        # IDs belong to the verified immutable sub string, not unsupported custom IAM claim keys.
        "token.actions.githubusercontent.com:sub" = "repo:Jasmin-Softbank@335003159/Railshot@1400202256:ref:${var.trusted_ref}"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "verify" {
  name = "one-read-only-document-on-one-control-instance"
  role = aws_iam_role.verify.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ssm:SendCommand", Resource = [
        aws_ssm_document.verify.arn,
        "arn:aws:ec2:ap-northeast-2:721622471953:instance/i-033ae2db907fde68e"
      ] },
      # AWS does not provide resource-level scope for this status/output read action.
      { Effect = "Allow", Action = "ssm:GetCommandInvocation", Resource = "*" }
    ]
  })
}

output "workflow_variables" {
  value = {
    RAILSHOT_PLATFORM_VERIFY_REF              = var.trusted_ref
    RAILSHOT_PLATFORM_VERIFY_ROLE_ARN         = aws_iam_role.verify.arn
    RAILSHOT_PLATFORM_VERIFY_DOCUMENT_VERSION = aws_ssm_document.verify.document_version
    RAILSHOT_PLATFORM_VERIFY_DOCUMENT_SHA256  = aws_ssm_document.verify.hash
  }
}

output "verifier_source_sha256" { value = sha256(local.verifier) }
