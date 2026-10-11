# Deployment Runtime 데모 실행 안내

기존 클러스터(컨테이너 실행 환경)의 정상 상태를 바로 보여주는 경로입니다. Dashboard·MCP·DB·GitOps·provider 네트워크는 이번 점검 범위가 아닙니다.

## 검증된 경로

| 항목 | 이번 작업에서 직접 확인한 값 |
| --- | --- |
| 대상 | 실행 중인 Lima VM(맥 안의 Linux 가상 서버) `railshot-runtime-test` |
| OS / CPU | Ubuntu 24.04.4 / arm64 |
| Runtime | 기존 K3s와 Cilium 재사용, 설치·재시작 없음 |
| namespace(앱 구역) | `railshot-demo` |
| Deployment / Service | `railshot-workload` / `railshot-workload` |
| Pod(앱 실행 단위) | `railshot-workload-56b77967cd-jld8v`, Running / Ready 1/1 |
| 이미지 | 기존 선언 `nginx:1.28.0-alpine`, 실행 이미지 고정값은 아래 fixture에 기록합니다. |
| NodePort(노드에서 여는 앱 포트) | `30080` |
| 노드 내부 health | `http://192.168.5.15:30080/`, HTTP 200, `Railshot Runtime OK` |
| 맥에서 VM으로 접근 | `http://127.0.0.1:30082/`, HTTP 200 |

[golden fixture](scripts/tests/fixtures/demo-golden-local.json)는 관찰된 nginx imageID를 고정합니다. 기존 Deployment를 해당 fixture로 재배포하지 않았습니다. 이 이미지 고정값은 이번 arm64 노드에서 확인한 값이며 AWS/GCP의 amd64 이미지 검증을 대신하지 않습니다. fixture의 `provider=openstack`은 입력 문맥입니다. Lima가 실제 OpenStack인 것은 아닙니다.

발표에서는 같은 노드·앱·Service·health 경로를 사용합니다. clean install(처음 설치), reinstall(재설치), full cleanup(전체 제거), bundle 생성, VM 생성, 네트워크·IAM·방화벽·LB·DNS 변경은 수행하지 않습니다. update(앱 교체)를 보여줄 경우 기존 소유권과 캐시된 이미지, 정상 준비 상태를 사전 리허설에서 확인해야 합니다. 이번 작업은 라이브 교체를 실행하지 않았습니다.

## 발표 직전 복붙: 준비된 로컬 VM

맥에서 아래 명령을 실행합니다. VM이 이미 실행 중이고 저장소 경로가 VM에 공유된 상태가 전제입니다.

```bash
cd "/Users/seungminlee/Desktop/Development/Softbank Hackerton 2026/Railshot"
limactl shell railshot-runtime-test sudo python3 "$PWD/deployment/scripts/demo_preflight.py" \
  --input "$PWD/deployment/scripts/tests/fixtures/demo-golden-local.json" \
  --expected-image-id docker.io/library/nginx@sha256:30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284 \
  --timeout-seconds 25
curl --noproxy '*' --connect-timeout 2 --max-time 3 -i http://127.0.0.1:30082/
```

노드 내부 점검 시간은 0.67초였습니다. 25초 제한은 Linux 점검 프로세스의 공통 예산입니다. VM 접속 자체가 멈추면 그 전에 실패할 수 있으므로 발표 전에 Lima 연결도 확인합니다. Linux에서는 이미 전달된 저장소의 `deployment/scripts/demo_preflight.py`를 `sudo python3`로 직접 실행할 수 있습니다.

점검은 `systemctl is-active`, `kubectl get`, 제한 시간 있는 `curl`만 사용합니다. Node Ready, Cilium DaemonSet(노드별 네트워크 앱) Ready 및 agent Running, namespace Active, Deployment의 최신 generation(설정 버전), Pod Ready, Service 선택 조건·포트, 준비된 EndpointSlice(실제 Service 연결 대상), node-local HTTP를 확인합니다. 옵션의 imageID는 실제 실행 이미지 고정값입니다. 여러 CPU를 묶은 manifest index digest와 비교해서는 안 됩니다.

stdout(기계용 출력)은 별도 점검 JSON이며 기존 DeploymentResult schema가 아닙니다. stderr(사람용 출력)에는 `[PASS]`, `[FAIL]`, `[UNKNOWN]`을 출력합니다. `ready`만 exit 0이고 `not_ready`와 `unknown`은 exit 1입니다. API 권한 부족·접속 실패·시간 초과·리소스 누락을 정상으로 간주하지 않습니다. 공개 URL과 DNS, 방화벽은 이 점검에서 검사하거나 변경하지 않습니다.

## 20~30초 판단 기준

