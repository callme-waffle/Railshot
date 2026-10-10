output "loadbalancer_id" { value = openstack_lb_loadbalancer_v2.app.id }
output "listener_id" { value = openstack_lb_listener_v2.https.id }
output "vip_address" { value = openstack_lb_loadbalancer_v2.app.vip_address }
output "vip_port_id" { value = openstack_lb_loadbalancer_v2.app.vip_port_id }
output "pool_ids" { value = { for key, pool in openstack_lb_pool_v2.app : key => pool.id } }
output "policy_ids" { value = { for key, policy in openstack_lb_l7policy_v2.app : key => policy.id } }
output "app_urls" { value = { for key, route in var.routes : key => "https://${route.host}${route.path_prefix}" } }
output "readiness" { value = "configured-references-only; member health, TLS, routing and public HTTP unverified" }
