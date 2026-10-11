# Bootstrap authority installs federation and the reviewed edge API permissions.
# Application lifecycle uses create/delete/attach verbs below; the executor checks
# exact app ownership where IAM cannot constrain resource names. No IAM mutation,
# service enable, VM mutation, or shared frontend deletion is granted.
locals {
  gcp_project_id     = "railshot-poc-20261001"
  gcp_project_number = "359201781699"
  aws_control_arn    = "arn:aws:sts::721622471953:assumed-role/railshot-control-poc/i-033ae2db907fde68e"
}

provider "google" {
  project = local.gcp_project_id
  region  = "asia-northeast3"
  zone    = "asia-northeast3-a"
}

data "google_project" "release" {
  project_id = local.gcp_project_id
  lifecycle {
    postcondition {
      condition     = tostring(self.number) == local.gcp_project_number
      error_message = "The WIF audience must belong to the reviewed GCP project number."
    }
  }
}

resource "google_project_service" "federation" {
  for_each = toset([
    "iam.googleapis.com", "iamcredentials.googleapis.com",
    "sts.googleapis.com", "cloudresourcemanager.googleapis.com", "iap.googleapis.com",
  ])
  project            = data.google_project.release.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_iam_workload_identity_pool" "release" {
  project                   = data.google_project.release.project_id
  workload_identity_pool_id = "railshot-aws-release"
  display_name              = "Railshot AWS control release"
  description               = "One existing AWS control instance updates the existing GCP edge."
  disabled                  = false
  depends_on                = [google_project_service.federation]
}

resource "google_iam_workload_identity_pool_provider" "release" {
  project                            = data.google_project.release.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.release.workload_identity_pool_id
  workload_identity_pool_provider_id = "aws-control"
  display_name                       = "Existing control instance"
  disabled                           = false
  aws { account_id = "721622471953" }
  attribute_mapping = {
    "google.subject"    = "assertion.arn"
    "attribute.account" = "assertion.account"
  }
  # Equality includes the instance-profile role AND the exact EC2 session name.
  attribute_condition = "assertion.account == '721622471953' && assertion.arn == '${local.aws_control_arn}'"
}

resource "google_service_account" "edge_release" {
  project      = data.google_project.release.project_id
  account_id   = "railshot-gcp-edge-release"
  display_name = "Railshot existing GCP edge release"
  # Retain historical identity metadata to avoid an unrelated service-account
  # update. The roles below, not this original description, define its authority.
  description = "Keyless AWS control-host federation; existing edge read and in-place updates only."
  depends_on  = [google_project_service.federation]
}

resource "google_service_account_iam_member" "control_federation" {
  service_account_id = google_service_account.edge_release.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.release.name}/subject/${local.aws_control_arn}"
}

# Ansible uses its existing operator SSH key through start-iap-tunnel; it does
# not publish keys to project/VM metadata or use OS Login.
resource "google_iap_tunnel_instance_iam_member" "runtime_release" {
  project  = data.google_project.release.project_id
  zone     = "asia-northeast3-a"
  instance = "railshot-gcp-poc"
  role     = "roles/iap.tunnelResourceAccessor"
  member   = "serviceAccount:${google_service_account.edge_release.email}"
  condition {
    title       = "operator-ssh-only"
    description = "Existing runtime VM SSH through IAP."
    expression  = "destination.port == 22"
  }
  depends_on = [google_project_service.federation]
}

# Observer connectivity is installed once by bootstrap authority. The release
# executor's gcp-edge state does not own this firewall or the existing VM.
data "google_compute_instance" "runtime" {
  project = data.google_project.release.project_id
  zone    = "asia-northeast3-a"
  name    = "railshot-gcp-poc"
}

resource "google_compute_firewall" "release_observer" {
  project                 = data.google_project.release.project_id
  name                    = "railshot-release-observer"
  network                 = one(data.google_compute_instance.runtime.network_interface).network
  direction               = "INGRESS"
  source_ranges           = ["52.78.97.236/32"]
  target_service_accounts = [one(data.google_compute_instance.runtime.service_account).email]
  description             = "Existing control observer reaches only the runtime metric NodePorts."
  allow {
    protocol = "tcp"
    ports    = ["31490", "31491"]
  }
}

