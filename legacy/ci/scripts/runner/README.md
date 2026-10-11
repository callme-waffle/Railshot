# 전용 빌드 노드의 GitHub Actions runner 컨테이너

runner는 **Ubuntu 24.04 amd64 전용 빌드 노드**에서 앱 저장소의 `railshot-deploy.yml` job을 한 번 실행합니다. 목표 배치는 운영 K3s의 build agent 위 [일회성 Job](../../../deployment/manifests/build-runner.yaml)이며, 기존 독립 VM은 `ci/runner-compose.yml`을 유지합니다. 일반 운영 노드나 고객 runtime에는 설치하지 않습니다. GitHub 공식 `actions/actions-runner:2.337.0` 이미지를 digest로 고정하고 Python·호스트 방화벽 확인 도구만 추가합니다. workflow의 `setup-python`이 Python 3.13을 설치하며 사용자 코드의 테스트는 기존 제한 컨테이너에서 실행합니다. 이미지 게시 job은 GitHub-hosted runner에 남깁니다. 아래 설정은 실제 노드 가입·고객 job 검증을 대신하지 않습니다.

이 컨테이너는 신뢰된 CI 실행기를 포장합니다. **전용 VM이 기존 root 신뢰 경계**입니다. 같은 VM의 Docker socket과 host network, Docker 기본 capability에 추가한 `NET_ADMIN`이 필요합니다. socket은 CI VM의 root 권한에 해당하며 운영 호스트 socket과 공유할 수 없습니다. `NET_ADMIN`은 기존 root-owned helper의 실제 nftables 정책 검증에, host network는 격리된 L3 컨테이너 IP의 HTTP 검사에 필요합니다. runner는 `privileged: false`, host PID 비공유, `SYS_ADMIN` 미부여, 권한 상승 금지를 유지합니다. Codex의 읽기 제한 sandbox가 만드는 user/mount/PID/network/IPC namespace만 허용하는 전용 seccomp·AppArmor 프로필을 사용합니다. 사용자 코드에는 socket·runner 인증·운영 자격을 전달하지 않고 기존의 비root/읽기전용/자원제한 Q 컨테이너와 제한된 BuildKit/L3 네트워크를 유지합니다.

BuildKit은 새로 만들지 않습니다. 먼저 `infrastructure/ansible/ci.yml`을 실행한 기존 bootstrap이 `railshot-buildkit` 컨테이너와 `railshot-quality` bridge/firewall을 준비하고 실제 네트워크 검증 receipt를 남겨야 합니다. runner는 자신만의 Docker config에 remote Buildx 연결 정보만 만들고 같은 gate 코드로 기존 BuildKit의 이미지·network·자원제한·실행 flags를 확인합니다. bootstrap의 기존 BuildKit 권한 설정은 이 패키징에서 변경하지 않습니다.

## 운영 K3s의 build agent 준비

기존 `railshot-build-worker-aws-01`을 전용 agent로 사용합니다. 운영 서버와 고객 K3s를 재설치하지 않습니다. 먼저 운영자가 기존 STOP 기한과 사설 통신 경로를 확인해야 합니다. 현재 CI Terraform의 보안 그룹은 Kubernetes 연결을 허용하지 않으므로 **그대로 시작하는 것만으로 가입되지 않습니다.** 현재 `control.sh` 소스는 K3s `v1.34.11+k3s1`, Cilium `1.20.2`/CLI `v0.20.1`, VXLAN, Pod CIDR `10.52.0.0/16`, Service CIDR `10.53.0.0/16`을 사용합니다. 기존 운영 서버의 Flannel 전환은 자동으로 수행하지 않으며, [운영 Cilium 전환 절차](../../../docs/operations/control-cilium-migration.md)와 실제 CNI 상태를 먼저 확인합니다. SG와 호스트 방화벽을 실제 버전에 맞추고 CI Docker `172.30.0.0/24`와 CIDR이 겹치지 않게 합니다.

