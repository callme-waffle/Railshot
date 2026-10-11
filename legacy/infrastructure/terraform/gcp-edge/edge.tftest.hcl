mock_provider "google" {
  mock_resource "google_compute_global_address" {
    defaults = { address = "198.51.100.10" }
  }
  mock_resource "google_certificate_manager_dns_authorization" {
    defaults = {
      dns_resource_record = [{ name = "_acme-challenge.example.test.", type = "CNAME", data = "authorization.example.test." }]
    }
  }
  mock_data "google_compute_instance" {
    defaults = {
      name = "railshot-gcp-poc"
      network_interface = [{
        network_ip = "10.66.0.2"
        network    = "projects/railshot-poc-20261001/global/networks/railshot-gcp-poc-vpc"
        subnetwork = "projects/railshot-poc-20261001/regions/asia-northeast3/subnetworks/railshot-gcp-poc-subnet"
      }]
      service_account = [{ email = "railshot-gcp-poc-node@railshot-poc-20261001.iam.gserviceaccount.com" }]
    }
  }
}

variables {
  project_id          = "railshot-poc-20261001"
  zone                = "asia-northeast3-a"
  instance_name       = "railshot-gcp-poc"
  expected_private_ip = "10.66.0.2"
  hostname            = "fixture-npm-js-3feba5a1b1cf.railshot.io"
}

run "existing_vm_gfe_only" {
  command = plan
  assert {
    condition     = google_compute_backend_service.app.load_balancing_scheme == "EXTERNAL_MANAGED" && google_compute_network_endpoint.app.ip_address == "10.66.0.2" && google_compute_network_endpoint.app.port == 30080 && google_compute_network_endpoint_group.app.network_endpoint_type == "GCE_VM_IP_PORT"
    error_message = "Use the existing private NodePort through a native global managed LB."
  }
  assert {
    condition     = google_compute_firewall.gfe.source_ranges == toset(["35.191.0.0/16", "130.211.0.0/22"]) && one(google_compute_firewall.gfe.allow).ports == tolist(["30080"]) && google_compute_firewall.gfe.target_service_accounts == toset(["railshot-gcp-poc-node@railshot-poc-20261001.iam.gserviceaccount.com"])
    error_message = "Only GFE sources may enter this VM service account on the allocated NodePort."
  }
  assert {
    condition     = length(google_compute_backend_service.routes) == 0 && length(google_compute_network_endpoint_group.routes) == 0 && length(google_certificate_manager_certificate.routes) == 0 && length(google_compute_url_map.app.host_rule) == 1 && length(google_compute_url_map.app.path_matcher) == 1 && length(google_compute_url_map.redirect.host_rule) == 0 && length(output.application_routes) == 0
    error_message = "Omitting routes must preserve the existing single-app resource addresses and configuration."
  }
}

run "wrong_vm_ip_rejected" {
  command = plan
  variables { expected_private_ip = "10.66.0.3" }
  expect_failures = [google_compute_network_endpoint_group.app]
}

run "stopped_app_keeps_identity_and_certificate_without_unhealthy_backend" {
  command = plan
  variables {
    routes = {
      app-stopped = { hostname = "stopped.railshot.io", node_port = 31001, health_path = "/ready", enabled = false }
      app-running = { hostname = "running.railshot.io", node_port = 31002, health_path = "/health" }
    }
  }
  assert {
    condition = (keys(google_compute_backend_service.routes) == ["app-running"] &&
      keys(google_compute_network_endpoint_group.routes) == ["app-running"] &&
      keys(google_compute_network_endpoint.routes) == ["app-running"] &&
      keys(google_compute_health_check.routes) == ["app-running"] &&
      keys(google_certificate_manager_certificate.routes) == ["app-running", "app-stopped"] &&
      one(google_compute_firewall.gfe.allow).ports == tolist(["30080", "31002"]) &&
    alltrue([for rule in google_compute_url_map.app.host_rule : !contains(rule.hosts, "stopped.railshot.io")]))
    error_message = "A stopped app must retain certificate ownership while removing only its backend, health check, host route and firewall port."
  }
}

