# AWS 환경 제어

공통 제어평면의 AWS 구현이다. 첫 프로파일은 서울, kubeadm control 1대와 worker
2대이며 standalone DB를 선택하면 PostgreSQL용 VM 1대를 별도로 만든다.
`aws.py`의 registry entry는 공통 제어평면 PR에 의존한다. 실제 AWS 인수는 아직
실행하지 않았다. Terraform plan/mock, 구문 검사 또는 저장된 설치 결과를
실환경 지원 완료로 취급하지 않는다.

## 인증과 입력 준비

공통 PR의 오프라인 helper로 private 입력 파일을 만든다. 기존 파일은 덮어쓰지
않으며 API key를 채팅, 명령줄 인자, 저장소에 넣지 않는다.

```sh
python3 infrastructure/providers/environments/setup.py init \
  --directory /private/operator/railshot-inputs --provider aws \
  --owner-id account:ACCOUNT_UUID
python3 infrastructure/providers/environments/setup.py validate \
  --config /private/operator/railshot-inputs/control.json
```

생성 파일은 실행기 소유 `0600`, 디렉터리는 `0700`을 유지하고 로컬 편집기로
빈 필드를 채운다. 생성된 예제의 빈 approval ID·가격·계정은 유효한 승인이 아니다.
validate는 cloud API를 호출하지 않으며 live 검증 성공을 반환하지 않는다.

| 입력 | 준비할 값 |
|---|---|
| registry account ID | Railshot 연결 별칭. AWS 12자리 계정 번호와 별개 |
| `runtime_permissions_boundary_arn` | 같은 계정의 `arn:aws:iam::<account_id>:policy/railshot-environment-runtime-boundary`. 운영자가 먼저 만들고 provisioner에는 boundary 수정/삭제 권한을 주지 않음 |
| `adapter.account_id` | 실제 AWS 12자리 계정 번호 |
| `auth.mode` | 고객 계정 `customer_role`, Railshot 계정 `managed_role` |
| `auth.role_arn` | 위 AWS 계정에 있는 정확한 provisioning role ARN |
| `auth.external_id` | 고객 계정에서 Railshot 연결별로 생성한 ExternalId. 고객 trust policy와 일치 |
| source identity | private `source_credentials_file` 또는 명시적 `source: instance_profile` 중 하나. 후자는 실행기가 EC2 instance profile을 사용하는 경우 |
| source credential JSON | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, 임시 자격이면 `AWS_SESSION_TOKEN`. 사용하지 않는 빈 session token 키는 제거 |
| AMI | 서울의 정확한 Canonical Ubuntu 24.04 amd64 AMI ID. 이동하는 image alias는 사용하지 않음 |
| SSH | 실행기 private key 파일과 대응 public key. EC2에 public key만 전달 |
| 네트워크 | 기본 `10.72.0.0/16`, 서울 AZ 2개. pod/service CIDR와 겹치지 않는 RFC1918 /16 |
| 컴퓨트 | 승인된 `t3.large/t3.xlarge/t3.2xlarge`, 노드별 data disk 20–1000 GiB |
| 공개 edge | 위임 완료된 기존 public Route53 zone, base domain, 선택한 ACM ARN 또는 DNS validation 발급, 초기 probe route host/NodePort/health path |
| 앱 template | CI tenant/source repository, GHCR pull secret 파일, GitOps checkout·branch·Argo context, CPU/memory 한도, ingress CIDR |
| 비용 | 승인 ID, 기간, 3/4 VM 수·사양·disk와 일치하는 견적, USD 상한, 산정 근거, 만료, `data_policy: retain`, 기존 비용 ledger와 신선한 billing quote |
| DB | 별도 PostgreSQL 모듈의 외부 backup 목적지/자격·보존기간·restore 입력. VM 생성만으로 DB readiness는 true가 아님 |

고객 role의 trust는 Railshot source role principal과 `sts:ExternalId`를 함께 제한한다.
source identity에도 대상 role의 `sts:AssumeRole` 권한이 필요하다. 관리 계정도
정확한 role을 AssumeRole하며 환경을 tenant별로 나눈다. STS account readback 후만
SDK/CLI를 사용한다. [AWS 제3자 role 접근](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_common-scenarios_third-party.html)

