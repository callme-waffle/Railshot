# Existing platform release bootstrap

This module is applied by the bootstrap operator using their existing AWS and
GCP authority. It installs the fixed SSM release entrypoint, GitHub OIDC role,
AWS-to-GCP federation and observer ingress for the existing machines. It does
not create a VM, security group, load balancer, service-account key or customer
application. Keep its local state separate from the provider edge states.

## CI runtime promotion

A trusted automatic platform release runs `ci-runtime` only when CI runner or
app-workflow source changes, after platform publication and live verification.
API/dashboard-only releases skip worker and provider reconciliation. Full provider
maintenance requires an explicit manual `multicloud` request. CI promotion runs even when
`RAILSHOT_MULTICLOUD_RELEASE` is disabled. It uses the same fixed SSM document,
control instance and OIDC role with `Scope=ci-runtime`; no additional IAM action
or resource is granted. Apply the reviewed document update and set its new
`RAILSHOT_RELEASE_DOCUMENT_VERSION` / `RAILSHOT_RELEASE_DOCUMENT_SHA256` outputs
before activating this workflow revision.

The root-owned 0600 `/etc/railshot/release.json` needs only these keys for CI:

```json
{
  "version": 1,
  "state_dir": "/var/lib/railshot-release",
  "workers": {
    "runner_url": "https://github.com/Jasmin-Softbank/railshot-apps",
    "build_node": "<existing build node>",
    "object_uids": "<object returned by platform_workers.py discover>"
  },
  "apps": {"repository": "Jasmin-Softbank/railshot-apps", "branch": "main"}
}
```

Replace `object_uids` with the discovered object, not the placeholder string.
The full provider configuration may coexist in this file, but CI does not read
provider credentials, target registrations, runtime policies or edge state.
The existing GitHub token needs the already used apps-repository workflow/content
and Actions-variable update permissions; it is never returned in a receipt.

Promotion suspends replenishment and waits for the current controller tick to
finish. It updates the controller image and future runner template, reads them
back, pins the apps workflow and `PLATFORM_REF` to the same admitted source, then
resumes replenishment and verifies controller execution. CI reads and updates
only the build controller CronJob and its ConfigMap; credential renewal belongs
to the provider rollout and cannot block CI promotion. Existing runner Jobs keep
their original images and running workflows. Requests dispatched after promotion
use the newly pinned workflow; previously dispatched runs retain their original
GitHub workflow revision. A failed or uncertain promotion keeps a private receipt
for reconciliation and is never automatically replayed.

CI receipts are under `state_dir/ci-runtime/<source-sha>` and are independent of
provider rollout receipts. The optional `multicloud` stage starts after CI and
reads back that same CI/apps promotion rather than applying it twice, and updates
and verifies only the credential renewal worker before its providers. A node
or edge failure cannot roll back a completed CI promotion. Both scopes share the
existing control-host lock. Missing release configuration now returns the fixed
`RELEASE_CONFIG_UNAVAILABLE` code instead of exiting before a JSON receipt exists.

The release executor cannot bootstrap its own authority. The same GCP service
account serves platform edge releases and API application registration/lifecycle.
Its original custom role retains all 44 read/update permissions and adds the 23
project/parent permissions described below. A second custom role conditionally
grants app backend deletion and use of the one existing runtime VM as an endpoint.
The existing IAP tunnel binding is unchanged. Neither role grants IAM mutation,
service enable, VM mutation, or shared frontend creation/deletion.

IAM limits API families and the project; it does not enforce exact app ownership
for every operation. The platform `edge_update.py` gate still rejects destructive
release plans. API `gcp_routes.py` and `application_cleanup.py` separately check
app bindings, original state lineage/IDs, saved plans and allowed shared edits.
These executor checks are required even when IAM allows an API call. The service
account's historical read/update description is deliberately retained to avoid an
unrelated identity update; its attached role documents define current authority.

## Acknowledge a repaired observation failure

After the operator repairs the observer/collector, an explicit command on the
control host can clear the previous-attempt barrier without replaying a failed
release. Use the reviewed source checkout and existing operator authentication:

```sh
python3 deployment/scripts/multicloud_release.py \
  --config /etc/railshot/release.json \
  --manifest /var/lib/railshot-release/<failed-source-sha>/manifest.json \
  --reconcile
```

