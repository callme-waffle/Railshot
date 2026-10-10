# 로컬 WireGuard 수신·OpenStack 제어 연결 시험

## 운영과 같은 분리 컨테이너 로컬 검증

`production.compose.yaml`은 일반 API, 공개 대시보드, root WireGuard 게이트웨이를 서로 다른 컨테이너로 실행합니다. API는 UID/GID 1000과 권한 없음으로 실행되고, 게이트웨이만 `NET_ADMIN`(네트워크 관리 권한)을 가집니다. 두 컨테이너는 API 네트워크 공간과 Unix 도메인 소켓만 공유합니다. 기본 호스트 포트는 API TCP 4193, 대시보드 TCP 4194, WireGuard UDP 51920이므로 기존 4173 서버를 바꾸지 않습니다.

아래 생성기는 외부 시스템에서 쓰지 않는 로컬 시험 전용 키와 토큰만 새 비공개 디렉터리에 만듭니다. 운영 Secret을 만들거나 추정하는 도구가 아니며, URL도 격리 Compose의 `dashboard` 이름만 허용합니다.

```sh
fixture=/private/tmp/railshot-personal-production-fixture
python3 deployment/scripts/prepare-personal-local-fixture.py --output "$fixture"
export RAILSHOT_PERSONAL_PRODUCTION_CONFIG_DIR="$fixture"

docker compose -f deployment/manifests/personal/production.compose.yaml config --quiet
docker compose -f deployment/manifests/personal/production.compose.yaml build
docker compose -f deployment/manifests/personal/production.compose.yaml up -d
docker compose -f deployment/manifests/personal/production.compose.yaml ps
```

one-shot `prepare` 컨테이너는 소켓 볼륨을 `root:1000`과 0770으로 만든 뒤 종료합니다. 게이트웨이는 시작할 때 이를 0750으로 제한하고 `root:1000`과 0660인 소켓을 만듭니다. API는 디렉터리를 통과해 소켓에 연결할 수 있지만 잠금 파일이나 소켓을 바꿀 수 없습니다. 실행 중인 실제 볼륨과 컨테이너 권한은 비밀값을 출력하지 않는 다음 검사로 확인합니다.

```sh
docker compose -f deployment/manifests/personal/production.compose.yaml run --rm --no-deps \
  --entrypoint sh gateway -ec \
  'test "$(stat -c %u:%g:%a /run/railshot-personal-gateway)" = 0:1000:750'
docker compose -f deployment/manifests/personal/production.compose.yaml exec api \
  python3 /app/deployment/scripts/personal-production-preflight.py \
  --config /var/lib/railshot/config/personal.json --test-allow-http
```

운영자 토큰과 다른 개인 클라이언트 bearer가 Nginx에서 그대로 전달되는지는 실제 대시보드 이미지의 `/api/v1/enrollments/<id>/claims`와 `/api/v1/targets/<id>/{heartbeats,receipts,runtimes}` 경로로 검사합니다. 그 밖의 `/api/` 경로는 대시보드의 중앙 토큰을 계속 사용합니다. 종료에는 `docker compose ... down`을 사용하며, 재시작 영속성 검증 전에는 `down -v`로 볼륨을 지우지 않습니다.

## Kubernetes 운영 연결

운영 개인 기능은 기본적으로 렌더링되지 않습니다. 기존 AWS와 일반 API 배포는 저장소 변수 `RAILSHOT_PERSONAL_ENABLED`가 정확히 `true`가 되기 전까지 개인 Secret, 영속 볼륨, UDP 포트, 게이트웨이 sidecar를 참조하지 않습니다. 활성화 배포는 API와 `personal-gateway` 이미지를 함께 빌드해야 합니다.

`personal.production.example.json`은 필드 설명용이며 그대로 적용할 수 없습니다. 먼저 대시보드 이미지가 생성한 `/personal/manifest.json`에서 배포 해시와 설치기 해시를 확인하고, 같은 해시 디렉터리의 두 URL을 실제 `personal.json`에 기록합니다. `runtime` 설정이 아직 없으면 해당 항목을 생략할 수 있지만 서버 readiness는 `RUNTIME_OPERATOR_NOT_CONFIGURED`로 차단됩니다.

