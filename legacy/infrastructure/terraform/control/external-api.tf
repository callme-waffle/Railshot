variable "external_k3s_api_cidrs" {
  type        = set(string)
  default     = []
  description = "Explicit external K3s API IPv4 /32 destinations reachable from the control node."
  validation {
    condition     = length(var.external_k3s_api_cidrs) <= 20 && alltrue([for cidr in var.external_k3s_api_cidrs : can(cidrnetmask(cidr)) && endswith(cidr, "/32")])
    error_message = "Use at most 20 explicit IPv4 /32 API destinations."
  }
}

resource "aws_security_group_rule" "external_k3s_api" {
  for_each          = var.external_k3s_api_cidrs
  type              = "egress"
  from_port         = 6443
  to_port           = 6443
  protocol          = "tcp"
  security_group_id = aws_security_group.edge.id
  cidr_blocks       = [each.value]
  description       = "Registered external K3s API; TLS and scoped credentials required"
}
