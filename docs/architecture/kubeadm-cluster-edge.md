# kubeadm 3노드 클러스터의 공개 접근

`runtime.profile=kubeadm-1c2w` 설치는 클러스터·공유 스토리지 준비 이후
`deployment/scripts/cluster_edge.py`를 호출한다. 한 실행의 공급자·계정/프로젝트·리전과
control 1대·compute 2대 inventory가 공개 자원 생성의 기준이다. 공개 backend에는 두
compute만 들어가며 control은 앱 NodePort 대상에서 제외한다.

## 입력과 책임

`runtime.edge`에는 실행기 소유의 `0600` JSON 설정 파일을 지정한다. 이 파일의
`cluster_id`, `provider`, `account_id`, `region`은 요청의 target과 같아야 한다.
`worker_bindings`의 키는 inventory의 compute resource_id 두 개와 정확히 일치한다.
IP·AZ·노드 이름은 별도 파일에서 덮어쓰지 않고 inventory에서 가져온다.

| 공급자 | compute 추가 참조 | 공개 경로 | DNS·인증서 |
|---|---|---|---|
| AWS | 각 instance ARN → 전용 target security group | ALB → 두 사설 IP·NodePort | 기존 public Route53 zone, 지역 ACM 참조 또는 DNS 검증 wildcard 생성 |
| GCP | 각 `projects/PROJECT/zones/ZONE/instances/NAME` → 빈 객체 | zonal NEG 두 개 → global Application LB | 기존 Cloudflare DNS writer, Certificate Manager DNS authorization |
| OpenStack | 각 Nova UUID → 해당 Neutron subnet UUID | Named Tunnel → 사설 Octavia HTTPS → 두 compute NodePort | 기존 DNS writer·Named Tunnel 참조, 기존 Barbican 인증서 참조 |

각 `*-edge` 디렉터리의 `cluster-edge-config.json.example`, `cluster-vars.json.example`를
운영자가 실제 자원 참조로 교체한다. 변수 예제에는 compute IP를 넣지 않는다. 실행기가
`compute_nodes`, 첫 compute 대상, AZ·계정 정보를 검증하고 추가한다. 운영자가 변수
파일에 `compute_nodes`를 직접 넣으면 거부한다. 기존 단일 노드 모듈 호출은 빈
`compute_nodes` 기본값과 기존 primary resource 주소를 유지한다.

공급자 인증은 필수 `credentials_file`의 실행기 소유 `0600` JSON을 사용한다. 같은 파일을
스토리지 단계와 재사용할 수 있다. 코어가 cloud 환경변수를 제거한 `child_env()`를
전달해도 이 파일에서 SDK/CLI 환경을 다시 구성하므로 인증 경로가 동일하다. 파일
내용의 hash는 binding에 포함하고 실제 인증 바이트를 tfvars, CLI 인자, 저장소나
공개 결과에 넣지 않는다. ambient provider 인증·`TF_VAR_*`·`TF_CLI_ARGS*`·workspace
설정은 사용하지 않는다. lock 파일에 고정된 provider를 사용할 Terraform이 필요하다.

- AWS JSON 키: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, 선택
  `AWS_SESSION_TOKEN`. process 환경으로만 전달하며 profile/credentials 파일 자동
  탐색과 instance metadata 인증을 끈다. STS Account와 provider allowed account가
  cluster account와 같아야 한다.
- GCP JSON: target project의 standard `type=service_account` credential.
  `project_id`, `client_email`, `private_key`, 공식 `token_uri`를 검증하고
  `GOOGLE_APPLICATION_CREDENTIALS`와 CLI credential override에 해당 private
  파일 경로를 지정한다. 기본 로그인 계정·ambient ADC를 선택하지 않는다.
- OpenStack JSON: `OS_AUTH_URL`(HTTPS), `OS_PROJECT_ID` 및
  `OS_USERNAME`/`OS_PASSWORD`/user domain, 또는
  `OS_APPLICATION_CREDENTIAL_ID`/`OS_APPLICATION_CREDENTIAL_SECRET`. 선택
  `OS_REGION_NAME`은 target region과 같아야 한다. process 환경을 명시적으로
  만들고 token issue의 project를 확인한다. 사설 identity CA는 private
  `OS_CACERT` 파일 참조로 제공하며 검증을 끄지 않는다.

