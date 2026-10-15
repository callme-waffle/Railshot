# NCP VPC environment implementation

## Scope and acceptance

Provide the common environment facade for NAVER Cloud Platform public KR VPC:
new VPC/subnets/routes/ACG, one kubeadm control VM plus two workers, NAS-backed
NFS CSI, application publication through native load balancer/TLS/DNS, and a
separate standalone PostgreSQL VM via the shared database runtime. Shared and
dedicated application ownership remains in the common application service.
Gov/financial endpoints, NKS, cloud database products and multi-control-plane HA
are excluded from this initial adapter. All nodes initially use one operator
selected KR zone; cross-zone NAS availability is not assumed.

Completion requires native readback, strict pinned SSH, kubeadm node observations,
same-PVC mobility across both workers and actual application registration. A
successful API create reply, plan, template or mocked response is not readiness.

## Implementation plan

1. Fixed-endpoint NCP HMAC HTTP transport with bounded output and secret-free
   error codes. Credentials remain private operator references.
2. Durable per-environment intent before every mutation; lost replies block
   automatic replay. Verify created IDs, native names and owner marker on reads.
3. New public VM subnet and load-balancer subnet, explicit route table with
   observed internet-gateway route, and ACG permitting SSH/API only from the
   operator management CIDR. Public IP management is an explicit cost input.
4. Generate unique per-VM SSH host keys privately, inject through the documented
   initScript API and pin the public key. Remove initScript after authenticated
   SSH readiness; never use ssh-keyscan or disabled host checking.
5. Create retained encrypted NAS with native server ACLs. Extend the existing
   pinned upstream NFS CSI installer and PVC movement proof for NCP.
6. Normalize NCP descriptors into existing kubeadm/guest transport; provide typed
   native edge operations to the common app route dispatcher.
7. Exercise lifecycle/ownership/failure paths locally, prepare private setup
   templates and document live acceptance inputs before provisioning resources.

The first implementation uses operator-selected image/spec/login key and TLS
certificate authority. The authenticated Certificate Manager response proves
memberNo, certificate number and wildcard domain; access-key fingerprint and
private credential reference bind enrollment. Native ownership also requires
persisted created IDs, names and environment markers. No console host-key API is
assumed.


## Operator inputs and offline setup

Use the common setup command after the common foundation is integrated:

```sh
python3 -m infrastructure.providers.environments.setup init --directory /private/ncp-account --provider ncp --owner-id account:UUID
python3 -m infrastructure.providers.environments.setup validate --config /private/ncp-account/control.json
```

`config_template()` writes nonsecret skeletons only. Files stay outside the
checkout, owned by the executor, mode 0600; directories use 0700. Empty generated
credentials, docker auth, CI token, application resource/ingress fields and missing
private references are invalid. `validate_config()` opens local references and
validates shape/permissions without making provider requests. `plan` subsequently
performs read-only account/certificate, zone, Ubuntu amd64 KVM image and compatible
server-spec checks. It is not a connectivity/PVC/HTTP acceptance result.

| Input | Required value / delivery |
|---|---|
| account_id / credential_ref | Common account enrollment IDs; application payload contains only account_id/environment_id |
| member_no / access_key_sha256 | Native member number and SHA256 of the enrolled access key; do not supply raw API secrets in chat |
| credentials_file | Private JSON access_key, secret_key, member_no; credentials require the selected project services' read/create/update/delete operations, certificate read and dedicated Global DNS zone write |
| region / zone | Public commercial KR, operator-selected KR-1 or KR-2; all workers/NAS/VMs use one zone initially |
| network | Nonoverlapping RFC1918 VPC, public VM and LB subnets; management_cidr is the executor's stable public egress /32 |
| nodes | Available Ubuntu 24.04 amd64 KVM server image number, compatible spec (at least 2 vCPU/4GiB), existing NCP login-key name, nonroot guest user and separate Ed25519 guest identity/public key files |
| nas.size_gib | 500–20000GiB in 100GiB increments; retained encrypted NAS and two worker ACLs |
| edge | Existing wildcard certificate number, dedicated base domain and Global DNS domain ID; signed certificate response must belong to member_no and be currently valid |
| application template | Existing CD checkout/context/branch, source repository, GHCR dockerconfigjson, tenant, app resource limits and ingress CIDRs |
| ingress.observability_config_file | Existing private observability registration schema, public observer egress /32 and exporter NodePorts; observer gets NodePort range access for application traffic counters |
| ingress.ci_credentials_file | Private JSON token for existing GitHub repository variables; no ambient credential dependency |
| database | Separate compatible VM spec and 10GiB-step CB1 data volume size, shared standalone PostgreSQL private config (TLS, client CIDRs, object backup and restore approval references) |
| budget | Explicit approved=true and max_monthly_krw>0 after reviewing actual provider price/quota; this amount records approval and does not impose a billing cap |

