terraform {
  required_version = ">= 1.5.7"
  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "3.4.0"
    }
  }
}

# Authentication and region come from OS_* or clouds.yaml; no credential inputs.
provider "openstack" {}