실제 키 예제를 저장소에 만들지 않는다. 운영자는 이 schema에 맞는 private 파일을
준비하고, provider CLI·Terraform의 해당 자격 권한과 HTTPS CA를 실환경에서 확인한다.

## 공급자별 선행조건

AWS는 기존 VPC와 public subnet을 사용한다. ALB subnet은 적어도 두 AZ에 있고 **두
compute의 모든 AZ를 활성화**해야 한다. 실제 instance의 IP·AZ·subnet/VPC·attached
SG가 참조와 일치하는지 provider data에서 확인한다. SG는 ALB에서 해당 NodePort로의
진입만 추가할 전용 rules group이어야 한다. 기존 ACM ARN은 같은 계정·리전에 있고
route hostname을 포함해야 한다. `certificate_arn`을 생략하거나 null로 두면 wildcard를
발급하고 Route53에 DNS validation을 기록한 뒤 ACM validation 완료를 기다린다.
새 public zone 생성은 cluster 실행 경로에서 사용하지 않는다. 기존 zone의 public 성격과
base_domain은 module이 확인하며, 외부 NS 위임은 선행조건이다.

GCP는 한 project·region의 기존 compute를 사용한다. 각 AZ의 NEG를 각각 만들며 두
NIC의 reviewed private IP와 같은 VPC를 확인한다. GFE source ranges에만 NodePort를
연다. compute service account는 앱 대상 노드용으로 운영한다. Certificate Manager
DNS authorization 생성 시 기존 DNS writer가 CNAME을 등록하고 공개 resolver에서
해당 authorization을 관측한 후 certificate 생성을 진행한다. 인증서 생성·참조 완료는
ACTIVE 상태와 실제 TLS 확인을 대신하지 않는다.

OpenStack은 Octavia amphora/L7·Barbican·기존 public Named Tunnel을 전제로 한다.
Nova project/AZ와 Neutron port의 IP/subnet을 실행 전에 관측한다. 기존 Neutron
subnet/router/SG 소유권은 유지한다. 준비된 네트워크는 양쪽 compute의 해당 NodePort에
실제 Amphora backend source CIDR의 TCP 접근을 허용해야 한다. member subnet별
Amphora source가 달라질 수 있으므로 listener allowed_cidrs와 구분하여 현장 소유자가
확인한다. 앱/Cilium 정책도 이 health·backend 트래픽을 허용해야 한다. 새 LB의 reviewed
unused `vip_address`와 tunnel `origin_vip`는 같아야 한다. tunnel credentials는 기존
Named Tunnel의 `AccountTag`, `TunnelSecret`, `TunnelID`를 담은 private JSON 파일로
제공한다. cloudflared 이미지는 기존 renderer의 digest를 사용한다. 새 프로파일은
전용 Namespace·Secret·ServiceAccount·ConfigMap·Deployment를 만들고 UID를 기록한다.
기존 객체는 자동 인수하지 않는다. Secret은 control SSH의 Kubernetes stdin으로만
전달한다. connector는 control-plane label을 제외하고 control taint를 toleration하지
않아 compute에 배치된다. 사설 origin CA를 쓰면 `ca_configmap`과 private `ca_file`을
함께 제공한다. HTTPS 검증을 끄는 옵션은 없다. 기존 단일 노드 tunnel config는 기존
등록·UID 검사 경로를 계속 사용한다.

## 고정 route와 앱 등록

이 작업은 운영자가 미리 정한 initial route를 연결한다. GCP는 primary hostname 하나,
AWS는 등록된 route map, OpenStack은 하나의 hostname 아래 설정한 host/path route를
사용한다. 코어 앱 등록은 같은 NodePort·HTTPS hostname·정확한 health path가 이
고정 route 안에 있는지 먼저 검사한다. 새 프로파일에서 단일 control IP를 기록하는
legacy automatic edge writer는 변경 전에 차단한다. 새 앱의 동적 route allocation,
hostname 추가·변경, 앱별 공개 자원 생명주기 개편은 별도 작업 범위다.

## 상태·재실행·실패 처리

코어가 전달한 edge state root 아래 `<cluster_id>/`에 전용 Terraform local state,
saved plan, 적용 journal, tunnel UID 기록을 저장한다. 디렉터리는 `0700`, 파일은
`0600`이며 cluster별 단일 writer lock을 사용한다. 최초 실행은 빈 stage만 허용하고,
기존 state나 `.terraform` 디렉터리를 발견하면 인수하지 않는다. plan은 이 edge
module의 신규 managed 자원만 만들 수 있다. 삭제·교체·import·다른 provider 자원은
거부한다. 스토리지·VM 자원은 이 stage가 만들지 않는다.