The management path is executable: new PUBLIC subnet → server public IP from
`associateWithPublicIp` → exact public-IP/VM/NIC ownership observation → SSH port 22
allowed only from management /32 → strict pinned SSH. Kubernetes private API
addresses remain in node TLS SANs, with public management endpoint using the
private TLS server name. The CI/Argo controller must use the same authorized egress
or an explicitly reviewed network configuration; this profile does not create a
VPN/peering path or silently assume private connectivity.

Host trust uses `ssh-keygen -t ed25519` in `/dev/shm`, a fixed init script uploaded
with **POST body**, and known_hosts pinned to the private IP through HostKeyAlias.
The generated host private key never enters persisted Railshot state, stdout or
PRs. The native initScript temporarily contains that key and is deleted/read back
absent after strict SSH succeeds. Provider-side audit/backup retention is outside
Railshot's deletion guarantee and must be accepted by the account operator.
Lost creation replies remain unknown and require reconciliation; no ssh-keyscan
or disabled host validation is used to recover.

PostgreSQL uses a separately created/attached retained CB1 disk. The executor
matches its native device, rejects any partition/filesystem/signature or mount,
journals an unknown intent before formatting the fresh disk, mounts ext4 at
`/var/lib/postgresql` by UUID and verifies the exact source. The shared PostgreSQL
installer verifies this mount independently. No existing data disk is adopted or
reformatted. NCP Object Storage uses the shared pgBackRest S3-compatible path with
an existing dedicated bucket/prefix and private access_key/secret_key reference;
bucket creation/IAM is not inferred by the database helper.

## Lifecycle and residual resources

Each app owns an HTTPS ALB, target group and CNAME. The TLS certificate and DNS
zone are existing retained account resources. The initial cluster has a bootstrap
ALB/TG/CNAME as well; budget for one additional ALB plus one ALB per app. Readback
checks listener/certificate/default-rule → exact target group → two worker VMs,
health path/port, and owned DNS record. Native configuration success never claims
successful application HTTPS or healthy backends.

App stop removes its two target memberships and retains LB/DNS/certificate; start
adds exactly those targets back. Delete removes its owned DNS, then LB, then TG.
Plans bind private state/config/binding hashes, and unknown mutations block replay.
Global DNS apply publishes all zone pending changes, so use a dedicated zone with
one Railshot writer; the executor serializes routes locally. Multiple controller
instances require external single-writer scheduling.

Environment deletion first revokes app/bootstrap runtime authority and native
routes. NAS remains retained. With standalone PG, the PG VM is stopped and its
root disk, separate data disk, public IP, NIC, VPC/subnets/route table/ACG are retained
for controlled recovery; these resources continue storage/IP/network charges.
Other cluster VMs, implicit NICs and public IPs are deleted and observed absent.
Retained resources are distinct from failed residual cleanup. A partially failed
initial provisioning without a registered runtime cannot claim a successful
ordinary environment delete; its private intent/native IDs require operator
reconciliation before cleanup.

## Validation and live acceptance

Local tests exercise HMAC/body transport, redirect denial, account mismatch,
unknown intent replay denial, resource drift/truncated observations, actual
kubeadm normalization and SSH inventory, NAS ACL/AZ isolation, NFS CSI Retain
rendering, private setup and native route lifecycle with synthetic API fixtures.
These are not live provision/backup/restore/HTTPS results.

