# Trusted image publisher only. User source is never executed by this project.
terraform {
  required_version = ">= 1.5.7"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.66.0" }
  }
}
variable "region" { default = "ap-northeast-2" }
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "An explicit reviewed AWS account is required."
  }
}
variable "platform_ref" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{40}$", var.platform_ref))
    error_message = "Use a published 40-character platform commit."
  }
}
variable "name" {
  default = "railshot-release-poc"
  validation {
    condition     = can(regex("^railshot-[a-z0-9-]{3,30}$", var.name))
    error_message = "A bounded railshot resource name is required."
  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags { tags = { Project = "railshot", Component = "trusted-release", ManagedBy = "terraform" } }
}
resource "aws_s3_bucket" "artifacts" { bucket = "${var.name}-${var.account_id}" }
resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "private-poc-evidence-retention"
    status = "Enabled"
    filter {}
    expiration { days = 3 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_ecr_repository" "web" {
  name                 = "${var.name}-web"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}
resource "aws_cloudwatch_log_group" "release" {
  name              = "/aws/codebuild/${var.name}"
  retention_in_days = 3
}
resource "aws_iam_role" "release" {
  name = var.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = { "aws:SourceAccount" = var.account_id }, ArnEquals = {
      "aws:SourceArn" = "arn:aws:codebuild:${var.region}:${var.account_id}:project/${var.name}"
    } }
  }] })
}
resource "aws_iam_role_policy" "release" {
  role = aws_iam_role.release.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.release.arn}:*" },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/bundles/*" },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.artifacts.arn}/receipts/*" },
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"], Resource = aws_ecr_repository.web.arn }
  ] })
}
resource "aws_codebuild_project" "release" {
  name                   = var.name
  service_role           = aws_iam_role.release.arn
  source_version         = var.platform_ref
  build_timeout          = 15
  queued_timeout         = 10
  concurrent_build_limit = 1
  auto_retry_limit       = 0
  project_visibility     = "PRIVATE"
  artifacts { type = "NO_ARTIFACTS" }
  source {
    type            = "GITHUB"
    location        = "https://github.com/Jasmin-Softbank/Jasmin.git"
    git_clone_depth = 1
    buildspec       = "ci/workflows/codebuild-release.yml"
  }
  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = false
    environment_variable {
      name  = "RAILSHOT_SOURCE_REF"
      value = var.platform_ref
    }
    environment_variable {
      name  = "RAILSHOT_ARTIFACT_BUCKET"
      value = aws_s3_bucket.artifacts.id
    }
    environment_variable {
      name  = "RAILSHOT_REGISTRY_PREFIX"
      value = trimsuffix(aws_ecr_repository.web.repository_url, "-web")
    }
  }
  logs_config {
    cloudwatch_logs { group_name = aws_cloudwatch_log_group.release.name }
  }
  depends_on = [aws_iam_role_policy.release, aws_s3_bucket_public_access_block.artifacts]
}
output "codebuild_descriptor" {
  value = {
    schema_version  = 1
    project_name    = aws_codebuild_project.release.name
    region          = var.region
    account_id      = var.account_id
    platform_ref    = var.platform_ref
    artifact_bucket = aws_s3_bucket.artifacts.id
    registry_prefix = trimsuffix(aws_ecr_repository.web.repository_url, "-web")
    role_arn        = aws_iam_role.release.arn
    scope           = "trusted-publish-only; one service named web; no GitOps/Argo/SDK access"
  }
}
