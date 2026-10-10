# 배포용 최소 관측 구성

대상별 노드/클러스터 기본 상태와 배포된 HTTP 주소를 확인합니다. 서비스 전체의 관측성,
CI 실행 엔진이나 배포 성공 판정기를 만드는 구성이 아닙니다. 제품 API/UI 연결은 [제품 관측 계약](../docs/api/observations.md)을 따릅니다.

## 배치와 범위

```text
AWS 또는 온프레미스 (대상마다 같은 구성)

관측 VM: Docker Compose                 단일 노드 k3s
  Prometheus -------------------------> node-exporter / kube-state-metrics
      |                                 (제한된 NodePort)
      +--> Blackbox Exporter --> 배포 앱의 HTTP(S) 주소
      |
    Grafana <--- 관리자 SSH 터널 / VPN

Railshot UI: CI 단계와 배포 결과 (기존 담당 영역)
Grafana: 기본 상태와 HTTP 검사 결과 (이 모듈)
```

- 관측 VM은 고객 k3s **밖의 별도 Linux VM**으로 가정합니다. VM 생성과 Docker Engine/
  Compose v2 설치는 운영자가 준비합니다. 관측 VM 자체의 HA는 제공하지 않습니다.
- 현재 통합 runtime에 맞춰 **단일 Linux 노드**만 지원합니다. 다중 노드는 자동 발견하지
  않습니다. Namespace는 `railshot-observability`, 기본 NodePort는 `30910`, `30081`입니다.
- AWS 관측 VM에서 AWS 노드 사설 IP를 수집하고, 온프레미스 관측 VM에서는 내부 IP를 수집합니다.
  운영자 접근은 AWS SSH 터널, 온프레미스 VPN + SSH 터널을 기본으로 합니다.
- 관측 VM에서 공개 주소를 검사해도 독립적인 인터넷 이용자 전체의 접속을 보장하지 않습니다.
  VPN 내부 주소를 검사한 결과를 인터넷 공개 성공으로 표시하면 안 됩니다.
- 수집 30초, Prometheus 보존 3일/1GB 중 먼저 도달한 조건으로 제한합니다. 실제 디스크에는
  WAL/head 등의 추가 공간이 필요합니다. 관측 VM은 초기 제안 2 vCPU/2GB RAM/10GB 여유이며,
  성능 검증된 최소 사양은 아닙니다. Compose 메모리 상한도 실제 사용량과 다릅니다.
- Loki, Hubble, tracing, Alertmanager, 중앙 집계, 컨테이너별 CPU/메모리는 넣지 않습니다.
  run/tenant/commit/digest 라벨을 추가하지 않습니다.

## 무엇을 보여주는가

Grafana 대시보드 하나, 기본 8개 패널:

1. 수집 대상 연결 상태 (`up`). 이것은 앱 정상 여부가 아닙니다.
2. Node Ready.
3. Deployment 목표/가용 replica 수.
4. 현재 Pod 대기 사유 (예: ImagePullBackOff, CrashLoopBackOff).
5. 최근 15분 컨테이너 재시작 증가량.
6. 노드 CPU/메모리/루트 디스크 사용률.
7. HTTP 검사 통과 여부 (`probe_success`).
8. HTTP 검사 소요 시간.

Argo 메트릭 주소를 운영자가 설정하면 Sync/Health 표 하나만 추가합니다. Argo 상태를 통해
이번 배포 revision 성공 여부를 승인하지는 않습니다. CI/Argo revision/digest/URL의 결합은
Railshot API/CD 담당 영역입니다. **이 모듈은 published를 deployed로 승격하지 않습니다.**

`probe_success=1`은 해당 검사 위치에서 리다이렉트 없이 2xx 응답을 받았다는 뜻입니다.
올바른 앱/버전/응답 본문 검증이나 챗봇 기능 검증은 아닙니다. 정확한 health URL을 등록합니다.
Node Ready와 Pod 상태는 kube-state-metrics가 관측한 API 객체 상태이므로 API 단절에 따른
반영 지연이 가능합니다. 전원 장애 즉시 실시간 판정을 보장하지 않습니다.

수집 실패 시 이전 샘플을 현재 정상처럼 보여주지 않도록 각 패널을 `up == 1`로 제한합니다.
`Unknown / no data`를 0이나 정상으로 바꾸지 않습니다. 대기 사유 패널이 비어 있으면
먼저 수집 상태를 확인합니다. Grafana는 운영자 화면이며 다중 사용자 격리를 구현하지 않습니다.

## 설치

