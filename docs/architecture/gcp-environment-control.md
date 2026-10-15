# GCP 환경 제어평면 명세와 계획

승인 범위는 서울 asia-northeast3, 사용자 WIF 또는 Railshot 관리 서비스 계정, 신규 VPC/subnet, kubeadm 1c2w, Regional Filestore, HTTPS LB·DNS·TLS, 공통 앱 운영 및 선택 단일 PostgreSQL이다. GKE는 제외한다. 공통 애플리케이션 레이어는 environment_id와 정규화 spec만 전달한다.

완료 조건은 명시 프로젝트·서비스 계정 인증, 단일 Terraform writer, 예산 ledger와 승인 비용, 소유권 관측, 재실행/unknown fence, 실증 readiness, native child의 갱신가능 자격, 데이터와 Filestore 의존 네트워크 보존, private 입력 양식과 로컬 계약검사다. 실환경 인증/비용 입력은 아직 없으므로 live 인수는 미실행으로 기록한다.

1. WIF source-file/서비스계정 참조·private CLI 상태와 계정/프로젝트 계약.
2. 공통 network owner와 kubeadm/Filestore/DB 방화벽, 기존 VM Terraform, IAP host-key 등록.
3. 기존 kubeadm/storage/edge 실행기, 별도 PG native child와 앱 bundle/bootstrap.
4. 상태·stop/start·backup/restore·보존 삭제 및 공통 앱 auth wrapper.
5. 입력 양식, 비용/권한/보존 문서, 의미있는 boundary와 기존경로 검사.

VM 이름은 기존 모듈의 25자 제한에 맞춘 hash alias를 사용한다. 기본 VM은 public egress IP를 보유하지만 SSH ingress는 IAP range만 허용한다. Regional Filestore는 현재 저장소 계약의 최소 1024GiB와 DIRECT_PEERING을 유지하며, 삭제 시 Filestore와 의존 VPC/subnet/peering을 의도적으로 보존한다. VM별 기본 deny egress보다 앞선 네트워크 규칙으로 kubeadm, NFS, DB private 통신을 허용한다.

