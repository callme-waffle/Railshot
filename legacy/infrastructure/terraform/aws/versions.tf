terraform {
  required_version = ">= 1.5.7" # CI pins 1.16.4 (PRD §7); the floor lets older local CLIs validate
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.66.0" }
  }
  # ponytail: local state for the one-time platform bootstrap; move to S3 (use_lockfile) when CI owns it
}

locals {
  product_tags = var.product_environment ? { ProjectOwner = "railshot-product", Target = var.target_id } : {}
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags { tags = merge({ Project = "railshot", ManagedBy = "terraform" }, local.product_tags) }
}
