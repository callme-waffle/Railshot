output "node_descriptor" {
  description = "Nonsecret provisioning references, NOT bootstrap/cluster readiness evidence. Runtime reconciliation must populate observed conditions."
  value = {
    host_egress      = { profile = var.host_egress_profile, runtime_verification = "unverified", tenant_isolation = "separate_guest_policy_required" }
    schema_version   = "v1"
    provider_kind    = "azure"
    compute          = { machine_type = var.vm_size, source = "configured" }
    execution_driver = "terraform"
    target_id        = var.target_id
    resource_id      = var.name
    owner_ref        = var.owner_ref
    instance_id      = try(azurerm_linux_virtual_machine.node[0].id, null)
    region           = var.location
    location         = { subscription_id = var.subscription_id, region = var.location }
    architecture     = "amd64"
    addresses = {
      private_ipv4 = azurerm_network_interface.node.private_ip_address
      public_ipv4  = azurerm_public_ip.node.ip_address
    }
    transport_ref = "azure-control-plane:${var.target_id}"
    identity_ref  = try(azurerm_linux_virtual_machine.node[0].identity[0].principal_id, null)
    storage = {
      data_disk_id      = azurerm_managed_disk.data.id
      size_gib          = var.data_disk_gib
      mount_path        = "/var/lib/rancher"
      retention         = "retain"
      backup_verified   = false
      terraform_guarded = true
    }
    bootstrap = {
      profile         = "ubuntu2404-scsi-host-v1"
      source_revision = null
      cloud_init_hash = sha256(local.custom_data)
      readiness       = "unverified"
    }
    runtime = { configuration_status = "not_configured", readiness = "not_configured" }
    gitops = {
      repository = null
      revision   = null
      path       = null
    }
  }
}
