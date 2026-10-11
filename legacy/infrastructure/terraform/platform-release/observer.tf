# One-time bootstrap owns these additional rules on the existing rules-only SG.
# The node module's inline-owned SG is deliberately not a target of this module.
resource "aws_vpc_security_group_ingress_rule" "release_observer" {
  for_each                     = toset(["31490", "31491"])
  security_group_id            = "sg-0ad2a18168c4eac2b"
  referenced_security_group_id = "sg-01a71e8be9a585b5a"
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
  description                  = "Railshot control observer runtime metrics ${each.key}"
}

# Import the four existing observer rules before applying this bootstrap module.
resource "aws_vpc_security_group_egress_rule" "release_observer" {
  for_each = {
    "aws-31490" = { provider = "aws", cidr = "172.31.13.147/32", port = 31490 }
    "aws-31491" = { provider = "aws", cidr = "172.31.13.147/32", port = 31491 }
    "gcp-31490" = { provider = "gcp", cidr = "34.47.68.21/32", port = 31490 }
    "gcp-31491" = { provider = "gcp", cidr = "34.47.68.21/32", port = 31491 }
  }
  security_group_id = "sg-02925a97753d8d3e9"
  cidr_ipv4         = each.value.cidr
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  description       = "Railshot shared observer ${each.value.provider} metrics ${each.value.port}"
}
