# Independent private administrator CI VM. Reuses the shared Ansible/worker code.
terraform {
  required_version = ">= 1.5.7"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.66.0" }
  }
}
variable "region" { default = "ap-northeast-2" }
variable "name" {
  type    = string
  default = "railshot-ci-poc"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,62}$", var.name))
    error_message = "Use a lowercase resource and target name."
  }
}
variable "instance_name" {
  description = "EC2 display Name only; keep name stable for IAM, security group and target identity."
  type        = string
  default     = null
  validation {
    condition     = var.instance_name == null ? true : can(regex("^[a-z][a-z0-9-]{0,62}$", var.instance_name))
    error_message = "Use a lowercase EC2 display name."
  }
}
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "An explicit reviewed AWS account is required."
  }
}
variable "ami_id" {
  type = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Use an exact regional Canonical Ubuntu 24.04 amd64 AMI."
  }
}
variable "platform_ref" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{40}$", var.platform_ref))
    error_message = "Use a published 40-character platform commit."
  }
}
variable "archive_sha256" {
  type = string
  validation {
    condition     = can(regex("^[a-f0-9]{64}$", var.archive_sha256))
    error_message = "Hash the exact codeload archive for the pinned commit."
  }
}
variable "existing_user_data_base64" {
  description = "Exact existing EC2 gzip user-data bytes, verified against AWS and state; null renders a new bootstrap."
  type        = string
  default     = null
  validation {
    condition = var.existing_user_data_base64 == null ? true : (
      length(var.existing_user_data_base64) <= 21848 && length(var.existing_user_data_base64) % 4 == 0 &&
      can(regex("^H4sI[A-Za-z0-9+/]*={0,2}$", var.existing_user_data_base64))
    )
    error_message = "Use the verified existing gzip/base64 EC2 user-data, or null for a new bootstrap."
  }
}
variable "admin_ssh_public_key" {
  type = string
  validation {
    condition     = can(regex("^ssh-ed25519 [A-Za-z0-9+/]+={0,2}$", var.admin_ssh_public_key))
    error_message = "Provide only the public Ed25519 key, without comments or private key material."
  }
}
variable "stop_at" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", var.stop_at)) && can(timecmp(var.stop_at, var.stop_at))
    error_message = "An absolute UTC stop deadline is required; bootstrap refuses expired use."
  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags { tags = { Project = "railshot", Component = "private-ci", ManagedBy = "terraform" } }
}
locals { name = var.name }
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}
data "aws_ami" "ubuntu" {
  owners = ["099720109477"]
  filter {
    name   = "image-id"
    values = [var.ami_id]
  }
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}
variable "control_security_group_id" {
  description = "Reviewed operations cluster peer SG; null keeps this module standalone."
  type        = string
  default     = null
  validation {
    condition     = var.control_security_group_id == null ? true : can(regex("^sg-[0-9a-f]{17}$", var.control_security_group_id))
    error_message = "Use an exact reviewed peer security group ID."
  }
}
locals {
  build_peer_health = var.control_security_group_id == null ? [] : [
    { protocol = "udp", from_port = 8472, to_port = 8472, description = "Cilium VXLAN node overlay" },
    { protocol = "tcp", from_port = 4240, to_port = 4240, description = "Cilium node health" },
    { protocol = "icmp", from_port = 8, to_port = 0, description = "Cilium ICMP echo health; stateful reply" }
  ]
  build_peer_api = var.control_security_group_id == null ? [] : [
    { protocol = "tcp", from_port = 6443, to_port = 6443, description = "K3s agent supervisor/API" }
  ]
}

resource "aws_security_group" "ci" {
  name        = local.name
  description = "No ingress. Administrator SSH traverses authenticated SSM only."
  vpc_id      = data.aws_vpc.default.id
  # Keep rules inline with the existing owner; do not mix standalone SG rules.
  # Empty by default. A reviewed peer only opens node overlay/health and API.
  ingress = [for rule in local.build_peer_health : {
    description      = rule.description
    from_port        = rule.from_port
    to_port          = rule.to_port
    protocol         = rule.protocol
    security_groups  = [var.control_security_group_id]
    cidr_blocks      = []
    ipv6_cidr_blocks = []
    prefix_list_ids  = []
    self             = false
  }]
  dynamic "egress" {
    for_each = concat(local.build_peer_health, local.build_peer_api)
    content {
      description     = egress.value.description
      from_port       = egress.value.from_port
      to_port         = egress.value.to_port
      protocol        = egress.value.protocol
      security_groups = [var.control_security_group_id]
    }
  }
  dynamic "egress" {
    for_each = [80, 443]
    content {
      from_port   = egress.value
      to_port     = egress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
  dynamic "egress" {
    for_each = ["udp", "tcp"]
    content {
      from_port   = 53
      to_port     = 53
      protocol    = egress.value
      cidr_blocks = ["1.1.1.1/32", "8.8.8.8/32"]
    }
  }
}
resource "aws_iam_role" "ci" {
  name = local.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.ci.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "deny_credentials" {
  role = aws_iam_role.ci.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Deny", Action = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "secretsmanager:GetSecretValue"], Resource = "*"
  }] })
}
resource "aws_iam_instance_profile" "ci" {
  name = local.name
  role = aws_iam_role.ci.name
}
resource "aws_instance" "ci" {
  ami                                  = data.aws_ami.ubuntu.id
  instance_type                        = "t3.xlarge"
  subnet_id                            = sort(data.aws_subnets.default.ids)[0]
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.ci.id]
  iam_instance_profile                 = aws_iam_instance_profile.ci.name
  instance_initiated_shutdown_behavior = "stop"
  credit_specification { cpu_credits = "standard" }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 60
    encrypted             = true
    delete_on_termination = false
  }
  user_data_base64 = var.existing_user_data_base64 != null ? var.existing_user_data_base64 : base64gzip(templatefile("${path.module}/cloud-init.yaml.tftpl", {
    platform_ref = var.platform_ref, archive_sha256 = var.archive_sha256,
    public_key   = var.admin_ssh_public_key, stop_at = var.stop_at
  }))
  user_data_replace_on_change = false
  tags                        = { Name = coalesce(var.instance_name, local.name) }
  depends_on                  = [aws_iam_role_policy_attachment.ssm, aws_iam_role_policy.deny_credentials]
}
output "node_descriptor" {
  value = {
    schema_version = 1, provider_kind = "aws", target_id = local.name,
    instance_id    = aws_instance.ci.id, resource_id = aws_instance.ci.arn,
    owner_ref      = "aws/${var.account_id}", execution_driver = "ssm-ssh",
    addresses      = { public = aws_instance.ci.public_ip, private = aws_instance.ci.private_ip },
    compute        = { machine_type = "t3.xlarge", vcpu = 4, memory_gib = 16, source = "configured" },
    bootstrap      = { status = "UNVERIFIED", platform_ref = var.platform_ref, archive_sha256 = var.archive_sha256 },
    transport_ref  = { kind = "ssm-ssh", region = var.region, user = "railshot-operator", port = 22 },
    worker         = { python = "/usr/bin/python3", source = null, root = "/var/lib/railshot-console", status = "NOT_CONFIGURED" },
    lifecycle      = { stop_at = var.stop_at, action = "guest-systemd-STOP", retained_boot_disk_gib = 60 }
  }
}
