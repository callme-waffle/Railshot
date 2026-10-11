# Named Tunnel → Octavia HTTPS

`render.py`는 self-managed K3s에서 실행할 **locally managed Cloudflare Named Tunnel**의 Kubernetes JSON List를 만든다. 등록 hostname을 기존 Octavia **사설 IPv4 HTTPS VIP:443**에 전달하며, hostname별 `originServerName`과 `httpHostHeader`를 유지한다. `/api`와 `/` 등의 경로 선택은 Octavia가 수행한다. 미등록 hostname은 마지막 `http_status:404` 규칙으로 끝난다.

Python 표준 라이브러리만 사용한다. 생성물은 ServiceAccount·ConfigMap·Deployment 세 개이며, API 호출이나 cloudflared 실행·배포를 자동으로 수행하지 않는다. namespace·터널·DNS·기존 credential Secret·CA ConfigMap·Octavia·K3s·DB·네트워크 정책은 생성하거나 삭제하지 않는다. 제품 API와 runtime 설치 호출에도 자동 연결하지 않는다.

`register.py`는 설치된 connector에 앱 hostname을 추가하는 별도 호출점이다. 기존 `environment`의 registry·SSH·kubectl helper와 의존성(`PyYAML`, `jsonschema`)을 재사용한다. 터널 생성·인증은 운영자가 한 번 준비하며, 앱마다 다시 로그인하거나 connector VM을 만들지 않는다.

## 기존 connector에 앱 등록

제품 배포 호출자는 `ensure(private_config_path, request)`를 호출한다. `request`는 `environment_id`, `application_id`, `app`, `tenant`, `hostname` 다섯 문자열만 받는다. hostname과 application ID는 기존 서비스 이름·앱 식별 규칙에서 계산한 값과 일치해야 한다. CLI는 같은 요청을 소유자 전용 JSON 파일로 받는다.

```sh
python3 deployment/cloudflared/register.py \
  --config /var/lib/railshot/config/openstack-tunnel.json \
  --request /private/path/application-request.json
```

운영 설정에는 `version: 1`, `state_dir`, `registry_file`, `environment_id`, `resource_id`, `runtime_private_address`, `base_domain`과 renderer의 `namespace`, `name`, `tunnel_id`, `credentials_secret`, `origin_vip`, `ca_configmap`, 그리고 설치 후 확인한 `configmap_uid`, `deployment_uid`를 기록한다. 파일은 절대 경로의 소유자 전용 파일이어야 한다. 기존 ConfigMap의 `railshot.io/application-owners` annotation에는 hostname → application ID JSON 매핑을 초기화한다.

writer는 단일 writer 잠금 아래 registry의 OpenStack VM과 기존 Kubernetes UID를 검증하고, 다른 앱의 ingress를 보존해 hostname을 합친다. TLS·SNI·VIP·Deployment 설정이 달라졌거나 hostname 소유자가 다르면 변경 전에 멈춘다. resourceVersion을 함께 검사하는 JSON Patch로 config·owners·template hash만 갱신한다. ConfigMap만 반영된 부분 실패는 같은 요청을 재시도하면 복구한다.

성공 결과의 `phase`는 `tunnel_configured`이고 `https_verified`는 항상 `false`다. `dns`에는 해당 터널의 CNAME과 `proxied: true`를 반환한다. 호출자가 기존 DNS writer에 이를 전달하고 앱 배포 후 외부 HTTPS 응답을 확인해야 한다. connector Ready만으로 backend나 공개 URL 성공을 기록하지 않는다. 잠금 충돌·변경 전 검증 실패는 `blocked`, 변경 시도 이후 불확실한 실패는 `unknown`으로 반환한다.

제품의 OpenStack environment는 `ingress.edge_config_file`에 기존 Octavia worker의 전용 SSH 설정, `ingress.tunnel_config_file`에 위 connector 설정, `ingress.dns_config_file`에 기존 Cloudflare DNS writer 설정을 연결한다. 세 파일 경로가 모두 설정돼야 자동 배포 선택지가 열린다. `application_routes.py`는 검증한 publication에서 나온 앱 식별자·hostname·NodePort·health 경로를 사용해 Octavia → Tunnel → DNS 순서로 연결하고, 이어서 기존 CD 경로가 앱을 배포하고 외부 HTTPS를 검증한다. Tunnel CNAME만 `proxied: true`와 자동 TTL을 사용하며 기존 AWS/GCP 레코드는 DNS-only 설정을 유지한다.

## 입력과 실행 경계

