# Separate bootstrap authority: one fixed release document on the existing control VM.
terraform {
  required_version = ">= 1.5.7"
  backend "local" {}
  required_providers {
    aws    = { source = "hashicorp/aws", version = "= 6.66.0" }
    google = { source = "hashicorp/google", version = "= 8.5.0" }
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
    error_message = "One exact reviewed release ref is required."
  }
}

data "aws_iam_openid_connect_provider" "github" {
  arn = "arn:aws:iam::721622471953:oidc-provider/token.actions.githubusercontent.com"
}

locals {
  entrypoint = file("${path.module}/../../../deployment/scripts/release_host.py")
  document = jsonencode({
    schemaVersion = "2.2"
    description   = "Approved Railshot CI runtime or three-provider release; source SHA256 ${sha256(local.entrypoint)}"
    parameters = {
      Scope                 = { type = "String", interpolationType = "ENV_VAR", default = "multicloud", allowedValues = ["ci-runtime", "multicloud"] }
      PublicationRunId      = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[1-9][0-9]{0,19}$" }
      PublicationRunAttempt = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[1-9][0-9]{0,19}$" }
      SourceSha             = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[a-f0-9]{40}$" }
      Revision              = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[a-f0-9]{40}$" }
      DashboardDigest       = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[a-f0-9]{64}$" }
      ApiDigest             = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[a-f0-9]{64}$" }
      RunnerDigest          = { type = "String", interpolationType = "ENV_VAR", allowedPattern = "^[a-f0-9]{64}$" }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "releaseThreeProviders"
      inputs = {
        timeoutSeconds = "4600"
        runCommand = concat([
          "set -eu",
          "install -d -m 700 /var/lib/railshot-release",
          "python3 - '${var.trusted_ref}' <<'RAILSHOT_RELEASE_PY'"
        ], split("\n", local.entrypoint), ["RAILSHOT_RELEASE_PY"])
      }
    }]
  })
}

resource "aws_ssm_document" "release" {
  name            = "Railshot-MulticloudRelease"
  document_type   = "Command"
  document_format = "JSON"
  content         = local.document
  lifecycle {
    postcondition {
      condition     = self.hash_type == "Sha256"
      error_message = "The workflow requires a pinned SHA256 document."
    }
  }
}

resource "aws_iam_role" "release" {
  name                 = "railshot-multicloud-release"
  max_session_duration = 7200
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:Jasmin-Softbank@335003159/Railshot@1400202256:ref:${var.trusted_ref}"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "release" {
  name = "one-release-document-on-one-control-instance"
  role = aws_iam_role.release.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ssm:SendCommand", Resource = [
        aws_ssm_document.release.arn,
        "arn:aws:ec2:ap-northeast-2:721622471953:instance/i-033ae2db907fde68e"
      ] },
      { Effect = "Allow", Action = "ssm:GetCommandInvocation", Resource = "*" }
    ]
  })
}

output "workflow_variables" {
  value = {
    RAILSHOT_RELEASE_DOCUMENT_VERSION = aws_ssm_document.release.document_version
    RAILSHOT_RELEASE_DOCUMENT_SHA256  = aws_ssm_document.release.hash
  }
}
