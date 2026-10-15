terraform {
  required_version = ">= 1.7.0, < 2.0"
  required_providers { google = { source = "hashicorp/google", version = "= 8.5.0" } }
}
variable "project_id" { type = string }
variable "name" { type = string }
variable "subnet_cidr" { type = string }
variable "filestore_cidr" { type = string }
variable "observer_source_cidrs" { type = list(string) }
variable "metrics_ports" { type = list(number) }
provider "google" {
  project = var.project_id
  region  = "asia-northeast3"
}
resource "google_compute_network" "environment" {
  name                    = var.name
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}
resource "google_compute_subnetwork" "environment" {
  name                     = var.name
  region                   = "asia-northeast3"
  network                  = google_compute_network.environment.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}
resource "google_compute_firewall" "cluster_ingress" {
  name        = "${var.name}-cluster-in"
  network     = google_compute_network.environment.id
  direction   = "INGRESS"
  priority    = 700
  source_tags = ["${var.name}-cluster"]
  target_tags = ["${var.name}-cluster"]
  allow { protocol = "all" }
}
resource "google_compute_firewall" "cluster_egress" {
  name               = "${var.name}-cluster-out"
  network            = google_compute_network.environment.id
  direction          = "EGRESS"
  priority           = 700
  destination_ranges = [var.subnet_cidr]
  target_tags        = ["${var.name}-cluster"]
  allow { protocol = "all" }
}
resource "google_compute_firewall" "filestore" {
  name               = "${var.name}-filestore"
  network            = google_compute_network.environment.id
  direction          = "EGRESS"
  priority           = 700
  destination_ranges = [var.filestore_cidr]
  target_tags        = ["${var.name}-worker"]
  # NFSv3 dynamic RPC ports remain within the dedicated Filestore /26 only.
  allow { protocol = "all" }
}
resource "google_compute_firewall" "database" {
  name        = "${var.name}-database"
  network     = google_compute_network.environment.id
  direction   = "INGRESS"
  priority    = 700
  source_tags = ["${var.name}-cluster"]
  target_tags = ["${var.name}-database"]
  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }
}
resource "google_compute_firewall" "metrics" {
  name          = "${var.name}-metrics"
  network       = google_compute_network.environment.id
  direction     = "INGRESS"
  priority      = 700
  source_ranges = var.observer_source_cidrs
  target_tags   = ["${var.name}-cluster"]
  allow {
    protocol = "tcp"
    ports    = [for port in var.metrics_ports : tostring(port)]
  }
}
output "network" {
  value = { name = google_compute_network.environment.name, resource_id = google_compute_network.environment.id, subnet_name = google_compute_subnetwork.environment.name, subnet_id = google_compute_subnetwork.environment.id }
}