- 대상 namespace와 **locally managed** 터널 UUID가 있어야 한다. 같은 namespace의 기존 Secret에는 해당 터널의 `credentials.json` 한 키가 필요하다. 원격 관리 터널 token은 이 입력 형식이 아니다. account 관리용 `cert.pem`, API token 또는 비밀번호를 Pod에 넣지 않는다.
- 공개 hostname은 해당 Cloudflare 계정의 활성 zone 또는 별도로 승인된 지원 DNS 구성에 등록되어야 한다. 터널 credential만으로 DNS가 생기지 않는다. 기존 Route53 위임을 이 모듈이 변경하지 않는다.
- `--hostname`을 반복해 1–50개를 지정한다. wildcard·IP hostname·URL·port·trailing dot·대문자·중복을 거절한다. `--origin-vip`는 RFC1918 IPv4만 받으며 HTTPS 443을 고정한다. public IP·metadata·CGNAT·loopback·IPv6·URL은 거절한다.
- Octavia 인증서는 등록 hostname 각각을 포함해야 한다. 사설 CA라면 기존 ConfigMap의 `ca.pem` 키를 `--ca-configmap`으로 연결한다. public CA는 시스템 trust store를 사용한다. **TLS 검증을 끄는 옵션은 없다.** hostname별 인증서 이름·HTTP Host를 동일하게 설정하므로 별도 내부 SNI hostname을 쓰는 구성은 이 모듈 범위 밖이다.
- Pod에서 VIP:443으로 도달할 수 있어야 한다. Octavia `allowed_cidrs`와 노드/현장 방화벽에는 실제 connector source 주소(환경에 따라 Pod 또는 SNAT된 노드 주소)를 허용한다. Tunnel에는 승인된 DNS resolver와 Cloudflare의 TCP/UDP 7844 outbound 경로가 필요하다. 이 모듈이 broad egress나 inbound 방화벽 규칙을 열지는 않는다.
- replica **1**, `Recreate`는 단일 노드 PoC의 의도된 제한이다. rollout·장애·노드 중단 시 접속이 끊길 수 있으며 HA를 주장하지 않는다.

## 생성·검증·설치

아래 UUID·주소·hostname은 예시다. 실제 승인된 값을 넣고 정확한 Kubernetes context/namespace를 확인한다. 기존 Secret은 소유자가 credential 파일에서 생성하며 값을 CLI 인수·Git·manifest·로그에 붙여 넣지 않는다. 예를 들어 `kubectl create secret generic tunnel-credentials --namespace edge-demo --from-file=credentials.json=/private/path/tunnel.json`처럼 **파일 경로**를 사용한다. 로컬 credential 파일은 소유자만 읽도록 보호하고, Secret API 조회 결과를 공유 artifact에 저장하지 않는다.

```sh
python3 deployment/cloudflared/render.py \
  --namespace edge-demo --name demo-tunnel \
  --tunnel-id 11111111-2222-4333-8444-555555555555 \
  --credentials-secret tunnel-credentials \
  --hostname web.example.com --hostname api.example.com \
  --origin-vip 10.20.0.50 > /private/path/tunnel-manifest.json
```

사설 CA 사용 시 같은 명령에 `--ca-configmap origin-ca`를 추가한다. 해당 ConfigMap은 public CA certificate bundle만 담고 private key를 포함하지 않아야 한다. 생성물의 `ConfigMap.data["config.json"]`은 cloudflared가 읽는 JSON 형식의 YAML 설정이다. credential/CA mount 경로는 고정돼 있다.

```sh
python3 -m unittest discover -s deployment/cloudflared -p 'test_*.py' -v
CLOUDFLARED=/private/tools/cloudflared python3 -m unittest discover -s deployment/cloudflared -p 'test_*.py' -v
kubectl --context APPROVED_CONTEXT apply --dry-run=server -f /private/path/tunnel-manifest.json
kubectl --context APPROVED_CONTEXT apply -f /private/path/tunnel-manifest.json
kubectl --context APPROVED_CONTEXT -n edge-demo rollout status deployment/demo-tunnel
```

`CLOUDFLARED`를 주면 실제 바이너리의 `tunnel ingress validate`, 등록·미등록 URL의 `ingress rule`, localhost `/ready`의 200/503 종료 코드를 검사한다. 터널 등록이나 연결은 시작하지 않는다. 환경변수가 없으면 native 검사 두 개는 **skip**이며 unit 검사 통과와 구분한다. 저장소 CI는 고정된 공식 Linux amd64 바이너리의 SHA256을 검증해 이 두 검사도 실행한다. server dry-run과 apply는 실제 클러스터 접근·권한이 필요하다.

