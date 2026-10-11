# Rules-only target SG: attach through the app-host module. ALB rules are
# owned by aws-edge; cluster management is only from the operations node.
resource "aws_security_group" "user_aws" {
  name        = "railshot-user-aws-routed"
  description = "Private RAILSHOT management and ALB traffic"
  vpc_id      = var.vpc_id
}
resource "aws_security_group_rule" "user_api_in" {
  type                     = "ingress"
  from_port                = 6443
  to_port                  = 6443
  protocol                 = "tcp"
  security_group_id        = aws_security_group.user_aws.id
  source_security_group_id = aws_security_group.control.id
}
resource "aws_security_group_rule" "user_api_out" {
  type                     = "egress"
  from_port                = 6443
  to_port                  = 6443
  protocol                 = "tcp"
  security_group_id        = aws_security_group.edge.id
  source_security_group_id = aws_security_group.user_aws.id
}
output "user_aws_security_group_id" { value = aws_security_group.user_aws.id }