운영 값은 출력하지 말고 운영자 소유의 절대경로 파일로 준비한 뒤 다음처럼 이름만 연결합니다.

```sh
kubectl -n railshot-system create secret generic railshot-personal-config \
  --from-file=personal.json=/absolute/private/personal.json --dry-run=client -o yaml | kubectl apply -f -
kubectl -n railshot-system create secret generic railshot-personal-gateway-key \
  --from-file=private-key=/absolute/private/wireguard-private-key --dry-run=client -o yaml | kubectl apply -f -
kubectl -n railshot-system create secret generic railshot-personal-gateway-ipc \
  --from-file=ipc-token=/absolute/private/gateway-ipc-token --dry-run=client -o yaml | kubectl apply -f -
kubectl -n railshot-system create configmap railshot-personal-settings \
  --from-literal=gateway_endpoint='<reviewed-public-host>:51820' --dry-run=client -o yaml | kubectl apply -f -
```

게이트웨이 개인키 Secret은 게이트웨이에만 마운트됩니다. IPC(프로세스 사이 통신) 토큰은 `private-config` 초기 컨테이너가 API 소유 0600 파일로 복사하고, API는 원본 Secret이나 개인키를 직접 마운트하지 않습니다. 게이트웨이와 WireGuard 상태는 각각 1GiB `ReadWriteOnce` 영속 볼륨에 남습니다. UDP 51820은 선택된 platform 노드의 hostPort로 열리므로 `gateway_endpoint`의 호스트가 그 노드를 가리키고 방화벽도 UDP 51820을 허용해야 합니다.

Secret·ConfigMap·PVC(영속 볼륨 요청)가 준비되고 로컬 분리 검증이 통과한 뒤에만 `RAILSHOT_PERSONAL_ENABLED=true`를 설정합니다. 배포 후 다음 무변경 사전검사를 통과해야 등록을 열 수 있습니다. 이 검사는 개인 설정 권한, 인증된 게이트웨이 health, 설치 manifest, 설치기 SHA256, 압축파일 SHA256과 크기를 확인하며 키·토큰을 출력하지 않습니다.

```sh
kubectl -n railshot-system exec deploy/railshot-api -c api -- \
  python3 /app/deployment/scripts/personal-production-preflight.py \
  --config /var/lib/railshot/config/personal.json
```

이 배치는 Docker의 Linux 컨테이너 하나에 API와 전용 WireGuard 게이트웨이를 둡니다. 같은 네트워크 공간을 사용하므로 API가 게이트웨이 주소를 출발지로 지정하여 고객의 전용 SSH 서비스에 접속할 수 있습니다. 서버는 고객 별도 VM의 준비 증거를 받아 실제 K3s 건강·권한과 중앙 배포 연결을 검증합니다. VM 선택·생성과 K3s 설치는 고객 설치기가 기존 기능을 호출하며, 이 이미지가 관리 호스트에 K3s를 설치하지 않습니다. 시험 앱은 배포하지 않습니다. 기존 운영 API 이미지와 별개인 연결 시험용 이미지입니다.

기본값은 localhost에만 포트를 공개합니다. 아래 예제는 승인된 사설망 시험에 한해 HTTP를 명시적으로 허용하고 VPN 주소로 수신하는 설정입니다. VPN 주소는 실행 전에 현재 값과 원격 호스트에서의 도달 여부를 확인해야 합니다. HTTP를 통한 등록 자격은 해당 망에서 암호화되지 않으므로 공개 인터넷에 이 예제를 그대로 배치하지 않습니다. 일반 설치 흐름의 HTTPS 기본값은 유지됩니다.

## 빌드와 시작

저장소 루트에서 실행합니다. Node 24, Python, WireGuard 도구, iproute2, iptables, sudo, SSH 클라이언트, git, CPU 종류별 고정 SHA256으로 검증한 kubectl v1.34.11이 이미지에 포함됩니다. WireGuard 키와 API 상태는 이미지에 넣지 않고 첫 시작 시 영속 볼륨에 생성합니다. 설치 압축파일은 검토한 공개 소스로 빌드하며 SHA256 검증값을 API의 개인 환경 설정에 기록합니다.