| 관찰 | 판단과 발표 행동 |
| --- | --- |
| Node / Cilium / Pod Ready, Service 연결 대상 정상, node-local HTTP 200 | Runtime 구간 정상입니다. 공개 URL 실패는 별도로 설명합니다. |
| 로컬 상태 정상이나 공개 URL timeout | 외부 의존성 또는 provider exposure(노드 밖 공개 경로) 문제입니다. Runtime 재설치로 대응하지 않습니다. timeout만으로 방화벽 원인을 확정하지 않습니다. |
| node-local HTTP 실패 또는 Pod / Cilium 미준비 | Runtime 구간 현재 미준비입니다. 원인은 별도 진단이 필요합니다. 아래 읽기 전용 fallback(대체 시연)으로 상태를 보여줍니다. |
| 25초 초과, API·VM 접속 불가, 권한 부족 | `unknown`으로 종료합니다. 라이브 시연을 중단하고 저장된 검증 자료를 보여줍니다. |

## 30초 이내 fallback: 로컬 VM

정상 앱이 남아 있다면 아래 조회로 바로 보여줍니다. 실패한 상태를 복구하거나 자동 rollback(이전 버전 복원)하는 명령은 아닙니다. 앱 자체가 준비되지 않으면 성공을 주장하지 않고 [실측 결과](scripts/tests/results/RUNTIME-DEMO-STABILITY-2026-10-03.md)를 보여줍니다.

```bash
limactl shell railshot-runtime-test sudo timeout 25s bash -c '
set -e
K=/usr/local/bin/k3s
"$K" kubectl --request-timeout=3s get nodes
"$K" kubectl --request-timeout=3s get pods -A
"$K" kubectl --request-timeout=3s -n kube-system get daemonset cilium
"$K" kubectl --request-timeout=3s -n railshot-demo get deployment railshot-workload
"$K" kubectl --request-timeout=3s -n railshot-demo get service railshot-workload -o wide
NODE_IP=$("$K" kubectl --request-timeout=3s get nodes -o jsonpath="{.items[0].status.addresses[?(@.type==\"InternalIP\")].address}")
curl --noproxy "*" --fail --connect-timeout 2 --max-time 3 -i "http://$NODE_IP:30080/"
'
```

## GCP에서 기존 상태만 보여줄 때

사용자가 알려주신 GCP 앱은 `tenant-demo/fixture-npm-js`, NodePort `30080`, health `/health`입니다. 위 로컬 nginx의 namespace·앱 이름·`/` 경로와 혼동하지 않습니다. 이미 허가된 접속 수단으로 해당 Linux 노드에 들어간 뒤 아래 조회만 실행합니다. 이번 작업에서 GCP 원격 명령은 실행하지 않았습니다.

```bash
sudo timeout 25s bash -c '
set -e
K=/usr/local/bin/k3s
"$K" kubectl --request-timeout=3s get nodes
"$K" kubectl --request-timeout=3s get pods -A
"$K" kubectl --request-timeout=3s -n kube-system get daemonset cilium
"$K" kubectl --request-timeout=3s -n tenant-demo get deployment fixture-npm-js
"$K" kubectl --request-timeout=3s -n tenant-demo get service fixture-npm-js -o wide
NODE_IP=$("$K" kubectl --request-timeout=3s get nodes -o jsonpath="{.items[0].status.addresses[?(@.type==\"InternalIP\")].address}")
curl --noproxy "*" --fail --connect-timeout 2 --max-time 3 -i "http://$NODE_IP:30080/health"
'
```

GCP Service 이름은 통합 담당자가 실제 선언과 일치하는지 마지막으로 확인해야 합니다. node-local HTTP 200과 `{"status":"ready"}`는 기존 사용자 확인 결과입니다. 외부 `34.47.68.21:30080` timeout은 공개 경로 미확인으로 남습니다. 발표에서 공개 URL이 필요하면 통합 담당자가 제공한 기존 정상 URL을 사전 확인합니다. 이 작업에서 공개 경로를 생성하거나 고치지 않습니다.

## 통합 시 주의사항

- Runtime JSON → Input Adapter(외부 입력 정규화) → DeploymentSpec(공통 실행 설정) → Engine → DeploymentResult 흐름과 0.1/0.2 출력 형식을 유지합니다.
- `exposure.verification_url` 실패는 0.2의 `exposure_status.status=degraded`에 기록합니다. Runtime `ready`와 외부 공개 성공을 분리해 표시해야 합니다. 0.1에서는 기존 필드만 출력하고 외부 실패 경고는 stderr로 전달합니다.
- 기존 `verify`는 일시적인 검사 Pod를 만듭니다. 발표의 읽기 전용 점검에는 위 `demo_preflight.py`를 사용합니다.
- 발표 전 fixture·namespace·앱/Service 이름·실행 imageID·health 경로·접속 방식은 통합 담당자와 고정합니다. 서로 다른 앱에 로컬 golden fixture를 덮어쓰지 않습니다.
- 최신 integration의 core Runtime 계약은 이번 비교에서 동일했습니다. Dashboard → S1 → Runtime 연결 전체가 검증된 것은 아닙니다.
