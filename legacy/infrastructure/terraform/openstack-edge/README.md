# OpenStack Amphora L7 edge for self-managed K3s

기존 Neutron subnet 위에 **Amphora LB 하나와 TLS 종료 listener 443**을 만들고, `hostname + path`로 기존 K3s 노드의 HTTP NodePort에 전달한다. self-managed K3s를 대상으로 하며 GKE Ingress나 Kubernetes Ingress controller를 설치하지 않는다. OpenStack provider는 `3.4.0`으로 고정한다.

이 모듈은 LB, listener, pool, member, HTTP health monitor, L7 policy/rule만 소유한다. DB·K3s·Amphora provider 설치, Neutron subnet/router/SG, Barbican 인증서, floating IP, Cloudflare/DNS는 기존 소유자가 관리한다. 기본 주소는 기존 RFC1918 subnet의 **사설 VIP**다. 이 주소가 인터넷에서 접속된다는 뜻은 아니다. 필요하면 별도 소유자가 공개 FIP를 `vip_port_id`에 연결하고 공개 경로를 인수한다.

## 입력과 사전조건

- Octavia **Amphora**가 실제 동작하고 LB/member subnet 사이 경로가 준비되어 있어야 한다. listener `allowed_cidrs` 때문에 최소 Octavia API **2.12 이상**을 전제로 하며 TLS 종료·L7·Host health check를 포함한 각 필드의 실환경 지원을 확인한다. OVN LB의 `ACTIVE/ONLINE` 기록은 Amphora 또는 L7/TLS 지원 증거가 아니다. 현재 현장 Amphora 설치·자격·도달성은 이 모듈로 실조회하지 않았다.
- 인증은 운영자의 `OS_*` 환경변수 또는 `OS_CLOUD`가 선택하는 `clouds.yaml`을 사용한다. tfvars에는 자격정보를 넣지 않는다. 관리자가 승인한 project/region을 확인하고 프로젝트 범위 자격으로 실행한다.
- `vip_subnet_id`는 기존 subnet UUID다. read-only data 조회로 그 CIDR 전체가 RFC1918 IPv4인지 검사한다. `member_subnet_id`는 해당 K3s 노드가 위치한 기존 subnet이다.
- `default_tls_container_ref`는 **기존 Barbican `/v1/containers/UUID` 또는 PKCS12 `/v1/secrets/UUID` HTTPS 참조**다. 인증서·키 payload를 읽거나 state에 쓰는 resource/data는 없다. 참조 URI와 자원 ID는 state에 남는다. 실제 certificate SAN, 유효기간, 체인과 Octavia의 접근 권한은 별도 확인한다. 인증서는 모든 등록 hostname을 포함해야 한다.
- `allowed_cidrs`는 listener 443의 명시적 IPv4 허용 목록이다. 빈 목록과 `/0`은 거절한다. 이 목록은 NodePort SG를 대신하지 않는다. 기존 노드 SG/방화벽은 현장 Amphora의 실제 source 주소에서 해당 NodePort와 health 경로만 허용하도록 소유자가 설정한다.
- `routes`는 1–50개다. `host`, `target_private_ip`, `node_port`, `health_path`는 기존 AWS edge 입력의 의미를 유지한다. `member_subnet_id`와 `path_prefix`(생략 시 `/`)를 추가한다. **AWS `priority`/provider_kind/SG 필드는 이 모듈 입력이 아니다.** Octavia position을 설정하지 않는다.
- path는 `/` 또는 slash로 구분한 영숫자·`_`·`-` segment만 허용한다. trailing slash, `.`, 정규식 문자, `%`, query, fragment, 공백과 줄바꿈은 입력에서 거절한다. path rewrite는 하지 않는다. 앱이 선택한 prefix 그대로 서비스해야 한다.
- monitor는 HTTP/1.1 `GET health_path`, `Host: route.host`, `200`을 사용한다. 따라서 호스트 기반 앱도 해당 hostname의 health 경로로 검사한다. TLS/앱 버전/외부 접속 확인을 대체하지 않는다.

