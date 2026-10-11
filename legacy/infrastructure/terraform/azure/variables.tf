variable "subscription_id" {
  description = "Administrator-registered Azure subscription ID; authentication comes from the execution environment."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be a UUID."
  }
}

variable "resource_group_mode" {
  description = "existing reads an administrator-registered RG; create explicitly creates a dedicated, deletion-protected RG. Do not change modes after creation."
  type        = string
  default     = "existing"
  validation {
    condition     = contains(["existing", "create"], var.resource_group_mode)
    error_message = "resource_group_mode must be existing or create."
  }
}

variable "resource_group_name" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.()-]{0,88}[A-Za-z0-9_()-]$", var.resource_group_name))
    error_message = "Use an administrator-approved resource group name, 2-90 ASCII characters and no trailing dot."
  }
}

variable "location" {
  description = "Explicit Azure region for this target; availability, image and quota must be checked before apply."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,30}$", var.location))
    error_message = "Use an Azure region code such as koreacentral."
  }
}

variable "target_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.target_id))
    error_message = "target_id must be an administrator-registered lowercase alias, 3-40 characters."
  }
}

variable "name" {
  description = "Stable logical resource name and VM hostname; changing this is a migration, not a display-name edit."
  type        = string
  default     = "railshot-azure"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,29}[a-z0-9]$", var.name))
    error_message = "name must be 3-31 lowercase letters, digits or hyphens, ending in a letter/digit."
  }
}

variable "compute_enabled" {
  description = "False removes the VM and attachment after a reviewed drain/delete plan, retaining the protected data disk, RG and network. This is deletion, not stop/deallocate."
  type        = bool
  default     = true
}

variable "vm_size" {
  description = "Administrator-approved x86_64 SCSI VM size; this minimal profile supports Dsv5 only."
  type        = string
  default     = "Standard_D4s_v5"
  validation {
    condition     = contains(["Standard_D2s_v5", "Standard_D4s_v5", "Standard_D8s_v5"], var.vm_size)
    error_message = "Choose a validated D2s/D4s/D8s_v5 profile; ARM/NVMe profiles need a separate bootstrap contract."
  }
}

variable "image_version" {
  description = "Exact Canonical ubuntu-24_04-lts:server marketplace version verified in this region; latest is forbidden."
  type        = string
  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.image_version))
    error_message = "image_version must be an exact numeric major.minor.patch marketplace version, never latest."
  }
}

variable "admin_username" {
  type    = string
  default = "railshotadmin"
  validation {
    condition     = can(regex("^[a-z][a-z0-9_]{2,30}$", var.admin_username)) && !contains(["root", "admin", "administrator", "ubuntu", "azureuser"], var.admin_username)
    error_message = "Use a non-reserved local administrator name (3-31 characters)."
  }
}

variable "admin_ssh_public_key" {
  description = "Public RSA (2048+ bits) or Ed25519 key only. No private key/password. The NSG blocks SSH, including VNet-origin SSH."
  type        = string
  validation {
    condition     = can(regex("^ssh-(rsa|ed25519) [A-Za-z0-9+/=]+( [^\\r\\n]*)?$", trimspace(var.admin_ssh_public_key)))
    error_message = "Supply one SSH public key, never a private key or password."
  }
}

variable "vnet_cidr" {
  description = "Dedicated IPv4 VNet; subnet 0 uses four additional prefix bits. Choose a non-overlapping range."
  type        = string
  default     = "10.90.0.0/16"
  validation {
    condition     = can(cidrnetmask(var.vnet_cidr)) && can(regex("/(1[6-9]|2[0-4])$", var.vnet_cidr))
    error_message = "vnet_cidr must be a valid IPv4 /16 through /24 network; the derived subnet is /20 through /28."
  }
}

variable "web_ports" {
  description = "Explicit public app ingress. Empty by default; enabling 443 does not install a certificate."
  type        = set(number)
  default     = []
  validation {
    condition     = alltrue([for p in var.web_ports : contains([80, 443], p)])
    error_message = "Only TCP 80 and/or 443 may be exposed; SSH and Kubernetes APIs are not public."
  }
}

variable "web_source_cidrs" {
  description = "Source allowlist for explicitly enabled web ports."
  type        = list(string)
  default     = ["0.0.0.0/0"]
  validation {
    condition     = length(var.web_source_cidrs) > 0 && alltrue([for c in var.web_source_cidrs : can(cidrnetmask(c))])
    error_message = "Supply at least one IPv4 CIDR."
  }
}

variable "data_disk_gib" {
  type    = number
  default = 100
  validation {
    condition     = var.data_disk_gib >= 32 && var.data_disk_gib <= 1024 && floor(var.data_disk_gib) == var.data_disk_gib
    error_message = "data_disk_gib must be an integer from 32 to 1024. Shrink is not supported."
  }
}

variable "owner_ref" {
  type        = string
  description = "Authoritative administrator state binding; common executor injects its exact state path."
  validation {
    condition     = startswith(var.owner_ref, "terraform:") && length(var.owner_ref) > 10 && length(var.owner_ref) < 2048
    error_message = "owner_ref must identify the registered Terraform state owner."
  }
}

variable "host_egress_profile" {
  type        = string
  default     = "single-node-https-v1"
  description = "Trusted app host: HTTPS bootstrap/registry, provider DNS/time and required guest agent traffic. Not a tenant/container isolation policy."
  validation {
    condition     = var.host_egress_profile == "single-node-https-v1"
    error_message = "Only the reviewed single-node-https-v1 host egress profile is implemented."
  }
}

variable "node_name" {
  type        = string
  default     = ""
  description = "Desired identity passed to the team runtime owner; no runtime is installed here. Stable Kubernetes node identity; empty uses name for NEW nodes. Existing clusters must preserve their original registered name, not the current cloud hostname."
  validation {
    condition     = var.node_name == "" || (length(var.node_name) <= 253 && can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$", var.node_name)))
    error_message = "node_name must be empty or a lowercase DNS subdomain, at most 253 characters. Renaming an existing node is not a normal bootstrap update."
  }
}

variable "initialize_empty_data_disk" {
  type        = bool
  default     = false
  description = "Consent to initialize this module-created blank disk only. Existing ext4 is never reformatted."
}
