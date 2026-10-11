terraform {
  required_version = ">= 1.7.0, < 2.0"
  required_providers {
    google = { source = "hashicorp/google", version = "= 8.5.0" }
  }
}
provider "google" {
  project = var.project_id
  region  = var.region
}
variable "cluster_id" { type = string }
variable "region" { type = string }
variable "project_id" { type = string }
variable "network" { type = string }
variable "instance_name" { type = string }
variable "share_name" { type = string }
variable "capacity_gib" {
  type = number
  validation {
    condition     = var.capacity_gib >= 1024 && var.capacity_gib <= 10240
    error_message = "Regional capacity must be 1024..10240 GiB in this profile."
  }
}
variable "reserved_ip_range" { type = string }
variable "workers" {
  type = map(object({ instance_name = string, private_ip = string, zone = string }))
  validation {
    condition     = length(var.workers) == 2
    error_message = "The first profile requires two workers."
  }
}
data "google_compute_instance" "worker" {
  for_each = var.workers
  name     = each.value.instance_name
  zone     = each.value.zone
}
resource "google_project_service" "filestore" {
  service            = "file.googleapis.com"
  disable_on_destroy = false
}
resource "google_filestore_instance" "cluster" {
  name     = var.instance_name
  location = var.region
  tier     = "REGIONAL"
  protocol = "NFS_V3"
  labels   = { managed-by = "railshot", railshot-cluster = var.cluster_id }
  file_shares {
    name        = var.share_name
    capacity_gb = var.capacity_gib
    nfs_export_options {
      ip_ranges   = [for worker in var.workers : "${worker.private_ip}/32"]
      access_mode = "READ_WRITE"
      squash_mode = "NO_ROOT_SQUASH"
    }
  }
  networks {
    network           = var.network
    modes             = ["MODE_IPV4"]
    connect_mode      = "DIRECT_PEERING"
    reserved_ip_range = var.reserved_ip_range
  }
  lifecycle {
    prevent_destroy = true
    precondition {
      condition = alltrue([for key, worker in var.workers :
        length(data.google_compute_instance.worker[key].network_interface) == 1 &&
        data.google_compute_instance.worker[key].network_interface[0].network_ip == worker.private_ip &&
        endswith(data.google_compute_instance.worker[key].network_interface[0].network, var.network) &&
        startswith(worker.zone, "${var.region}-")
      ])
      error_message = "Observed workers must match admitted private IPs, region and project network."
    }
  }
  depends_on = [google_project_service.filestore]
}
output "backend_id" { value = google_filestore_instance.cluster.name }
output "readiness" { value = "configured; independent Filestore/CSI/PVC observations required" }