```hcl
name                      = "railshot-onprem-edge"
vip_subnet_id             = "11111111-2222-3333-4444-555555555555"
default_tls_container_ref = "https://barbican.example.com:9311/v1/containers/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
allowed_cidrs             = ["192.0.2.10/32"] # 예시; 실제 승인된 origin client CIDR로 교체
enabled                   = false
routes = {
  web = {
    host              = "demo.example.com"
    path_prefix       = "/"
    target_private_ip = "10.20.0.10"
    member_subnet_id  = "22222222-3333-4444-5555-666666666666"
    node_port         = 30080
    health_path       = "/health"
  }
  api = {
    host              = "demo.example.com"
    path_prefix       = "/api"
    target_private_ip = "10.20.0.10"
    member_subnet_id  = "22222222-3333-4444-5555-666666666666"
    node_port         = 30081
    health_path       = "/health"
  }
}
```

## Host/path 선택과 기본 응답

각 policy는 `HOST_NAME EQUAL_TO`와 `PATH REGEX`를 **AND**한다. `/api` 패턴은 `^/api(/|$)`이므로 `/api`와 `/api/items`를 받고 `/apix`는 받지 않는다. 입력 alphabet에 정규식 메타문자를 허용하지 않아 문자열 보간에 추가 escaping이 필요하지 않다.

같은 host에서 더 구체적인 prefix를 가진 모든 child에 대해 parent policy에 `PATH REGEX + invert=true` 규칙을 추가한다. `/` policy에는 `NOT ^/api(/|$)`가 들어가며, `/api/admin`이 추가되면 `/`와 `/api` 모두 그 prefix를 제외한다. 따라서 **가장 긴 path prefix 하나만 일치**하고 Octavia가 position을 다시 매겨도 결과가 달라지지 않는다. 같은 host/prefix 중복은 거절한다. 최대 50 routes에서 exclusion은 1,225개까지 생길 수 있으므로 현장 L7 quota도 확인한다.

listener에 `default_pool_id`를 설정하지 않고 pool도 `listener_id`가 아닌 `loadbalancer_id`로 연결한다. 미등록 host 또는 어떤 prefix에도 일치하지 않는 요청은 앱으로 전달하지 않는다. Octavia의 문서상 기본 응답은 **503**이다. 등록된 `/` fallback이 있으면 그 host의 `/apix`는 의도대로 web으로 간다.

## 검증과 실행

Terraform 1.5.7을 유지한다. 아래 앞 세 검사는 실제 cloud 호출 없이 수행할 수 있다. `init`은 provider 다운로드를 위해 registry에 접속한다. `test_inputs.py`는 임시 source-only 디렉터리에서 Terraform console로 입력과 실제 생성 패턴을 평가한다. 요청 행렬·정책 순서·경계 실패를 검사하지만 Amphora의 실제 트래픽 검증은 아니다.

```sh
# 저장소 root에서 모듈 디렉터리로 이동
cd infrastructure/terraform/openstack-edge
terraform init -backend=false -input=false
terraform fmt -check
terraform validate
python3 -m unittest discover -s . -p 'test_*.py' -v
```

실행할 때에는 운영자 소유의 private 작업 디렉터리와 승인한 backend를 사용한다. state/plan/tfvars를 Git 또는 공유 artifact에 올리지 않는다. 적용 전 읽기 전용으로 `openstack loadbalancer provider list`, 대상 subnet, Barbican 참조 메타데이터, NodePort HTTP를 확인한다. API 권한이나 현장 접속이 없으면 설치 여부를 추정하지 않고 적용을 보류한다.

1. `enabled=false` 입력으로 saved plan을 만들고 실제 자원·project·subnet·규칙 diff를 검토한 다음 적용한다. listener는 비활성 상태다.
2. LB/provider/VIP, listener protocol·CIDR·TLS ref, pool/member/monitor, policy의 host/path와 inverted child 규칙 및 default pool 부재를 API로 readback한다. `openstack loadbalancer status show <LB_ID>`와 `openstack loadbalancer l7policy list --listener <LISTENER_ID>`, 각 policy의 `openstack loadbalancer l7rule list <POLICY_ID>` 등을 사용한다.
3. 설정 전체를 readback한 뒤 **`enabled`만 true로 바꾸는 별도 saved plan/apply**를 수행하고 member health와 실제 TLS·HTTP를 확인한다. 기존 활성 환경의 변경·제거 전에는 **현재 routes 등 나머지 입력을 그대로 유지한 `enabled=false`만의 saved plan/apply → 실제 listener `admin_state_up=false` readback**을 먼저 완료한다. 비활성화와 route 변경·삭제를 한 plan에 섞으면 resource 작업이 동시에 진행될 수 있어 안전한 선행 중지를 보장하지 않는다. 그 뒤 비활성 상태로 구성을 변경하고 2–3을 반복한다. 이는 운영 절차이며 모듈이 변경 이력을 보고 자동으로 강제하는 기능은 아니다.
4. 인수는 등록 root와 `/api`, 중첩 경로, `/apix`, 미등록 host를 모두 포함한다. 아래 명령은 VIP에 연결하면서 요청 Host/SNI와 인증서 이름을 유지한다. 신뢰된 내부 CA라면 `--cacert /private/path/ca.pem`을 사용하고 **`-k`로 검증을 생략하지 않는다.** 응답 body의 실제 기대 앱/버전과 image digest·배포 revision을 대조한다. HTTP 200 또는 health만으로 다른 backend/이전 버전을 성공 처리하지 않는다.

