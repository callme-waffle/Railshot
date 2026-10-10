variable "project_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use the reviewed GCP project ID."
  }
}
variable "zone" { type = string }
variable "instance_name" { type = string }
variable "expected_private_ip" {
  type = string
  validation {
    condition     = can(cidrnetmask("${var.expected_private_ip}/32"))
    error_message = "Use the existing VM's reviewed IPv4 address."
  }
}
variable "name" {
  type    = string
  default = "railshot-gcp-edge"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,42}[a-z0-9]$", var.name))
    error_message = "Use a lowercase GCP resource name of 2-44 characters."
  }
}
variable "hostname" {
  type = string
  validation {
    condition     = length(var.hostname) <= 253 && can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", var.hostname))
    error_message = "Use one exact lowercase DNS hostname without a wildcard or trailing dot."
  }
}
variable "node_port" {
  type    = number
  default = 30080
  validation {
    condition     = var.node_port == floor(var.node_port) && var.node_port >= 30000 && var.node_port <= 32767
    error_message = "Use the allocated Kubernetes NodePort."
  }
}
variable "health_path" {
  type    = string
  default = "/health"
  validation {
    condition     = can(regex("^/[A-Za-z0-9/._~-]*$", var.health_path)) && !strcontains(var.health_path, "//") && !contains(split("/", var.health_path), "..") && !contains(split("/", var.health_path), ".")
    error_message = "Use a plain absolute HTTP path without query, fragment or traversal."
  }
}

variable "application_certificate" {
  description = "An already ACTIVE shared wildcard certificate for new platform app routes. Existing routes retain their certificates."
  type        = object({ id = string, domain = string })
  default     = null
}

variable "routes" {
  description = "Additional application bindings on the existing VM and shared public load balancer, keyed by application ID."
  type = map(object({
    hostname       = string
    node_port      = number
    health_path    = string
    enabled        = optional(bool, true)
    certificate_id = optional(string)
  }))
  default  = {}
  nullable = false
  validation {
    condition = alltrue([for id, route in var.routes :
      can(regex("^[a-z][a-z0-9-]{0,61}[a-z0-9]$", id)) &&
      length(route.hostname) <= 253 && can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", route.hostname)) &&
      route.node_port == floor(route.node_port) && route.node_port >= 30000 && route.node_port <= 32767 &&
      can(regex("^/[A-Za-z0-9/._~-]*$", route.health_path)) && !strcontains(route.health_path, "//") &&
      !contains(split("/", route.health_path), "..") && !contains(split("/", route.health_path), ".")
    ])
    error_message = "Routes need a lowercase application ID, exact DNS hostname, allocated NodePort and plain absolute health path."
  }
  validation {
    condition = (
      length(distinct([for route in var.routes : route.hostname])) == length(var.routes) &&
      length(distinct([for route in var.routes : route.node_port])) == length(var.routes)
    )
    error_message = "Applications on this VM must have distinct hostnames and NodePorts."
  }
}
