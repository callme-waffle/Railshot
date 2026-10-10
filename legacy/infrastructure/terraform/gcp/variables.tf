variable "project_id" {
  type        = string
  description = "Existing, billing-enabled GCP project. This module does not create projects or enable APIs."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use an explicit existing GCP project ID."
  }
}

variable "region" {
  type = string
  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
    error_message = "Use an explicit GCP region, for example asia-northeast3."
  }
}

variable "zone" {
  type = string
  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+-[a-z]$", var.zone))
    error_message = "Use an explicit GCP zone in the chosen region."
  }
}

variable "name" {
  type    = string
  default = "railshot-gcp"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,23}[a-z0-9]$", var.name))
    error_message = "name must be 3-25 lowercase letters/digits/hyphens, starting with a letter and ending with a letter/digit."
  }
}

variable "target_id" {
  type        = string
  description = "Administrator-registered target alias; not a user-supplied cloud account."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,62}$", var.target_id))
    error_message = "Use a 3-63 character lowercase target alias."
  }
}

variable "machine_type" {
  type        = string
  default     = "e2-standard-4"
  description = "Single x86_64 app node; default is 4 vCPU / 16 GiB. Availability/quota are checked by the live plan."
  validation {
    condition     = contains(["e2-standard-2", "e2-standard-4", "e2-standard-8"], var.machine_type)
    error_message = "This initial node profile supports e2-standard-2, e2-standard-4, or e2-standard-8 only."
  }
}

variable "boot_image" {
  type        = string
  description = "Exact public Ubuntu 24.04 amd64 image; mutable image families are deliberately rejected."
  validation {
    condition     = can(regex("^projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v[0-9]+$", var.boot_image))
    error_message = "Select a real, pinned projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-vYYYYMMDD image."
  }
}

variable "boot_disk_gib" {
  type    = number
  default = 30
  validation {
    condition     = var.boot_disk_gib >= 20 && var.boot_disk_gib <= 200 && floor(var.boot_disk_gib) == var.boot_disk_gib
    error_message = "boot_disk_gib must be an integer from 20 to 200."
  }
}

variable "data_disk_gib" {
  type    = number
  default = 100
  validation {
    condition     = var.data_disk_gib >= 20 && var.data_disk_gib <= 1000 && floor(var.data_disk_gib) == var.data_disk_gib
    error_message = "data_disk_gib must be an integer from 20 to 1000; shrinking an existing disk is unsupported."
  }
}

variable "initialize_empty_data_disk" {
  type        = bool
  default     = false
  description = "Explicit first-provision consent to format this module's blank, unpartitioned disk. Existing ext4 is reused; other signatures are rejected."
}

variable "subnet_cidr" {
  type    = string
  default = "10.66.0.0/24"
  validation {
    condition     = can(cidrnetmask(var.subnet_cidr))
    error_message = "subnet_cidr must be an IPv4 CIDR."
  }
}

variable "allow_http" {
  type    = bool
  default = false
}

variable "enable_gcp_registry_pull" {
  type        = bool
  default     = false
  description = "Opt in to storage read-only OAuth scope on this VM. Requires separately registered repository-scoped reader IAM and a verified kubelet credential provider; scope alone does not prove private image pulls. Updating an existing VM requires an explicit stop/start maintenance operation."
}

variable "allow_https" {
  type    = bool
  default = false
}

variable "allow_iap_ssh" {
  type        = bool
  default     = false
  description = "Allow TCP 22 only from Google's IAP forwarding range. IAP permissions and either OS Login or the explicit operator public key are managed separately."
}

variable "operator_ssh_public_key" {
  type        = string
  default     = null
  description = "Optional first-boot OpenSSH public key for railshot-operator over IAP. Setting it disables OS Login on this VM; null preserves OS Login. Never pass a private key."
  validation {
    condition     = var.operator_ssh_public_key == null ? true : can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,3}( [^\\r\\n]+)?$", var.operator_ssh_public_key))
    error_message = "Supply one supported OpenSSH public key on a single line, or null."
  }
}