노드만 등록할 때 `register.py` 요청에서 `app`, `namespace`, `probe_url` 세 필드를 모두
생략합니다. node/cluster scrape와 실제 환경 ID는 유지하며 HTTP job은 앱 probe가 있는 행만
수집합니다. 샘플 앱을 등록하지 않아도 노드 관측을 켤 수 있습니다.
GCP의 검증된 public management endpoint가 등록되어 있으면 같은 IP의 metrics NodePort를
사용합니다. NAT 뒤의 실제 송신 주소가 관측 VM 사설 IP와 다르면 등록 설정에
`observer_source_cidr`를 실제 송신 IPv4 `/32`로 지정합니다. Prometheus 접속 주소는 바꾸지 않습니다.

`bootstrap.py`와 `register.py`의 운영자 설정은 등록된 관측 VM의 SSH transport를 기본으로
사용합니다. 실행기와 관측 스택을 기존 control VM에 함께 배치하고 그 사설 주소에 직접
접속할 때만 `"observer_transport": "direct"`를 명시할 수 있습니다. 이 설정도 등록된
`observer_ip`, 전용 SSH identity와 고정 known-hosts를 사용하며, 임의 접속 주소를 받지
않습니다. 같은 VM에서는 해당 사설 송신 주소로 제한한 별도 키를 생성하고 사용자 개인 키를
복사하지 않습니다. 기본 AWS SSM/GCP IAP 동작은 유지됩니다.

실제 IP와 자격증명은 `.local/`에만 둡니다. 예제 `192.0.2.x`와 `example.com`은 설명용입니다.
아래 생성 명령은 **관측 Linux VM**의 이 저장소에서 실행하는 것을 권장합니다.

```sh
mkdir -p observability/.local
cp observability/target.example.json observability/.local/target.json
# 편집기로 target.json에 실제 운영자 설정을 입력
python3 observability/render.py observability/.local/target.json observability/.local/aws-demo
```

| 입력 | 의미 |
|---|---|
| `name` | 대시보드 제목용 이름. 메트릭 라벨로 추가하지 않음 |
| `node_ip` | 관측 VM에서 도달 가능한 k3s 노드 IPv4. AWS에서는 사설 IP 권장 |
| `observer_source_cidr` | 대상에서 보이는 관측 VM의 송신 IPv4 `/32`. NAT/VPN 변환 후 주소 확인 |
| `node_metrics_port`, `cluster_metrics_port` | 충돌하지 않는 두 NodePort. 기본 30910/30081 |
| `probe_urls` | 운영자가 승인한 정확한 HTTP(S) health URL 0~10개. 노드만 수집하면 빈 목록. 비밀 쿼리·인증정보 금지 |
| `argocd_metrics` | 기본 null. 필요할 때 기존 Argo controller 메트릭의 사설/VPN IPv4:port |

1. k3s 노드가 한 대인지, Cilium 등 NetworkPolicy 구현이 동작하는지 확인합니다.
2. **적용 전** AWS/GCP/OpenStack 방화벽과 필요한 호스트 방화벽에서 두 NodePort 및
   노드 exporter의 native TCP 9100 접근을 관측 VM 송신 주소 `/32`로 제한합니다.
   NodePort는 여러 노드 인터페이스에서 열릴 수 있으므로 퍼블릭 서브넷을 안전 경계로 보지 않습니다.
   VPN 내부에서도 관측 송신 주소만 허용합니다. 규칙을 이 모듈이 자동 변경하지 않습니다.
3. 관리 kubeconfig를 가진 실행기에서 생성된 `cluster.json`을 적용합니다. 관측 VM에 관리자
   kubeconfig를 둘 필요는 없습니다. 기존 권한이 있는 관리 실행기로 이 파일만 전달해도 됩니다.
   서버 dry-run은 같은 List의 Namespace를 실제로 생성하지 않으므로 전용 Namespace를 먼저 준비합니다.

```sh
kubectl --context YOUR_TARGET get nodes
kubectl --context YOUR_TARGET create namespace railshot-observability --dry-run=client -o yaml | \
  kubectl --context YOUR_TARGET apply -f -
# 실제 등록에는 아래 List 전체를 일괄 apply하지 않고 register.py의 정책 반영 gate를 사용합니다.
kubectl --context YOUR_TARGET apply --dry-run=server -f observability/.local/aws-demo/cluster.json
python3 observability/register.py --config /private/observer-registration.json \
  --request /private/target-observation.json --out /private/observation-receipt.json
kubectl --context YOUR_TARGET -n railshot-observability rollout status deployment/cluster-metrics --timeout=120s
kubectl --context YOUR_TARGET -n railshot-observability rollout status daemonset/node-metrics --timeout=120s
```

4. 관측 VM에서 Compose를 시작합니다. JSON은 YAML의 부분집합이며 Prometheus/Blackbox/
   Grafana 설정은 표준 JSON serializer로 생성합니다. Grafana provisioning 파일 확장자는 YAML입니다.