run "two_apps_share_existing_frontend" {
  # Plan checks topology without running the DNS creation hook against a fake
  # provider. DNS publication and propagation are covered by test_gcp_routes.py.
  command = plan
  variables {
    routes = {
      app-calculator = { hostname = "calculator.railshot.io", node_port = 31001, health_path = "/healthz" }
      app-notes      = { hostname = "notes.railshot.io", node_port = 31002, health_path = "/ready" }
    }
  }
  assert {
    condition = alltrue([for id, route in var.routes :
      google_compute_network_endpoint.routes[id].port == route.node_port &&
      google_compute_network_endpoint.routes[id].ip_address == "10.66.0.2" &&
      google_compute_network_endpoint.routes[id].network_endpoint_group == google_compute_network_endpoint_group.routes[id].name &&
      google_compute_network_endpoint_group.routes[id].default_port == route.node_port &&
      length(google_compute_backend_service.routes[id].backend) == 1 &&
      length(google_compute_backend_service.routes[id].health_checks) == 1 &&
      one(google_compute_health_check.routes[id].http_health_check).port == route.node_port &&
      one(google_compute_health_check.routes[id].http_health_check).host == route.hostname &&
      one(google_compute_health_check.routes[id].http_health_check).request_path == route.health_path &&
      one([for rule in google_compute_url_map.app.host_rule : rule.path_matcher if contains(rule.hosts, route.hostname)]) == local.route_names[id] &&
      length([for matcher in google_compute_url_map.app.path_matcher : matcher if matcher.name == local.route_names[id]]) == 1
    ])
    error_message = "Each exact hostname must reach its own backend, private NodePort and app-specific health path."
  }
  assert {
    condition = (
      one(google_compute_firewall.gfe.allow).ports == tolist(["30080", "31001", "31002"]) &&
      google_compute_firewall.gfe.source_ranges == toset(["35.191.0.0/16", "130.211.0.0/22"]) &&
      google_compute_firewall.gfe.target_service_accounts == toset(["railshot-gcp-poc-node@railshot-poc-20261001.iam.gserviceaccount.com"])
    )
    error_message = "Add only allocated app ports while retaining the existing GFE and VM service-account restrictions."
  }
  assert {
    condition = alltrue([for id, route in var.routes :
      output.application_routes[id].hostname == route.hostname &&
      output.application_routes[id].backend_service == google_compute_backend_service.routes[id].name &&
      google_certificate_manager_certificate_map_entry.routes[id].map == google_certificate_manager_certificate_map.app.name &&
      google_certificate_manager_certificate_map_entry.routes[id].hostname == route.hostname &&
      one(google_certificate_manager_certificate.routes[id].managed).domains == tolist([route.hostname]) &&
      google_certificate_manager_dns_authorization.routes[id].domain == route.hostname
    ]) && google_compute_global_forwarding_rule.https.port_range == "443" && google_compute_global_forwarding_rule.http.port_range == "80"
    error_message = "All applications must reuse the existing certificate map with separate domain authorizations and HTTP/HTTPS listeners."
  }
  assert {
    condition = alltrue([for id, route in var.routes :
      one([for rule in google_compute_url_map.redirect.host_rule : rule.path_matcher if contains(rule.hosts, route.hostname)]) == local.route_names[id] &&
      one([for matcher in google_compute_url_map.redirect.path_matcher : one(matcher.default_url_redirect).host_redirect if matcher.name == local.route_names[id]]) == route.hostname &&
      one([for matcher in google_compute_url_map.redirect.path_matcher : one(matcher.default_url_redirect).https_redirect if matcher.name == local.route_names[id]])
    ])
    error_message = "An application's HTTP request must redirect to its own HTTPS hostname, never the legacy app."
  }
}

run "legacy_hostname_collision_rejected" {
  command = plan
  variables {
    routes = { app-calculator = { hostname = "fixture-npm-js-3feba5a1b1cf.railshot.io", node_port = 31001, health_path = "/healthz" } }
  }
  expect_failures = [google_compute_url_map.app]
}

run "legacy_port_collision_rejected" {
  command = plan
  variables {
    routes = { app-calculator = { hostname = "calculator.railshot.io", node_port = 30080, health_path = "/healthz" } }
  }
  expect_failures = [google_compute_url_map.app]
}

run "app_hostname_collision_rejected" {
  command = plan
  variables {
    routes = {
      app-calculator = { hostname = "calculator.railshot.io", node_port = 31001, health_path = "/healthz" }
      app-notes      = { hostname = "calculator.railshot.io", node_port = 31002, health_path = "/ready" }
    }
  }
  expect_failures = [var.routes]
}

run "app_port_collision_rejected" {
  command = plan
  variables {
    routes = {
      app-calculator = { hostname = "calculator.railshot.io", node_port = 31001, health_path = "/healthz" }
      app-notes      = { hostname = "notes.railshot.io", node_port = 31001, health_path = "/ready" }
    }
  }
  expect_failures = [var.routes]
}

run "unallocated_app_port_rejected" {
  command = plan
  variables {
    routes = { app-calculator = { hostname = "calculator.railshot.io", node_port = 8080, health_path = "/healthz" } }
  }
  expect_failures = [var.routes]
}

run "unsafe_app_health_path_rejected" {
  command = plan
  variables {
    routes = { app-calculator = { hostname = "calculator.railshot.io", node_port = 31001, health_path = "/../ready" } }
  }
  expect_failures = [var.routes]
}
