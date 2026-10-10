# RailShot Deployment Runtime

이 영역은 **준비된 Linux 노드에서 K3s·Cilium 기반 공통 실행 환경을 구성하고, 앱 배포부터 HTTP 상태 확인까지 수행**합니다. AWS·GCP·On-Prem(OpenStack 계열)의 인프라 생성은 Provider(인프라 제공 환경) 계층이 담당합니다.

현재는 전용 Linux 서버 **한 대의 K3s 구성**을 지원합니다. 기존 EKS·GKE 또는 다른 CNI(컨테이너 네트워크)가 설치된 클러스터를 자동 변환하는 기능은 없습니다. 이미 설치된 K3s는 이 runtime이 만든 동일 설정·버전인 경우 재사용합니다.

## 목차

- [책임과 구조](#책임과-구조)
- [입력과 실행](#입력과-실행)
- [JSON 출력과 상태](#json-출력과-상태)
- [설치와 앱 배포 흐름](#설치와-앱-배포-흐름)
- [검증과 오류 처리](#검증과-오류-처리)
- [제거](#제거)
- [자동 테스트](#자동-테스트)
- [별도 VM에서 Provider 모의 통합 검사](#별도-vm에서-provider-모의-통합-검사)
- [네트워크 검사와 Online/Offline 배포](#네트워크-검사와-onlineoffline-배포)
- [Release artifact 배포](#release-artifact-배포)
- [Provider 및 Ansible 연결](#provider-및-ansible-연결)
- [확정이 필요한 계약과 후속 모듈](#확정이-필요한-계약과-후속-모듈)

## 책임과 구조

```text
deployment/
├── airgap/          # 로컬 bundle 준비·검증·이미지 preload 및 버전 기준입니다.
├── bootstrap/       # Linux 사전 조건·K3s 설치·상태 확인·전체 제거입니다.
├── cilium/          # Cilium 설치·상태 확인·최소 통신 정책 템플릿입니다.
├── cloudflared/     # 향후 외부 접속 터널을 연결할 위치입니다.
├── sealed-secrets/  # 향후 암호화된 비밀정보 관리를 연결할 위치입니다.
├── cnpg/            # 향후 PostgreSQL 운영 도구를 연결할 위치입니다.
├── manifests/       # namespace·Deployment·Service 템플릿입니다.
├── scripts/         # JSON 변환·공통 모델·실행 엔진·CLI·검사입니다.
└── README.md
```

입력과 실행 계층을 다음과 같이 분리했습니다.

```text
JSON Input
    ↓ input_adapter.py — 외부 JSON 검증 및 변환입니다.
DeploymentSpec — provider와 원격 접속 변수에 의존하지 않는 공통 설정입니다.
    ↓ engine.py — Linux/Kubernetes 명령과 상태 검사를 수행합니다.
DeploymentResult — 실행 결과와 실패 단계를 기록합니다.
    ↓ runtime.py — 요청의 provider와 environment_id를 결과에 결합합니다.
JSON Output
```

`models.py`의 `DeploymentSpec`에는 provider 이름, SSH 사용자, Ansible 변수명 또는 REST 경로가 없습니다. provider별 JSON fixture(시험용 입력)는 같은 공통 설정으로 정규화됩니다. 공급자별 차이를 공통 엔진의 분기문으로 처리하지 않습니다.

## 입력과 실행

### 실행 전제조건

- 대상 Linux 노드 안에서 Python 3.10 이상과 root(관리자) 권한으로 실행합니다. SSH 연결 자체는 상위 계층이 수행합니다.
- Ubuntu 22.04/24.04 또는 Debian 12/13, systemd(서비스 관리 기능), amd64/arm64, Linux 커널 5.10 이상, swap(디스크를 메모리처럼 쓰는 기능) 비활성화가 필요합니다. **실제 OS 검증은 Ubuntu 24.04 arm64에서 수행했습니다.**
- 권장 사양은 CPU 2개, RAM 4 GiB, 여유 디스크 10 GiB 이상입니다. 기존의 관리하지 않는 K3s/CNI가 없는 전용 노드를 사용합니다.
- `bash`, `curl`, `tar`, `sha256sum`, `systemctl`, `ip`, `flock`, `sysctl`, `cmp`, `awk`, `grep` 등 기본 명령이 필요합니다. 최소 이미지에서는 다음과 같이 보완할 수 있습니다.

```bash
sudo apt-get update
sudo apt-get install -y python3 curl ca-certificates iproute2 util-linux procps
```

- `online` 실행에는 승인된 GitHub artifact·helm.cilium.io·quay.io·Docker Hub 및 앱 registry(이미지 저장소)의 DNS·HTTPS 접근이 필요합니다. `offline` 실행에는 같은 CPU 구조의 검증된 로컬 bundle이 필요합니다. 선택 registry와 프로토콜은 아래 capability(통신 가능 항목) 검사에서 구분합니다.
- Pod CIDR(앱 네트워크 주소 범위) `10.42.0.0/16`, Service CIDR `10.43.0.0/16`이 기존 네트워크와 겹치지 않아야 합니다.
- 외부 클라이언트에서 NodePort(서버 포트로 앱을 공개하는 방식)에 접근하려면 방화벽·보안 그룹·NAT(주소 변환) 구성이 필요합니다. 이 runtime은 이를 변경하지 않습니다.

### 입력 JSON

기존 형식은 **팀의 최종 외부 API 계약이 아닌 임시 계약 `0.1`**입니다. 네트워크·bundle·선택 공개 경로에는 새 `0.2`를 제공합니다. 기존 입력과 출력 형식은 유지하며 자세한 차이는 아래 새 배포 절에서 설명합니다. [입력 schema(데이터 형식 규약)](scripts/schemas/input.schema.json)를 제공합니다. 구문·타입 검증 외에 필드 사이의 의미 제약은 adapter가 검사합니다.

```json
{
  "schema_version": "0.1",
  "provider": "gcp",
  "environment_id": "railshot-runtime-demo",
  "node": {
    "host": "10.0.0.12",
    "ssh_user": "ubuntu"
  },
  "workload": {
    "image": "nginx:1.28.0-alpine",
    "namespace": "railshot-demo",
    "replicas": 1,
    "container_port": 80,
    "health_path": "/",
    "sample_content": true
  },
  "exposure": {
    "type": "nodeport",
    "node_port": 30080
  },
  "runtime": {
    "timeout_seconds": 180
  }
}
```

`node.host`와 `ssh_user`는 상위 계층의 접속 정보입니다. **이 CLI가 해당 주소로 SSH를 실행하거나 입력 주소와 실행 노드가 같은지 인증하지는 않습니다.** 상위 계층에서 신뢰할 수 있는 대상 노드를 선택하고, 그 노드 안에서 실행해야 합니다. 실제 결과의 endpoint는 입력 host 대신 Kubernetes가 관측한 Node InternalIP(노드 내부 주소)로 계산합니다.

| 필드 | 설명과 기본값 |
| --- | --- |
| `provider` | `aws`, `gcp`, `openstack` 중 하나입니다. 결과의 요청 문맥으로만 사용합니다. |
| `environment_id` | 소유권을 구분하는 소문자·숫자·하이픈 식별자입니다. 최대 63자입니다. |
| `node.host` | 필수 접속 정보입니다. IPv4 또는 호스트 이름 형식의 문자열을 받습니다. |
| `node.ssh_user` | 선택 접속 사용자입니다. 공통 엔진에서는 사용하지 않습니다. |
| `workload.image` | 필수 이미지입니다. 명시적인 tag(버전 이름) 또는 64자리 SHA-256 digest(불변 이미지 식별값)가 필요합니다. |
| `workload.namespace` | 필수 리소스 구역 이름입니다. 기본·시스템 구역은 거부합니다. |
| `workload.replicas` | 앱 개수이며 기본 1개, 입력 범위는 1~10개입니다. 여러 앱 복제본은 다중 노드 지원을 의미하지 않습니다. |
| `workload.container_port` | 앱이 실제로 듣는 포트이며 기본 80입니다. 이미지 내부의 서버 설정을 자동 변경하지 않습니다. |
| `workload.health_path` | HTTP 정상 응답 확인 주소이며 기본 `/`입니다. query와 fragment는 받지 않습니다. |
| `workload.sample_content` | 기본 `false`입니다. 샘플 nginx에서는 `true`로 지정하여 `Railshot Runtime OK` 본문을 제공합니다. nginx의 명시적 tag 또는 SHA256 digest(이미지 고정값)·포트 80·경로 `/` 조합으로 제한합니다. |
| `exposure.type` | `0.1`은 `nodeport`입니다. `0.2`는 기존 공개 URL을 확인하는 선택 `cloudflare-tunnel` hook(연결 지점)도 받습니다. 터널을 자동 설치하지는 않습니다. |
| `exposure.node_port` | 기본 30080, 범위 30000~32767입니다. |
| `exposure.verification_url` | 선택 추가 HTTP(S) 검사 URL입니다. **health path를 포함한 전체 주소**를 지정합니다. 자격정보·query·fragment는 거부합니다. |
| `runtime.node_ip` | 선택 내부 IPv4입니다. 지정하면 실행 노드 NIC(네트워크 장치)에 실제 할당된 주소인지 확인합니다. 공인 NAT 주소를 넣지 않습니다. |
| `runtime.timeout_seconds` | 준비·상태 확인 대기이며 기본 180초, 범위 5~900초입니다. 다운로드와 진단을 포함한 작업 전체 제한과는 다릅니다. |
| `runtime.k3s_version` | 승인된 기본 `v1.34.11+k3s1`입니다. 다른 profile(검증된 버전 조합)은 별도 검사·승격 전까지 거부하며 자동 업그레이드하지 않습니다. |
| `runtime.cilium_version` | 기본 `1.20.2`입니다. |
| `runtime.cilium_cli_version` | 기본 `v0.20.1`입니다. 다운로드 checksum(파일 무결성 값)을 확인합니다. |

임의 앱은 `sample_content`를 생략합니다. 예를 들어 포트 8080의 `/healthz`를 제공하는 이미지를 지정하시면 됩니다. digest 예시는 실제 64자리 값으로 입력해야 하며 `sha256:...` 같은 생략 문자열은 유효한 입력이 아닙니다.

### 실행 명령

아래 명령은 **대상 Linux 노드**의 저장소 루트에서 실행합니다. 입력을 파일이나 표준 입력으로 전달할 수 있습니다.

```bash
sudo ./deployment/scripts/deploy.sh --input deployment/scripts/tests/fixtures/openstack.json
sudo ./deployment/scripts/verify.sh --input deployment/scripts/tests/fixtures/openstack.json

# JSON 결과와 사람이 읽는 진행 로그를 분리합니다.
sudo ./deployment/scripts/deploy.sh --input input.json > result.json 2> deployment.log

# 입력을 표준 입력으로 전달할 수도 있습니다.
sudo ./deployment/scripts/deploy.sh < input.json
```

stdout(표준 출력)에는 최종 JSON 한 개를 반환합니다. 진행 상태와 오류 진단은 stderr(표준 오류 출력)에 기록합니다. 성공은 종료 코드 0, 실패는 1이며, 입력 오류·명령 실패·시간 초과도 JSON 오류로 반환합니다. `--help`는 일반 CLI 사용 안내를 출력합니다. 강제 프로세스 종료나 전원 차단 시 최종 JSON 반환은 보장하지 않습니다.

## JSON 출력과 상태

[출력 schema](scripts/schemas/output.schema.json)를 제공합니다. 정상 배포의 핵심 결과는 다음과 같습니다. 실제 출력에는 `action`, `endpoint_scope`와 상태·UTC 시각의 `states` 배열도 포함됩니다.

```json
{
  "schema_version": "0.1",
  "action": "deploy",
  "status": "ready",
  "provider": "gcp",
  "environment_id": "railshot-runtime-demo",
  "cluster_status": "ready",
  "cilium_status": "healthy",
  "workload_status": "ready",
  "endpoint": "http://192.168.5.15:30080",
  "endpoint_scope": "node-local",
  "error": null,
  "states": []
}
```

위 예시는 필드 설명을 위해 `states`를 생략한 형태로 비워 두었습니다. 실제 성공 응답에는 다음 순서의 전이와 시각이 들어갑니다.

```text
NODE_READY → K3S_INSTALLING → K3S_READY
           → CILIUM_INSTALLING → CILIUM_READY
           → WORKLOAD_DEPLOYING → WORKLOAD_READY → ENDPOINT_READY
실패 시 → FAILED
```

- `NODE_READY`는 Linux 실행 전제조건 확인을 의미합니다. 이 시점에 Kubernetes Node Ready가 확인된 것은 아닙니다.
- `K3S_READY`는 API 준비 완료이며 `cluster_status`는 `api_ready`입니다. Cilium과 Kubernetes Node 준비까지 확인한 뒤 `ready`로 변경합니다.
- `ENDPOINT_READY`는 Pod의 DNS·Service 경로와 노드에서의 HTTP 200 확인 완료를 의미합니다.
- `endpoint_scope: node-local`은 서버에서 확인했다는 뜻입니다. 별도 외부 클라이언트의 접근 성공이나 인터넷 공개를 보장하지 않습니다. 추가 URL 검사도 **같은 노드에서** 실행하며, `node-local-with-additional-url`로 표시합니다.
- 실패 응답은 `error.code`, `error.stage`, `error.message`, 하위 명령의 `exit_code`와 가능한 `diagnostics`를 제공합니다. 입력 검증 실패 단계는 `INPUT_ADAPTER`입니다.
- 제거 성공 상태는 `cleaned`입니다. 확인하지 않은 구성 요소는 `unknown`, 제거한 대상은 `absent`로 구분합니다.

## 설치와 앱 배포 흐름

1. OS·root·kernel·systemd·swap·도구를 확인하고, 공통 잠금으로 deploy·verify·cleanup의 동시 실행을 막습니다.
2. K3s의 기존 설정·버전을 확인합니다. 관리하지 않는 설치, 다른 설정, 추가 config 디렉터리를 자동 병합하지 않습니다. 동일한 설정·버전이면 기존 서비스를 재사용합니다.
3. 배포에는 기본 `auto` 네트워크 검사·버전 기준 확인을 수행합니다. 외부 접근이 불가능하면 입력으로 지정한 검증된 bundle을 사용합니다. 자세한 내용은 [Airgap 설명](airgap/README.md)을 확인하실 수 있습니다.
4. K3s 설정은 `flannel-backend: none`, `disable-network-policy: true`이며 kube-proxy(기본 Service 트래픽 처리 기능)는 유지합니다. Traefik·ServiceLB·metrics-server·local-storage는 비활성화합니다. kubeconfig(접속 설정)는 `/etc/rancher/k3s/k3s.yaml`, 권한은 `0600`입니다.
5. 공식 Cilium CLI로 설치하거나 동일 선언을 재적용합니다. `kubeProxyReplacement=false`, 단일 operator(관리 기능), VXLAN(가상 네트워크 통로), Pod 주소 범위 `10.42.0.0/16`을 사용합니다. Cilium·Node·CoreDNS 상태를 확인합니다.
6. namespace와 기존 앱·Service·ConfigMap(설정 데이터)의 소유권을 확인합니다. `app.kubernetes.io/managed-by=railshot-runtime`과 `railshot.io/environment`가 일치하지 않으면 덮어쓰기를 거부합니다.
7. JSON 템플릿을 구조화된 Kubernetes 객체로 변환하고 적용합니다. 이미지·복제본·포트·health path는 공통 설정에서 주입하며 셸 문자열로 실행하지 않습니다.
8. Deployment는 `maxUnavailable=0`(기존 준비 앱 유지), `maxSurge=1`(추가 앱 한 개), 준비 확인 3초, 종료 전 대기 5초를 사용합니다. startup·readiness·liveness probe(시작·준비·생존 검사)를 HTTP 경로에 연결합니다.
9. rollout(순차 교체) 완료와 실제 앱 설정을 확인하고, 내부 통신과 endpoint를 검사합니다.

K3s 구성 근거는 [K3s configuration](https://docs.k3s.io/installation/configuration), Cilium 구성 근거는 [Cilium K3s installation](https://docs.cilium.io/en/stable/installation/k3s/)을 참고했습니다.

## 검증과 오류 처리

- `verify`는 설치나 앱 apply를 수행하지 않습니다. Cilium·Node·Deployment 준비와 입력 대비 실제 이미지·포트·준비 경로·Service 설정을 확인합니다.
- 임의 앱 이미지에 `curl`이나 `wget`이 있다고 가정하지 않습니다. 일시적인 `curlimages/curl:8.12.1` 검사 Pod를 생성하여 DNS → Service → HTTP 200 경로를 확인하고 삭제합니다. 이 이미지의 다운로드 권한과 검사 Pod의 통신 권한이 필요합니다.
- 노드에서 InternalIP·NodePort·health path에 HTTP 요청합니다. nginx 샘플 모드에서는 정상 본문도 확인합니다. 일반 앱은 HTTP 200을 필수 확인합니다.
- 추가 `exposure.verification_url`은 전체 health URL을 그대로 검사합니다. 노드 내부 검증에 성공한 뒤 최대 3초의 별도 외부 접근 진단을 수행하며, 실패해도 Runtime `status=ready`와 node-local endpoint를 유지합니다. 0.2 출력은 `exposure_status.status=degraded`와 `additional_verification`에 이유를 기록합니다. 0.1 출력 필드와 상태 순서는 유지하지만, 외부 URL 실패의 의미는 Runtime 실패에서 외부 의존성 경고로 바뀝니다. offline 경로에서는 외부 요청을 하지 않습니다.
- 실패 시 대기 이유와 최근 이벤트를 수집하며, Secret·컨테이너 환경변수·kubeconfig 내용은 조회하지 않습니다. 앱 상태를 자동으로 정상이라고 간주하지 않습니다.
- 자동 rollback(이전 정상 버전 복원)은 구현하지 않습니다. 잘못된 새 이미지가 준비되지 않으면 rollout 실패로 반환하고, 가능하면 기존 준비 Pod가 계속 실행됩니다. 재시도나 이전 이미지 선택은 상위 계층이 결정합니다.
- Cilium Helm release(설치 상태)가 `pending-install` 등에 남은 경우 자동 복구를 보장하지 않습니다. 전용 노드 재설치 또는 승인된 별도 복구 절차가 필요합니다.

외부 확인은 다른 클라이언트에서 다음과 같이 수행합니다. 서버의 외부 접근 가능한 주소로 바꿔 실행해 주시기 바랍니다.

```bash
curl --noproxy '*' --connect-timeout 5 --max-time 10 -i http://SERVER_REACHABLE_IP:30080/
```

### 최소 NetworkPolicy 템플릿

`cilium/network-policy.json.template`은 같은 namespace의 `railshot.io/client=allowed` Pod만 앱 포트에 접근하도록 허용하는 **선택 템플릿**입니다. 기본 배포에는 자동 적용하지 않습니다.

그대로 적용하면 외부 NodePort와 진단 Pod가 차단될 수 있습니다. ingress(앱으로 들어오는 요청) 주체·검사 Pod·DNS·필요한 egress(앱 밖으로 나가는 요청)를 먼저 합의하고 렌더링해야 합니다. 현재 템플릿은 클러스터 전체 보안이나 테넌트(사용자별 구역) 격리를 완성한 정책이 아닙니다.

`render.render_policy(DeploymentSpec)` 함수로 실제 namespace와 앱 포트를 주입할 수 있습니다. 자동 테스트에서는 두 클라이언트가 정책 적용 전에 모두 접근 가능한지 확인한 뒤, 적용 후 승인한 클라이언트만 HTTP 200을 받고 다른 클라이언트는 차단되는지 검사합니다. 정책과 시험용 Pod는 이후 제거합니다.

## 제거

```bash
# 이 환경이 소유한 이름의 Deployment·Service·샘플 ConfigMap만 삭제합니다.
sudo ./deployment/scripts/cleanup.sh --input input.json

# 전용 테스트 노드의 K3s·Cilium 및 클러스터 전체 데이터를 삭제합니다.
sudo ./deployment/scripts/cleanup.sh --input input.json --all --disposable-node
```

일반 제거는 namespace와 다른 앱을 보존합니다. 이미 없는 앱을 다시 제거할 수 있습니다. 전체 제거는 runtime 관리 표식과 공식 K3s uninstall 경로를 확인하고 Cilium → 네트워크 링크 → K3s 순서로 처리합니다. **다른 환경의 앱과 데이터도 삭제되므로 전용 노드에서만 실행합니다.**

전체 제거 후에도 모든 커널 상태가 원래대로 복원된다는 보장은 없습니다. forwarding(네트워크 전달)의 현재 값은 자동 복원하지 않습니다. 완전한 초기 환경이 필요하면 노드를 재부팅하거나 VM을 재생성합니다.

## 자동 테스트

발표에서는 기존 준비 상태를 조회하는 [Runtime 데모 실행 안내](DEMO-RUNBOOK.md)를 사용합니다. `demo_preflight.py`는 공통 JSON 입력을 읽고 최대 25초 동안 조회만 수행합니다. `verify`와 달리 검사 Pod도 만들지 않습니다. `ready`는 노드 내부 Runtime 정상이며 공개 주소 접근 성공을 의미하지 않습니다.

### 로컬 계약·엔진 검사

```bash
python3 -m venv /tmp/railshot-runtime-tests
/tmp/railshot-runtime-tests/bin/pip install -r deployment/scripts/tests/requirements.txt
kubectl kustomize gitops/argo > /tmp/railshot-runtime-argocd-schema.yaml
PATH=/tmp/railshot-runtime-tests/bin:$PATH \
  RAILSHOT_ARGO_SCHEMA_MANIFEST=/tmp/railshot-runtime-argocd-schema.yaml \
  PYTHONDONTWRITEBYTECODE=1 python \
  -m unittest discover -s deployment/scripts/tests -v
```

runtime 자체는 Python 표준 라이브러리만 사용합니다. `jsonschema`와 `PyYAML`은 계약·선언 검증용 개발 의존성입니다. 위 CRD 렌더링은 고정된 선언의 schema를 읽기 위한 작업이며 Argo CD를 설치하지 않습니다. 전체 테스트에는 해당 파일과 개발 의존성이 필요합니다. GitHub Actions의 `Deployment Runtime`은 이 검사와 Runtime 파일 문법 검사만 수행하며 실제 CSP(클라우드 제공자) 자원을 만들지 않습니다.

### 실제 Linux 검사

```bash
sudo ./deployment/scripts/test-poc.sh --disposable-node \
  --results /var/tmp/railshot-runtime-results
```

**새 테스트 노드에서 실행합니다.** 잘못된 이미지·준비 경로 주입, 앱 업데이트, 전체 클러스터 제거·재설치를 수행합니다. 사람이 읽는 PASS/FAIL은 stderr로 출력하며 요약 JSON과 단계별 결과·로그를 보존합니다.

AWS·GCP·OpenStack fixture가 같은 로컬 노드의 동일 엔진을 통과하는지 확인합니다. 이는 실제 CSP(퍼블릭 클라우드 사업자)의 자원이나 네트워크를 시험한 결과가 아닙니다. 새 노드라면 clean install(최초 설치)을 수행하고, 이미 설치돼 있으면 해당 항목을 SKIP합니다. 마지막 전체 제거·재설치는 별도로 검사합니다.

2026-10-02의 실제 수행 결과는 [검증 기록](scripts/tests/results/VALIDATION-2026-10-02.md)에 정리합니다.

## 별도 VM에서 Provider 모의 통합 검사

`provider_simulation.py`는 AWS·GCP·OpenStack이 준비한 Linux 노드를 각각 **새 Lima VM(맥에서 실행하는 Linux 가상 머신)**으로 모의 구성합니다. 기존 fixture(시험용 입력)를 사용하며, provider별 분기문을 공통 엔진에 추가하지 않습니다. `deploy.sh`·`verify.sh`·`cleanup.sh`가 호출하는 동일한 `runtime.py`에 JSON을 전달합니다.

호스트 전제조건은 macOS Apple Silicon, Lima, Python 3.12 이상, 인터넷 연결입니다. 검증한 호스트는 RAM 16 GiB이며, 각 VM은 CPU 2개·RAM 4 GiB·디스크 20 GiB를 사용합니다. 메모리를 절약하기 위해 세 VM을 순서대로 실행합니다. 실행 중인 다른 VM이 많다면 먼저 충분한 메모리를 확보해 주시기 바랍니다.

```bash
# 저장소 루트의 Mac 터미널에서 실행합니다.
python3 deployment/scripts/tests/provider_simulation.py --disposable-vms \
  > provider-result.json 2> provider-test.log
```

`--disposable-vms`는 이 검사에서 생성한 전용 VM 안의 K3s·Cilium·앱을 마지막에 제거한다는 명시적 실행 옵션입니다. 이미 있는 VM을 대상으로 실행하지 않습니다. VM 디스크와 로그는 보존하고 VM만 정지합니다.

| 모의 대상 | hostname(노드 이름) | 시험용 NIC(네트워크 장치) / 주소 | Mac 확인 포트 |
| --- | --- | --- | --- |
| AWS | `ip-10-250-10-11` | `awsnic0` / `10.250.10.11` | `30084` |
| GCP | `gce-railshot-01` | `gcpnic0` / `10.250.20.12` | `30085` |
| OpenStack | `openstack-railshot-01` | `osnic0` / `10.250.30.13` | `30086` |

세 노드는 Ubuntu 24.04 **arm64**입니다. 시험용 NIC는 dummy(소프트웨어 가상 장치)이며, 이미지 다운로드에는 Lima 기본 NIC를 사용합니다. 각 IP가 실제 노드에 할당되고 K3s Node InternalIP 및 결과 endpoint에 반영되는지 확인합니다. EC2·GCE의 실제 NIC·라우팅을 재현한 것은 아닙니다. **amd64 실행은 아직 검증하지 않았습니다.**

각 노드에서 다음 8개 검사를 수행합니다.

1. K3s 파일이 없는 새 노드에 K3s → Cilium → nginx → Service를 설치합니다.
2. 같은 입력으로 재실행하고 Deployment·Pod UID(리소스 고유 식별값)가 유지되는지 확인합니다.
3. Cilium·Node 준비 상태, DNS(서비스 이름 조회) → Service → HTTP 200 및 노드 endpoint를 확인합니다.
4. nginx 이미지를 `1.28.0-alpine`에서 `1.28.1-alpine`으로 업데이트합니다.
5. 업데이트한 앱의 상태와 endpoint를 다시 확인합니다.
6. 앱을 제거하고 Deployment·Service가 없는지 확인합니다.
7. 원래 입력으로 앱을 다시 배포합니다.
8. K3s·Cilium 전체 제거 후 K3s 바이너리·데이터·설정이 없는지 확인합니다.

위 과정 중 Mac → Lima SSH 포워딩(포트를 VM에 전달하는 연결) → NodePort 경로에서도 HTTP 200과 샘플 본문을 확인합니다. Runtime 소스의 SHA-256(파일 내용 식별값)을 비교하여 모든 노드에서 동일한 엔진을 실행했는지도 확인합니다. 진행 결과는 stderr, provider별 결과 JSON은 stdout으로 반환하며 단계별 원본 JSON·로그는 `scripts/tests/results/provider-simulation/<실행 ID>/`에 보존합니다.

실제 결과는 [Provider 모의 통합 검증 기록](scripts/tests/results/PROVIDER-SIMULATION-2026-10-02.md)에서 확인하실 수 있습니다. VM을 정지한 뒤에는 확인 포트가 열려 있지 않습니다. 디스크까지 제거하려면 결과를 보존한 후 아래 명령의 이름을 결과 JSON에 기록된 해당 VM 이름으로 바꿔 실행합니다.

```bash
limactl delete --force railshot-sim-aws-<실행-ID>
limactl delete --force railshot-sim-gcp-<실행-ID>
limactl delete --force railshot-sim-openstack-<실행-ID>
```

이 검사는 AWS IAM·Security Group·Elastic IP·VPC routing, GCP IAM·VPC Firewall·External IP 및 실제 cloud metadata semantics를 검증하지 않습니다. 해당 항목은 모두 **`requires real cloud smoke test`(실제 클라우드에서 짧은 통합 확인 필요)**입니다. 공통 엔진이 metadata(클라우드가 노드에 제공하는 정보)를 읽지 않으므로 metadata mock은 추가하지 않았습니다.

## 네트워크 검사와 Online/Offline 배포

기존 JSON → Input Adapter(입력 변환 계층) → DeploymentSpec(공통 배포 설정) → Engine(실행 엔진) → Result(결과) 구조를 유지합니다. 노드 전제조건 확인과 실행 잠금 후 endpoint별 capability(통신 가능 항목)를 검사하여 설치 경로를 선택합니다.

```text
NODE_READY
    ↓ Network Capability Preflight
    ├─ 필수 경로 가능 → online → 승인 버전 artifact 다운로드 또는 로컬 재사용
    └─ 필수 경로 불가 → bundle 검증 → airgap → 로컬 binary/system images 준비
    ↓ K3s → 추가 이미지 preload → Cilium → workload → node-local health
    ↓ 선택 Exposure(외부 공개 경로) → endpoint/result
```

```bash
# 대상 Linux 노드에서 실행합니다. 기본 모드는 auto입니다.
sudo ./deployment/scripts/deploy.sh --mode auto --bundle /var/lib/railshot-deployment/bundle --input input.json
sudo ./deployment/scripts/deploy.sh --mode online --input input.json
sudo ./deployment/scripts/deploy.sh --mode offline --bundle /var/lib/railshot-deployment/bundle --input input.json
```

| 모드 | 선택 규칙 |
| --- | --- |
| `auto` | 필수 endpoint가 가능하면 online입니다. 실패하고 유효한 local bundle이 있으면 airgap입니다. 둘 다 불가능하면 오류입니다. |
| `online` | 외부 artifact/registry 접근을 요구하고 승인된 버전을 유지합니다. 자동 airgap 전환은 하지 않습니다. |
| `offline` | 외부 capability 검사와 외부 URL 접근을 생략하고 bundle만 사용합니다. 내부 Kubernetes 통신은 계속 필요합니다. |

DNS(이름 해석)·HTTPS 443·버전 고정 K3s/GitHub/Cilium chart 주소·quay·Docker Hub·registry.k8s.io·필요한 workload registry를 검사합니다. GHCR은 해당 workload를 쓰는 경우 필수입니다. 개별 검사 제한은 기본 3초이며 병렬 실행합니다. Cloudflare TCP/TLS 7844는 선택 검사입니다. WireGuard는 지원 경로와 capability 목록에서 제거했습니다. QUIC 검증을 수행했다고 주장하지 않습니다. 불명확/미검사 capability는 `null`입니다. 실제 방화벽 규칙 변경은 Runtime 기능이 아닙니다.

### 새 JSON 계약과 기존 consumer 호환성

기존 `schema_version: "0.1"` 입력은 기존 출력 필드와 `states` enum(허용 상태 목록)을 그대로 반환합니다. 새 옵션 없이 실행해도 선택 모드는 `auto`이므로 **예전보다 필수 외부 경로 검사가 엄격해진 동작 차이**는 있습니다. `verify`·`cleanup`은 설치 전 네트워크 검사를 요구하지 않습니다.

입력에 `schema_version: "0.2"`를 지정하거나 CLI의 `--mode`·`--bundle`·`--bundle-sha256`을 명시하면 새 출력 `0.2`를 반환합니다. 기존 schema의 `additionalProperties=false` 제약을 고려하여 새 필드를 몰래 `0.1`에 추가하지 않습니다. 새 schema는 [input-v0.2](scripts/schemas/input-v0.2.schema.json)와 [output-v0.2](scripts/schemas/output-v0.2.schema.json)에 있습니다.

```json
{
  "schema_version": "0.2",
  "provider": "openstack",
  "environment_id": "railshot-demo",
  "node": {"host": "localhost"},
  "workload": {
    "image": "nginx:1.28.0-alpine",
    "namespace": "railshot-demo",
    "sample_content": true
  },
  "runtime": {
    "mode": "auto",
    "bundle_path": "/var/lib/railshot-deployment/bundle",
    "preflight_timeout_seconds": 3
  },
  "exposure": {"type": "nodeport", "node_port": 30080}
}
```

새 결과에는 기존 필드와 함께 `deployment_mode`(`online`/`airgap`), `bundle_version`, `bundle_verified`, `network_capabilities`, `network_details`, `fallback_used`, `fallback_reason`, `preload`, `exposure_status`를 제공합니다. `NETWORK_CHECKING`·`BUNDLE_VERIFYING`·`AIRGAP_PRELOADING`은 별도 `network_states`에 기록하여 기존 `states` 목록을 바꾸지 않습니다. 실패의 `error.stage`는 새 단계 이름을 포함할 수 있으므로 오류 소비자는 알 수 없는 단계도 표시할 수 있어야 합니다.

승인된 버전은 `airgap/versions.json`에 고정합니다. 기존 입력의 형식은 유지하지만 이 기준 밖의 Runtime 버전은 `VERSION_NOT_APPROVED`로 거부합니다. 최신 버전 조회는 별도의 `airgap/check-updates.sh`에서 수행하며 실제 설치 버전을 자동 변경하지 않습니다. 배포 중에는 최신 버전을 조회하지 않아 `update_available`는 `null`입니다.

### 선택 공개 경로와 두 번의 Touch

`exposure.type: "cloudflare-tunnel"`은 이미 구성된 HTTPS `public_url`을 확인하는 선택 hook(연결 지점)입니다. token/domain/auth 계약이 없어 자동 설치·Tunnel 생성·DNS 변경은 구현하지 않습니다. 공개 URL이 없거나 접근이 실패하면 `exposure_status.status: "degraded"`를 반환하고 정상 node-local endpoint를 유지합니다. Offline에서는 공개 URL 확인을 생략합니다. NodePort는 기본 공개 방식이며 실제 인터넷 공개 여부는 별도 클라이언트에서 확인해야 합니다.

**첫 번째 Touch(최초 준비)**는 Linux 전제조건·실행 권한·Runtime·bundle 전달·신뢰한 manifest digest 전달·원격 연결 준비입니다. 수동/반자동이어도 허용합니다. **두 번째 Touch 이후(반복 배포)**에는 상위 도구가 저장한 bundle 경로와 요청 JSON을 전달하면 Runtime이 기본 `auto`로 경로를 선택합니다. 사용자가 매번 online/offline을 고를 필요가 없습니다.

이는 Internet-independent(외부 인터넷 없이 내부망/로컬 파일로 배포 가능)입니다. 중앙과 노드 간 통신까지 없는 Network-independent 원격 배포는 주장하지 않습니다. Bundle 준비·digest·preload·업데이트·제거·실제 시험 범위는 [airgap/README.md](airgap/README.md)와 [실제 검증 기록](scripts/tests/results/AIRGAP-VALIDATION-2026-10-02.md)에 설명합니다.

## Release artifact 배포

Online 앱 이미지의 기준 저장소는 GHCR(깃허브 이미지 저장소), offline bundle은 GitHub Release asset(버전별 첨부 파일)입니다. 공식 시스템 이미지는 공식 registry에서 준비합니다. 앱 빌드·GHCR push는 CI 담당 영역이며 이 Runtime에서 구현하지 않습니다.

`airgap/release-pack.sh`로 로컬 파일을 만들고, 선택 `release-upload.sh`·`release-download.sh`로 정확한 버전을 전달합니다. 큰 archive·생성 bundle·dist는 Git에서 제외합니다. CPU 구조별 manifest·checksum·metadata만 Git에 보존합니다. 이번 arm64 archive는 약 713 MiB이며 amd64는 아직 실제 생성·검증하지 않았습니다. GitHub 인증과 쓰기 권한은 확인했지만 실제 Release 업로드·원격 다운로드는 수행하지 않았습니다. 자세한 명령과 검증 범위는 [Airgap Release 설명](airgap/README.md#artifact-storage와-github-release)을 확인하시면 됩니다.

## Provider 및 Ansible 연결

| 단계 | 담당과 인계 내용 |
| --- | --- |
| 인프라 준비 | Provider 계층에서 Linux 노드 생성, 접속 권한, 네트워크·보안 그룹·이미지 다운로드 경로를 준비합니다. |
| 원격 실행 | Ansible·SSH·SSM 등 상위 실행 계층에서 `deployment/`와 JSON을 대상 노드로 전달하고 관리자 권한으로 CLI를 실행합니다. |
| 입력 변환 | adapter에서 외부 입력을 `DeploymentSpec`으로 정규화합니다. Ansible 변수나 API 필드 변경은 이 경계에서 처리합니다. |
| 배포와 검증 | 동일한 engine이 K3s·Cilium·앱·HTTP를 처리합니다. 기존에 CNI가 설치된 다른 클러스터는 자동 채택하지 않습니다. |
| 결과 수집 | stdout JSON, 종료 코드, stderr 로그를 별도로 수집합니다. 외부 endpoint 검사는 상위 실행 머신에서도 수행합니다. |

AWS/GCP는 NodePort까지의 보안 그룹·방화벽·라우팅을 해당 담당자가 준비해야 합니다. OpenStack은 보안 그룹·floating IP(외부 연결 주소)·온프레 네트워크 연결을 준비해야 합니다. 여기서는 각 provider SDK, Terraform, Ansible playbook을 구현하지 않습니다.

## 확정이 필요한 계약과 후속 모듈

- 외부 JSON 최종 필드·버전과 상위 `ReadyTarget`(준비된 배포 대상) 모델의 매핑을 합의해야 합니다. 현재 `node.host`는 실행 대상 인증 기능이 아닙니다.
- 클러스터 준비를 어느 계층이 담당하는지, Ansible이 만든 기존 K3s를 어떻게 안전하게 채택할지 합의해야 합니다. 기존 설치 표식이나 변수명을 자동으로 신뢰하지 않습니다.
- namespace 소유권·환경 식별자·앱 이름 규약이 필요합니다. 현재 namespace당 관리 앱 이름은 `railshot-workload` 한 개입니다.
- NodePort·외부 주소·TLS·공개 접근 검사·health path·검증 Pod 권한을 합의해야 합니다.
- 이미지 digest 사용·private registry 자격정보·실행 사용자·Pod 보안·NetworkPolicy를 팀의 승인된 계약과 연결해야 합니다. 현재 generic 템플릿은 임의 앱의 UID나 읽기 전용 파일시스템을 강제하지 않습니다.
- CIDR 변경·여러 NIC·다중 노드·운영 클러스터 업그레이드·작업 중단 복구·설치 drift(선언과 실제 설정 차이)는 추가 설계 대상입니다.
- Airgap preload(인터넷 없는 환경의 이미지 사전 적재)는 이번에 추가했습니다. Argo CD, cloudflared 자동 설치, NFD(노드 기능 탐지), CNPG, Sealed Secrets, Gateway API, 복잡한 GitOps는 구현하지 않습니다.
- 향후 모듈은 `cilium` 준비 다음과 앱 배포 전/후 등 승인된 단계에 연결합니다. 지금은 빈 확장 위치를 README로 보존하며 임의 명령 실행이나 plugin 로딩 기능은 추가하지 않습니다.
- Terraform provider·자원 provisioning·CI/GitHub Actions·MCP server·Dashboard·Patroni·cross-cloud HA·멀티 클라우드 DB 복제는 담당 범위에 포함하지 않습니다.

후속 Release 분리와 16개 Linux 시나리오의 실제 결과는 [최신 Release 검증 기록](scripts/tests/results/RELEASE-VALIDATION-2026-10-02.md)에 정리했습니다.

새 Railshot 위치의 재검증·첫 실패·최종 통과 결과는 [이관 검증 기록](scripts/tests/results/MIGRATION-VALIDATION-2026-10-02.md)을 확인하시면 됩니다.

후속 상태는 [Deployment 통합 준비 점검](scripts/tests/results/DEPLOYMENT-REVIEW-2026-10-02.md)과 [실제 클라우드 smoke test 점검](scripts/tests/results/CLOUD-SMOKE-2026-10-02.md)에 기록했습니다. 이번 unit 재검사는 49개 통과이며, 신규 AWS/GCP Runtime 배포는 현재 controller의 권한·인증 도구 미준비로 미실행입니다. 기존 공개 앱의 HTTP 200 확인과 Runtime 신규 배포 검증은 구분합니다. PR #1은 팀원에 의해 이미 통합된 상태입니다.