```sh
terraform plan -var-file=/private/path/edge.tfvars -out=/private/path/edge.plan
terraform apply /private/path/edge.plan
curl --fail --show-error --resolve demo.example.com:443:10.20.0.50 https://demo.example.com/
curl --fail --show-error --resolve demo.example.com:443:10.20.0.50 https://demo.example.com/api
curl --fail --show-error --resolve demo.example.com:443:10.20.0.50 https://demo.example.com/apix
```

미등록 host 검사는 인증서가 실제로 포함하는 별도의 테스트 hostname을 사용해야 TLS 뒤 HTTP의 미매칭 동작까지 확인할 수 있다. `app_urls`는 예상 주소이며 DNS 설정, FIP, Cloudflare 연결 또는 외부 HTTP 성공 증거가 아니다. 공개 경로를 따로 연결했다면 실제 외부 resolver·TLS·기대 응답도 다시 확인한다.

## 중지와 제거

`enabled=false` 적용은 listener 접근을 중지하며 LB/Amphora 비용은 남는다. 마지막 route를 지울 때 빈 map은 거절한다. 이 전용 모듈의 LB 전체를 제거하려면 위의 **기존 구성 유지 → disable-only 적용 → 비활성 live readback**을 완료하고 외부 소유 FIP/DNS 참조를 정리한다. 그 뒤 정확한 state에서 `terraform plan -destroy -var-file=… -out=…`을 검토하고 saved plan을 적용한다. LB와 이 모듈의 하위 자원만 제거 대상이며 기존 subnet, K3s, 인증서, DB는 유지된다. state 삭제를 teardown으로 취급하지 않는다.

API 생성 뒤 ACTIVE 대기 중 timeout/프로세스 중단이 발생하면 실제 자원이 만들어졌어도 provider가 ID를 state에 기록하지 못할 수 있다. **destroy 성공 또는 빈 state만으로 정리 완료를 선언하지 않는다.** 해당 project의 LB UUID와 하위 listener/pool/member/monitor/policy/rule을 live 조회하고 state와 대조한다. 소유권을 확인한 누락 자원만 import하여 reviewed destroy에 포함하거나 관리자가 확인한 UUID로 삭제하고, 마지막 live 조회 결과를 기록한다. 최초 LB UUID도 잃었다면 중단 요청·project·생성 시각을 근거로 관리자가 자원을 식별해야 한다. 이름이 같다는 이유만으로 임의 삭제하지 않는다.

## 공식 근거와 검증 범위

- [OpenStack provider 3.4.0](https://registry.terraform.io/providers/terraform-provider-openstack/openstack/3.4.0/docs): 실제 provider 설치 후 schema validation 수행.
- [Octavia L7](https://docs.openstack.org/octavia/latest/user/guides/l7.html): policy AND, invert, position 재번호와 default pool 없는 미매칭 동작.
- [Octavia TLS cookbook](https://docs.openstack.org/octavia/latest/user/guides/basic-cookbook.html#deploy-a-tls-terminated-https-load-balancer): TLS 참조·PKCS12 사용.

이 단계의 증거는 로컬 format/validate, offline routing/input tests와 provider lock뿐이다. 현장 apply/destroy, Amphora 설치·접속, 인증서 권한, health/TLS/HTTP, Cloudflare는 아직 이 모듈의 실제 실행으로 검증하지 않았다.
