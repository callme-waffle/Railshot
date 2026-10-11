terraform {
  required_version = ">= 1.7.0, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
variable "cluster_id" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "vpc_id" { type = string }
variable "workers" {
  type = map(object({ instance_id = string, private_ip = string, az = string, subnet_id = string }))
  validation {
    condition     = length(var.workers) == 2
    error_message = "The first profile requires two workers."
  }
}
data "aws_instance" "worker" {
  for_each    = var.workers
  instance_id = each.value.instance_id
}
data "aws_subnet" "worker" {
  for_each = var.workers
  id       = each.value.subnet_id
}
locals {
  tags = { managed-by = "railshot", railshot-cluster = var.cluster_id }
  # EFS requires exactly one mount target per AZ, including same-AZ workers.
  zone_subnets = { for az in distinct([for worker in var.workers : worker.az]) : az => sort([for worker in var.workers : worker.subnet_id if worker.az == az])[0] }
}
resource "aws_efs_file_system" "cluster" {
  creation_token = "railshot-${var.cluster_id}"
  encrypted      = true
  tags           = local.tags
  lifecycle {
    prevent_destroy = true
    precondition {
      condition = alltrue([for key, worker in var.workers :
        data.aws_instance.worker[key].instance_state == "running" &&
        data.aws_instance.worker[key].private_ip == worker.private_ip &&
        data.aws_instance.worker[key].availability_zone == worker.az &&
        data.aws_instance.worker[key].subnet_id == worker.subnet_id &&
        data.aws_subnet.worker[key].vpc_id == var.vpc_id &&
        data.aws_subnet.worker[key].availability_zone == worker.az
      ])
      error_message = "Observed workers must match admitted private addresses, subnets, AZs and VPC."
    }
  }
}
resource "aws_security_group" "nfs" {
  name_prefix = "railshot-${var.cluster_id}-efs-"
  vpc_id      = var.vpc_id
  tags        = local.tags
  ingress {
    description = "Only the two admitted compute nodes"
    protocol    = "tcp"
    from_port   = 2049
    to_port     = 2049
    cidr_blocks = [for worker in var.workers : "${worker.private_ip}/32"]
  }
  # No egress needed: security groups allow response packets statefully.
}
resource "aws_efs_mount_target" "worker_az" {
  for_each        = local.zone_subnets
  file_system_id  = aws_efs_file_system.cluster.id
  subnet_id       = each.value
  security_groups = [aws_security_group.nfs.id]
}
output "backend_id" {
  value      = aws_efs_file_system.cluster.id
  depends_on = [aws_efs_mount_target.worker_az]
}
output "readiness" { value = "configured; independent EFS/CSI/PVC observations required" }
