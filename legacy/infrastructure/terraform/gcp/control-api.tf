variable "k3s_control_source_cidrs" {
  type        = set(string)
  default     = []
  description = "Explicit control-plane public IPv4 /32 sources for the existing K3s API."
  validation {
    condition     = length(var.k3s_control_source_cidrs) <= 20 && alltrue([for cidr in var.k3s_control_source_cidrs : can(cidrnetmask(cidr)) && endswith(cidr, "/32")])
    error_message = "Use at most 20 explicit IPv4 /32 control sources."
  }
}

resource "google_compute_firewall" "control_api" {
  count                   = length(var.k3s_control_source_cidrs) == 0 ? 0 : 1
  name                    = "${var.name}-control-api"
  network                 = local.network_ref
  direction               = "INGRESS"
  source_ranges           = var.k3s_control_source_cidrs
  target_service_accounts = [google_service_account.node.email]
  allow {
    protocol = "tcp"
    ports    = ["6443"]
  }
}