```sh
export PERSONAL_GATEWAY_ENDPOINT=10.113.0.5:51820
export PERSONAL_PUBLIC_URL=http://10.113.0.5:4183
export PERSONAL_ARTIFACT_BASE_URL=http://10.113.0.5:4184
export RAILSHOT_PERSONAL_TEST_ALLOW_HTTP=1
export RAILSHOT_LISTEN_ADDRESS=0.0.0.0
export RAILSHOT_ALLOWED_HOSTS=127.0.0.1,localhost,10.113.0.5

docker compose -f deployment/manifests/personal/compose.yaml build
docker compose -f deployment/manifests/personal/compose.yaml up -d
docker compose -f deployment/manifests/personal/compose.yaml ps
curl --fail http://127.0.0.1:4183/healthz
```

포트는 API의 TCP 4183, 공개 설치 코드의 TCP 4184, WireGuard의 UDP 51820입니다. 컨테이너 내부 API 포트는 4173입니다. 기존 localhost:4173 미리보기와 충돌하지 않으며, 이를 교체할 때는 기존 프로세스를 먼저 정리하고 `RAILSHOT_API_PORT=4173`으로 실행합니다. `PERSONAL_PUBLIC_URL`을 바꾸면 이미 저장된 설정과 일치하지 않아 시작을 거절하므로 기존 설정 파일의 명시적 변경도 필요합니다.

브라우저는 `http://127.0.0.1:4183` 또는 `http://localhost:4183`에서 접속합니다. 허용한 브라우저 출처는 기본 설정에 4173과 4183의 localhost만 들어 있습니다. 원격 고객 설치기는 브라우저 출처를 보내지 않으며, 등록용 일회성 자격과 이후 클라이언트 자격으로 인증합니다.

## 권한과 영속 상태

컨테이너의 첫 프로세스는 root로 볼륨과 WireGuard만 준비한 뒤 API와 공개 코드 서버를 UID 1000의 `railshot` 계정으로 실행합니다. API의 활성·상속 권한은 제거하고, root 소유의 고정 `railshot-personal-gateway --request …` 명령만 제한된 sudo 규칙으로 허용합니다. `privileged`, 호스트 네트워크, Docker 소켓, systemd 대체 스크립트를 사용하지 않습니다.

컨테이너 권한은 모두 제거한 뒤 아래 항목만 추가합니다.

- `NET_ADMIN`: 해당 컨테이너의 WireGuard, 전용 경로, 전달 차단 규칙 관리
- `SETUID`, `SETGID`: 일반 사용자로 전환하고 고정 sudo 도구 실행
- `CHOWN`, `DAC_OVERRIDE`, `FOWNER`: root 설정과 API 전용 볼륨의 소유권·파일 권한 처리
- `KILL`: Docker의 root 종료 신호 전달 프로세스(tini)가 UID 1000으로 전환된 API에 종료 신호를 전달

일반 사용자 API가 이 권한들을 직접 가지는 것은 아닙니다. sudo 실행을 위해 `no-new-privileges` 설정은 사용하지 않습니다. Linux 커널의 WireGuard 지원이 필요하며, 커널 기능이 없으면 시작에 실패합니다. 별도 사용자 공간 WireGuard 구현이나 넓은 컨테이너 권한으로 자동 우회하지 않습니다.

`KILL`이 빠진 이전 컨테이너는 정지·재시작 시 tini의 신호 전달이 `Operation not permitted`로 실패할 수 있습니다. 권한 변경은 단순 `restart`로 적용되지 않습니다. 기존 환경변수와 같은 Compose 프로젝트를 사용해 `docker compose -f deployment/manifests/personal/compose.yaml up -d --no-build --force-recreate gateway`로 컨테이너를 재생성해야 합니다. 이 권한 수정만 적용할 때는 이미지 재빌드가 필요하지 않으며 영속 볼륨을 삭제하지 않습니다. 이전 컨테이너를 내리는 순간에는 기존 오류가 한 번 더 발생할 수 있으므로, 새 컨테이너의 다음 정지·재시작에서 오류가 사라졌는지 확인합니다.

| 볼륨 | 경로 | 소유자·내용 |
| --- | --- | --- |
| `api-state` | `/var/lib/railshot` | UID 1000, 개인 환경·세션·앱 상태와 API 개인 설정 |
| `gateway-config` | `/etc/railshot-personal-gateway` | root, 게이트웨이 키와 고정 설정 |
| `gateway-state` | `/var/lib/railshot-personal-gateway` | root, 연결 주소 할당·폐기 기록 |
| `wireguard-config` | `/etc/wireguard` | root, 소유 표식이 있는 인터페이스 설정 |

