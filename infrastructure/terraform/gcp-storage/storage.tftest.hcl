mock_provider "google" {}
variables {
  cluster_id        = "demo"
  region            = "asia-northeast3"
  project_id        = "railshot-project"
  network           = "projects/railshot-project/global/networks/private"
  instance_name     = "demo-files"
  share_name        = "apps"
  capacity_gib      = 1024
  reserved_ip_range = "10.0.8.0/26"
  workers = {
    compute-a = { instance_name = "compute-a", private_ip = "10.0.1.10", zone = "asia-northeast3-a" }
    compute-b = { instance_name = "compute-b", private_ip = "10.0.2.10", zone = "asia-northeast3-b" }
  }
}
run "regional_shared_backend" {
  command = plan
  override_data {
    target = data.google_compute_instance.worker["compute-a"]
    values = { network_interface = [{ network_ip = "10.0.1.10", network = "https://www.googleapis.com/compute/v1/projects/railshot-project/global/networks/private" }] }
  }
  override_data {
    target = data.google_compute_instance.worker["compute-b"]
    values = { network_interface = [{ network_ip = "10.0.2.10", network = "https://www.googleapis.com/compute/v1/projects/railshot-project/global/networks/private" }] }
  }
  assert {
    condition     = google_filestore_instance.cluster.tier == "REGIONAL" && google_filestore_instance.cluster.location == "asia-northeast3"
    error_message = "Storage must be a Regional Filestore instance."
  }
  assert {
    condition     = length(one(google_filestore_instance.cluster.file_shares).nfs_export_options[0].ip_ranges) == 2 && google_filestore_instance.cluster.labels["railshot-cluster"] == "demo"
    error_message = "Owned Filestore export must permit exactly the two workers."
  }
  assert {
    condition     = one(google_filestore_instance.cluster.networks).network == "projects/railshot-project/global/networks/private" && one(google_filestore_instance.cluster.networks).reserved_ip_range == "10.0.8.0/26"
    error_message = "Filestore must use the admitted private project network."
  }
}
