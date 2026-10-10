# Mock providers exercise the rendered IAM/credential contract without cloud writes.
mock_provider "aws" {
  mock_resource "aws_ssm_document" {
    defaults = { hash_type = "Sha256" }
  }
}

mock_provider "google" {
  mock_data "google_project" {
    defaults = { number = "359201781699" }
  }
  mock_data "google_compute_instance" {
    defaults = {
      network_interface = [{ network = "projects/railshot-poc-20261001/global/networks/railshot-runtime" }]
      service_account   = [{ email = "runtime@railshot-poc-20261001.iam.gserviceaccount.com" }]
    }
  }
  mock_resource "google_iam_workload_identity_pool" {
    defaults = { name = "projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release" }
  }
  mock_resource "google_iam_workload_identity_pool_provider" {
    defaults = { name = "projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release/providers/aws-control" }
  }
  mock_resource "google_service_account" {
    defaults = {
      email = "railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com"
      name  = "projects/railshot-poc-20261001/serviceAccounts/railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com"
    }
  }
}

run "exact_instance_and_application_lifecycle_authority" {
  command = apply

  assert {
    condition = (
      jsondecode(aws_ssm_document.release.content).parameters.Scope.default == "multicloud" &&
      toset(jsondecode(aws_ssm_document.release.content).parameters.Scope.allowedValues) == toset(["ci-runtime", "multicloud"]) &&
      !contains(jsondecode(aws_ssm_document.release.content).mainSteps[0].inputs.runCommand, "test -f /etc/railshot/release.json")
    )
    error_message = "The fixed executor must accept only CI or full release scope and report missing configuration through its JSON receipt."
  }

  assert {
    condition = (
      google_iam_workload_identity_pool_provider.release.aws[0].account_id == "721622471953" &&
      google_iam_workload_identity_pool_provider.release.attribute_mapping["google.subject"] == "assertion.arn" &&
      google_iam_workload_identity_pool_provider.release.attribute_condition == "assertion.account == '721622471953' && assertion.arn == 'arn:aws:sts::721622471953:assumed-role/railshot-control-poc/i-033ae2db907fde68e'" &&
      google_service_account_iam_member.control_federation.member == "principal://iam.googleapis.com/projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release/subject/arn:aws:sts::721622471953:assumed-role/railshot-control-poc/i-033ae2db907fde68e" &&
      google_service_account_iam_member.control_federation.role == "roles/iam.workloadIdentityUser"
    )
    error_message = "Only the exact AWS account, role and EC2 session may impersonate the release service account."
  }

  assert {
    condition = (
      google_project_iam_member.edge_release.project == "railshot-poc-20261001" &&
      google_project_iam_member.edge_release.member == "serviceAccount:railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com" &&
      google_project_iam_member.edge_release.role == google_project_iam_custom_role.edge_release.name &&
      length(google_project_iam_member.edge_release.condition) == 0 &&
      google_project_iam_custom_role.edge_release.role_id == "railshotExistingEdgeRelease" &&
      toset(google_project_iam_custom_role.edge_release.permissions) == toset([
        "resourcemanager.projects.get",
        "serviceusage.services.list",
        "serviceusage.services.use",
        "compute.instances.get",
        "compute.instances.list",
        "compute.disks.get",
        "compute.networkEndpointGroups.get",
        "compute.networkEndpointGroups.use",
        "compute.firewalls.get",
        "compute.firewalls.update",
        "compute.networks.updatePolicy",
        "compute.healthChecks.get",
        "compute.healthChecks.update",
        "compute.healthChecks.useReadOnly",
        "compute.backendServices.get",
        "compute.backendServices.update",
        "compute.backendServices.use",
        "compute.urlMaps.get",
        "compute.urlMaps.update",
        "compute.urlMaps.use",
        "compute.targetHttpProxies.get",
        "compute.targetHttpProxies.setUrlMap",
        "compute.targetHttpProxies.use",
        "compute.targetHttpsProxies.get",
        "compute.targetHttpsProxies.setUrlMap",
        "compute.targetHttpsProxies.setCertificateMap",
        "compute.targetHttpsProxies.use",
        "compute.globalAddresses.get",
        "compute.globalAddresses.setLabels",
        "compute.globalForwardingRules.get",
        "compute.globalForwardingRules.setLabels",
        "compute.globalForwardingRules.setTarget",
        "compute.globalForwardingRules.update",
        "compute.globalOperations.get",
        "certificatemanager.dnsauthorizations.get",
        "certificatemanager.dnsauthorizations.update",
        "certificatemanager.certs.get",
        "certificatemanager.certs.update",
        "certificatemanager.certs.use",
        "certificatemanager.certmaps.get",
        "certificatemanager.certmaps.update",
        "certificatemanager.certmaps.use",
        "certificatemanager.certmapentries.get",
        "certificatemanager.certmapentries.update",
        "certificatemanager.operations.get",
        "compute.networkEndpointGroups.create",
        "compute.networkEndpointGroups.delete",
        "compute.networkEndpointGroups.attachNetworkEndpoints",
        "compute.networkEndpointGroups.detachNetworkEndpoints",
        "compute.networkEndpointGroups.list",
        "compute.networks.use",
        "compute.subnetworks.use",
        "compute.zoneOperations.get",
        "compute.healthChecks.create",
        "compute.healthChecks.delete",
        "compute.healthChecks.list",
        "compute.backendServices.create",
        "compute.backendServices.list",
        "certificatemanager.dnsauthorizations.create",
        "certificatemanager.dnsauthorizations.delete",
        "certificatemanager.dnsauthorizations.list",
        "certificatemanager.dnsauthorizations.use",
        "certificatemanager.certs.create",
        "certificatemanager.certs.delete",
        "certificatemanager.certs.list",
        "certificatemanager.certmapentries.create",
        "certificatemanager.certmapentries.delete",
        "certificatemanager.certmapentries.list",
      ])
    )
    error_message = "Preserve existing permissions, including the VPC updatePolicy dependency required by firewall PATCH, with no wildcard or extra grants."
  }

  assert {
    condition = (
      google_project_iam_custom_role.app_edge_bound.project == "railshot-poc-20261001" &&
      google_project_iam_custom_role.app_edge_bound.role_id == "railshotAppBoundEdgeUse" &&
      toset(google_project_iam_custom_role.app_edge_bound.permissions) == toset(["compute.backendServices.delete", "compute.instances.use"]) &&
      google_project_iam_member.app_edge_bound.project == "railshot-poc-20261001" &&
      google_project_iam_member.app_edge_bound.member == google_project_iam_member.edge_release.member &&
      google_project_iam_member.app_edge_bound.role == google_project_iam_custom_role.app_edge_bound.name &&
      length(google_project_iam_member.app_edge_bound.condition) == 1 &&
      google_project_iam_member.app_edge_bound.condition[0].title == "registered-app-backend-and-runtime" &&
      google_project_iam_member.app_edge_bound.condition[0].expression == "(resource.type == 'compute.googleapis.com/BackendService' && resource.name.startsWith('projects/railshot-poc-20261001/global/backendServices/railshot-gcp-edge-')) || (resource.type == 'compute.googleapis.com/Instance' && resource.name == 'projects/railshot-poc-20261001/zones/asia-northeast3-a/instances/railshot-gcp-poc')" &&
      !contains(google_project_iam_custom_role.edge_release.permissions, "compute.backendServices.delete") &&
      !contains(google_project_iam_custom_role.edge_release.permissions, "compute.instances.use")
    )
    error_message = "The two name-scoped grants must never enter the unconditional role; bind only the app backend prefix and exact registered runtime VM."
  }

  assert {
    condition = toset([for permission in setunion(
      toset(google_project_iam_custom_role.edge_release.permissions),
      toset(google_project_iam_custom_role.app_edge_bound.permissions)
      ) : permission if can(regex("\\.(create|delete|attachNetworkEndpoints|detachNetworkEndpoints)$", permission))]) == toset([
      "compute.networkEndpointGroups.create", "compute.networkEndpointGroups.delete",
      "compute.networkEndpointGroups.attachNetworkEndpoints", "compute.networkEndpointGroups.detachNetworkEndpoints",
      "compute.healthChecks.create", "compute.healthChecks.delete",
      "compute.backendServices.create", "compute.backendServices.delete",
      "certificatemanager.dnsauthorizations.create", "certificatemanager.dnsauthorizations.delete",
      "certificatemanager.certs.create", "certificatemanager.certs.delete",
      "certificatemanager.certmapentries.create", "certificatemanager.certmapentries.delete",
    ])
    error_message = "Only the app resource API families may gain lifecycle verbs; shared frontend, VM, firewall, URL map and certificate map creation/deletion remain ungranted."
  }

  assert {
    condition = (
      google_iap_tunnel_instance_iam_member.runtime_release.project == "railshot-poc-20261001" &&
      google_iap_tunnel_instance_iam_member.runtime_release.zone == "asia-northeast3-a" &&
      google_iap_tunnel_instance_iam_member.runtime_release.instance == "railshot-gcp-poc" &&
      google_iap_tunnel_instance_iam_member.runtime_release.role == "roles/iap.tunnelResourceAccessor" &&
      google_iap_tunnel_instance_iam_member.runtime_release.member == google_project_iam_member.edge_release.member &&
      google_iap_tunnel_instance_iam_member.runtime_release.condition[0].expression == "destination.port == 22"
    )
    error_message = "IAP tunnel access must be limited to SSH on the one existing runtime VM."
  }

  assert {
    condition = (
      google_compute_firewall.release_observer.network == "projects/railshot-poc-20261001/global/networks/railshot-runtime" &&
      google_compute_firewall.release_observer.source_ranges == toset(["52.78.97.236/32"]) &&
      google_compute_firewall.release_observer.target_service_accounts == toset(["runtime@railshot-poc-20261001.iam.gserviceaccount.com"]) &&
      one(google_compute_firewall.release_observer.allow).protocol == "tcp" &&
      toset(one(google_compute_firewall.release_observer.allow).ports) == toset(["31490", "31491"]) &&
      alltrue([for port, rule in aws_vpc_security_group_ingress_rule.release_observer :
        rule.security_group_id == "sg-0ad2a18168c4eac2b" &&
        rule.referenced_security_group_id == "sg-01a71e8be9a585b5a" &&
        rule.ip_protocol == "tcp" && rule.from_port == tonumber(port) && rule.to_port == tonumber(port)
      ])
    )
    error_message = "Observer bootstrap must only open the two metric ports from the existing control source."
  }

  assert {
    condition = (
      toset(keys(aws_vpc_security_group_egress_rule.release_observer)) == toset(["aws-31490", "aws-31491", "gcp-31490", "gcp-31491"]) &&
      alltrue([for key, rule in aws_vpc_security_group_egress_rule.release_observer :
        rule.security_group_id == "sg-02925a97753d8d3e9" &&
        rule.cidr_ipv4 == (startswith(key, "aws-") ? "172.31.13.147/32" : "34.47.68.21/32") &&
        rule.ip_protocol == "tcp" && rule.from_port == tonumber(split("-", key)[1]) && rule.to_port == rule.from_port &&
        rule.description == "Railshot shared observer ${split("-", key)[0]} metrics ${split("-", key)[1]}"
      ])
    )
    error_message = "Observer egress must preserve exactly the four existing provider/metric-port tuples on the control rules-only SG."
  }

  assert {
    condition = (
      jsondecode(output.gcp_external_account_json).type == "external_account" &&
      jsondecode(output.gcp_external_account_json).audience == "//iam.googleapis.com/projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release/providers/aws-control" &&
      jsondecode(output.gcp_external_account_json).service_account_impersonation_url == "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com:generateAccessToken" &&
      jsondecode(output.gcp_external_account_json).credential_source.imdsv2_session_token_url == "http://169.254.169.254/latest/api/token" &&
      !can(jsondecode(output.gcp_external_account_json).private_key) &&
      !can(jsondecode(output.gcp_external_account_json).refresh_token)
    )
    error_message = "The host credential file must be keyless AWS IMDSv2 configuration for the exact audience and service account."
  }
}