기존 API 상태를 가져올 때는 원본을 보존한 일관된 복사본만 `api-state`에 넣고, 파일과 디렉터리를 UID/GID 1000으로 설정합니다. 실행 중인 SQLite 파일만 복사하면 최신 기록이나 쓰기 기록이 빠질 수 있으므로 기존 서버를 정지한 뒤 복사하거나 데이터베이스의 정식 백업 기능을 사용해야 합니다. 초기화 도구는 기존 상태를 덮어쓰거나 재귀적으로 소유권을 변경하지 않습니다. 저장된 개인 설정과 요청한 엔드포인트·배포본 해시가 다르면 운영자가 확인하도록 시작을 거절합니다.

## 재시작과 실제 상태 확인

게이트웨이 설정의 `lifecycle`은 기본적으로 `systemd`이며 기존 Ubuntu 배치를 유지합니다. 이 컨테이너는 `direct`를 명시합니다. 직접 실행 모드는 인터페이스가 없을 때 `wg-quick up`을 실행하고, 이후 `wg syncconf`로 연결 상대를 적용합니다. 시작 시 root 전용 `--restore`가 저장된 등록 및 폐기 기록을 읽어 복원합니다. 이 복원 옵션은 API의 sudo wrapper에 노출하지 않습니다.

`Table = off`는 매번 복원 도구가 실행되는 `direct` 설정에만 들어갑니다. 기본 `systemd` 설정은 WireGuard의 자동 경로 생성을 유지하여, 호스트 재부팅 시 `wg-quick` 서비스만 시작되어도 등록된 고객 `/32` 경로가 복원됩니다.

기존 인터페이스는 WireGuard 종류, root 소유 설정의 표식, 실제 공개키가 모두 일치해야만 사용합니다. 고객 주소별 `/32` 경로를 적용하며 다른 인터페이스의 경로를 인수하지 않습니다. 인터페이스를 통한 양방향 전달 차단 규칙이 기존 허용 규칙보다 앞에 있는지도 확인합니다. 등록·제거·복원 후 실제 공개키·포트·주소·연결 상대·경로·전달 차단 상태를 다시 조회합니다. 오래된 연결 확인 시각만으로 연결되었다고 보고하지 않습니다.

다음 명령은 개인키를 출력하지 않습니다.

```sh
docker compose -f deployment/manifests/personal/compose.yaml top gateway
docker compose -f deployment/manifests/personal/compose.yaml exec gateway \
  sh -c 'wg show railshotwg public-key; wg show railshotwg allowed-ips; wg show railshotwg latest-handshakes'
docker compose -f deployment/manifests/personal/compose.yaml exec gateway \
  ip -j -4 route show dev railshotwg
docker compose -f deployment/manifests/personal/compose.yaml exec gateway \
  iptables -w -S FORWARD
docker compose -f deployment/manifests/personal/compose.yaml exec gateway \
  iptables -w -t mangle -S FORWARD
docker compose -f deployment/manifests/personal/compose.yaml exec gateway \
  python3 /opt/railshot/deployment/scripts/personal_wireguard.py \
    --config /etc/railshot-personal-gateway/config.json --verify-forwarding
docker compose -f deployment/manifests/personal/compose.yaml restart gateway
```

재시작 후 같은 공개키와 고객 주소가 유지되고 제거한 연결이 복원되지 않는지 확인합니다. OpenStack 제어 연결 확인은 API가 고객 터널을 통해 실제 OpenStack 조회를 실행하고 `connection_status=ready`를 반환하는 것입니다. 최종 `status=ready`는 별도 VM의 K3s 건강·제한된 배포 권한·동적 앱 연결까지 검증해야 합니다. WireGuard 자체 연결 시험과 OpenStack 자격·명령 실행 시험은 구분해서 기록합니다.

