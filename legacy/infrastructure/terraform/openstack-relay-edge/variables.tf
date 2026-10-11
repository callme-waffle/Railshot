variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Use the registered AWS account ID."
  }
}
variable "region" { type = string }
variable "vpc_id" {
  type = string
  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "Use the existing target group's VPC."
  }
}
variable "target_group_name" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9-]{0,30}[A-Za-z0-9]$", var.target_group_name))
    error_message = "Use the existing target group name."
  }
}
variable "backend_port" {
  type = number
  validation {
    condition     = var.backend_port >= 1 && var.backend_port <= 65535 && floor(var.backend_port) == var.backend_port
    error_message = "Use the existing relay TCP port."
  }
}
variable "health_path" {
  type = string
  validation {
    condition     = length(var.health_path) <= 1024 && can(regex("^/[^\\r\\n]*$", var.health_path))
    error_message = "Use the existing HTTP health path."
  }
}
variable "listener_arn" { type = string }
variable "priority" {
  type = number
  validation {
    condition     = var.priority >= 1 && var.priority <= 50000 && floor(var.priority) == var.priority
    error_message = "Use the existing listener rule priority."
  }
}
variable "hostname" {
  type = string
  validation {
    condition     = length(var.hostname) <= 253 && can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", var.hostname))
    error_message = "Use the existing exact hostname without wildcard."
  }
}
variable "zone_id" { type = string }
variable "alb_dns_name" { type = string }
variable "alb_zone_id" { type = string }
variable "target_group_tags" { type = map(string) }
variable "listener_rule_tags" { type = map(string) }