variable "wireguard_peer_public_cidrs" {
  type        = list(string)
  default     = []
  description = "Retired input retained only to reject old WireGuard configurations. Remove it from target settings."
  validation {
    condition     = length(var.wireguard_peer_public_cidrs) == 0
    error_message = "WireGuard is retired. Remove wireguard_peer_public_cidrs from the target configuration; existing live tunnels require a separate reviewed cutover."
  }
}

variable "max_run_duration_seconds" {
  type        = number
  default     = null
  description = "Optional per-start VM runtime limit, 30 seconds to 120 days. STOP retains disks; null keeps the normal scheduling policy."
  validation {
    condition = var.max_run_duration_seconds == null ? true : (
      var.max_run_duration_seconds >= 30 && var.max_run_duration_seconds <= 10368000 && floor(var.max_run_duration_seconds) == var.max_run_duration_seconds
    )
    error_message = "max_run_duration_seconds must be null or an integer from 30 to 10368000 (120 days)."
  }
}

variable "web_source_ranges" {
  type    = list(string)
  default = ["0.0.0.0/0"]
  validation {
    condition     = length(var.web_source_ranges) > 0 && alltrue([for cidr in var.web_source_ranges : can(cidrnetmask(cidr))])
    error_message = "web_source_ranges must contain explicit IPv4 CIDRs. They apply only to enabled HTTP/HTTPS ports."
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

variable "purpose" {
  type        = string
  default     = "runtime"
  description = "Approved node role; database prepares PostgreSQL storage without installing a runtime or database."
  validation {
    condition     = contains(["runtime", "database"], var.purpose)
    error_message = "purpose must be runtime or database."
  }
}

variable "database_ingress" {
  type        = list(object({ port = number, cidr = string }))
  default     = []
  description = "Approved private TCP ingress for PostgreSQL, etcd and Patroni; RFC1918 /16-/32 only. No routing or database installation."
  validation {
    condition = length(var.database_ingress) <= 64 && alltrue([for rule in var.database_ingress :
      contains([5432, 2379, 2380, 8008], rule.port) && can(cidrnetmask(rule.cidr)) &&
      can(regex("^(10\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|192\\.168\\.)[0-9.]+/(1[6-9]|2[0-9]|3[0-2])$", rule.cidr))
    ])
    error_message = "Supply at most 64 private IPv4 /16-/32 rules on TCP 5432, 2379, 2380 or 8008."
  }
}

variable "database_egress" {
  type        = list(object({ port = number, cidr = string }))
  default     = []
  description = "Approved private TCP egress for PostgreSQL, etcd and Patroni; RFC1918 /16-/32 only. No routing or database installation."
  validation {
    condition = length(var.database_egress) <= 64 && alltrue([for rule in var.database_egress :
      contains([5432, 2379, 2380, 8008], rule.port) && can(cidrnetmask(rule.cidr)) &&
      can(regex("^(10\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|192\\.168\\.)[0-9.]+/(1[6-9]|2[0-9]|3[0-2])$", rule.cidr))
    ])
    error_message = "Supply at most 64 private IPv4 /16-/32 rules on TCP 5432, 2379, 2380 or 8008."
  }
}

variable "existing_network_name" {
  type        = string
  default     = null
  description = "Existing same-project network; provide both names to share one environment network."
  validation {
    condition     = var.existing_network_name == null ? true : can(regex("^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$", var.existing_network_name))
    error_message = "Use a registered GCP network name or null."
  }
}

variable "existing_subnetwork_name" {
  type        = string
  default     = null
  description = "Existing same-project subnetwork; provide both names to share one environment network."
  validation {
    condition     = var.existing_subnetwork_name == null ? true : can(regex("^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$", var.existing_subnetwork_name))
    error_message = "Use a registered GCP subnetwork name or null."
  }
}