이번 실제 연결 시험에서는 서버 컨테이너만 재시작한 뒤 고객 서비스를 재시작하지 않아도 약 2분 후 새 WireGuard 연결 확인 시각이 기록되고 API 상태가 자동 복원되었습니다. 재협상과 후속 상태 확인을 포함해 약 2~3분이 걸릴 수 있다는 관측이며, 원인이나 모든 환경의 복구 시간을 확정한 것은 아닙니다. 재시작 직후 연결 상대 주소가 비어 있거나 연결 확인 시각이 0인 것만으로 영구 장애를 판단하지 않습니다. 반대로 이전 `ready` 표시나 경과 시간만으로 복구를 인정하지 않고, 새 연결 확인 시각·전송량과 재시작 이후의 OpenStack 조회 결과를 확인합니다.

`docker compose down`은 컨테이너만 정리하며 영속 기록은 남습니다. `down -v`는 키와 소유권 기록까지 삭제하므로 일반 재시작 절차로 사용하지 않습니다.

## 자동 검사 범위

`deployment/scripts/tests/test_personal_gateway.py`는 실제 명령과 같은 형태의 입력·출력을 모의 실행하여 직접 실행, 재시작 복원, 소유권 충돌, 다른 경로 보존, 전달 규칙 순서와 상태 변경 탐지를 검사합니다. 이 검사의 성공은 Docker 커널·VPN·고객 OpenStack의 실제 연결 성공을 의미하지 않습니다. 실제 시험 결과는 별도 인수 기록으로 남겨야 합니다.

## 로컬 중앙 제어면

`control.compose.yaml`은 기존 `railshot-personal-local` 게이트웨이의 네트워크 공간 안에서 native arm64 K3s와 Argo CD를 실행합니다. K3s API는 호스트 포트로 공개하지 않습니다. Argo application-controller와 로컬 자격 갱신 Job은 `hostNetwork`를 사용하여 게이트웨이의 로컬 출력 경로를 공유합니다. WireGuard 인터페이스를 통과하는 컨테이너 간 전달은 mangle 및 filter FORWARD 차단 규칙으로 계속 거절합니다.

게이트웨이 컨테이너를 재생성하면 `network_mode: container:railshot-personal-local` 연결도 끊기므로 control, bootstrap, token-minter를 함께 재생성해야 합니다. K3s나 kube-proxy를 시작한 직후에는 bootstrap이 공식 `--restore`를 실행하고 `--verify-forwarding`으로 mangle/filter 차단을 다시 읽습니다. token-minter도 30초마다 같은 복원을 반복합니다. 전달 허용 규칙을 추가해 우회하지 않습니다.

이미지 명령이 저장소 밖의 새 비공개 설정 디렉터리를 0700으로 생성합니다. 명령이 끝난 뒤 그 안에 `credentials-policy.json`을 두고, 별도 GitHub 토큰 원본은 0600으로 준비합니다. 초기 기반 검사만 할 때 정책은 아래 빈 값일 수 있습니다. 이는 로컬 arm64 이미지로 실제 갱신 CronJob을 한 번 실행해 이미지·서비스 계정·권한 경계를 확인하지만, 고객 자격 갱신이나 전체 배포 준비 완료를 의미하지 않습니다.

```json
{"version":1,"targets":[]}
```

먼저 새 API 이미지를 현재 소스에서 arm64 OCI 아카이브로 빌드하고, 소스 SHA256·아카이브 SHA256·manifest digest·image ID를 검증한 뒤 K3s containerd로 가져옵니다. 출력 디렉터리는 저장소 밖의 비어 있는 새 경로여야 합니다.

```sh
python3 deployment/manifests/personal/control-bootstrap.py image \
  --output "$RAILSHOT_LOCAL_CONTROL_CONFIG_DIR" \
  --control-container railshot-personal-control
```

그 다음 아래 변수를 설정하여 선언을 검사합니다. `docker compose config`가 기존 외부 볼륨 이름과 비공개 경로를 먼저 확인하며 토큰 내용은 출력하지 않습니다.

```sh
export RAILSHOT_LOCAL_CONTROL_CONFIG_DIR=/absolute/private/railshot-control
export RAILSHOT_GITHUB_TOKEN_PATH=/absolute/private/github-token
export RAILSHOT_GITHUB_USER=callme-waffle

docker compose -f deployment/manifests/personal/control.compose.yaml config --quiet
docker compose -f deployment/manifests/personal/control.compose.yaml up -d control
# 위 image 명령으로 OCI 이미지를 가져온 뒤 실행합니다.
docker compose -f deployment/manifests/personal/control.compose.yaml up --no-deps bootstrap
docker compose -f deployment/manifests/personal/control.compose.yaml up -d --no-deps token-minter
```

