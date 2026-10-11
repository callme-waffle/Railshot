terraform {
  required_version = ">= 1.7.0, < 2.0"
  required_providers {
    google = { source = "hashicorp/google", version = "= 8.5.0" }
  }
}

provider "google" {
  project = var.project_id
  zone    = var.zone
}

data "google_compute_instance" "backend" {
  name = var.instance_name
  zone = var.zone
}

locals {
  nic           = data.google_compute_instance.backend.network_interface[0]
  gfe_sources   = ["35.191.0.0/16", "130.211.0.0/22"]
  route_names   = { for id in keys(var.routes) : id => "${var.name}-${substr(sha256(id), 0, 16)}" }
  active_routes = { for id, route in var.routes : id => route if route.enabled }
  extra_compute = { for id, node in var.compute_nodes : id => node if node.instance_name != var.instance_name || node.availability_zone != var.zone }
  extra_route_compute = { for item in flatten([for route_id, route in local.active_routes : [
    for node_id, node in local.extra_compute : { key = "${route_id}:${node_id}", route_id = route_id, node_id = node_id, node = node }
  ]]) : item.key => item }
  dedicated_certificates = { for id, route in var.routes : id => route if route.certificate_id == null }
}

resource "google_project_service" "certificates" {
  service            = "certificatemanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_firewall" "gfe" {
  name          = "${var.name}-gfe"
  network       = local.nic.network
  direction     = "INGRESS"
  source_ranges = local.gfe_sources
  target_service_accounts = distinct(concat([one(data.google_compute_instance.backend.service_account).email],
  [for instance in data.google_compute_instance.compute : one(instance.service_account).email]))
  allow {
    protocol = "tcp"
    ports    = concat([tostring(var.node_port)], [for id in sort(keys(local.active_routes)) : tostring(local.active_routes[id].node_port)])
  }
}

resource "google_compute_network_endpoint_group" "app" {
  name                  = var.name
  zone                  = var.zone
  network               = local.nic.network
  subnetwork            = local.nic.subnetwork
  network_endpoint_type = "GCE_VM_IP_PORT"
  default_port          = var.node_port
  lifecycle {
    precondition {
      condition     = length(var.compute_nodes) == 0 || anytrue([for node in values(var.compute_nodes) : node.instance_name == var.instance_name && node.availability_zone == var.zone && node.private_ip == var.expected_private_ip])
      error_message = "The primary endpoint must be one of the two bound compute nodes."
    }
    precondition {
      condition     = length(data.google_compute_instance.backend.network_interface) == 1 && local.nic.network_ip == var.expected_private_ip
      error_message = "The existing VM must have one NIC with the reviewed private IP."
    }
  }
}

resource "google_compute_network_endpoint" "app" {
  network_endpoint_group = google_compute_network_endpoint_group.app.name
  zone                   = var.zone
  instance               = data.google_compute_instance.backend.name
  ip_address             = local.nic.network_ip
  port                   = var.node_port
}

resource "google_compute_health_check" "app" {
  name = var.name
  http_health_check {
    port         = var.node_port
    request_path = var.health_path
    host         = var.hostname
  }
}

# Legacy single endpoint remains valid; the kubeadm profile adds the other compute endpoint.
resource "google_compute_backend_service" "app" {
  name                  = var.name
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  health_checks         = [google_compute_health_check.app.id]
  backend {
    group                 = google_compute_network_endpoint_group.app.id
    balancing_mode        = "RATE"
    max_rate_per_endpoint = 100
  }
  dynamic "backend" {
    for_each = local.extra_compute
    content {
      group                 = google_compute_network_endpoint_group.compute[backend.key].id
      balancing_mode        = "RATE"
      max_rate_per_endpoint = 100
    }
  }
}

resource "google_compute_network_endpoint_group" "routes" {
  for_each              = local.active_routes
  name                  = local.route_names[each.key]
  zone                  = var.zone
  network               = local.nic.network
  subnetwork            = local.nic.subnetwork
  network_endpoint_type = "GCE_VM_IP_PORT"
  default_port          = each.value.node_port
}

resource "google_compute_network_endpoint" "routes" {
  for_each               = local.active_routes
  network_endpoint_group = google_compute_network_endpoint_group.routes[each.key].name
  zone                   = var.zone
  instance               = data.google_compute_instance.backend.name
  ip_address             = local.nic.network_ip
  port                   = each.value.node_port
}

resource "google_compute_health_check" "routes" {
  for_each = local.active_routes
  name     = local.route_names[each.key]
  http_health_check {
    port         = each.value.node_port
    request_path = each.value.health_path
    host         = each.value.hostname
  }
}

resource "google_compute_backend_service" "routes" {
  for_each              = local.active_routes
  name                  = local.route_names[each.key]
  load_balancing_scheme = "EXTERNAL_MANAGED"
  protocol              = "HTTP"
  health_checks         = [google_compute_health_check.routes[each.key].id]
  backend {
    group                 = google_compute_network_endpoint_group.routes[each.key].id
    balancing_mode        = "RATE"
    max_rate_per_endpoint = 100
  }
  dynamic "backend" {
    for_each = local.extra_compute
    content {
      group                 = google_compute_network_endpoint_group.route_compute["${each.key}:${backend.key}"].id
      balancing_mode        = "RATE"
      max_rate_per_endpoint = 100
    }
  }
}

resource "google_compute_url_map" "app" {
  name = var.name
  default_url_redirect {
    host_redirect = var.hostname
    strip_query   = false
  }
  host_rule {
    hosts        = [var.hostname]
    path_matcher = "app"
  }
  path_matcher {
    name            = "app"
    default_service = google_compute_backend_service.app.id
  }
  dynamic "host_rule" {
    for_each = local.active_routes
    content {
      hosts        = [host_rule.value.hostname]
      path_matcher = local.route_names[host_rule.key]
    }
  }
  dynamic "path_matcher" {
    for_each = local.active_routes
    content {
      name            = local.route_names[path_matcher.key]
      default_service = google_compute_backend_service.routes[path_matcher.key].id
    }
  }
  lifecycle {
    precondition {
      condition = alltrue([for route in var.routes :
        route.hostname != var.hostname && route.node_port != var.node_port
      ])
      error_message = "Additional applications cannot reuse the existing application's hostname or NodePort."
    }
  }
}

resource "google_certificate_manager_dns_authorization" "app" {
  name       = var.name
  domain     = var.hostname
  type       = "PER_PROJECT_RECORD"
  depends_on = [google_project_service.certificates]
  provisioner "local-exec" {
    interpreter = ["python3", "-c"]
    command     = var.certificate_dns_owner_id == null ? "pass" : "from gcp_routes import certificate_dns; certificate_dns()"
    environment = {
      RAILSHOT_GCP_CERTIFICATE_DNS_REQUEST = jsonencode({
        application_id       = var.certificate_dns_owner_id
        purpose              = "certificate"
        application_hostname = var.hostname
        hostname             = trimsuffix(self.dns_resource_record[0].name, ".")
        type                 = "CNAME"
        content              = trimsuffix(self.dns_resource_record[0].data, ".")
      })
    }
  }
}

resource "google_certificate_manager_certificate" "app" {
  name = var.name
  managed {
    domains            = [var.hostname]
    dns_authorizations = [google_certificate_manager_dns_authorization.app.id]
  }
}

resource "google_certificate_manager_certificate_map" "app" {
  name       = var.name
  depends_on = [google_project_service.certificates]
}

resource "google_certificate_manager_certificate_map_entry" "app" {
  name         = var.name
  map          = google_certificate_manager_certificate_map.app.name
  hostname     = var.hostname
  certificates = [google_certificate_manager_certificate.app.id]
}

resource "google_certificate_manager_dns_authorization" "routes" {
  for_each   = local.dedicated_certificates
  name       = local.route_names[each.key]
  domain     = each.value.hostname
  type       = "PER_PROJECT_RECORD"
  depends_on = [google_project_service.certificates]

  # Complete public DNS before the dependent certificate starts authorization.
  # Reuse the application's DNS writer and ownership journal; add no resource.
  provisioner "local-exec" {
    interpreter = ["python3", "-c"]
    command     = "from gcp_routes import certificate_dns; certificate_dns()"
    environment = {
      RAILSHOT_GCP_CERTIFICATE_DNS_REQUEST = jsonencode({
        application_id       = each.key
        purpose              = "certificate"
        application_hostname = each.value.hostname
        hostname             = trimsuffix(self.dns_resource_record[0].name, ".")
        type                 = "CNAME"
        content              = trimsuffix(self.dns_resource_record[0].data, ".")
      })
    }
  }
}

resource "google_certificate_manager_certificate" "routes" {
  for_each = local.dedicated_certificates
  name     = local.route_names[each.key]
  managed {
    domains            = [each.value.hostname]
    dns_authorizations = [google_certificate_manager_dns_authorization.routes[each.key].id]
  }
}

resource "google_certificate_manager_certificate_map_entry" "routes" {
  for_each     = var.routes
  name         = local.route_names[each.key]
  map          = google_certificate_manager_certificate_map.app.name
  hostname     = each.value.hostname
  certificates = [each.value.certificate_id != null ? each.value.certificate_id : google_certificate_manager_certificate.routes[each.key].id]
}

resource "google_compute_target_https_proxy" "app" {
  name            = var.name
  url_map         = google_compute_url_map.app.id
  certificate_map = "//certificatemanager.googleapis.com/${google_certificate_manager_certificate_map.app.id}"
}

resource "google_compute_global_address" "app" {
  name         = var.name
  address_type = "EXTERNAL"
  ip_version   = "IPV4"
}

resource "google_compute_global_forwarding_rule" "https" {
  name                  = "${var.name}-https"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_https_proxy.app.id
  ip_address            = google_compute_global_address.app.id
  port_range            = "443"
}

resource "google_compute_url_map" "redirect" {
  name = "${var.name}-redirect"
  default_url_redirect {
    host_redirect          = var.hostname
    https_redirect         = true
    redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
    strip_query            = false
  }
  dynamic "host_rule" {
    for_each = local.active_routes
    content {
      hosts        = [host_rule.value.hostname]
      path_matcher = local.route_names[host_rule.key]
    }
  }
  dynamic "path_matcher" {
    for_each = local.active_routes
    content {
      name = local.route_names[path_matcher.key]
      default_url_redirect {
        host_redirect          = path_matcher.value.hostname
        https_redirect         = true
        redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
        strip_query            = false
      }
    }
  }
}

resource "google_compute_target_http_proxy" "redirect" {
  name    = "${var.name}-redirect"
  url_map = google_compute_url_map.redirect.id
}

resource "google_compute_global_forwarding_rule" "http" {
  name                  = "${var.name}-http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_target_http_proxy.redirect.id
  ip_address            = google_compute_global_address.app.id
  port_range            = "80"
}

output "frontend_ip" { value = google_compute_global_address.app.address }
output "dns_authorization_record" { value = google_certificate_manager_dns_authorization.app.dns_resource_record[0] }
output "health_url" { value = "https://${var.hostname}${var.health_path}" }
output "backend_service" { value = google_compute_backend_service.app.name }
output "required_workload_ingress_cidrs" { value = local.gfe_sources }
output "readiness" { value = "configured-references-only; certificate, backend health and public HTTPS require live verification" }
output "application_routes" {
  value = { for id, route in var.routes : id => {
    frontend_ip              = google_compute_global_address.app.address
    hostname                 = route.hostname
    backend_service          = local.route_names[id]
    dns_authorization_record = try(google_certificate_manager_dns_authorization.routes[id].dns_resource_record[0], null)
  } }
}

# One NEG per additional compute and zone. Never add the control-plane VM.
data "google_compute_instance" "compute" {
  for_each = local.extra_compute
  name     = each.value.instance_name
  zone     = each.value.availability_zone
  lifecycle {
    postcondition {
      condition     = length(self.network_interface) == 1 && self.network_interface[0].network_ip == each.value.private_ip && self.network_interface[0].network == local.nic.network
      error_message = "Compute endpoints must match the reviewed private IP in the same VPC."
    }
  }
}
resource "google_compute_network_endpoint_group" "compute" {
  for_each              = local.extra_compute
  name                  = "${var.name}-${substr(sha256(each.key), 0, 8)}"
  zone                  = each.value.availability_zone
  network               = data.google_compute_instance.compute[each.key].network_interface[0].network
  subnetwork            = data.google_compute_instance.compute[each.key].network_interface[0].subnetwork
  network_endpoint_type = "GCE_VM_IP_PORT"
  default_port          = var.node_port
}
resource "google_compute_network_endpoint" "compute" {
  for_each               = local.extra_compute
  network_endpoint_group = google_compute_network_endpoint_group.compute[each.key].name
  zone                   = each.value.availability_zone
  instance               = data.google_compute_instance.compute[each.key].name
  ip_address             = each.value.private_ip
  port                   = var.node_port
}
resource "google_compute_network_endpoint_group" "route_compute" {
  for_each              = local.extra_route_compute
  name                  = "${local.route_names[each.value.route_id]}-${substr(sha256(each.value.node_id), 0, 8)}"
  zone                  = each.value.node.availability_zone
  network               = data.google_compute_instance.compute[each.value.node_id].network_interface[0].network
  subnetwork            = data.google_compute_instance.compute[each.value.node_id].network_interface[0].subnetwork
  network_endpoint_type = "GCE_VM_IP_PORT"
  default_port          = local.active_routes[each.value.route_id].node_port
}
resource "google_compute_network_endpoint" "route_compute" {
  for_each               = local.extra_route_compute
  network_endpoint_group = google_compute_network_endpoint_group.route_compute[each.key].name
  zone                   = each.value.node.availability_zone
  instance               = data.google_compute_instance.compute[each.value.node_id].name
  ip_address             = each.value.node.private_ip
  port                   = local.active_routes[each.value.route_id].node_port
}
