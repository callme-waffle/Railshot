variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "name" {
  type    = string
  default = "railshot"
}

variable "product_environment" {
  type        = bool
  default     = false
  nullable    = false
  description = "Opt in new product targets to ProjectOwner=railshot-product and Target=target_id tags at resource creation. False preserves legacy tags; do not enable on existing infrastructure without a separate lifecycle review."
}

variable "instance_type" {
  type    = string
  default = "t3.large" # Nitro x86_64 only: device naming is an explicit profile contract.
  validation {
    condition     = contains(["t3.large", "t3.xlarge", "t3.2xlarge"], var.instance_type)
    error_message = "Reviewed Nitro profile supports t3.large, t3.xlarge, t3.2xlarge only."
  }
}

variable "root_volume_gb" {
  type    = number
  default = 40
}

variable "github_repo" {
  description = "owner/repo allowed to assume the read-only CI role via OIDC"
  type        = string
  default     = "Jasmin-Softbank/Jasmin"
}

variable "budget_usd" {
  type    = number
  default = 30
}

variable "budget_email" {
  description = "Budget alert recipient; empty disables the budget"
  type        = string
  default     = ""
}

variable "https_enabled" {
  description = "Legacy direct public HTTPS. Disable with http_enabled for nodes reached through the separately managed ALB."
  type        = bool
  default     = false
}

variable "http_enabled" {
  type        = bool
  default     = true
  description = "Legacy direct public HTTP. Set false for ALB-only customer nodes."
}

variable "vpc_id" {
  type        = string
  default     = null
  description = "Existing registered VPC. Set together with subnet_id; null preserves legacy default VPC selection."
  validation {
    condition     = var.vpc_id == null ? true : can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "vpc_id must be null or an AWS VPC ID."
  }
}

variable "subnet_id" {
  type        = string
  default     = null
  description = "Explicit existing subnet in vpc_id. Its egress route/public address policy must support SSM and HTTPS bootstrap."
  validation {
    condition     = var.subnet_id == null ? true : can(regex("^subnet-[0-9a-f]{8,17}$", var.subnet_id))
    error_message = "subnet_id must be null or an AWS subnet ID."
  }
}

variable "additional_security_group_ids" {
  type        = list(string)
  default     = []
  description = "Separately reviewed same-VPC groups, for ALB NodePort ingress and trusted management/cluster paths. This module never opens public SSH."
  validation {
    condition     = length(var.additional_security_group_ids) <= 4 && alltrue([for id in var.additional_security_group_ids : can(regex("^sg-[0-9a-f]{8,17}$", id))])
    error_message = "Supply at most four existing security group IDs."
  }
}

variable "allocate_eip" {
  type        = bool
  default     = true
  description = "Legacy stable public IP; false uses the subnet-assigned public IP, if any. Neither option creates NAT or a route."
}

variable "create_ci_plan_role" {
  type        = bool
  default     = true
  description = "Legacy account-level GitHub OIDC/read-only role. Set false for customer app hosts; reuse the existing platform identity separately."
}

variable "existing_instance_profile" {
  type        = string
  default     = null
  description = "Existing operator-reviewed EC2 instance profile name. Reuse avoids creating node IAM resources; its SSM permissions and Parameter Store deny remain the profile owner's responsibility. Null preserves managed node IAM resources."
  validation {
    condition     = var.existing_instance_profile == null ? true : can(regex("^[A-Za-z0-9_+=,.@-]{1,128}$", var.existing_instance_profile))
    error_message = "existing_instance_profile must be null or an IAM instance profile name, not an ARN or path."
  }
}

variable "operator_ssh_public_key" {
  type        = string
  default     = null
  description = "Optional OpenSSH public key for railshot-operator, used over trusted SSM forwarding. Never pass a private key. Applied at first boot only."
  validation {
    condition     = var.operator_ssh_public_key == null ? true : can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/]+={0,3}( [^\\r\\n]+)?$", var.operator_ssh_public_key))
    error_message = "Supply one supported OpenSSH public key on a single line, or null."
  }
}

variable "ami_id" {
  type        = string
  description = "Reviewed exact Canonical Ubuntu 24.04 amd64 AMI in this region; no moving SSM image alias."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "ami_id must be an exact regional AMI ID."
  }
}
variable "target_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.target_id))
    error_message = "target_id must be a registered lowercase alias."
  }
}
variable "account_id" {
  type        = string
  description = "Registered AWS account; provider rejects credentials for another account."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be the explicitly registered 12-digit account."
  }
}
variable "data_disk_gib" {
  type    = number
  default = 100
  validation {
    condition     = var.data_disk_gib >= 20 && var.data_disk_gib <= 1000 && floor(var.data_disk_gib) == var.data_disk_gib
    error_message = "data_disk_gib must be 20-1000 whole GiB; shrinking existing data is unsupported."
  }
}
variable "initialize_empty_data_disk" {
  type        = bool
  default     = false
  description = "Consent to initialize this module-created blank disk only. Existing ext4 is never reformatted."
}
variable "owner_ref" {
  type        = string
  description = "Authoritative administrator state binding; common executor injects its exact state path."
  validation {
    condition     = startswith(var.owner_ref, "terraform:") && length(var.owner_ref) > 10 && length(var.owner_ref) < 2048
    error_message = "owner_ref must identify the registered Terraform state owner."
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

variable "max_run_duration_seconds" {
  type        = number
  default     = null
  description = "Optional per-start guest poweroff timer. EC2 stop retains disks; cloud-init installs it only on new instances."
  validation {
    condition = var.max_run_duration_seconds == null ? true : (
      var.max_run_duration_seconds >= 1800 && var.max_run_duration_seconds <= 604800 && floor(var.max_run_duration_seconds) == var.max_run_duration_seconds
    )
    error_message = "max_run_duration_seconds must be null or an integer from 1800 to 604800."
  }
}