[WIF 공식 문서](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-providers)에 따른 refreshable credential configuration을 사용한다. [Compute 서비스 계정 문서](https://docs.cloud.google.com/compute/docs/access/service-accounts)에 따라 DB 전용 service account의 bucket IAM과 OAuth scope를 함께 제한한다. [Filestore 연결 요구](https://docs.cloud.google.com/filestore/docs/network-ip-requirements)와 [비용 기준](https://cloud.google.com/filestore/pricing)을 운영자 견적에 포함한다.

## 운영자 입력과 인수

`python3 infrastructure/providers/environments/setup.py init --directory /private/operator/railshot-inputs --provider gcp --owner-id account:ACCOUNT_UUID`로 0600 양식을 생성한다. 값과 인증서를 해당 private 파일에 입력한 뒤 `python3 infrastructure/providers/environments/setup.py validate --config /private/operator/railshot-inputs/control.json`을 실행한다. 이 명령은 클라우드 API를 호출하지 않으며, 누락 필드/오류 코드만 출력한다. 빈 예산 승인 ID나 빈 인증서는 허용되지 않는다.

`database:none`이면 `database_config_file`을 빈 문자열로 두며, standalone을 선택할 때 생성된 `postgres-config.json`을 완성한 후 그 private 경로를 설정한다. 검증기는 DB 참조가 존재하면 TLS/backup 필드를 검사하고, 4개 VM 예산에는 DB 참조를 필수로 요구한다. 설정 파일 검사만으로 live readiness를 만들지 않는다.

| 입력 | 내용 |
|---|---|
| project_id/project_number | 이미 결제 및 Compute/IAP/IAM Credentials/STS/Filestore/Certificate Manager API가 준비된 서울 프로젝트 |
| auth | 사용자 `workload_identity` 또는 관리 `managed_service_account`; WIF ADC JSON과 갱신 가능한 private subject-token 파일, 대상 서비스 계정 이메일. 실행 파일/URL token source는 불허 |
| WIF 신뢰 | 외부 issuer/audience/subject 및 조직 속성을 제한하고 해당 principal에 대상 SA의 workloadIdentityUser만 부여. SA impersonation과 프로젝트 권한은 별도 승인 |
| VM | immutable Ubuntu 24.04 amd64 이미지, 3개 서울 AZ, e2-standard-2/4/8, disk 크기, 공개키와 private SSH key 참조 |
| 네트워크 | 신규 /24 subnet과 겹치지 않는 Filestore /26, Pod/Service CIDR과 중복 불허. 기본 DB 주소는 subnet+14 (`10.76.0.14`), DB TLS 인증서에 이 IP SAN 필수 |
| 관리 연결 | management_source_cidrs는 Argo/운영자 egress public /32. control VM 6443만 허용. observer_source_cidrs는 실제 collector egress /32와 일치해야 하며 두 metrics NodePort만 허용. SSH는 IAP만 사용 |
| 앱·DNS·관측 | 실제 앱 template의 tenant/repository/resources/ingress, GHCR JSON, CD 저장소/컨텍스트, Cloudflare zone/token private 참조, observer 기존 설정, GitHub CI token JSON 참조 |
| DB | 별도 VM, TLS CA/cert/key, SCRAM probe password, client private CIDRs, pgBackRest 전용 GCS bucket. instance 모드는 DB SA에만 bucket-scoped objectAdmin과 cloud-platform scope 부여; service credential 모드도 지원 |
| 비용 | 운영자가 승인한 금액·기간·만료·실제 원가 산정 근거, 기존 budget ledger scope와 견적. 3개(선택 DB 포함 4개) VM/public reserved IP, Regional Filestore 최소 1024GiB, LB/Certificate/DNS, 저장장치·egress·백업·삭제 후 보존 비용 포함 |

서비스 계정은 프로젝트의 compute instance/network/firewall/disk/address 생성·조회·정리, 해당 node SA 생성·사용, Filestore 생성·조회, HTTPS LB/Certificate Manager 관리 권한이 필요하다. DB instance identity를 선택하면 지정 백업 bucket의 IAM 변경 권한이 추가로 필요하다. VM SSH는 IAP tunnel 접근과 대상 VM 조회, 등록 공개키를 사용한다. 미리 준비한 프로젝트 외의 조직/결제/프로젝트 생성 권한은 요구하지 않는다. 제품에는 사용자 credential bytes 대신 registry의 credential_ref와 policy revision을 저장한다. child별 격리된 CLOUDSDK_CONFIG와 refreshable ADC를 사용하므로 앱 작업도 원래 파일 참조에서 갱신한다.

삭제는 공통 CI/Argo/관측/edge teardown을 먼저 수행하고 VM·공인 IP·node SA·방화벽을 정리한다. PD data disk와 Filestore, 이에 필요한 VPC/subnet/peering은 retained 목록으로 반환한다. DB backup 객체는 지우지 않는다. unknown Terraform mutation은 재전송하지 않으며 native state/ownership 대조 후 재조정이 필요하다. 환경 삭제 후 데이터·네트워크를 영구 삭제하는 별도 작업은 이번 범위에 포함하지 않는다.

로컬 검사는 인증 바인딩/토큰 회전/외부 source 거부, common port projection, unknown 중복 실행 거부, static IP/DB IAM 분리, stopped readiness와 기존 storage/edge 경로를 검증한다. live 생성, IAP/Argo/collector 연결, 실제 PVC 쓰기, 공개 HTTPS, PG backup/restore, stop/resume 및 보존 삭제는 운영자 입력과 비용 승인이 준비된 뒤 별도 인수한다. 로컬 성공은 live 지원 검증 완료를 의미하지 않는다.