The command requires the existing release GitHub credential in its process
environment (load the existing Kubernetes Secret in memory; never paste it into
the command or a file). It accepts only node-only failures whose runtime apply
already verified an unchanged policy. It reads the current verified CI/platform
and credentials worker, then checks all three original node identities,
registrations, management TLS, exporters and fresh canonical collector samples.
It performs no runtime install, observer registration, edge operation, workflow
promotion or application deployment. Coordinate it with other releases so the
platform and canonical registrar remain stable during verification.

Success is `reconciled`, not `verified`: the original failed receipts,
`last-attempt.json` and `current.json` remain unchanged. Separate private
`reconciliation.json` files bind the original receipt bytes and live evidence;
target markers retain the original receipt path and reference that evidence.
Only a later source can proceed through the automatic pipeline. The same failed
source still cannot be replayed. If one target remains unhealthy, no aggregate
acknowledgement is written; retain any partial target evidence and investigate
before another explicit invocation. Changed runtime policies, application/edge
failures and unknown runtime mutations need a separate recovery procedure.

## Keyless control-host authentication

The AWS provider trusts account `721622471953` and requires this exact STS caller
ARN, including the EC2 instance-profile session name:

```
arn:aws:sts::721622471953:assumed-role/railshot-control-poc/i-033ae2db907fde68e
```

The same exact subject receives `roles/iam.workloadIdentityUser` on
`railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com`.
The pool belongs to project number `359201781699`; a project-ID/number mismatch
blocks the bootstrap plan. Keep the existing AWS role trust limited to EC2.
Changing the control instance or role requires a reviewed bootstrap update.

After reviewing and applying the bootstrap plan, export the non-secret credential
configuration from **that module's applied state**:

```sh
terraform -chdir=infrastructure/terraform/platform-release output -json gcp_release_identity
terraform -chdir=infrastructure/terraform/platform-release output -raw gcp_external_account_json > external-account.json
```

Install this configuration as a root-owned, operator-readable file on the control
host and bind its absolute path as `gcp_credentials_file` in
`/etc/railshot/release.json`. The release executor sets both
`GOOGLE_APPLICATION_CREDENTIALS` (Terraform) and
`CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` (gcloud) to that file. It must not inherit
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` or `AWS_SESSION_TOKEN` from another
identity: the client otherwise prioritizes those variables over EC2 metadata.

The JSON contains an AWS IMDSv2 token endpoint, metadata credential source,
exact WIF provider audience and dedicated service-account impersonation URL.
It contains no private key, refresh token, personal ADC or AWS credential.
The client obtains short-lived AWS role credentials from EC2 metadata and
exchanges signed STS identity proof for short-lived GCP credentials. The file is
equivalent to this local configuration generation command (no login or cloud
mutation):

```sh
gcloud iam workload-identity-pools create-cred-config \
  projects/359201781699/locations/global/workloadIdentityPools/railshot-aws-release/providers/aws-control \
  --service-account=railshot-gcp-edge-release@railshot-poc-20261001.iam.gserviceaccount.com \
  --aws --enable-imdsv2 --output-file=external-account.json
