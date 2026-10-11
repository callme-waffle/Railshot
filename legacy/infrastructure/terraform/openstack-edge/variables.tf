variable "name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,38}[a-z0-9]$", var.name))
    error_message = "Use a 3-40 character lowercase edge name."
  }
}

variable "vip_subnet_id" {
  description = "Existing RFC1918 IPv4 Neutron subnet for this module's new LB VIP."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.vip_subnet_id))
    error_message = "Provide the existing Neutron subnet UUID."
  }
}

variable "default_tls_container_ref" {
  description = "Existing Barbican TLS container or PKCS12 secret URL; never certificate/key bytes. Must cover every route host."
  type        = string
  validation {
    condition     = can(regex("^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/v1/(containers|secrets)/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.default_tls_container_ref))
    error_message = "Provide an HTTPS Barbican /v1/containers/UUID or /v1/secrets/UUID reference without credentials, query or fragment."
  }
}

variable "allowed_cidrs" {
  description = "Explicit IPv4 clients allowed on listener 443; no implicit world-open access."
  type        = set(string)
  validation {
    condition     = length(var.allowed_cidrs) > 0 && alltrue([for cidr in var.allowed_cidrs : can(cidrnetmask(cidr)) && try(tonumber(split("/", cidr)[1]) > 0, false)])
    error_message = "Provide nonempty IPv4 source CIDRs with a prefix length greater than zero."
  }
}

variable "enabled" {
  description = "Enable only after inactive-listener route readback. Disable before changing routes."
  type        = bool
  default     = false
}

variable "routes" {
  description = "Administrator-assigned longest-prefix host/path routes, one existing K3s NodePort member per route."
  type = map(object({
    host              = string
    path_prefix       = optional(string, "/")
    target_private_ip = string
    member_subnet_id  = string
    node_port         = number
    health_path       = string
  }))
  validation {
    condition = length(var.routes) > 0 && length(var.routes) <= 50 && alltrue([for key, route in var.routes :
      can(regex("^[a-z][a-z0-9-]{1,39}$", key)) &&
      length(route.host) <= 253 && can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", route.host)) &&
      can(cidrnetmask("${route.target_private_ip}/32")) &&
      can(regex("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)", route.target_private_ip)) &&
      can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", route.member_subnet_id)) &&
      route.node_port >= 30000 && route.node_port <= 32767 && floor(route.node_port) == route.node_port &&
      length(route.path_prefix) <= 200 && can(regex("^/([A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*)?$", route.path_prefix)) &&
      length(route.health_path) <= 1024 && can(regex("^/([A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*)?$", route.health_path))
    ])
    error_message = "Use 1-50 named routes: lowercase DNS host, RFC1918 IPv4, subnet UUID, integer NodePort 30000-32767 and absolute slash-separated alphanumeric/underscore/hyphen paths. Query, percent escapes, dots, whitespace and regex syntax are not accepted."
  }
  validation {
    condition     = length(distinct([for route in values(var.routes) : "${route.host}${route.path_prefix}"])) == length(var.routes)
    error_message = "Each host/path prefix must be unique. Root fallback and nested paths are supported."
  }
}
