terraform {
  required_version = ">= 1.7"
  required_providers { aws = { source = "hashicorp/aws", version = "= 6.66.0" } }
}
variable "account_id" { type = string }
variable "environment_id" { type = string }
variable "owner_id" { type = string }
variable "cidr" { type = string }
variable "zones" { type = list(string) }
variable "management_source_cidrs" { type = list(string) }
variable "observer_source_cidrs" { type = list(string) }
variable "metrics_ports" { type = list(number) }
variable "database_enabled" {
  type    = bool
  default = false
}
variable "database_backup" {
  type    = object({ bucket = string, prefix = string })
  default = null
}
provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = [var.account_id]
  default_tags { tags = { ManagedBy = "railshot", Environment = var.environment_id, Owner = var.owner_id } }
}
resource "aws_vpc" "environment" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
}
resource "aws_internet_gateway" "environment" { vpc_id = aws_vpc.environment.id }
resource "aws_subnet" "environment" {
  for_each                = { for i, zone in var.zones : zone => i }
  vpc_id                  = aws_vpc.environment.id
  cidr_block              = cidrsubnet(var.cidr, 8, each.value)
  availability_zone       = each.key
  map_public_ip_on_launch = true
}
resource "aws_route_table" "egress" {
  vpc_id = aws_vpc.environment.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.environment.id
  }
}
resource "aws_route_table_association" "environment" {
  for_each       = aws_subnet.environment
  subnet_id      = each.value.id
  route_table_id = aws_route_table.egress.id
}
resource "aws_security_group" "cluster" {
  name_prefix = "${var.environment_id}-cluster-"
  vpc_id      = aws_vpc.environment.id
  ingress {
    protocol  = "-1"
    from_port = 0
    to_port   = 0
    self      = true
  }
  egress {
    protocol  = "-1"
    from_port = 0
    to_port   = 0
    self      = true
  }
  # EFS mount targets have their own restrictive source rules.
  egress {
    protocol    = "tcp"
    from_port   = 2049
    to_port     = 2049
    cidr_blocks = [var.cidr]
  }
  egress {
    protocol    = "tcp"
    from_port   = 5432
    to_port     = 5432
    cidr_blocks = [var.cidr]
  }
}
resource "aws_security_group" "targets" {
  name_prefix = "${var.environment_id}-edge-"
  vpc_id      = aws_vpc.environment.id
  # Rules are owned exclusively by the separate edge Terraform state.
}
resource "aws_security_group" "control_api" {
  name_prefix = "${var.environment_id}-api-"
  vpc_id      = aws_vpc.environment.id
  ingress {
    protocol    = "tcp"
    from_port   = 6443
    to_port     = 6443
    cidr_blocks = var.management_source_cidrs
  }
}
resource "aws_security_group" "metrics" {
  name_prefix = "${var.environment_id}-metrics-"
  vpc_id      = aws_vpc.environment.id
  dynamic "ingress" {
    for_each = toset(var.metrics_ports)
    content {
      protocol    = "tcp"
      from_port   = ingress.value
      to_port     = ingress.value
      cidr_blocks = var.observer_source_cidrs
    }
  }
}
resource "aws_security_group" "database" {
  name_prefix = "${var.environment_id}-database-"
  vpc_id      = aws_vpc.environment.id
  ingress {
    protocol        = "tcp"
    from_port       = 5432
    to_port         = 5432
    security_groups = [aws_security_group.cluster.id]
  }
}
resource "aws_iam_role" "node" {
  for_each           = toset(var.database_enabled ? ["control", "worker", "database"] : ["control", "worker"])
  name               = "${var.environment_id}-${each.key}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  for_each   = aws_iam_role.node
  role       = each.value.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "deny_parameters" {
  for_each = aws_iam_role.node
  role     = each.value.id
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Deny", Action = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory"], Resource = "*" }] })
}
resource "aws_iam_instance_profile" "node" {
  for_each = aws_iam_role.node
  name     = each.value.name
  role     = each.value.name
}
output "network" {
  value = { vpc_id = aws_vpc.environment.id, subnets = { for z, s in aws_subnet.environment : z => s.id }, cluster_sg = aws_security_group.cluster.id, target_sg = aws_security_group.targets.id, database_sg = aws_security_group.database.id, control_api_sg = aws_security_group.control_api.id, metrics_sg = aws_security_group.metrics.id, profiles = { for k, p in aws_iam_instance_profile.node : k => p.name }, control_role = aws_iam_role.node["control"].name }
}

resource "aws_iam_role_policy" "database_backup" {
  count = var.database_enabled && var.database_backup != null ? 1 : 0
  role  = aws_iam_role.node["database"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetBucketLocation"], Resource = "arn:aws:s3:::${var.database_backup.bucket}" },
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = "arn:aws:s3:::${var.database_backup.bucket}", Condition = { StringLike = { "s3:prefix" = ["${var.database_backup.prefix}", "${var.database_backup.prefix}/*"] } } },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], Resource = "arn:aws:s3:::${var.database_backup.bucket}/${var.database_backup.prefix}/*" }
  ] })
}
