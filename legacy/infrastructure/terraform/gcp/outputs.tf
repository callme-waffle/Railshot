output "node_descriptor" {
  description = "Secret-free adapter input. Terraform creation does not prove guest/cluster readiness."
  value = {
    host_egress      = { profile = var.host_egress_profile, runtime_verification = "unverified", tenant_isolation = "separate_guest_policy_required" }
    purpose          = var.purpose
    schema_version   = "v1"
    provider_kind    = "gcp"
    compute          = { machine_type = var.machine_type, source = "configured" }
    target_id        = var.target_id
    resource_id      = google_compute_instance.node.id
    execution_driver = "terraform"
    owner_ref        = var.owner_ref
    instance_id      = google_compute_instance.node.instance_id
    location         = { project_id = var.project_id, region = var.region, zone = var.zone }
    architecture     = "x86_64"
    image_ref        = var.boot_image
    addresses = {
      private = google_compute_instance.node.network_interface[0].network_ip
      public  = google_compute_address.node.address
    }
    transport_ref = local.iap_ssh == null ? null : local.iap_ssh.transport_ref
    runtime_limit = local.runtime_limit
    data_disk = {
      resource_id  = google_compute_disk.data.id
      size_gib     = var.data_disk_gib
      device       = "/dev/disk/by-id/google-railshot-data"
      mount_path   = var.purpose == "database" ? "/var/lib/postgresql" : "/var/lib/rancher"
      preservation = "retain"
    }
    bootstrap = {
      profile       = "ubuntu-2404-amd64-host-v1"
      method        = "host-preparation-only"
      repo          = null
      revision      = null
      bundle_sha256 = null
      status        = "unverified"
    }
    gitops  = { repo = null, path = null, revision = null }
    runtime = { configuration_status = "not_configured", readiness = "not_configured" }
  }
}
