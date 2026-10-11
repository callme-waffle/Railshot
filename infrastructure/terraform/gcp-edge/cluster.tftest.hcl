mock_provider "google" {
  mock_resource "google_certificate_manager_dns_authorization" {
    defaults = { dns_resource_record = [{ name = "_acme-challenge.app.example.com.", type = "CNAME", data = "fixture.authorize.certificatemanager.goog." }] }
  }
  mock_data "google_compute_instance" {
    defaults = {
      name              = "compute-a"
      network_interface = [{ network_ip = "10.10.0.2", network = "projects/railshot-project/global/networks/cluster", subnetwork = "projects/railshot-project/regions/asia-northeast3/subnetworks/cluster" }]
      service_account   = [{ email = "worker@railshot-project.iam.gserviceaccount.com" }]
    }
  }
}
variables {
  project_id          = "railshot-project"
  zone                = "asia-northeast3-a"
  instance_name       = "compute-a"
  expected_private_ip = "10.10.0.2"
  hostname            = "app.example.com"
  node_port           = 30080
  health_path         = "/health"
  # Apply uses only the mock provider; disable the native DNS provisioner in this offline fixture.
  certificate_dns_owner_id = null
  compute_nodes = {
    compute-a = { instance_name = "compute-a", private_ip = "10.10.0.2", availability_zone = "asia-northeast3-a" }
    compute-b = { instance_name = "compute-b", private_ip = "10.10.0.3", availability_zone = "asia-northeast3-b" }
  }
}
run "two_zonal_compute_negs" {
  command = apply
  override_data {
    target = data.google_compute_instance.compute["compute-b"]
    values = {
      name              = "compute-b"
      network_interface = [{ network_ip = "10.10.0.3", network = "projects/railshot-project/global/networks/cluster", subnetwork = "projects/railshot-project/regions/asia-northeast3/subnetworks/cluster" }]
      service_account   = [{ email = "worker@railshot-project.iam.gserviceaccount.com" }]
    }
  }
  override_resource {
    target = google_compute_network_endpoint_group.app
    values = { id = "projects/railshot-project/zones/asia-northeast3-a/networkEndpointGroups/app-a" }
  }
  override_resource {
    target = google_compute_network_endpoint_group.compute["compute-b"]
    values = { id = "projects/railshot-project/zones/asia-northeast3-b/networkEndpointGroups/app-b" }
  }
  assert {
    condition     = google_compute_network_endpoint.app.ip_address == "10.10.0.2" && google_compute_network_endpoint.compute["compute-b"].ip_address == "10.10.0.3" && google_compute_network_endpoint.compute["compute-b"].zone == "asia-northeast3-b" && length(google_compute_backend_service.app.backend) == 2
    error_message = "Register the two compute endpoints through their own zonal NEGs on one backend service."
  }
}
