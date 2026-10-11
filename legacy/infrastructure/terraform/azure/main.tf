locals {
  resource_group_name = var.resource_group_mode == "create" ? azurerm_resource_group.node[0].name : data.azurerm_resource_group.node[0].name
  tags = {
    project     = "railshot"
    environment = "poc"
    component   = "app-cluster"
    resource_id = var.name
    target_id   = var.target_id
    managed_by  = "terraform"
  }
  host_config = yamlencode({
    name           = var.name
    node_name      = coalesce(var.node_name, var.name)
    cloud_provider = "azure"
    runtime_status = "not_configured"
  })
  custom_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    host_config                = local.host_config
    initialize_empty_data_disk = var.initialize_empty_data_disk ? "true" : "false"
  })
}

data "azurerm_resource_group" "node" {
  count = var.resource_group_mode == "existing" ? 1 : 0
  name  = var.resource_group_name
}

resource "azurerm_resource_group" "node" {
  count    = var.resource_group_mode == "create" ? 1 : 0
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_virtual_network" "node" {
  name                = "${var.name}-vnet"
  location            = var.location
  resource_group_name = local.resource_group_name
  address_space       = [var.vnet_cidr]
  tags                = local.tags
}

resource "azurerm_subnet" "node" {
  name                 = "${var.name}-subnet"
  resource_group_name  = local.resource_group_name
  virtual_network_name = azurerm_virtual_network.node.name
  address_prefixes     = [cidrsubnet(var.vnet_cidr, 4, 0)]
}

resource "azurerm_network_security_group" "node" {
  name                = "${var.name}-nsg"
  location            = var.location
  resource_group_name = local.resource_group_name
  tags                = local.tags

  dynamic "security_rule" {
    for_each = var.web_ports
    content {
      name                       = "web-${security_rule.value}"
      priority                   = security_rule.value == 80 ? 100 : 110
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = tostring(security_rule.value)
      source_address_prefixes    = var.web_source_cidrs
      destination_address_prefix = "*"
    }
  }
  dynamic "security_rule" {
    for_each = local.host_egress_rules
    content {
      name                       = "host-egress-${security_rule.key}"
      priority                   = security_rule.value.priority
      direction                  = "Outbound"
      access                     = "Allow"
      protocol                   = security_rule.value.protocol
      source_port_range          = "*"
      source_address_prefix      = "*"
      destination_port_ranges    = security_rule.value.ports
      destination_address_prefix = security_rule.value.destination
    }
  }
  security_rule {
    name                       = "deny-other-outbound"
    priority                   = 4096
    direction                  = "Outbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
  # Override default AllowVNetInBound too: no SSH/6443/Argo UI ingress.
  security_rule {
    name                       = "deny-other-inbound"
    priority                   = 1000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_public_ip" "node" {
  name                = "${var.name}-ip"
  location            = var.location
  resource_group_name = local.resource_group_name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.tags
}

resource "azurerm_network_interface" "node" {
  name                = "${var.name}-nic"
  location            = var.location
  resource_group_name = local.resource_group_name
  tags                = local.tags
  ip_configuration {
    name                          = "primary"
    subnet_id                     = azurerm_subnet.node.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.node.id
  }
}

resource "azurerm_network_interface_security_group_association" "node" {
  network_interface_id      = azurerm_network_interface.node.id
  network_security_group_id = azurerm_network_security_group.node.id
}

resource "azurerm_managed_disk" "data" {
  name                 = "${var.name}-data"
  location             = var.location
  resource_group_name  = local.resource_group_name
  storage_account_type = "StandardSSD_LRS"
  create_option        = "Empty"
  disk_size_gb         = var.data_disk_gib
  tags                 = merge(local.tags, { retention = "retain-until-separately-approved" })
  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_linux_virtual_machine" "node" {
  count                           = var.compute_enabled ? 1 : 0
  name                            = var.name
  resource_group_name             = local.resource_group_name
  location                        = var.location
  size                            = var.vm_size
  computer_name                   = var.name
  admin_username                  = var.admin_username
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.node.id]
  custom_data                     = base64encode(local.custom_data)
  disk_controller_type            = "SCSI"
  tags                            = local.tags

  admin_ssh_key {
    username   = var.admin_username
    public_key = trimspace(var.admin_ssh_public_key)
  }
  identity {
    type = "SystemAssigned"
  }
  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "StandardSSD_LRS"
    disk_size_gb         = 40
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = var.image_version
  }
  boot_diagnostics {}
  depends_on = [azurerm_network_interface_security_group_association.node]
}

resource "azurerm_virtual_machine_data_disk_attachment" "data" {
  count              = var.compute_enabled ? 1 : 0
  managed_disk_id    = azurerm_managed_disk.data.id
  virtual_machine_id = azurerm_linux_virtual_machine.node[0].id
  lun                = 0
  caching            = "None"
}

locals {
  # https://learn.microsoft.com/azure/virtual-network/what-is-ip-address-168-63-129-16
  # https://learn.microsoft.com/azure/virtual-machines/linux/time-sync
  # Native NSG profile for trusted app hosts; provider metadata/agent exceptions
  # are NOT tenant isolation. No VNet-wide outbound or cross-node cluster allow.
  host_egress_rules = {
    https   = { priority = 2000, protocol = "Tcp", ports = ["443"], destination = "Internet" }
    dns_udp = { priority = 2010, protocol = "Udp", ports = ["53"], destination = "AzurePlatformDNS" }
    dns_tcp = { priority = 2011, protocol = "Tcp", ports = ["53"], destination = "AzurePlatformDNS" }
    ntp     = { priority = 2020, protocol = "Udp", ports = ["123"], destination = "Internet" }
    agent   = { priority = 2030, protocol = "Tcp", ports = ["80", "32526"], destination = "168.63.129.16/32" }
  }
}