```sh
docker compose -f observability/.local/aws-demo/compose.yaml config --quiet
docker compose -f observability/.local/aws-demo/compose.yaml up -d
docker compose -f observability/.local/aws-demo/compose.yaml ps
```

5. 운영자 PC에서 관측 VM으로 터널을 열고 `http://127.0.0.1:3000`에 접속합니다.
   온프레미스는 먼저 VPN을 연결합니다. 원격 비밀번호를 평문 HTTP로 전달하지 않습니다.

```sh
ssh -N -L 3000:127.0.0.1:3000 -L 9090:127.0.0.1:9090 OPERATOR@OBSERVER_HOST
# 관측 VM에서 최초 로그인 비밀번호 확인 (공유 로그/스크린샷에 노출 금지)
cat observability/.local/aws-demo/secrets/grafana_password
```

계정은 `admin`, 비밀번호는 렌더 시 무작위 생성합니다. Grafana 기존 볼륨의 비밀번호는
새 secret 파일만으로 변경되지 않습니다. 렌더러는 기존 출력 폴더 덮어쓰기를 거부합니다.
설정 갱신은 새 폴더에서 검토한 후 기존 구성의 비밀번호·볼륨을 보존해서 반영합니다.
별도 관측 VM마다 Compose 프로젝트 이름이 같아도 문제없지만, 한 Docker 호스트에서 여러
관측 구성을 동시에 띄우는 것은 이 MVP 범위 밖입니다.

## 네트워크와 권한

- Grafana 3000, Prometheus 9090은 관측 VM loopback에만 바인딩합니다. Blackbox 9115는
  Docker 내부에서만 접근합니다. Blackbox는 대상 URL을 받아 요청할 수 있으므로 공개 금지입니다.
- Exporter NodePort와 native TCP 9100은 SG/호스트 방화벽에서 관측 송신 주소로 제한합니다.
  `observer-only` NetworkPolicy는 cluster-metrics Pod를 보호하지만 hostNetwork node-metrics의
  접근 제한을 대신하지 않습니다. 별도의 `railshot-observer-host-metrics` Cilium 정책이 모든
  일반 Pod에서 host/remote-node의 TCP 9100과 노드 metrics NodePort로 나가는 트래픽을 거부합니다.
  `register.py`는 이 정책의 UID·내용 hash가 Cilium에 반영된 revision을 확인하고
  `cilium-dbg policy wait`가 성공한 뒤에만 exporter를 적용합니다. Cilium이 준비되지 않았거나
  정책 반영이 실패하면 exporter와 제품 관측 binding을 갱신하지 않습니다. 이 정책은 다른
  통신을 허용하지 않으며 hostNetwork 클라이언트나 외부 호스트의 방화벽을 대신하지 않습니다.
  CNI/NAT 처리에 따라 관측 주소가 달라질 수 있습니다. 수집 실패를 해결하기 위해 `/0`을 열지 않습니다.
- kube-state-metrics는 nodes/pods/deployments의 list/watch만 가능합니다. Secret 읽기와 쓰기 권한은 없습니다.
- node-exporter는 비특권/non-root이며 hostPID를 사용하지 않습니다. CPU/memory/filesystem/netdev를
  켜고, 노드 네트워크 네임스페이스의 실제 인터페이스를 읽도록 hostNetwork를 사용합니다.
  9100 listener는 Downward API `status.hostIP`가 제공한 실제 노드 IP에만 바인딩합니다. Pod 네임스페이스의 트래픽을 노드 값으로
  표시하지 않습니다. 호스트 `/proc`, `/sys`, `/`를 읽기 전용 마운트하므로 신뢰된 운영자용 설치입니다.
  Pod Security restricted 정책은 hostPath를 거부할 수 있습니다. 별도 검토 없이 정책을 낮추지 않습니다.
- 출력 루트와 secrets 디렉터리는 Linux에서 0700입니다. secret 파일은 컨테이너의 비루트 UID가
  읽을 수 있도록 0444이며 0700 부모가 호스트 접근을 제한합니다. Windows ACL 보호를 보장하지 않으므로
  운영 비밀번호 생성은 Linux 관측 VM에서 수행합니다.
- Argo를 선택하면 운영자가 기존 메트릭을 관리망으로 제공해야 합니다. 이 모듈은 Argo를 설치하거나
  공개하지 않습니다. 같은 Argo가 양쪽 환경을 관리하면 패널에 양쪽 앱이 나올 수 있습니다.

## 최소 인수 검사

