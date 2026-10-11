mock_provider "aws" {
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0", availability_zone = "ap-northeast-2a" }
  }
  mock_data "aws_route53_zone" {
    defaults = { private_zone = false, name = "example.com." }
  }
  mock_data "aws_instance" {
    defaults = { private_ip = "10.10.0.2", availability_zone = "ap-northeast-2a", subnet_id = "subnet-0123456789abcdef0", vpc_security_group_ids = ["sg-22222222222222222"] }
  }
}
variables {
  name              = "railshot-cluster"
  account_id        = "123456789012"
  region            = "ap-northeast-2"
  vpc_id            = "vpc-0123456789abcdef0"
  public_subnet_ids = ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
  base_domain       = "example.com"
  zone_id           = "ZEXAMPLE"
  certificate_arn   = "arn:aws:acm:ap-northeast-2:123456789012:certificate/abcd"
  routes = {
    app = { host = "app.example.com", provider_kind = "aws", target_private_ip = "10.10.0.2", target_security_group_id = "sg-22222222222222222", node_port = 30080, health_path = "/health", expected_status = 200, priority = 10 }
  }
  compute_nodes = {
    compute-a = { resource_id = "i-22222222222222222", private_ip = "10.10.0.2", availability_zone = "ap-northeast-2a", security_group_id = "sg-22222222222222222" }
    compute-b = { resource_id = "i-33333333333333333", private_ip = "10.10.0.3", availability_zone = "ap-northeast-2b", security_group_id = "sg-33333333333333333" }
  }
}
run "two_compute_targets_across_az" {
  command = plan
  override_data {
    target = data.aws_route53_zone.public[0]
    values = { private_zone = false, name = "example.com." }
  }
  override_data {
    target = data.aws_subnet.alb["subnet-0123456789abcdef1"]
    values = { vpc_id = "vpc-0123456789abcdef0", availability_zone = "ap-northeast-2b" }
  }
  override_data {
    target = data.aws_instance.compute["compute-b"]
    values = { private_ip = "10.10.0.3", availability_zone = "ap-northeast-2b", subnet_id = "subnet-0123456789abcdef1", vpc_security_group_ids = ["sg-33333333333333333"] }
  }
  assert {
    condition     = aws_lb_target_group_attachment.app["app"].target_id == "10.10.0.2" && aws_lb_target_group_attachment.compute["app:compute-b"].target_id == "10.10.0.3" && length(aws_lb_target_group_attachment.compute) == 1
    error_message = "Register both compute IPs once while retaining the primary attachment address."
  }
  assert {
    condition     = one(aws_lb_target_group.app["app"].health_check).matcher == "200" && length(aws_security_group_rule.target_from_alb) == 2
    error_message = "Use exact application success and open only both bound compute SGs on the NodePort."
  }
}
