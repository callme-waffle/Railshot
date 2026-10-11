# Restored existing operations node; customer app nodes and isolated CI builder are separate.
terraform {
  required_version = ">= 1.5.7"
  backend "local" {}
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.66.0" }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags { tags = { Project = "railshot", Component = "control-poc", ManagedBy = "terraform" } }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Use the existing registered AWS account."
  }
}
variable "enable_product_executor" {
  type        = bool
  default     = false
  description = "Operator opt-in for the reviewed product executor. Bootstrap must verify the fixed Cilium metadata deny policy before applying this setting."
}
variable "product_metadata_hop_limit" {
  type        = number
  default     = 2
  description = "Verified control-node pod return path. Use 3 only for an extra Cilium routing hop; the metadata deny policy remains required."
  validation {
    condition     = contains([2, 3], var.product_metadata_hop_limit)
    error_message = "Use only the verified two- or three-hop control path."
  }
}
variable "vpc_id" { type = string }
variable "registered_runtime_instance_ids" {
  type        = set(string)
  default     = []
  description = "Existing runtime instances explicitly handed to app registration. Do not retag their ownership to grant access."
  validation {
    condition     = length(var.registered_runtime_instance_ids) <= 20 && alltrue([for id in var.registered_runtime_instance_ids : can(regex("^i-[0-9a-f]{17}$", id))])
    error_message = "Use at most 20 exact existing instance IDs."
  }
}
variable "subnet_id" {
  type        = string
  description = "Exact existing operations-node subnet; do not select a new default subnet."
}
data "aws_subnet" "control" { id = var.subnet_id }
variable "ami_id" {
  type        = string
  description = "Reviewed exact Canonical Ubuntu 24.04 amd64 image; required for future plans. Existing VM is not changed by this source edit."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "Provide an exact regional AMI ID, never a moving image alias."
  }
}
data "aws_ami" "ubuntu" {
  owners = ["099720109477"]
  filter {
    name   = "image-id"
    values = [var.ami_id]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}
data "aws_caller_identity" "current" {}

locals {
  name           = "railshot-control-poc"
  auth_parameter = "/railshot/codex/operator-primary/auth"
  pull_parameter = "/railshot/registry/ghcr/pull_token"
  readable_parameters = [for path in [local.auth_parameter, local.pull_parameter] :
  "arn:aws:ssm:${var.region}:${var.account_id}:parameter${path}"]
}

variable "build_worker_security_group_id" {
  description = "Reviewed operations cluster peer SG; null keeps this module standalone."
  type        = string
  default     = null
  validation {
    condition     = var.build_worker_security_group_id == null ? true : can(regex("^sg-[0-9a-f]{17}$", var.build_worker_security_group_id))
    error_message = "Use an exact reviewed peer security group ID."
  }
}
locals {
  build_peer_health = var.build_worker_security_group_id == null ? [] : [
    { protocol = "udp", from_port = 8472, to_port = 8472, description = "Cilium VXLAN node overlay" },
    { protocol = "tcp", from_port = 4240, to_port = 4240, description = "Cilium node health" },
    { protocol = "icmp", from_port = 8, to_port = 0, description = "Cilium ICMP echo health; stateful reply" }
  ]
  build_peer_api = var.build_worker_security_group_id == null ? [] : [
    { protocol = "tcp", from_port = 6443, to_port = 6443, description = "K3s agent supervisor/API" }
  ]
}

variable "openstack_app_ingress" {
  description = "Existing ALB-to-OpenStack relay rule on the inline-owned control SG; preserve its live source SG, port and description."
  type = object({
    source_security_group_id = string
    port                     = number
    description              = string
  })
  default = null
  validation {
    condition = var.openstack_app_ingress == null ? true : (
      can(regex("^sg-[a-f0-9]{8,17}$", var.openstack_app_ingress.source_security_group_id)) &&
      var.openstack_app_ingress.port >= 1 && var.openstack_app_ingress.port <= 65535 &&
      floor(var.openstack_app_ingress.port) == var.openstack_app_ingress.port &&
      length(var.openstack_app_ingress.description) > 0 && length(var.openstack_app_ingress.description) <= 255 &&
      length(regexall("[\\r\\n]", var.openstack_app_ingress.description)) == 0
    )
    error_message = "Bind the existing ALB SG, integer TCP port and single-line ingress description, or leave null."
  }
}

resource "aws_security_group" "control" {
  name        = local.name
  description = "Administrator PoC: no ingress; SSM and HTTPS outbound"
  vpc_id      = var.vpc_id
  # Keep rules inline with the existing owner; do not mix standalone SG rules.
  # Empty by default. A reviewed peer only opens node overlay/health and API.
  ingress = concat([for rule in concat(local.build_peer_health, local.build_peer_api) : {
    description      = rule.description
    from_port        = rule.from_port
    to_port          = rule.to_port
    protocol         = rule.protocol
    security_groups  = [var.build_worker_security_group_id]
    cidr_blocks      = []
    ipv6_cidr_blocks = []
    prefix_list_ids  = []
    self             = false
    }], var.openstack_app_ingress == null ? [] : [{
    description      = var.openstack_app_ingress.description
    from_port        = var.openstack_app_ingress.port
    to_port          = var.openstack_app_ingress.port
    protocol         = "tcp"
    security_groups  = [var.openstack_app_ingress.source_security_group_id]
    cidr_blocks      = []
    ipv6_cidr_blocks = []
    prefix_list_ids  = []
    self             = false
  }])
  dynamic "egress" {
    for_each = local.build_peer_health
    content {
      description     = egress.value.description
      from_port       = egress.value.from_port
      to_port         = egress.value.to_port
      protocol        = egress.value.protocol
      security_groups = [var.build_worker_security_group_id]
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
}

# No inline ingress/egress: aws-edge owns this group's explicit rules.
# The original base SG identity is retained and is never passed to aws-edge.
resource "aws_security_group" "edge" {
  name        = "${local.name}-edge"
  description = "RAILSHOT edge rules managed by aws-edge"
  vpc_id      = var.vpc_id
}

resource "aws_iam_role" "control" {
  name = local.name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.control.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "registered_runtimes" {
  count = var.enable_product_executor && length(var.registered_runtime_instance_ids) > 0 ? 1 : 0
  role  = aws_iam_role.control.id
  name  = "railshot-registered-runtimes"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ssm:StartSession"
      Resource = [for id in var.registered_runtime_instance_ids : "arn:aws:ec2:${var.region}:${var.account_id}:instance/${id}"]
      Condition = {
        StringEquals = { "aws:RequestedRegion" = var.region }
        BoolIfExists = { "ssm:SessionDocumentAccessCheck" = "true" }
      }
    }]
  })
}
resource "aws_iam_role_policy" "codex_auth" {
  role = aws_iam_role.control.id
  name = "read-single-operator-auth"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameter"]
      Resource = local.readable_parameters
      }, {
      # SSMManagedInstanceCore itself grants parameter reads on '*'. Narrow those too.
      Effect      = "Deny"
      Action      = ["ssm:GetParameter", "ssm:GetParameters"]
      NotResource = local.readable_parameters
      }, {
      Effect   = "Deny"
      Action   = ["ssm:GetParametersByPath", "ssm:GetParameterHistory"]
      Resource = "*"
    }]
  })
}
resource "aws_iam_role_policy" "product_executor" {
  count  = var.enable_product_executor ? 1 : 0
  role   = aws_iam_role.control.id
  name   = "railshot-product-executor"
  policy = file("${path.module}/product-executor-policy.json")
  lifecycle {
    precondition {
      condition     = var.account_id == "721622471953" && var.region == "ap-northeast-2" && var.vpc_id == "vpc-085e5a8268cf2b206"
      error_message = "The reviewed executor policy is bound to the existing registered account, region and VPC."
    }
  }
}
# Kept separate: the reviewed VM policy plus edge would exceed the role's
# aggregate inline-policy quota. This document fits one managed policy.
resource "aws_iam_policy" "product_edge" {
  count       = var.enable_product_executor ? 1 : 0
  name        = "railshot-product-edge"
  description = "Registered Railshot app routes; native edge plan validation is required"
  policy      = file("${path.module}/product-edge-policy.json")
}
resource "aws_iam_role_policy_attachment" "product_edge" {
  count      = var.enable_product_executor ? 1 : 0
  role       = aws_iam_role.control.name
  policy_arn = aws_iam_policy.product_edge[0].arn
}
resource "aws_iam_instance_profile" "control" {
  name = local.name
  role = aws_iam_role.control.name
}