제어 role에는 승인한 VPC/subnet/routes/IGW/SG, EC2/EBS, 환경별 node IAM role과
instance profile, SSM 명령/세션, EFS/mount target, ALB/target/listener,
Route53 record, ACM 발급·조회·정리 작업 권한이 필요하다. `iam:PassRole`은 해당
계정의 환경별 node role ARN과 EC2 service에 제한한다. 기존 zone이나 외부 자원을
자동 인수하지 않는다. 고객 관리자가 실제 계정의 SCP·permission boundary·쿼터와
함께 역할 정책을 검토한다. [PassRole 제한](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_passrole.html)

## 실행과 비용 경계

`plan`은 의존 순서·자원수·승인된 비용 근거를 반환한다. 실제 자원 ID가 필요한
후속 단계는 선행 단계 후 native saved plan을 생성하고 해시·소유권·파괴적 변경을
검사한다. 새 환경 apply 전에 기존 `terraform_tools.provision.reserve_budget`의
계정별 authoritative ledger에 환경 ID로 비용을 예약한다. 비용 데이터가 없거나
오래됐으면 생성하지 않는다. 전체 EC2/EBS/EFS/ALB/public IPv4/DNS/전송/보존
비용을 승인 견적에 포함해야 한다. 견적과 ledger 예약은 AWS 청구의 강제 상한이나
보존 데이터의 자동 삭제를 의미하지 않는다.

VPC와 두 subnet은 public IP를 통한 outbound HTTPS/SSM 경로를 사용한다. NAT 비용을
생성하지 않는다. 노드의 public SSH/HTTP inbound는 열지 않으며 관리 SSH는 SSM
forwarding을 사용한다. private-only subnet/NAT 프로파일은 이 첫 구현에 포함하지
않는다. SSM Agent·node profile·endpoint 접근 및 AWS CLI/session-manager-plugin이
필요하다. [SSM 선행조건](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-prerequisites.html)

각 Terraform stage는 private state와 immutable source/owner binding을 가진다.
실패한 apply는 unknown으로 저장하고 자동 재시도하지 않는다. 새 create 시도 전에
정확한 state와 cloud resource를 reconcile해야 한다. 하나의 자원을 Terraform과
별도 SDK writer가 동시에 소유하지 않는다. STS session은 각 최대 30분 child 전에
최소 35분 TTL을 확보하고 단계 사이 갱신한다. 자격 bytes는 durable binding이 아니며
role/account/credential reference/policy revision이 바뀌면 다른 authority로 취급한다.

## EFS CSI identity

EFS backend를 생성·조회한 뒤, **해당 filesystem ARN**에서만 access point를 만들고
해당 environment tag를 가진 access point만 삭제할 수 있는 policy를 control node
role에 붙인다. worker role에는 EFS API 권한을 주지 않는다. CSI controller는 control
node hostname에 고정하고 control-plane taint를 명시적으로 toleration하는
`hostNetwork` 1 replica를 사용한다. IMDSv2 required/hop limit 1은 기존 VM 모듈을
유지한다. 일반 앱은 worker에 배치되고 공통 앱 admission에서 hostNetwork/privileged
접근을 금지한다. 1c2w 자체가 control-plane HA를 제공하지 않는다.

