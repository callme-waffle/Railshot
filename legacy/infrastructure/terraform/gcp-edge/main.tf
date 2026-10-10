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
  nic                    = data.google_compute_instance.backend.network_interface[0]
  gfe_sources            = ["35.191.0.0/16", "130.211.0.0/22"]
  route_names            = { for id in keys(var.routes) : id => "${var.name}-${substr(sha256(id), 0, 16)}" }
  active_routes          = { for id, route in var.routes : id => route if route.enabled }
  dedicated_certificates = { for id, route in var.routes : id => route if route.certificate_id == null }
}

resource "google_project_service" "certificates" {
  service            = "certificatemanager.googleapis.com"
  disable_on_destroy = false
}

resource "google_compute_firewall" "gfe" {
  name                    = "${var.name}-gfe"
  network                 = local.nic.network
  direction               = "INGRESS"
  source_ranges           = local.gfe_sources
  target_service_accounts = [one(data.google_compute_instance.backend.service_account).email]
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

# ponytail: one existing VM remains a failure domain; add healthy endpoints for HA.
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