resource "aws_instance" "control" {
  ami                                  = data.aws_ami.ubuntu.id
  instance_type                        = "t3.medium"
  subnet_id                            = var.subnet_id
  associate_public_ip_address          = true
  vpc_security_group_ids               = [aws_security_group.control.id, aws_security_group.edge.id]
  source_dest_check                    = false
  iam_instance_profile                 = aws_iam_instance_profile.control.name
  instance_initiated_shutdown_behavior = "stop"
  credit_specification { cpu_credits = "standard" }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = var.enable_product_executor ? var.product_metadata_hop_limit : 1
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    encrypted             = true
    delete_on_termination = false
  }
  # Preserve the existing bootstrap bytes. This recovery does not rerun or
  # reconstruct legacy cloud-init/Ansible/accounts files in their old layout.
  user_data_replace_on_change = false
  tags                        = { Name = local.name }
  depends_on                  = [aws_iam_role_policy_attachment.ssm, aws_iam_role_policy.codex_auth, aws_iam_role_policy.product_executor, aws_iam_role_policy_attachment.product_edge]
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [user_data, user_data_base64]
    precondition {
      condition     = data.aws_subnet.control.vpc_id == var.vpc_id
      error_message = "The existing operations subnet must belong to the registered VPC."
    }
  }
}

output "instance_id" { value = aws_instance.control.id }
output "auth_parameter_name" { value = local.auth_parameter }
output "connect" { value = "aws ssm start-session --region ${var.region} --target ${aws_instance.control.id}" }

output "private_ip" { value = aws_instance.control.private_ip }
output "primary_network_interface_id" { value = aws_instance.control.primary_network_interface_id }
output "edge_security_group_id" { value = aws_security_group.edge.id }
output "source_dest_check" { value = aws_instance.control.source_dest_check }