```

Source: [Google AWS WIF setup and impersonation](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds),
[AIP-4117 AWS credential format and environment precedence](https://google.aip.dev/auth/4117).

## Existing runtime and observer paths

The IAP binding belongs only to `railshot-gcp-poc` in `asia-northeast3-a`, with
`destination.port == 22`. Ansible uses its already registered operator SSH key
and `gcloud compute start-iap-tunnel`. `compute.instances.get/list` supports that
command; OS Login, metadata publication, service-account impersonation of the
VM's identity and `gcloud compute ssh` key management are not granted.
[Google's IAP permission table and port condition](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding)
define this distinction.

Bootstrap also owns these observer-only ingress declarations:

| Existing target | Exact source | Allowed TCP ports |
| --- | --- | --- |
| AWS rules-only SG `sg-0ad2a18168c4eac2b` | Control SG `sg-01a71e8be9a585b5a` | 31490, 31491 |
| GCP runtime VM's existing network and service account | Control public IP `52.78.97.236/32` | 31490, 31491 |

The AWS node module's inline-owned SG and existing SSH rules are untouched. GCP
uses its existing public IP for metric scraping; these two firewall ports do not
open public SSH. Runtime Cilium policy and observer configuration must authorize
the same source separately. These bootstrap firewall resources are not part of
the executor's edge state ownership.

## Permission evidence

The roles follow the existing shared resources, per-app routes and instance data
source in `../gcp-edge`, with Google provider **8.5.0**. Future additions such as
Cloud Armor, TLS policy or VM changes require an explicit authority review.

| gcp-edge declaration | Refresh and existing-update authority |
| --- | --- |
| Project service | Project `get`, enabled-service `list`, API quota `use`; no service enable/disable |
| Backend instance data | Instance `get`; provider expands boot disk with disk `get` |
| NEG and endpoint | NEG `get` also authorizes endpoint listing; `use` for backend reference |
| Firewall | `get`, `update` |
| Health check | `get`, `update`, `useReadOnly` |
| Backend service | `get`, `update`, `use` |
| Two URL maps | `get`, `update`, `use` |
| HTTP / HTTPS proxies | `get`, `setUrlMap`, `use`; HTTPS `setCertificateMap` |
| Global address | `get`, `setLabels` |
| Two global forwarding rules | `get`, `setLabels`, `setTarget`, `update` |
| DNS authorization, certificate, map, map entry | Corresponding `get` and `update`; certificate and map `use` |
| Async update completion | Compute global-operation `get`, Certificate Manager operation `get` |

The pinned provider's [Compute sources](https://github.com/hashicorp/terraform-provider-google/tree/v8.5.0/google/services/compute),
[Certificate Manager sources](https://github.com/hashicorp/terraform-provider-google/tree/v8.5.0/google/services/certificatemanager),
and [project-service refresh](https://github.com/hashicorp/terraform-provider-google/blob/v8.5.0/google/services/resourcemanager/resource_google_project_service.go)
establish the actual methods. Google documents the
[NEG list permission](https://docs.cloud.google.com/compute/docs/reference/rest/v1/networkEndpointGroups/listNetworkEndpoints),
[backend reference permissions](https://docs.cloud.google.com/compute/docs/reference/rest/v1/backendServices/update),
[certificate-map reference permission](https://docs.cloud.google.com/compute/docs/reference/rest/v1/targetHttpsProxies/setCertificateMap)
and [Certificate Manager permissions](https://docs.cloud.google.com/iam/docs/roles-permissions/certificatemanager).

### Application lifecycle additions and their scope

Stopping an app removes its NEG, endpoint, health check and backend, then removes
its shared URL-map entries and firewall port. Its certificate, authorization and
certificate-map entry remain. Starting recreates those four backend resources.
Deleting removes the retained certificate resources too; registering a new app
creates all seven app resources. Shared IP, proxies, forwarding rules, URL maps,
firewall and certificate map remain. A delete-only grant would therefore leave
start and new application registration broken.

The original unconditional role adds exactly these **23 permissions**:

| API family | Added suffixes | Why they are needed |
| --- | --- | --- |
| `compute.networkEndpointGroups` | `create`, `delete`, `attachNetworkEndpoints`, `detachNetworkEndpoints`, `list` | App NEG/endpoint creation, removal and absence verification |
| `compute.networks`, `compute.subnetworks` | `use` on each | NEG creation references the existing VM's network/subnet |
| `compute.zoneOperations` | `get` | Provider waits for zonal NEG/endpoint operations |
| `compute.healthChecks` | `create`, `delete`, `list` | App health check lifecycle and absence verification |
| `compute.backendServices` | `create`, `list` | App backend creation and absence verification |
| `certificatemanager.dnsauthorizations` | `create`, `delete`, `list`, `use` | DNS authorization lifecycle and use by a new certificate |
| `certificatemanager.certs` | `create`, `delete`, `list` | App certificate lifecycle and absence verification |
| `certificatemanager.certmapentries` | `create`, `delete`, `list` | App map-entry lifecycle and absence verification |

The additional role `railshotAppBoundEdgeUse` contains exactly two permissions:

| Permission | IAM condition |
| --- | --- |
| `compute.backendServices.delete` | `BackendService` names starting with `projects/railshot-poc-20261001/global/backendServices/railshot-gcp-edge-` |
| `compute.instances.use` | The `Instance` named `projects/railshot-poc-20261001/zones/asia-northeast3-a/instances/railshot-gcp-poc` |

The backend prefix matches the registered module's default `name` and its
`route_names`: `railshot-gcp-edge-` followed by the first 16 hexadecimal characters
of the application ID's SHA256. The legacy baseline is `railshot-gcp-edge`, without
the trailing hyphen, so this delete grant excludes it. The prefix alone does not
prove that a backend belongs to an application or validate its hash suffix; the
executor verifies the exact ownership record. A different module `name` requires
a reviewed IAM change. Tests execute the sibling gcp-edge module to check this
naming contract rather than assuming the default.

The [NEG insert API](https://docs.cloud.google.com/compute/docs/reference/rest/v1/networkEndpointGroups/insert)
requires network/subnetwork `use`. Endpoint
[attach](https://docs.cloud.google.com/compute/docs/reference/rest/v1/networkEndpointGroups/attachNetworkEndpoints)
and [detach](https://docs.cloud.google.com/compute/docs/reference/rest/v1/networkEndpointGroups/detachNetworkEndpoints)
require `compute.instances.use` for the referenced VM. That permission does not
grant VM creation, update or deletion. The provider polls zonal operations after
these requests. Certificate creation also requires `dnsauthorizations.use`, while
map-entry creation checks its parent map and the existing `certs.use` grant.
[Certificate Manager's operation-to-permission table](https://docs.cloud.google.com/certificate-manager/docs/permissions)
describes these reference checks.

**The 23 project/parent permissions are not app-name-restricted IAM grants.**
Google's documented [resource-name support](https://docs.cloud.google.com/iam/docs/conditions-resource-attributes)
covers Backend Service and Instance, but does not list NEG, Health Check or
Certificate Manager resources. Project/parent create and list checks cannot be
treated as checks of the proposed child's name. Adding unsupported name conditions
would block operations rather than establish the intended app boundary. The
existing role also retains its original project-wide read/update scope; this
change does not tighten those 44 permissions.

In particular, IAM does not restrict certificate/DNS-authorization names or the
certificate-map-entry parent to this app. The executor checks their exact hashed
names, registered hostname, shared map ID and owned state before mutation. A
compromised executor credential could exercise the newly granted API families
against other resources in the same project, including legacy NEG/health-check/
certificate resources. The project name is not evidence that it contains only
RAILSHOT resources. Applying this role change therefore requires review of actual
project scope and acceptance of this IAM-versus-executor boundary. A requirement
for IAM-only app ownership isolation needs a separate authority/isolation design.

No new shared firewall, URL map, certificate map, IP, forwarding-rule, proxy or VM
creation/deletion is granted. `firewalls.list` and `urlMaps.list` are also omitted:
they are useful to resource-specific permission diagnostics, but the current
lifecycle reads/updates these exact shared resources and does not list them for
absence verification. Cloudflare app/certificate DNS uses its separately bound
authority and is not covered by this GCP role.

## Local validation and deployment boundary

```sh
terraform -chdir=infrastructure/terraform/platform-release init -backend=false -input=false
terraform -chdir=infrastructure/terraform/platform-release validate
terraform -chdir=infrastructure/terraform/platform-release test
```

Tests use mocked AWS/Google providers. They check the exact caller, wrong project
number rejection, all 44 preserved and 25 additional permissions, the two exact
conditional grants, real app/backend naming, forbidden shared-resource lifecycle
verbs, single-VM IAP port, observer ingress and IMDSv2 configuration without cloud
writes. Native Terraform 1.7.5 is supported.
Check in `.terraform.lock.hcl` with the module, including both AWS 6.66.0 and
Google 8.5.0 provider checksums; do not let a global ignore rule omit it.

Source validation and mock tests do not prove deployed IAM or successful app
lifecycle operations. The lifecycle IAM candidate should change only the existing
`google_project_iam_custom_role.edge_release` plus the new
`google_project_iam_custom_role.app_edge_bound` and
`google_project_iam_member.app_edge_bound`. Review a full refreshed saved plan
against the original bootstrap state; unexpected changes need separate review.
After a separately approved apply, verify the live role documents/bindings,
actual API Pod WIF identity, project/resource permission results and the bounded
application lifecycle. Preserve the pool/provider condition, IAP binding, observer
rules and existing release refresh checks. Neither a mock result nor a role
document is evidence that stop/start/delete completed successfully.
