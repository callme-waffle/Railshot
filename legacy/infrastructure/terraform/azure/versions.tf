terraform {
  required_version = ">= 1.5.7, < 2.0.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "= 5.7.0"
    }
  }
  # Select an encrypted, locked remote backend before shared use; no credentials here.
}

provider "azurerm" {
  subscription_id                 = var.subscription_id
  resource_provider_registrations = "none"
  features {
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
    virtual_machine {
      delete_os_disk_on_deletion = true
    }
  }
}