| 경로 | 필요한 사설 통신 |
|---|---|
| build agent → 운영 server | K3s API/supervisor TCP 6443 |
| 운영 cluster 노드 사이 | 목표 Cilium VXLAN의 UDP 8472; 인터넷 공개 금지 |
| 운영 cluster 노드 사이 | 필요한 kubelet TCP 10250; 승인된 노드/관리 경로로 한정 |
| 운영 cluster 노드 사이 | Cilium health 검사를 위한 TCP 4240 또는 ICMP echo; 검토한 사설 노드 사이에서만 허용 |
| 고객 코드 컨테이너 | 기존 CI bridge 정책 유지. 운영 API·메타데이터·사설 DB 접근 차단 |

통신 근거는 [K3s 요구사항](https://docs.k3s.io/installation/requirements)과 [Cilium 방화벽 요구사항](https://docs.cilium.io/en/stable/operations/system_requirements/#firewall-rules)을 따른다. Cilium agent/Envoy 같은 필수 DaemonSet이 build taint를 허용하고 새 노드에서도 정상 기동하는지 확인합니다. 일반 제품 workload에는 build toleration을 추가하지 않습니다.

운영 서버와 같은 검토된 K3s binary/installer를 사용합니다. build 노드의 root 소유 `0600` `/etc/rancher/k3s/config.yaml`은 다음 입력으로 준비합니다. `server`와 별도 `token-file`은 실제 운영 cluster 값이며 join token을 문서·Git·Pod에 넣지 않습니다. 다른 config drop-in은 허용하지 않습니다.

```yaml
server: https://REPLACE_WITH_OPS_PRIVATE_ADDRESS:6443
token-file: /etc/rancher/k3s/agent-token
node-name: railshot-build-worker-aws-01
node-label:
  - railshot.io/node-role=build
node-taint:
  - railshot.io/dedicated=build:NoSchedule
```

`k3s agent`로 가입하며 기본 containerd를 유지합니다. `--docker`로 K3s와 CI Docker를 합치지 않습니다. 운영자 context에서 해당 Node의 Ready·hostname·build label·taint와 기존 서비스/DNS가 정상인지 확인한 후, **build 노드에서만** 실행합니다.

```sh
sudo ansible-playbook -i localhost, -c local infrastructure/ansible/ci.yml \
  -e '{"railshot_ci_k3s_build_worker":true}'
sudo bash ci/scripts/runner/prepare-host.sh --k3s-build-worker
```

bootstrap은 일반 Kubernetes/control-plane을 계속 거부합니다. 위 명시적 프로필도 agent 서비스·root 소유 config의 전용 label/taint·server 부재를 확인합니다. 기존 Docker/방화벽 설정과 native probe는 유지하므로 Docker 재시작 및 CNI 변경 후에도 운영 통신과 고객 코드 격리를 함께 재검증합니다. CI hook 앞에 CNI 규칙이 생기면 receipt는 보수적으로 거부됩니다. 실제 공존 시험 전에는 이 경로를 운영 검증 완료로 취급하지 않습니다.

운영자 context에서 `railshot-build` namespace와 이미지 pull Secret `ghcr-pull`, 새 runner 등록 토큰 Secret `railshot-build-runner-registration`의 `token` 키를 준비합니다. namespace는 이 검토된 hostNetwork/hostPath Job에 맞는 Pod Security 정책이 필요합니다. 일반 팀 workload를 이 namespace에 생성하도록 권한을 주지 않습니다. 검토된 이미지 digest JSON으로 다음을 렌더하고 별도로 적용합니다. 기본 platform Argo Application에는 이 Job/RBAC를 넣지 않습니다.

```sh
python3 deployment/scripts/render-platform.py /private/images.json \
  --build-runner-name railshot-build-attempt01 \
  --runner-url https://github.com/Jasmin-Softbank/railshot-apps \
  --build-node railshot-build-worker-aws-01 > /private/build-runner.json
kubectl --context "$OPS_CONTEXT" apply -f /private/build-runner.json
```

Job은 정확한 build 노드에만 배치되며 다른 노드로 fallback하지 않습니다. 전용 ServiceAccount는 build namespace의 Pod 조회와 지정된 Node 한 개 조회만 허용합니다. Secret 조회·배포 변경 권한은 없습니다. runner는 인증된 API 응답으로 자기 Pod의 UID·권한·마운트와 Node의 역할·taint를 확인합니다. K3s 자격 디렉터리와 관리자 kubeconfig는 마운트하지 않습니다. 이 metadata token도 고객 코드 컨테이너에는 전달하지 않습니다.

Job의 제한은 runner에 적용됩니다. 호스트 Docker가 실행하는 BuildKit/Q/L3는 기존 Docker 제한을 사용하므로 Kubernetes quota에 자동 합산되지 않습니다. runner별 checkout/temp 경로와 lock을 분리합니다. 동일 노드의 무거운 gate와 공유 Codex 호출은 root 소유 executor profile의 `concurrency.gate`/`concurrency.agent` 슬롯으로 제한합니다. 이 설계는 운영 Pod와 빌드의 노드를 나누지만, privileged BuildKit과 운영 cluster의 control plane 공유에 따른 위험까지 제거하지는 않습니다. 다음 job에는 새로운 등록 token·Job 이름을 사용합니다. 연속 제품 요청에는 아래의 작은 CronJob controller가 이를 보충합니다.

### 연속 요청을 위한 runner 보충

[replenish.py](replenish.py)는 기존 API 이미지의 Python 표준 라이브러리만 사용합니다. [CronJob](../../../deployment/manifests/build-controller.yaml)은 platform 노드에서 매분 실행하며 `concurrencyPolicy: Forbid`, 재시도 0, 55초 deadline을 갖습니다. HTTP는 요청당 최대 5초·전체 45초로 제한합니다. 별도 daemon, 이미지, DB, controller용 PVC는 필요 없습니다.

운영자는 `railshot-system`의 `railshot-runner-controller-github` Secret `token` 키에 해당 앱 저장소의 Actions 조회·runner 등록 권한을 가진 자격을 준비합니다. 이 자격은 controller Pod에만 마운트합니다. `railshot-build`의 `railshot-build-runner-registration` Secret도 운영자가 미리 만들며 `token` 키의 최초 값은 빈 문자열이어도 됩니다. controller는 이 이름의 Secret만 조회·수정할 수 있고 Secret 생성·목록 조회 권한은 없습니다. 앱 저장소의 세부 권한은 GitHub 공식 [job 조회](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run)와 [registration token 발급](https://docs.github.com/en/rest/actions/self-hosted-runners#create-a-registration-token-for-a-repository) 계약을 따릅니다. 등록 token은 runner에만 전달되고 장기 GitHub 자격은 전달하지 않습니다.

```sh
python3 deployment/scripts/render-platform.py /private/images.json \
  --build-controller \
  --runner-url https://github.com/Jasmin-Softbank/railshot-apps \
  --build-node railshot-build-worker-aws-01 > /private/build-controller.json
kubectl --context "$OPS_CONTEXT" apply -f /private/build-controller.json
```

이 모드는 검토한 `api`와 `ci-runner` GHCR digest를 요구합니다. 기존 `render_build_runner`가 만든 Job을 ConfigMap에 보관하고 SHA-256·저장소·노드·이미지·권한을 검증합니다. 실제 생성 시 바뀌는 값은 고유 Job/runner 이름뿐이며, runner ServiceAccount와 기존의 좁은 Pod/Node 조회 권한은 그대로 사용합니다. controller ServiceAccount에는 build namespace의 Job get/list/create와 등록 Secret 한 개의 get/update/patch만 부여합니다. 기본 platform Argo Application과 별도로 설치합니다.

각 tick은 다음 순서로 동작합니다.

1. 기존 runner Job을 조회하고 `runner_count` 범위의 빈 슬롯을 계산합니다. 이전 형식의 runner가 실행 중이면 먼저 완료를 기다립니다.
2. 슬롯별 생성 intent를 실제 Job으로 확인합니다. 연속 세 Job이 실패한 슬롯은 보충을 중단합니다.
3. 앱 저장소의 queued/in_progress run과 필요한 runner label을 확인합니다. 조회는 상태별 최대 4페이지, 전체 45초 안으로 제한합니다.
4. 필요한 슬롯마다 등록 Secret annotation에 pending 이름을 먼저 저장합니다. `resourceVersion` 경합이 있으면 생성하지 않습니다. 검증한 Job에 고유 runner 이름과 슬롯 annotation을 넣어 한 번 생성합니다.

생성 응답이 유실되면 해당 슬롯의 `unknown` intent를 보존하고 같은 작업을 재생성하지 않습니다. 다른 빈 슬롯은 계속 처리합니다. 슬롯 축소 시 남아 있는 intent를 묵시적으로 버리지 않습니다. 운영자가 CronJob을 suspend하고 해당 Job·Pod·등록 상태를 확인한 뒤 조정해야 합니다.

`--runner-count 12`로 동일한 build 노드의 슬롯 수를 지정할 수 있습니다. 기본값은 1입니다. 지속 배포에서는 private worker config의 `runner_count`에도 같은 값을 넣어야 합니다. [기존 호스트 병렬 CI 운영 규약](../../../docs/operations/parallel-ci-capacity.md)을 따릅니다.

로컬 테스트는 실제 loopback HTTP 요청으로 연속 두 queued 요청, 실행 중 Job, 생성 응답 유실, Secret 충돌, 세 연속 실패, template/registry/권한 거부를 확인합니다. 운영 GitHub 등록·Kubernetes 설치·고객 빌드의 실제 성공은 별도 E2E 검증이 필요합니다.

```sh
python3 -m unittest discover -s ci/scripts/runner -p test_replenish.py -v
```

## 기존 독립 VM의 Compose 실행

다음은 bootstrap을 마친 전용 CI VM의 저장소 루트에서 수행합니다. 호스트 준비 명령은 root가 필요하며 Kubernetes 설치 흔적을 발견하면 중단합니다. Docker Compose v2는 호스트에 별도로 설치되어 있어야 합니다.

```sh
sudo bash ci/scripts/runner/prepare-host.sh
docker build -f ci/scripts/runner/Dockerfile -t railshot-ci-runner:local .
docker run --rm railshot-ci-runner:local --check-image
```

운영자는 GitHub의 해당 **비공개 앱 저장소** runner 설정에서 짧은 유효기간의 registration token을 발급받아 root 소유·0600 일반 파일에 저장합니다. PAT·GitHub App private key를 대신 넣지 않습니다. 토큰은 저장소나 `.env`에 넣지 않고 shell 인수·로그에 출력하지 않습니다. 다음 환경 변수에는 파일 경로만 넣습니다. Compose 명령은 해당 파일과 Docker에 접근할 수 있는 운영자/root shell에서 실행합니다.

```sh
export RAILSHOT_RUNNER_TOKEN_FILE=/private/github-runner-registration-token
export RAILSHOT_RUNNER_URL=https://github.com/YOUR_ORG/YOUR_PRIVATE_APPS_REPO
export RAILSHOT_RUNNER_NAME=railshot-ci-vm01-attempt01
export RAILSHOT_RUNNER_LABELS=railshot-ci
export RAILSHOT_CI_RUNNER_IMAGE=railshot-ci-runner:local
docker compose -f ci/runner-compose.yml config --quiet
docker compose -f ci/runner-compose.yml up --no-build --force-recreate
```

게시된 이미지를 사용할 때는 `RAILSHOT_CI_RUNNER_IMAGE`에 검토한 digest를 지정하고 `docker compose -f ci/runner-compose.yml pull`을 먼저 실행합니다. 위의 로컬 build 명령은 생략할 수 있습니다. 이미지 빌드 성공과 `--check-image`는 runner 의존성 검사이며 GitHub 등록·실제 CI 실행·격리 검증 완료를 뜻하지 않습니다.

GitHub 앱 저장소에는 기존 workflow의 변수도 설정합니다.

| 저장소 변수 | 값/역할 |
| --- | --- |
| `RAILSHOT_CI_RUNNER_LABELS` | `["self-hosted","Linux","X64","railshot-ci"]`; runner의 추가 label과 일치 |
| `RAILSHOT_RUN_ROOT` | `/var/lib/railshot-runner/runs`; `_work`·`RUNNER_TEMP`와 분리된 재시도 상태 |
| `QUALITY_NETWORK` | `railshot-quality` |
| `PLATFORM_REF` | 설치한 executor 계약과 같은 검토된 platform commit 40자리 SHA |
| `RAILSHOT_MAX_REPAIR_ATTEMPTS` | fixer 호출 상한 0·1·2, 기본 2. 0은 패키징 옵션과 관계없이 모든 SDK 호출을 끄며 모델 인증·SDK 설치 없이 결정적 baseline을 실행한다. fixer 재계획도 이 상한에 포함한다. 게이트 실패를 성공으로 바꾸거나 검사를 생략하지 않는다. |
| `RAILSHOT_MAX_PACKAGING_ATTEMPTS` | 초기 adapter 호출 상한 0·1·2, 기본 2. 기본 전체 상한은 패키징 2회 + 수정 2회 = 4회이며 역할 간 횟수는 빌려 쓰지 않는다. 명세가 이미 있으면 fixer 최대 2회다. 0은 모델 패키징을 끄며 규칙 기반 패키징은 유지한다. |
| `AGENT_PROVIDER`, `AGENT_AUTH_MODE` | 기존 workflow 계약의 provider와 `subscription` 또는 `api-key` |
| `RAILSHOT_CODEX_HOME` | subscription일 때 `/var/lib/railshot-runner/codex`; 운영자가 해당 전용 디렉터리에 `auth.json` 준비 |

API key 모드는 기존 workflow의 GitHub Actions secret을 사용합니다. Codex subscription 디렉터리는 토큰 갱신을 위해 runner에만 쓰기 가능하게 연결되며 작업 workspace와 분리됩니다. API key 모드에서는 해당 디렉터리를 비워둡니다. AWS/GCP/Azure/OpenStack 자격, Docker registry login 설정이나 운영 KUBECONFIG는 이 VM/컨테이너에 전달하지 않습니다. CI VM instance role은 기존 bootstrap/관리용으로 제한하고 운영 cloud 권한을 부여하지 않습니다.

## 호스트 경로와 실패 조건

workspace(`/var/lib/railshot-runner/work`), run root(`/var/lib/railshot-runner/runs`), `TMPDIR`(`/var/lib/railshot-runner/work/_temp`)는 **컨테이너와 호스트의 절대경로가 같아야** 합니다. gate는 source와 L4의 임시 image archive를 호스트 Docker에 bind mount하므로 경로가 다르면 실행할 수 없습니다. Compose는 경로를 고정하고 시작 전 실제 `docker inspect`로 mount source/destination과 읽기·쓰기 모드를 검사합니다. `prepare-host.sh`가 만든 root-owned 전용 CI 마커, 호스트의 K3s 디렉터리, helper·executor 계약·실제 네트워크 검증 receipt도 확인합니다. 허용 목록 밖의 mount, 변경된 권한·네트워크·추가 capability는 등록 전에 거부합니다.

helper·executor 계약·receipt는 원래 절대경로에 읽기전용으로 연결합니다. helper의 lock 파일만 같은 경로에 쓰기 가능하게 공유하여 호스트 정책 변경과 검사가 동일한 lock을 사용합니다. Docker 재시작 후 receipt가 무효화됐으면 호스트의 기존 `railshot-ci-verify.service`/`test-ci-network`가 성공해야 합니다. helper나 이미지 계약을 갱신했으면 runner 컨테이너도 다시 생성합니다. 정책 실패를 우회해 runner를 등록하지 않습니다.

runner는 `--ephemeral`로 **한 job 뒤 등록을 해제하고 종료**하며 Compose의 restart는 `no`입니다. 다음 job은 새 registration token과 고유 runner name을 준비한 뒤 `up --force-recreate`로 새 컨테이너를 실행합니다. 자동 재등록용 PAT나 무한 재시작은 넣지 않습니다. 한 VM에서 이 Compose runner는 하나만 실행합니다. job 전에 중단된 runner는 GitHub 설정에서 잔여 등록을 확인해 제거한 뒤 다시 등록합니다. `work`와 `runs`는 호스트에 남아 재시도 증거를 보존하므로 필요 없는 실행 자료 삭제는 운영자가 별도로 관리합니다. `--disableupdate`를 사용하므로 GitHub가 요구하는 runner 업데이트 기한에 맞춰 base digest를 갱신하고 이미지를 다시 배포해야 합니다.

로컬 최소 검사:

```sh
python3 -m unittest discover -s ci/scripts/runner -p test_container.py
bash -n ci/scripts/runner/entrypoint.sh ci/scripts/runner/prepare-host.sh
```

실제 완료 판정에는 전용 Linux VM에서 bootstrap 네트워크 검증, ephemeral GitHub job, SDK의 native 읽기 제한, Q/L2/L4/L3를 통과한 evidence가 필요합니다. 실제 runner Pod에서 SDK sandbox 사전검사가 실패하면 모델을 호출하기 전에 `SDK_SANDBOX_UNAVAILABLE`로 중단합니다. 동일 정책으로 모델 호출을 반복하지 않으며, 검토된 설정을 적용한 새 Pod에서 사전검사가 통과한 뒤 원래 입력을 다시 실행합니다. Argo CD와 K3s 고객 runtime은 기존 `gitops/` 및 `deployment/` 구성으로 관리합니다.

공식 근거: [Actions runner 2.337.0 Dockerfile](https://github.com/actions/runner/blob/v2.337.0/images/Dockerfile), [ephemeral runner와 업데이트 운영](https://docs.github.com/en/actions/reference/runners/self-hosted-runners).

## Codex namespace 충돌과 실행 전 검사

`RuntimeDefault`의 namespace 생성 차단은 Codex 0.159.3의 파일 읽기 제한과 충돌합니다. 전용 빌드 노드에서만 `prepare-host.sh`가 root 소유 프로필을 설치합니다. Kubernetes는 `Localhost` 프로필을 사용하고 Compose는 같은 seccomp 파일과 AppArmor 이름을 사용합니다. 이미지와 host 프로필을 함께 갱신하고, 기존 Job이 종료된 뒤 controller template을 새 이미지로 교체합니다. 프로필이 없으면 시작을 거부합니다.

seccomp는 containerd의 실행 중 amd64 기본 정책에서 기본 거부와 기존 syscall 규칙을 보존합니다. 추가 허용은 Codex bwrap의 정확한 `clone` flags `0x78020011`, `mount`, `pivot_root`, `umount2(MNT_DETACH)`입니다. `unshare`, `setns`, `SYS_ADMIN`, privileged, host PID는 추가하지 않습니다. mount는 호스트 namespace에서 여전히 kernel capability 검사로 거부되고, AppArmor는 bwrap의 `/tmp`, `/newroot`, `/oldroot`와 root 전환 경로로 대상을 제한합니다. 기존 `/proc`·`/sys` 보호도 유지합니다. 고객 코드 Q/L3 컨테이너의 정책은 변경하지 않습니다.

SDK 실행 직전에는 같은 프로세스 환경과 permission profile로 허용된 읽기, 금지된 읽기·쓰기·네트워크를 실제 검사합니다. SDK turn이 완료됐더라도 내부 명령이 sandbox 생성 오류로 실패하면 인프라 오류로 기록하고 파일 제안을 적용하지 않습니다. `agents/DONT.md`는 adapter/fixer의 실제 instructions에 합성됩니다.

프로필 원형은 [containerd AppArmor template](https://github.com/containerd/containerd/blob/main/contrib/apparmor/template.go)와 실제 OCI 기본 seccomp입니다. Copyright The Docker Authors, The Moby Authors, The containerd Authors. Apache-2.0 라이선스 사본은 [LICENSE.apache-2.0](LICENSE.apache-2.0)에 있습니다. RAILSHOT 변경은 위에 기술한 namespace·mount 범위에 한정합니다. [Codex 0.159.3의 filesystem sandbox](https://github.com/openai/codex/blob/rust-v0.159.3/codex-rs/linux-sandbox/README.md)는 읽기 거부 정책에서 bubblewrap을 요구하므로 legacy Landlock나 sandbox 비활성화로 대체하지 않습니다.
