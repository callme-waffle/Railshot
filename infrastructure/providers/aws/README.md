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

삭제는 공통 facade가 연결 앱이 없음을 확인한 뒤 edge, CSI policy, EFS mount
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