run "wrong_project_number_rejected" {
  command = plan
  override_data {
    target = data.google_project.release
    values = { number = "359201781698" }
  }
  expect_failures = [data.google_project.release]
}

# Exercise the actual sibling module, so an app-name or default-name change cannot
# silently put new backends outside the IAM prefix (or include the legacy app).
run "app_backend_prefix_matches_native_module" {
  command = plan
  module { source = "../gcp-edge" }
  variables {
    project_id          = "railshot-poc-20261001"
    zone                = "asia-northeast3-a"
    instance_name       = "railshot-gcp-poc"
    expected_private_ip = "10.66.0.2"
    hostname            = "baseline.railshot.io"
    routes = {
      app-0123456789abcdef01234567 = { hostname = "app-012345abcdef.railshot.io", node_port = 31001, health_path = "/health" }
    }
  }
  override_data {
    target = data.google_compute_instance.backend
    values = {
      name = "railshot-gcp-poc"
      network_interface = [{
        network_ip = "10.66.0.2"
        network    = "projects/railshot-poc-20261001/global/networks/railshot-runtime"
        subnetwork = "projects/railshot-poc-20261001/regions/asia-northeast3/subnetworks/railshot-runtime"
      }]
      service_account = [{ email = "runtime@railshot-poc-20261001.iam.gserviceaccount.com" }]
    }
  }
  assert {
    condition = (
      google_compute_backend_service.app.name == "railshot-gcp-edge" &&
      google_compute_backend_service.routes["app-0123456789abcdef01234567"].name == "railshot-gcp-edge-${substr(sha256("app-0123456789abcdef01234567"), 0, 16)}" &&
      startswith("projects/railshot-poc-20261001/global/backendServices/${google_compute_backend_service.routes["app-0123456789abcdef01234567"].name}", "projects/railshot-poc-20261001/global/backendServices/railshot-gcp-edge-") &&
      !startswith("projects/railshot-poc-20261001/global/backendServices/${google_compute_backend_service.app.name}", "projects/railshot-poc-20261001/global/backendServices/railshot-gcp-edge-") &&
      !startswith("projects/another-project/global/backendServices/${google_compute_backend_service.routes["app-0123456789abcdef01234567"].name}", "projects/railshot-poc-20261001/global/backendServices/railshot-gcp-edge-")
    )
    error_message = "The registered default module must hash app IDs under the allowed prefix while excluding the legacy baseline and every other project."
  }
}
