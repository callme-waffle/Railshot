data "aws_vpc" "default" {
  count   = var.vpc_id == null ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.subnet_id == null ? 1 : 0
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_subnet" "selected" {
  id = var.subnet_id == null ? sort(data.aws_subnets.default[0].ids)[0] : var.subnet_id
}
data "aws_ami" "ubuntu" {
  owners = ["099720109477"] # Canonical
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

locals {
  # Keep the explicit deny: the managed SSM agent policy includes broad parameter reads.
  # Host preparation consumes no GitOps, registry, or other Parameter Store secret.
  node_parameter_policy = {
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Deny"
      Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "ssm:GetParameterHistory"]
      Resource = "*"
    }]
  }
}

resource "aws_security_group" "node" {
  name = "${var.name}-node"
  # Preserve the legacy description: changing it can replace the security group.
  description = "Web in only, no SSH (SSM). Out: 80/443 only; control plane pulls (platform/ZERO-TRUST.md)"
  vpc_id      = var.vpc_id == null ? data.aws_vpc.default[0].id : var.vpc_id

  dynamic "ingress" {
    for_each = var.purpose == "database" ? [] : concat(var.http_enabled ? [80] : [], var.https_enabled ? [443] : [])
    content {
      description = "web ${ingress.value}"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
  dynamic "ingress" {
    for_each = var.database_ingress
    content {
      description = "database private ingress"
      from_port   = ingress.value.port
      to_port     = ingress.value.port
      protocol    = "tcp"
      cidr_blocks = [ingress.value.cidr]
    }
  }
  dynamic "egress" {
    for_each = var.database_egress
    content {
      description = "database private egress"
      from_port   = egress.value.port
      to_port     = egress.value.port
      protocol    = "tcp"
      cidr_blocks = [egress.value.cidr]
    }
  }
  dynamic "egress" {
    for_each = [80, 443] # apt mirrors and SSM; no runtime installed. AWS DNS and IMDS bypass SGs.
    content {
      description = "out ${egress.value}"
      from_port   = egress.value
      to_port     = egress.value
      protocol    = "tcp"
      cidr_blocks = ["0.0.0.0/0"]
    }
  }
}

resource "aws_iam_role" "node" {
  count = var.existing_instance_profile == null ? 1 : 0
  name  = "${var.name}-node"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  count      = var.existing_instance_profile == null ? 1 : 0
  role       = aws_iam_role.node[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "node_params" {
  count  = var.existing_instance_profile == null ? 1 : 0
  name   = "read-railshot-params"
  role   = aws_iam_role.node[0].id
  policy = jsonencode(local.node_parameter_policy)
}

resource "aws_iam_instance_profile" "node" {
  count = var.existing_instance_profile == null ? 1 : 0
  name  = "${var.name}-node"
  role  = aws_iam_role.node[0].name
}

resource "aws_instance" "node" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnet.selected.id
  vpc_security_group_ids = concat([aws_security_group.node.id], var.additional_security_group_ids)
  iam_instance_profile   = var.existing_instance_profile == null ? aws_iam_instance_profile.node[0].name : var.existing_instance_profile

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1 # pods cannot reach instance credentials
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gb
    encrypted             = true
    delete_on_termination = false # Old root data must survive an explicitly approved migration/replacement.
  }
  user_data = local.cloud_init
  credit_specification { cpu_credits = "standard" }
  user_data_replace_on_change = false # guest changes require a separate approved configuration job
  tags                        = { Name = "${var.name}-node" }
  lifecycle {
    precondition {
      condition     = var.purpose == "database" || length(var.database_ingress) == 0
      error_message = "Database ingress requires an approved database node."
    }
    precondition {
      condition     = (var.vpc_id == null) == (var.subnet_id == null)
      error_message = "Specify both vpc_id and subnet_id, or neither for legacy default selection."
    }
    precondition {
      condition     = var.vpc_id == null ? true : data.aws_subnet.selected.vpc_id == var.vpc_id
      error_message = "The selected subnet must belong to the registered VPC."
    }
  }
}

resource "aws_eip" "node" {
  count    = var.allocate_eip ? 1 : 0
  instance = aws_instance.node.id
  domain   = "vpc"
}

# Read-only role for CI (terraform plan); applies stay with the platform admin for now.
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_ci_plan_role ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "ci_plan" {
  count = var.create_ci_plan_role ? 1 : 0
  name  = "${var.name}-ci-plan"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github[0].arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ci_plan" {
  count      = var.create_ci_plan_role ? 1 : 0
  role       = aws_iam_role.ci_plan[0].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_budgets_budget" "monthly" {
  count        = var.budget_email == "" ? 0 : 1
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.selected.availability_zone
  type              = "gp3"
  size              = var.data_disk_gib
  encrypted         = true
  tags              = { Name = "${var.name}-data", Target = var.target_id, Retention = "retain-until-approved" }
  lifecycle { prevent_destroy = true }
}
resource "aws_volume_attachment" "data" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.data.id
  instance_id                    = aws_instance.node.id
  force_detach                   = false
  stop_instance_before_detaching = false # Caller must complete the separate drain/stop maintenance operation.
}
locals {
  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    operator_ssh_public_key  = var.operator_ssh_public_key
    max_run_duration_seconds = var.max_run_duration_seconds
    host_config = yamlencode({
      name           = var.name, node_name = coalesce(var.node_name, var.name), cloud_provider = "aws", region = var.region,
      runtime_status = "not_configured"
    })
    bootstrap_manifest = jsonencode({ method = "host-preparation-only", image_ref = var.ami_id, runtime_status = "not_configured" })
    bootstrap_script = templatefile("${path.module}/bootstrap.sh.tftpl", {
      device                     = "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${replace(aws_ebs_volume.data.id, "-", "")}",
      initialize_empty_data_disk = var.initialize_empty_data_disk ? "true" : "false"
      data_mount_path            = var.purpose == "database" ? "/var/lib/postgresql" : "/var/lib/rancher"
    })
  })
}

# Preserve existing resource identities when the legacy defaults remain enabled.
moved {
  from = aws_iam_role.node
  to   = aws_iam_role.node[0]
}
moved {
  from = aws_iam_role_policy_attachment.ssm
  to   = aws_iam_role_policy_attachment.ssm[0]
}
moved {
  from = aws_iam_role_policy.node_params
  to   = aws_iam_role_policy.node_params[0]
}
moved {
  from = aws_iam_instance_profile.node
  to   = aws_iam_instance_profile.node[0]
}
moved {
  from = aws_eip.node
  to   = aws_eip.node[0]
}
moved {
  from = aws_iam_openid_connect_provider.github
  to   = aws_iam_openid_connect_provider.github[0]
}
moved {
  from = aws_iam_role.ci_plan
  to   = aws_iam_role.ci_plan[0]
}
moved {
  from = aws_iam_role_policy_attachment.ci_plan
  to   = aws_iam_role_policy_attachment.ci_plan[0]
}