resource "google_project_iam_custom_role" "edge_release" {
  project     = data.google_project.release.project_id
  role_id     = "railshotExistingEdgeRelease"
  title       = "Railshot existing edge release"
  description = "Existing edge updates and app lifecycle API families; project/parent scope, with exact app ownership enforced by the executor."
  permissions = [
    # google_project_service.Read checks project existence and lists enabled APIs.
    "resourcemanager.projects.get",
    "serviceusage.services.list",
    "serviceusage.services.use",
    # Backend data source expands boot-disk details; gcloud IAP requires get/list.
    "compute.instances.get",
    "compute.instances.list",
    "compute.disks.get",
    # Existing NEG reads and backend reference permission are preserved.
    "compute.networkEndpointGroups.get",
    "compute.networkEndpointGroups.use",
    # Existing Compute edge resources and references used by their updates.
    "compute.firewalls.get",
    "compute.firewalls.update",
    # Firewall PATCH also checks this permission on the referenced VPC.
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
    # Certificate Manager PATCH operations, map references, and LRO polling.
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
    # These 23 additional permissions use project/parent scope or resource types
    # without documented IAM resource.name support. They are not app-name scoped
    # IAM grants; gcp_routes/application_cleanup enforce exact app-owned plans.
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
  ]
}

# Preserve the original binding and its existing read/update scope. This role
# cannot contain backend deletion or VM use: its binding is unconditional.
resource "google_project_iam_member" "edge_release" {
  project = data.google_project.release.project_id
  role    = google_project_iam_custom_role.edge_release.name
  member  = "serviceAccount:${google_service_account.edge_release.email}"
}

# Only these two added permissions support the resource names needed here.
# gcp-edge's default name is railshot-gcp-edge; app names append a hyphen and
# 16 hex characters from the application ID hash. The baseline has no suffix.
resource "google_project_iam_custom_role" "app_edge_bound" {
  project     = data.google_project.release.project_id
  role_id     = "railshotAppBoundEdgeUse"
  title       = "Railshot app backend deletion and existing VM endpoint use"
  description = "App backend prefix deletion; use only the registered runtime VM as a NEG endpoint."
  permissions = ["compute.backendServices.delete", "compute.instances.use"]
}

resource "google_project_iam_member" "app_edge_bound" {
  project = data.google_project.release.project_id
  role    = google_project_iam_custom_role.app_edge_bound.name
  member  = "serviceAccount:${google_service_account.edge_release.email}"
  condition {
    title       = "registered-app-backend-and-runtime"
    description = "Existing baseline backend excluded; VM use does not permit VM mutation or deletion."
    expression  = "(resource.type == 'compute.googleapis.com/BackendService' && resource.name.startsWith('projects/${local.gcp_project_id}/global/backendServices/railshot-gcp-edge-')) || (resource.type == 'compute.googleapis.com/Instance' && resource.name == 'projects/${local.gcp_project_id}/zones/asia-northeast3-a/instances/railshot-gcp-poc')"
  }
}

output "gcp_release_identity" {
  value = {
    project_id      = local.gcp_project_id
    project_number  = local.gcp_project_number
    pool            = google_iam_workload_identity_pool.release.name
    provider        = google_iam_workload_identity_pool_provider.release.name
    service_account = google_service_account.edge_release.email
    aws_subject     = local.aws_control_arn
  }
}

# Public configuration, not a key or token. AIP-4117 AWS IMDSv2 credential shape;
# equivalent to gcloud create-cred-config --aws --enable-imdsv2 --service-account.
output "gcp_external_account_json" {
  value = jsonencode({
    universe_domain                   = "googleapis.com"
    type                              = "external_account"
    audience                          = "//iam.googleapis.com/${google_iam_workload_identity_pool_provider.release.name}"
    subject_token_type                = "urn:ietf:params:aws:token-type:aws4_request"
    token_url                         = "https://sts.googleapis.com/v1/token"
    service_account_impersonation_url = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${google_service_account.edge_release.email}:generateAccessToken"
    credential_source = {
      environment_id                 = "aws1"
      region_url                     = "http://169.254.169.254/latest/meta-data/placement/availability-zone"
      url                            = "http://169.254.169.254/latest/meta-data/iam/security-credentials"
      regional_cred_verification_url = "https://sts.{region}.amazonaws.com?Action=GetCallerIdentity&Version=2011-06-15"
      imdsv2_session_token_url       = "http://169.254.169.254/latest/api/token"
    }
  })
}