| 검사 | 합격 기준 |
|---|---|
| 설정/수집 | `http://127.0.0.1:9090/targets`에서 node/cluster/http의 scrape가 모두 UP |
| 실제 데이터 | Grafana에서 자원 값·Node Ready·기존 Deployment replica 수가 표시됨. 빈 패널을 PASS로 처리하지 않음 |
| HTTP | `probe_success`가 정확한 URL에 대해 1. 잘못된 health 경로는 0 |
| 실패 구분 | 테스트 exporter를 중단하면 수집 실패/데이터 없음. 앱 정상으로 유지하지 않음 |
| 배포 장애 | 전용 샘플의 잘못된 이미지에서 대기 사유/replica 부족 표시. 운영 앱 장애 주입 금지 |
| 접근 제한 | 관측 VM은 두 NodePort 접근 가능. 허용하지 않은 호스트는 두 NodePort와 native 9100 접근 불가 |

수집 성공(`up`)과 검사 성공(`probe_success`)을 각각 확인합니다. 첫 CPU 및 네트워크 rate 계산은 최소 두 번
수집이 필요합니다. 제품 API의 네트워크 수신·송신은 loopback을 제외한 노드 인터페이스의
최근 2분 평균 bytes/s 합계이며 가상 인터페이스도 포함합니다. Pod 재시작 증가량은 exporter가
놓친 짧은 Pod 수명을 완전히 복원하지 못합니다.
플랫폼 API는 기본 localhost 바인딩이며, 명시적 Host·token 설정을 갖춘 비로컬 모드도 지원합니다.
플랫폼 API의 `/healthz`는 자동 공개하거나 수집하지 않습니다. 등록된 런타임의 native K3s `/healthz`는
앱과 독립적으로 CA/TLS를 검증해 30초마다 수집합니다. 설정은 [등록 경로](../docs/api/observer-registration.md)를 참조하세요.
승인된 접근 경로가 생기면 `probe_urls`에 추가하되 설정 유무와 실제 CI 실행 가능 여부를 구분합니다.

## 버전과 검증

- Prometheus 3.15.0, Grafana 13.2.3, Blackbox Exporter 0.28.0, node-exporter 1.12.1.
- kube-state-metrics 2.18.0: 통합 k3s 1.34 계열과 client-go 1.34를 맞춘 선택입니다.
  최신 버전이라는 뜻이 아니며 업데이트 시 호환성과 보안 공지를 다시 확인합니다.
- 이미지 태그는 고정했지만 digest lock은 아직 아닙니다. 실제 노드 pull/아키텍처 검증은 인수 대상입니다.

```sh
python3 -m unittest discover -s observability -p 'test_*.py' -v
# promtool 설치 시 대시보드 PromQL과 Prometheus 설정까지 검사
PROMTOOL=/path/to/promtool python3 -m unittest discover -s observability -p 'test_*.py' -v
# Blackbox 바이너리가 있으면 로컬 HTTP 200/503/302 실프로세스 검사도 포함
PROMTOOL=/path/to/promtool BLACKBOX=/path/to/blackbox_exporter \
  python3 -m unittest discover -s observability -p 'test_*.py' -v
```

2026-10-02 로컬 검증:

- 기본 계약 12개 + Prometheus 설정/12개 대시보드 쿼리 검사 + Blackbox 실프로세스 검사: 14개 통과.
- `docker compose config --quiet`: 통과. Docker 엔진이 실행 중이 아니므로 컨테이너 기동 검증은 제외.
- kubeconform 0.8.0, Kubernetes 1.34 스키마: 생성 리소스 9개 모두 strict 검증 통과.
- 도구는 공식 release checksum을 확인한 Linux 바이너리를 사용했습니다.
- 로컬 작업 지침 `AGENTS.md`, 렌더 출력과 비밀번호는 gitignore로 제외합니다.

2026-10-02 [AWS 실자원 인수 검증](../docs/integration/observability-acceptance-20261002.md)에서
설치·수집·정상/실패 HTTP·수집 중단·앱 이미지 장애·NodePort/Cilium 접근 제한과 자원 정리를 확인했습니다.
Grafana는 API 및 패널 질의 결과를 확인했으며 브라우저 화면 렌더는 검증하지 않았습니다.
온프레미스/VPN 도달성과 실제 제품 API/CD 연결은 별도 검증 대상입니다. 제품 관측 helper/UI의 모의 검사는 클라우드 연결 증거가 아닙니다.

중지 시 `docker compose -f .../compose.yaml down`은 named volume을 보존합니다. `down -v`는 데이터를
삭제하므로 기본 절차로 사용하지 않습니다. Exporter 제거는 해당 `cluster.json`의 리소스만 검토 후
삭제합니다. 목록에 Namespace가 포함되므로 이 네임스페이스에 다른 앱을 넣지 않습니다.

## 근거

- [Prometheus 계측](https://prometheus.io/docs/practices/instrumentation/)
- [Prometheus 보안](https://prometheus.io/docs/operating/security/)
- [Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)
- [kube-state-metrics 호환성](https://github.com/kubernetes/kube-state-metrics#compatibility-matrix)
- [Argo CD metrics](https://argo-cd.readthedocs.io/en/stable/operator-manual/metrics/)
