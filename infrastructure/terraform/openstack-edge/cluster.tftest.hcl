mock_provider "openstack" {
  mock_data "openstack_networking_subnet_v2" {
    defaults = { cidr = "10.10.0.0/24" }
  }
}
variables {
  name                      = "railshot-cluster"
  vip_subnet_id             = "11111111-1111-1111-1111-111111111111"
  vip_address               = "10.10.0.10"
  default_tls_container_ref = "https://barbican.example.com/v1/containers/11111111-1111-1111-1111-111111111111"
  allowed_cidrs             = ["10.10.0.0/24"]
  routes = {
    app = { host = "app.example.com", target_private_ip = "10.10.0.2", member_subnet_id = "22222222-2222-2222-2222-222222222222", node_port = 30080, health_path = "/health" }
  }
  compute_nodes = {
    compute-a = { resource_id = "22222222-2222-2222-2222-222222222222", private_ip = "10.10.0.2", availability_zone = "az-a", subnet_id = "22222222-2222-2222-2222-222222222222" }
    compute-b = { resource_id = "33333333-3333-3333-3333-333333333333", private_ip = "10.10.0.3", availability_zone = "az-b", subnet_id = "33333333-3333-3333-3333-333333333333" }
  }
}
run "two_private_compute_members" {
  command = plan
  assert {
    condition     = openstack_lb_member_v2.app["app"].address == "10.10.0.2" && openstack_lb_member_v2.compute["app:compute-b"].address == "10.10.0.3" && openstack_lb_member_v2.compute["app:compute-b"].subnet_id == "33333333-3333-3333-3333-333333333333" && length(openstack_lb_member_v2.compute) == 1
    error_message = "Both compute IPs must be registered in the pool with their own subnets."
  }
  assert {
    condition     = openstack_lb_monitor_v2.app["app"].expected_codes == "200"
    error_message = "A successful backend health check must be exact 200."
  }
}