등록된 DNS hostname이 올바른 터널 UUID를 가리키는지 운영자가 별도로 확인한다. Ready는 Cloudflare 연결 상태이며 origin의 TLS·앱·Octavia path routing 성공을 뜻하지 않는다. 실제 인수는 외부 HTTPS로 등록 host의 `/`, `/api`, `/apix` 등을 호출하고 **기대 backend와 앱 버전/배포 revision**을 응답과 대조한다. 인증서를 정상 검증하고 `curl -k`로 오류를 숨기지 않는다. 미등록 host의 404는 parser 검사와 실제 Cloudflare DNS/TLS를 거친 응답 검증을 구분한다.

## Pod 권한과 갱신

ServiceAccount에는 Role/RoleBinding을 만들지 않으며 API token 자동 mount도 끈다. Secret은 kubelet의 read-only volume mount로 전달한다. Pod는 UID/GID 65532, `runAsNonRoot`, `RuntimeDefault` seccomp, read-only root filesystem, 모든 capability 제거, privilege escalation 금지로 실행한다. hostNetwork/hostPID/hostIPC를 사용하지 않는다. CPU는 100m request/500m limit, 메모리는 128Mi request/256Mi limit이다.

metrics는 **127.0.0.1:2000**에만 바인딩하며 Service나 hostPort를 만들지 않는다. readiness는 distroless 이미지에 포함된 `cloudflared tunnel --metrics 127.0.0.1:2000 ready`를 exec한다. 외부 연결 장애 때문에 재시작을 반복하는 liveness probe는 추가하지 않는다.

비밀 없는 config 내용의 SHA256을 Pod annotation에 넣으므로 hostname/VIP 변경은 rollout을 일으킨다. 같은 이름의 Secret/CA ConfigMap **내용** 교체는 이 checksum에 포함되지 않는다. 교체 뒤 운영자가 `kubectl rollout restart deployment/demo-tunnel -n edge-demo`를 수행하고 TLS·origin 응답을 재확인한다. 이전 config가 남은 외부 replica가 있다면 별도로 정리해야 한다. 이 모듈의 replica 수가 다른 곳의 connector까지 제어하지는 않는다.

## 정리와 소유권

먼저 hostname/DNS 트래픽 중단 또는 전환을 소유자와 확인한다. **생성에 사용한 정확한 manifest**로 `kubectl --context APPROVED_CONTEXT delete -f /private/path/tunnel-manifest.json --ignore-not-found`를 실행하면 여기서 만든 Deployment·ConfigMap·ServiceAccount만 제거한다. 고유 name을 사용하고 기존 동명 자원의 소유권을 확인해 덮어쓰지 않는다.

Kubernetes Deployment/Pod가 사라졌는지와 Cloudflare의 해당 터널 connector가 실제로 끊겼는지를 따로 확인한다. 타임아웃·부분 적용 후에는 같은 namespace/name과 labels로 잔존 자원을 확인한다. 기존 credential Secret·CA ConfigMap·namespace·Cloudflare 터널/DNS·Octavia는 유지된다. 더 이상 쓰지 않는 터널·credential·DNS는 소유자가 다른 사용처를 확인한 뒤 별도 철회·삭제한다. manifest 삭제나 rollout 성공만으로 외부 자원 정리 완료를 주장하지 않는다.

## 공식 판본과 현재 검증

- [공식 cloudflared 2026.9.3 release](https://github.com/cloudflare/cloudflared/releases/tag/2026.9.3), 2026-09-24 공개. [공식 Dockerfile](https://github.com/cloudflare/cloudflared/blob/2026.9.3/Dockerfile)의 distroless nonroot UID 65532를 따른다.
- 공식 `cloudflare/cloudflared:2026.9.3` Docker Hub index를 2026-10-02 조회하고 body SHA256을 검증했다. amd64/arm64를 포함한 multiarch index digest는 `sha256:072c067d25ccbe61d46e18f0d0723255f2bb5304f7317caa95b27031520ff92c`이며 이미지에 고정했다.
- [로컬 설정/ingress 검사](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/configuration-file/), [origin TLS·CA 설정](https://developers.cloudflare.com/tunnel/troubleshooting/https-origins/), [Kubernetes 운영](https://developers.cloudflare.com/tunnel/guides/kubernetes/)을 참고했다.

renderer 검사는 공식 Darwin arm64 바이너리의 native 검사 두 개를 포함한다. 바이너리 archive SHA256은 공식 release의 `587c2cfb1c230fe36c7fa7727da78be459dae028cabe8c001291999350f07095`와 대조했다. writer 검사는 기존 host 보존, 소유권·UID·TLS 경계, 부분 실패 복구, 요청·runtime 검증, 잠금과 rollout 시간 제한을 확인한다. 2026-10-03 운영 설치에서는 기존 OpenStack runtime의 Deployment Ready 1개와 Cloudflare 연결 4개를 확인했다. 이는 실제 앱의 외부 HTTPS 응답 검증과 구분한다.
