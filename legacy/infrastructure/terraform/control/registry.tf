# Trusted release environment can deliver one read:packages credential. Its
# value is written by GitHub Actions, never by Terraform or in its state.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}
resource "aws_iam_role" "pull_sync" {
  name = "railshot-ghcr-pull-sync"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:Jasmin-Softbank@335003159/railshot-apps@1397698801:environment:railshot-release"
      } }
    }]
  })
}
resource "aws_iam_role_policy" "pull_sync" {
  name = "write-one-encrypted-pull-credential"
  role = aws_iam_role.pull_sync.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow", Action = ["ssm:PutParameter"]
      Resource = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${local.pull_parameter}"
    }]
  })
}
output "pull_sync_role_arn" { value = aws_iam_role.pull_sync.arn }