동일한 성공 요청의 재실행은 private state/lineage/자원 ID·module digest를 확인하고
**새 읽기 전용 Terraform plan**을 만든다. 정확한 no-op이어야 하며 Cloudflare DNS와
Named Tunnel도 읽어서 기존 연결을 확인한다. 자원 삭제나 drift는 재생성·수정 없이
차단한다. 입력·버전·source module 변경은 별도 검토가 필요하다. 성공 전후 identity가
달라지거나 적용/연결 중 오류·timeout이 나면 journal은 `applying`/`configuring`에 남고
`CLUSTER_EDGE_OUTCOME_UNKNOWN`을 반환한다. 자동 재적용·삭제·롤백은 하지 않는다.

복구 시 운영자는 private journal·saved plan·실제 provider state·DNS ownership journal·
Kubernetes object UID를 비교하고, 발생한 변경을 정확히 확인한 뒤 별도의 검토된 복구
작업으로 처리한다. journal 삭제, state import 또는 새 cluster ID로 재생성하여 검사를
우회하지 않는다. 변경 전 실패한 read-only plan은 같은 입력으로 다시 수행할 수 있다.
전체 core deadline과 edge timeout 중 먼저 도달하는 시간을 따른다. Terraform/DNS
자식 프로세스도 동일한 실행기 deadline 안에서 종료한다.

| 결과 | 의미 |
|---|---|
| `edge_ready`, `configured` | 자원 적용·exact no-op state·DNS API/tunnel 객체 연결 readback 완료 |
| `lb_healthy` | 실제 backend health 미검증이므로 이번 provision에서는 false |
| `dns_resolved` | 공개 application DNS 위임·전파 미검증이므로 false |
| `tls_verified` | 외부 hostname의 인증서·체인·만료 미검증이므로 false |
| `public_http_verified` | 실제 앱 상태 코드·응답 marker 미검증이므로 false |

첫 설치 시 앱이 없어도 자원 연결은 완료될 수 있다. 구성 성공은 공개 서비스 정상
응답을 뜻하지 않는다. GCP certificate authorization CNAME 관측은 application DNS
전파와도 구분한다.

## 향후 실환경 인수

앱 Service는 기존 NodePort·`externalTrafficPolicy: Local` 계약을 유지한다. 앱이 없는
compute의 NodePort는 health check에 실패하는 것이 정상이다. 단일 replica가 다른
compute로 이동하면 새 대상이 건강해진 뒤 트래픽이 이동하므로 중단 시간이 있을 수
있다. 두 compute를 모두 건강하게 만들려면 각 노드의 실제 ready endpoint 배치가
필요하다. [Kubernetes Local 정책](https://kubernetes.io/docs/tutorials/services/source-ip/)

health path는 해당 앱의 준비 상태를 정확한 200으로 응답해야 한다. GCP·Octavia는
route hostname을 health check에 사용하며 AWS ALB의 native target health 동작도
앱이 수용해야 한다. ALB는 모든 대상이 unhealthy이면 fail-open하므로 provider health
만으로 공개 성공을 판단하지 않는다. [ALB health 동작](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html)

실환경에서는 다음을 각각 기록한다.

1. provider API의 **정확한 두 compute IP·NodePort** 등록과 실제 endpoint의 건강 상태
2. 공개 DNS가 해당 ALB/frontend IP/Named Tunnel을 가리키는지 관측
3. HTTPS의 hostname·인증서 체인·만료를 검증하고 HTTP redirect를 따라가지 않음
4. 등록한 앱의 health path에 정확한 200과 **사전에 정한 앱·클러스터 JSON marker**가
   동시에 있는지 확인; generic 200이나 다른 앱의 응답은 성공으로 기록하지 않음
5. Pod를 다른 compute/AZ로 이동하고 같은 PVC 데이터·LB health·공개 응답을 다시 확인

이번 작업의 테스트는 로컬 contract/stateful transport·Terraform mock plan·validate이며
실제 LB·DNS·인증서·3노드 트래픽은 미검증이다. 새로운 CI는 추가하지 않는다.