고정 Helm 4.5.1의 `controller.tags`가 실제 `--tags` 인자로 렌더링되는 것을 검사한다.
CSI는 instance role의 임시 자격을 갱신하며 Kubernetes Secret에 장기 AWS key를
저장하지 않는다. 기존 Secret 모드는 기존 환경 호환용으로 유지한다. IAM 전파가
끝나지 않았거나 실제 PVC 생성·worker 간 읽기/쓰기가 실패하면 runtime은 ready가
되지 않는다. [공식 EFS driver identity 방식](https://github.com/kubernetes-sigs/aws-efs-csi-driver/blob/master/docs/install.md),
[AWS EFS CSI policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonEFSCSIDriverPolicy.html)

현재 상태 조회는 실제 EC2 ownership·상태, SSM/SSH의 3노드 Ready·IP·control taint,
cluster UID, CSI workloads, 기존 proof PVC/PV UID·StorageClass filesystem binding,
EFS/mount target 접근과 edge state를 읽는다. ALB 구성·DNS record와 실제 앱 HTTPS
성공은 서로 다른 evidence다. 앱 registration은 공통 bootstrap이 실제로 수행하고
그 receipt를 facade가 확인해야 deployment binding이 성립한다.

삭제는 공통 facade가 연결 앱이 없음을 확인한 뒤 fresh STS child에서 CI/Argo/관측/edge
공통 teardown을 먼저 완료하고, CSI policy, EFS mount
targets, VM, network 순서로 수행한다. EFS filesystem/access points와 EBS data/root
volume은 ID를 기록해 보존하고 비용이 계속 발생할 수 있다. 보존된 파일시스템과
볼륨의 추가 폐기는 이 동작에 포함하지 않는다. 부분 삭제/timeout은 unknown으로
남겨 state 확인 없이 재시도하지 않는다.

## 로컬 검사와 실제 인수

```sh
PYTHONPATH=. python3 -m unittest infrastructure.providers.aws.test_adapter
PYTHONPATH=infrastructure/ansible python3 -m unittest discover -s infrastructure/ansible -p test_storage_profile.py
PYTHONPATH=deployment/scripts:infrastructure/ansible python3 -m unittest discover -s deployment/scripts/tests -p test_cluster_edge.py
terraform -chdir=infrastructure/terraform/aws-network init -backend=false -lockfile=readonly
terraform -chdir=infrastructure/terraform/aws-network validate
terraform -chdir=infrastructure/terraform/aws-network test
terraform -chdir=infrastructure/terraform/aws-csi-role init -backend=false -lockfile=readonly
terraform -chdir=infrastructure/terraform/aws-csi-role validate
terraform -chdir=infrastructure/terraform/aws-csi-role test
```

실환경 인수에는 위 private 입력 및 명시적 실행 승인이 필요하다. 고객/관리 계정
각각 신규 생성, credential 갱신, 앱 배포·업데이트·정지/재개, PVC mobility, DNS/TLS,
장애 후 관측, 앱 삭제와 환경 정리/데이터 보존을 확인한다. DB backup/restore는
별도 PostgreSQL 모듈과 통합해 검증한다. 아직 실행하지 않은 항목은 미실행이며
미검증 결과를 성공으로 기록하지 않는다.

### 앱 작업의 자격 갱신과 추가 입력

생성된 `authority.json`은 `config:{auth,account_id}`, `context:{environment_id,owner_id,account_id,credential_ref,policy_revision}`, `credentials_file`만 기록합니다. auth는 source private file/instance-profile 참조이며 STS secret bytes를 authority에 저장하지 않습니다. 공통 `environment_action.py` 고정 wrapper는 등록·라우트·수명주기·로그·관측 등 각 실행에 앞서 exact role/account/binding을 검증하고 새 Session을 발급합니다. 갱신된 child 환경은 SSM에도 전달됩니다. 기존 1시간짜리 `session.json`을 며칠 뒤 그대로 쓰는 경로는 지원 완료로 보지 않습니다.

앱 template `profile.ingress.observability_config_file`은 기존 observability/register.py 형식 private 설정, `ci_credentials_file`은 GitHub Actions repository variables write 권한이 있는 0600 JSON `{ "token": "직접 private 파일에만 입력" }` 참조가 필요합니다. 이 값을 채팅/PR/로그에 넣지 않습니다. 생성된 `aws_authority_file`은 provider가 채우며 사용자 임의 환경 credential override가 아닙니다.

### 단일 PostgreSQL 연결

`database_config_file`은 [단일 PostgreSQL 입력 계약](../../../docs/architecture/standalone-postgres.md)의 private 파일이다. helper는 `postgres-config.json`과 빈 복구 승인 양식을 생성한다. standalone 선택 시 예산은 4개 VM·디스크와 외부 object 저장소 비용을 포함해야 한다. provider는 DB VM의 등록된 SSH key/known-hosts를 결합하고, fresh STS child에서 설치→SQL/TLS·외부 full backup 확인→application bundle 생성→공통 bootstrap을 순서대로 실행한다. 이후 describe/status/backup/restore 역시 fresh STS child를 사용한다.

S3 `instance` 모드는 DB 전용 EC2 role/profile을 생성하고, 지정 bucket의 `<prefix>/<environment_id>/*`에만 object read/write/delete/multipart 권한을 부여한다. bucket 목록은 같은 prefix로 제한한다. control/worker 역할에는 backup 권한을 추가하지 않는다. Seoul S3 endpoint만 허용하며 S3-compatible 외부 저장소는 `secret` 참조 모드로 사용할 수 있다. SSE-KMS bucket은 별도 승인된 KMS 정책이 필요하며 이 adapter는 임의 KMS 권한을 부여하지 않는다.

`database.ready`는 실제 standalone receipt가 true일 때만 반영하며 VM 생성, configuration file 또는 IAM 연결만으로 ready를 만들지 않는다. 실환경 role 전파/외부 bucket 접근·복구는 아직 검증하지 않았다.

### 고정 관리 주소와 오프라인 검증

runtime 3개 VM에는 EIP를 할당하며 예산 파일의 `elastic_ip_count: 3` 및 실제 원가 산정에 포함한다. control-plane API 6443은 `management_source_cidrs`의 실제 Argo/운영자 egress public /32만 허용한다. `observer_source_cidrs`는 관측 설정의 `observer_source_cidr`와 일치해야 하며 두 metrics NodePort만 허용한다. 앱 workers와 DB에는 공개 API ingress를 추가하지 않는다. SSH는 계속 SSM만 사용한다. native EIP allocation의 instance/private IP를 조회하여 고정 주소를 검증하며 공통 registry는 CA 및 private IP TLS serverName을 유지한다.

DB는 첫 subnet의 +14 고정 주소를 사용한다(기본 `10.72.0.14`). 미리 준비하는 PostgreSQL 서버 인증서에는 이 IP SAN이 필요하다. VM readback 주소가 다르면 설치하지 않는다. DB VM은 EIP 없이 SSM과 별도 역할을 사용한다. `database:none` 입력에서는 `database_config_file`을 빈 문자열로 두고, standalone을 선택할 때 생성된 `postgres-config.json`을 완성한 후 해당 private 경로를 설정한다.

오프라인 validate는 실제 앱 schema, GHCR 자격 형태, Route53 zone/domain/route, CI token 파일, observer 설정·포트·허용 source 일치, DB 선택 시 TLS/backup 입력, 예산 승인·만료를 확인한다. 네이티브 API 호출이나 live 준비 완료를 의미하지 않는다. bootstrap 중간 실패로 공통 binding receipt가 없는 경우 안전한 teardown 권한을 입증할 때까지 unknown cleanup으로 남긴다.

## Runtime IAM boundary prerequisite

`aws-network` creates `env-<24 hex>-control`, `-worker`, and (when enabled) `-database` roles with the required `runtime_permissions_boundary_arn`. The adapter rejects a missing, foreign-account, or differently named boundary before credential loading; Terraform also checks the account. The boundary is administrator-managed and is a maximum, not an attached permission grant. Keep the existing SSM attachment, parameter-read deny, control-only EFS inline policy and database-only bucket/prefix inline policy. Provisioner `CreateRole` must require this exact boundary; `PutRolePolicy` is restricted to these bounded runtime roles; `PassRole` is limited to EC2. Do not grant the provisioner boundary policy editing or removal. An existing deployment lacking a boundary requires a separately reviewed administrator migration; do not relax the condition to adopt it.

The administrator must add the exact existing backup bucket and environment prefix to the boundary before PostgreSQL backup/restore. The base boundary must not grant arbitrary bucket access. Supply the existing Route53 zone/domain separately. Policy JSON and local transport tests do not establish live IAM acceptance or a billing cap; the approved test cost remains below USD 10 and retained EBS/EFS can continue charging after teardown.

Boundary follow-up local validation: AWS Python adapter/common dispatcher 27 tests passed; the new `boundary.tftest.hcl` mock-provider plan checks passed 3/3. The existing network mock-plan suite still fails on computed ingress / null and collection comparison assertions, so the full Terraform suite is not reported as passing. No real AWS policy or resource mutation was executed.
