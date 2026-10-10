data "openstack_networking_subnet_v2" "vip" {
  subnet_id = var.vip_subnet_id
  lifecycle {
    postcondition {
      condition = alltrue([for offset in [0, -1] :
        can(cidrnetmask(self.cidr)) && can(regex("^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)", cidrhost(self.cidr, offset)))
      ])
      error_message = "The existing VIP subnet must lie entirely inside an RFC1918 IPv4 range."
    }
  }
}

resource "openstack_lb_loadbalancer_v2" "app" {
  name                  = var.name
  vip_subnet_id         = data.openstack_networking_subnet_v2.vip.id
  loadbalancer_provider = "amphora"
  admin_state_up        = true
}

resource "openstack_lb_listener_v2" "https" {
  name                      = "${var.name}-https"
  loadbalancer_id           = openstack_lb_loadbalancer_v2.app.id
  protocol                  = "TERMINATED_HTTPS"
  protocol_port             = 443
  default_tls_container_ref = var.default_tls_container_ref
  allowed_cidrs             = sort(tolist(var.allowed_cidrs))
  admin_state_up            = var.enabled
  # No default_pool_id: unmatched host/path requests must not reach an app.
}

resource "openstack_lb_pool_v2" "app" {
  for_each        = var.routes
  name            = "${var.name}-${each.key}"
  loadbalancer_id = openstack_lb_loadbalancer_v2.app.id
  protocol        = "HTTP"
  lb_method       = "ROUND_ROBIN"
  # Deliberately not listener_id: assigning a pool directly to the listener
  # would make it a default pool and bypass the host/path policy on misses.
}

resource "openstack_lb_member_v2" "app" {
  for_each      = var.routes
  name          = each.key
  pool_id       = openstack_lb_pool_v2.app[each.key].id
  subnet_id     = each.value.member_subnet_id
  address       = each.value.target_private_ip
  protocol_port = each.value.node_port
}

resource "openstack_lb_monitor_v2" "app" {
  for_each       = var.routes
  name           = "${var.name}-${each.key}"
  pool_id        = openstack_lb_pool_v2.app[each.key].id
  type           = "HTTP"
  delay          = 10
  timeout        = 5
  max_retries    = 3
  http_method    = "GET"
  http_version   = "1.1"
  domain_name    = each.value.host
  url_path       = each.value.health_path
  expected_codes = "200"
}

resource "openstack_lb_l7policy_v2" "app" {
  for_each         = var.routes
  name             = "${var.name}-${each.key}"
  listener_id      = openstack_lb_listener_v2.https.id
  action           = "REDIRECT_TO_POOL"
  redirect_pool_id = openstack_lb_pool_v2.app[each.key].id
  # No position: host/path plus inverted descendant rules are mutually exclusive.
}

resource "openstack_lb_l7rule_v2" "host" {
  for_each     = var.routes
  l7policy_id  = openstack_lb_l7policy_v2.app[each.key].id
  type         = "HOST_NAME"
  compare_type = "EQUAL_TO"
  value        = each.value.host
}

resource "openstack_lb_l7rule_v2" "path" {
  for_each     = var.routes
  l7policy_id  = openstack_lb_l7policy_v2.app[each.key].id
  type         = "PATH"
  compare_type = "REGEX"
  value        = local.path_patterns[each.key]
}

resource "openstack_lb_l7rule_v2" "exclude_child" {
  for_each     = local.path_exclusions
  l7policy_id  = openstack_lb_l7policy_v2.app[each.value.parent].id
  type         = "PATH"
  compare_type = "REGEX"
  value        = local.path_patterns[each.value.child]
  invert       = true
}
