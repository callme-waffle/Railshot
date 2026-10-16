terraform {
  required_version = ">= 1.7"
  required_providers { aws = { source = "hashicorp/aws", version = "= 6.66.0" } }
}
variable "account_id" { type = string }
variable "role_name" { type = string }
variable "filesystem_id" { type = string }
variable "cluster_id" { type = string }
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = [var.account_id]
}
resource "aws_iam_role_policy" "efs" {
  name = "railshot-efs-${var.cluster_id}"
  role = var.role_name
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["elasticfilesystem:DescribeAccessPoints", "elasticfilesystem:DescribeFileSystems", "elasticfilesystem:DescribeMountTargets", "ec2:DescribeAvailabilityZones"], Resource = "*" },
    { Effect = "Allow", Action = "elasticfilesystem:CreateAccessPoint", Resource = "arn:aws:elasticfilesystem:ap-northeast-2:${var.account_id}:file-system/${var.filesystem_id}", Condition = { StringEquals = { "aws:RequestTag/railshot-cluster" = var.cluster_id }, Null = { "aws:RequestTag/efs.csi.aws.com/cluster" = "false" } } },
    { Effect = "Allow", Action = "elasticfilesystem:TagResource", Resource = "arn:aws:elasticfilesystem:ap-northeast-2:${var.account_id}:access-point/*", Condition = { StringEquals = { "elasticfilesystem:CreateAction" = "CreateAccessPoint", "aws:RequestTag/railshot-cluster" = var.cluster_id } } },
    { Effect = "Allow", Action = "elasticfilesystem:DeleteAccessPoint", Resource = "arn:aws:elasticfilesystem:ap-northeast-2:${var.account_id}:access-point/*", Condition = { StringEquals = { "aws:ResourceTag/railshot-cluster" = var.cluster_id }, Null = { "aws:ResourceTag/efs.csi.aws.com/cluster" = "false" } } }
  ] })
}
