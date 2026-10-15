mock_provider "google" {}
run "private_cluster_with_collector_only_metrics" {
  command = plan
  variables {
    project_id            = "railshot-test"
    name                  = "rs-network-test"
    subnet_cidr           = "10.76.0.0/24"
    filestore_cidr        = "10.77.0.0/26"
    observer_source_cidrs = ["8.8.8.8/32"]
    metrics_ports         = [30910, 30081]
  }
  assert {
    condition     = google_compute_firewall.metrics.source_ranges == toset(["8.8.8.8/32"]) && google_compute_firewall.metrics.target_tags == toset(["rs-network-test-cluster"])
    error_message = "Only the registered collector may reach runtime metrics."
  }
  assert {
    condition     = google_compute_firewall.filestore.destination_ranges == toset(["10.77.0.0/26"]) && google_compute_firewall.filestore.target_tags == toset(["rs-network-test-worker"])
    error_message = "Filestore egress must remain worker-only and range-bound."
  }
  assert {
    condition     = google_compute_firewall.cluster_egress.priority < 2000 && google_compute_firewall.database.target_tags == toset(["rs-network-test-database"])
    error_message = "Cluster egress must precede the VM deny rule; PG ingress must target only DB."
  }
}