bootstrap 성공 영수증은 `railshot-personal-control-bootstrap-state` 볼륨에, 한 시간 제한 서비스 계정 토큰과 GitHub 토큰 사본은 `railshot-personal-control-auth` 볼륨에 기록합니다. 관리자 K3s 설정은 control-config에만 남으며 게이트웨이에 마운트하지 않습니다. 게이트웨이는 auth 볼륨을 `/run/railshot-kubernetes`에 읽기 전용으로 마운트하고 다음 경로만 사용해야 합니다.

```text
KUBECONFIG=/run/railshot-kubernetes/kubeconfig
GITHUB_TOKEN_FILE=/run/railshot-kubernetes/github-token
RAILSHOT_TARGET_ID=local-control
```

GitHub owner/repository/ref/workflow/tenant 값도 검토한 게이트웨이 환경에 명시합니다. Cloudflare 경로, 고객 runtime 등록, 실제 `credentials-policy.json` 대상이 없으면 readiness는 차단 상태를 유지해야 합니다. 공개 터널 자격이나 GHCR 자격이 없다는 이유로 로컬 이미지 기반 Argo·갱신 기반 검사까지 생략하지 않습니다.

다음 확인은 비밀을 출력하지 않습니다.

```sh
docker exec railshot-personal-local iptables -w -t mangle -S FORWARD
docker exec railshot-personal-local python3 \
  /opt/railshot/deployment/scripts/personal_wireguard.py \
  --config /etc/railshot-personal-gateway/config.json --verify-forwarding
docker exec railshot-personal-control kubectl get nodes -o wide
docker exec railshot-personal-control kubectl -n argocd get \
  statefulset/argocd-application-controller cronjob/railshot-credentials
```

## 중앙 배포 연결 설정

OpenStack 연결까지만 시험할 때는 `PERSONAL_RUNTIME_CONFIG_FILE`을 비워 둡니다. 이 경우 실행환경 준비는 `RUNTIME_OPERATOR_NOT_CONFIGURED`로 차단되며 배포 가능으로 표시하지 않습니다.

배포 연결까지 준비하려면 UID/GID 1000 소유·0600인 공통 설정을 `/var/lib/railshot/config/personal-runtime.json` 등에 두고 `PERSONAL_RUNTIME_CONFIG_FILE=/var/lib/railshot/config/personal-runtime.json`을 지정합니다. 내용과 중앙 Argo·CI·이미지/Git·공개 경로 전제는 [개인 환경 호출 규격](../../../docs/api/personal-environments.md)을 따릅니다. 설정이 참조하는 자격 파일·kubeconfig도 컨테이너 안의 절대 경로여야 하고 해당 UID가 읽을 수 있어야 합니다. API의 `KUBECONFIG`, `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_REF`, `RAILSHOT_TENANT`, 기존 CI의 `RAILSHOT_TARGET_ID` 등은 비공개 Compose 추가 설정 파일의 `environment`와 명시적 읽기 전용 자격 마운트로 전달합니다. GitHub 토큰은 셸 명령·공개 Compose·로그에 기록하지 않습니다. `RAILSHOT_TARGET_ID`는 기존 CI 서비스 시작 설정이며 새 개인 환경의 소유권을 대신하는 ID가 아닙니다.

초기화 도구는 `PERSONAL_RUNTIME_CONFIG_FILE`이 지정되면 해당 파일의 소유권·권한을 확인하고 `personal.json`의 `runtime.config_path`에 반영할 예상값을 만듭니다. 기존 `personal.json`은 자동 수정하지 않습니다. 기존 볼륨에 이 설정을 추가할 때는 API를 정지하고 비공개 `personal.json`에 동일한 `runtime` 항목만 명시적으로 추가한 뒤 권한 0600과 UID 1000을 보존하여 다시 시작합니다. 경로·해시·엔드포인트가 예상값과 다르면 기존처럼 시작을 거절합니다. 실행 중 API 상태나 게이트웨이 기록은 변경하지 않습니다.
