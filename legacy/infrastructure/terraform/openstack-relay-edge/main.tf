terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.66.0" }
  }
}
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}

# Existing AWS entrypoint for the OpenStack app. Import these three identities
# before enabling updates. Control ingress and ALB egress retain their owners.
resource "aws_lb_target_group" "app" {
  name             = var.target_group_name
  vpc_id           = var.vpc_id
  port             = var.backend_port
  protocol         = "HTTP"
  protocol_version = "HTTP1"
  target_type      = "ip"
  health_check {
    enabled             = true
    protocol            = "HTTP"
    port                = "traffic-port"
    path                = var.health_path
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }
  tags = var.target_group_tags
  lifecycle { prevent_destroy = true }
}

resource "aws_lb_listener_rule" "app" {
  listener_arn = var.listener_arn
  priority     = var.priority
  condition {
    host_header { values = [var.hostname] }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
  tags = var.listener_rule_tags
  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = startswith(var.listener_arn, "arn:aws:elasticloadbalancing:${var.region}:${var.account_id}:listener/app/")
      error_message = "Use the existing ALB listener in the bound account and region."
    }
  }
}

resource "aws_route53_record" "app" {
  zone_id = var.zone_id
  name    = var.hostname
  type    = "A"
  alias {
    name                   = var.alb_dns_name
    zone_id                = var.alb_zone_id
    evaluate_target_health = true
  }
  lifecycle { prevent_destroy = true }
}

# Existing target registration is external. edge_update.py verifies its exact
# target tuple and health, plus the inline-owned control SG ingress, pre/post.