Before a live run supply the private files above, outbound executor/Argo/observer
addresses, available quota (VPC/subnets/VMs/public IPs/NAS/LB/block storage), dedicated
DNS/certificate, database TLS/object store and a time/cost ceiling. Verify create,
three-node Ready, worker-to-worker same-PVC data, app deploy/health/metrics,
standalone SQL/TLS, object backup and approved restore, app stop/start/delete and
retained-resource inventory. NAS cross-AZ and Gov/financial API environments remain
unverified and outside this initial profile.

## Official API references

- [API Gateway authentication](https://api.ncloud-docs.com/docs/common-ncpapi)
- [Server creation](https://api.ncloud-docs.com/docs/en/compute-vserver-server-createserverinstances), [initScript creation](https://api.ncloud-docs.com/docs/en/compute-vserver-initscript-createinitscript), [image lookup](https://api.ncloud-docs.com/docs/en/compute-vserver-serverimage-getserverimagelist), [server specification lookup](https://api.ncloud-docs.com/docs/en/compute-vserver-server-common-getserverspeclist)
- [VPC](https://api.ncloud-docs.com/docs/en/networking-vpc-vpcmanagement-createvpc), [subnet](https://api.ncloud-docs.com/docs/en/networking-vpc-subnetmanagement-createsubnet), [route-table associations](https://api.ncloud-docs.com/docs/networking-vpc-routetable-getroutetablesubnetlist), [ACG rule observations](https://api.ncloud-docs.com/docs/compute-vserver-acg-getaccesscontrolgrouprulelist)
- [NAS](https://api.ncloud-docs.com/docs/en/storage-vnas-volume-createnasvolumeinstance), [block storage create](https://api.ncloud-docs.com/docs/en/compute-vserver-storage-createblockstorageinstance), [attach](https://api.ncloud-docs.com/docs/en/compute-vserver-storage-attachblockstorageinstance)
- [Certificate/member identity](https://api.ncloud-docs.com/docs/en/security-certificatemanager-getcertificatelist), [HTTPS load balancer](https://api.ncloud-docs.com/docs/en/networking-vloadbalancer-loadbalancer-createloadbalancerinstance), [listener rules](https://api.ncloud-docs.com/docs/networking-vloadbalancer-loadbalancer-getloadbalancerrulelist), [target groups](https://api.ncloud-docs.com/docs/networking-vloadbalancer-targetgroup-gettargetgrouplist)
- [Global DNS records](https://api.ncloud-docs.com/docs/en/networking-globaldns-record-createrecordlist), [zone apply](https://api.ncloud-docs.com/docs/en/networking-globaldns-record-applydomain)

### Bounded operator quote

`budget.quote` must bind `account_id`, `region`, `spec_sha256` and
`configuration_sha256` (canonical `control.digest` of adapter config excluding
`budget`). It records `estimated_monthly_krw` for the complete environment plus
`max_applications` ALBs, `approved_at`, `expires_at`, `test_duration_seconds` and
`data_retention: retain`. The estimate must be positive and no higher than the
approved monthly amount; approval lasts at most 24 hours and each test window is
60–43200 seconds. This is a reviewed operator estimate, not a live-price quote or
provider billing limit. Include retained NAS, PG disk/root disk, stopped VM IPs,
bootstrap/app LBs, requests and traffic when approving the estimate.

The apply transaction persists the quote hash and the earlier of approval expiry
or test deadline **before** the first native create. Subsequent creates and app
starts check that reservation; app allocation enforces max_applications. Read-only
checks and stop/delete remain available after expiry. Native operations already
accepted may finish after expiry, and expiry does not automatically shut down
resources; the operator must schedule stop/delete and verify retained costs.
A new quote is not silently substituted into an immutable running environment.
Use a reviewed account/environment migration for continued provisioning.

For offline quote preparation, `digest(spec)` and
`digest({k:v for k,v in adapter.items() if k!='budget'})` use sorted canonical JSON
from `infrastructure.providers.environments.control`; the digest command need not
print any credential values. Plan does not claim an authoritative price estimate.

단일 PostgreSQL VM은 `vm_subnet_cidr`의 network address + 14를 고정 private IPv4로 요청하며 NIC readback에서 동일 주소를 확인합니다. VM 생성 전 TLS server certificate의 IP SAN을 이 주소로 발급해야 합니다. 예를 들어 `10.90.1.0/24`이면 `10.90.1.14`입니다. 인증서/private key는 저장소 밖 private config 참조로만 제공합니다.
