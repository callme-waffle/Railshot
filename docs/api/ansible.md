# Ansible 실행 인터페이스

버전 1.2 · 2026-10-02 · 팀 통합 기준

> 상위 API가 확정한 자원과 작업별 입력을 Ansible inventory·변수로 변환하는 인터페이스입니다. VM 생성, guest 준비 확인, 런타임 설치, 앱 배포의 책임을 구분합니다. DB는 K3s 밖의 별도 HA 클러스터이며, 승인된 profile에 한해 팀의 PostgreSQL·Patroni·etcd·HAProxy 플레이북을 실행합니다. 단일 DB 설치는 계속 차단합니다.

## A. Directory Architecture

```text
contracts/
├── ansible-job.schema.json          # 상위 API의 작업·변수 입력
└── ansible-request.schema.json      # 내부 CLI 실행 요청
infrastructure/ansible/
├── api.py                          # 인증, 등록 자원 해석, 작업 접수·조회
├── inputs.py                       # 작업별 입력 검사, inventory·변수 요약
├── run.py                          # Provider 결과 변환, 공통 잠금·실행·중복 방지
├── database.py / database.yml       # 승인된 HA profile과 팀 DB 플레이북 연결
├── transport.py                    # AWS SSM / GCP IAP 관리 접속
├── guest.yml                       # 정빈 님 공통 guest 검사
└── runtime.yml                     # 승민 님 K3s·Cilium 설치 코드 호출
examples/ansible/                    # 자격증명이 없는 입력 예제
```

| 구성요소 | 책임 | 경계 |
|---|---|---|
| 상위 API·MCP | 서버 등록 대상·지원 작업·입력 검사, 생성→조회→설치 순서 조율 | 제품은 로그인 없는 공유 workspace입니다. 임의 IP·명령·키 경로를 요청받지 않습니다. |
| OpenStack Controller / CSP 코드 | VM 생성·상태 조회, 프로젝트·자원 정보 반환 | OpenStack 생성의 `202 accepted`를 guest 준비 완료로 해석하지 않습니다. |
| Ansible 연결부 | 등록 target 조회, 입력 변환, 실행 상태와 준비 확인 결과 반환 | 기존 단일 worker가 CLI와 같은 실행 함수를 호출합니다. |
| 정빈 님 guest / 승민 님 runtime | OS 선행조건 확인 / 단일 노드 K3s·Cilium 설치 | 원본 설치 로직을 재작성하지 않습니다. |
| 화균 님 DB 구현 | PostgreSQL·Patroni 설치 정책과 플레이북 | 등록된 profile의 HA 배치만 `playbooks/site.yml`로 실행합니다. 단일 PostgreSQL VM 설치는 지원하지 않습니다. |
| Argo CD 경로 | 준비된 실행 클러스터에 앱 선언 적용 | `runtime.install`에서 앱을 배포하지 않습니다. |

```mermaid
flowchart LR
    API["상위 API<br>등록 대상·입력 검사"] --> R["등록 자원 해석<br>Provider 결과 + 운영자 접속 설정"]
    R --> V["입력 검사<br>inventory·변수 매핑"]
    V --> P["validate<br>요약 반환·원격 실행 없음"]
    V --> J["jobs<br>기록 저장 후 202 반환"]
    J --> G["guest.yml<br>VM 준비 확인"]
    G --> K["runtime.yml<br>K3s·Cilium 설치"]
    K --> S["준비 확인 기록 검증<br>작업 상태 저장"]
    J --> D["database.yml<br>승인된 팀 HA 구성·검증"]
    D --> S
```

DB HA 요청은 승인 profile·배치·TLS/Vault 참조를 확인한 뒤 접수합니다. standalone, 부족한 HA topology, profile 없는 요청은 `501`로 차단하며 작업을 만들지 않습니다.

## B. Request / Response Contract

### B-0. 제품 REST API와 내부 실행기 경계

2026-10-02 제품 API 구현에 맞춰 갱신했습니다. 현행 공개 계약은 [제품 API](product.md)와 [OpenAPI](product.openapi.json)가 정본입니다. 사용자 계정·로그인·팀원 allowlist 없이 모든 사용자가 같은 workspace에서 업로드·빌드·배포를 요청합니다. 실행 범위는 서버에 등록한 target·profile과 입력 검사로 결정합니다. 아래 B-1 이후의 **내부 Ansible HTTP 계약 1.2와 DB HA 계약은 별도로 유지**합니다.

| 제품 자원 | 생성·요청 | 조회 | Ansible과의 관계 |
|---|---|---|---|
| 빌드 | `POST /api/v1/builds` | `GET /api/v1/builds/{id}` | 소스 검사·검증 이미지 게시. Ansible 설치 호출 없음 |
| 배포 | `POST /api/v1/deployments` | `GET /api/v1/deployments/{id}` | 등록한 target의 CI→GitOps/Argo→기대 공개 HTTP 검증 |
| 실행 대상 | 없음 | `GET /api/v1/targets` | 서버 등록 대상과 지원 기능. 등록만으로 runtime 준비 완료 처리하지 않음 |
| 환경 사양 | 없음 | `GET /api/v1/profiles` | 운영자가 등록한 Provider·site·용도·지원 범위 |
| 환경 계획 | `POST /api/v1/plans` | `GET /api/v1/plans/{id}` | 입력·정책 검사와 Terraform saved plan 생성·저장. VM apply·설치 없음 |
| 실행 환경 | `POST /api/v1/environments` | `GET /api/v1/environments/{id}` | 저장한 plan의 자원 준비 후 환경별 snapshot으로 native Ansible CLI 실행 |

경로는 짧은 제품 자원의 복수 명사로 정하고 대시·밑줄로 내부 실행 용어를 조합하지 않습니다. 명명은 팀 규칙이고 HTTP 메서드·상태 의미는 표준을 따릅니다. 화균 님 OpenStack Controller의 상세 자원 직접 반환, 목록 `items/next_marker`, 접수 `202 + Location`, 공통 오류 객체를 재사용합니다. 동기 계획 저장은 `201 + Location`, 비동기 환경·배포 접수는 202입니다. 접수 본문은 `resource_id`, `action`, `status: accepted`, `request_id`; 오류는 `error: {code, message, request_id, retryable, outcome_unknown}`입니다.

제품 HTTP의 `request_id`는 매 요청 서버가 생성하는 추적 ID입니다. 내부 Ansible의 `request_id`는 작업 조회·중복 방지에 사용하는 영속 ID이므로 제품의 guest/runtime 기록에서는 `ansible_job_id`로 구분합니다. 제품 `Idempotency-Key`도 별개입니다. 응답이 유실되거나 결과가 unknown이면 새 작업 ID로 무조건 재실행하지 않습니다.

현재 제품 환경 실행 코드에는 **등록된 AWS/GCP 단일 amd64 runtime, 선택적 `database.mode=patroni`, 배포 대상 등록**이 연결되어 있습니다. `database.mode=none`은 DB 단계를 생략하고, `standalone`은 `STANDALONE_DATABASE_UNSUPPORTED`로 차단합니다. Patroni는 운영자가 등록한 DB profile의 역할·배치와 요청이 일치해야 하며, DB 노드 2개 이상·홀수 DCS voter 3개 이상·별도 proxy 1개 이상을 요구합니다. 제품 API는 저장한 plan의 만료·정책·snapshot·digest·apply 이력을 확인하고 계획을 한 환경에 한 번만 연결합니다. 순서는 **계획 확인 → Terraform apply → Provider descriptor 고정 → 환경별 등록 snapshot → native 입력 검사 → guest/runtime 실행 → 선택적 DB 구성 → 설정된 배포 대상 등록 → 결과 저장**입니다.

[environments.js](../../apps/api/src/environments.js)는 Provider 결과와 운영자 SSH 참조로 비공개 `<target_id>.json` descriptor와 `targets.json`을 생성합니다. 이 snapshot을 사용해 기존 `run.py --validate-only`, `guest.check`, `runtime.install`을 순서대로 실행하고 각 결과의 요청 ID·target·operation·준비 상태를 확인합니다. 기존 상시 HTTP 실행기의 target registry를 변경하거나 실행기를 재시작하지 않으므로 C-1의 수동 등록 운영과 구분합니다. 브라우저는 SSH 키·운영자 Bearer·파일 경로를 받지 않습니다.

Patroni를 선택하면 [cluster.py](../../infrastructure/ansible/cluster.py)에 환경별 DB registry·cluster spec을 전달합니다. 제품은 반환된 `database_ready`, 작업 ID, 비공개 binding 파일 경로와 digest를 확인한 뒤에만 `database.status=succeeded`를 저장합니다. 이 경로는 아래 내부 HTTP의 운영자 등록 HA profile을 호출하는 방식과 구분하며, 두 경로 모두 기존 DB 플레이북을 사용합니다.

환경의 runtime 준비와 제품 CI/CD 대상 등록도 구분합니다. runtime profile에 배포 설정이 있으면 [environment.py register](../../deployment/scripts/environment.py)를 호출하며, 필요한 DB binding을 전달하고 등록 성공을 확인한 뒤에만 `binding.status=succeeded`, `deployment_supported=true`가 됩니다. [product.js](../../apps/api/src/product.js)는 완료된 환경·plan·target·앱의 연결을 확인해 이 대상을 CI/CD에 사용합니다. 배포 설정이 없으면 runtime 또는 DB 준비가 성공해도 `deployment_supported=false`, `DEPLOYMENT_TARGET_NOT_REGISTERED`를 유지합니다. 이 자동 등록은 서버의 사전 설정 범위에서 수행되며 임의 자격·경로를 브라우저 입력으로 받지 않습니다.

**운영 인수 상태와 구현 범위는 별개입니다.** 2026-10-02 운영 확인에서 공개 `GET /api/v1/profiles` 목록은 `items: []`로, 새 환경·DB profile은 아직 활성화되지 않았습니다. 기존 등록 대상의 업로드→CI→CD→공개 HTTPS 성공은 새 VM 생성이나 runtime 1대와 DB용 3대를 포함한 실제 4VM 경로의 검증 결과가 아닙니다. 해당 4VM 생성·Patroni 준비·binding·앱 연결은 별도 운영 검증이 남아 있으며, 로컬 HTTP·모의 실행 시험으로 완료를 주장하지 않습니다. DB 역할·배치·TLS/Vault 매핑과 내부 HTTP의 승인 profile 기반 `database.configure` 계약은 아래를 따릅니다.

책임 경계는 [CI 백엔드 설계](ci-backend-design.md), 이름·응답 규칙은 [REST 컨벤션](conventions.md), 저장소 작업 지침은 [AGENTS.md](../../AGENTS.md)를 따릅니다. 성공·오류 외형의 기준 구현은 [화균 님 OpenStack schema](../../infrastructure/providers/openstack/src/control_plane/api/schemas.py)입니다.

### B-1. 호출 경로

| 메서드·경로 | 입력 | 응답 |
|---|---|---|
| `POST /v1/ansible/validate` | 작업·등록 target·작업별 parameters | `200`: 입력 검사와 inventory·변수 요약. SSH·설치·작업 기록 생성 없음 |
| `POST /v1/ansible/jobs` | 같은 입력 형식 | 신규 작업은 `202`와 `Location`, `Retry-After: 2` |
| `GET /v1/ansible/jobs/{request_id}` | 접수한 작업 ID | `200`: 저장한 상태와 준비 확인 결과 |

기계가독 명세는 [OpenAPI 3.1](ansible.openapi.json), 호출 입력은 [Job JSON Schema](../../contracts/ansible-job.schema.json)를 따릅니다. 기본 주소는 실행기 내부의 `http://127.0.0.1:4180`이며 모든 경로에서 운영자 Bearer 인증을 확인합니다. 브라우저·유저에게 직접 공개하는 API가 아닙니다. 상위 API가 다른 호스트에 있다면 인증된 내부 연결 방법을 별도로 정해야 합니다.

### B-2. 공통 입력과 작업별 변수

| 필드 | 규칙 |
|---|---|
| `request_id` | 1–128자 영문·숫자·`.`·`_`·`-`. 작업 접수의 중복 방지 키입니다. `validate`는 ID를 예약하지 않습니다. |
| `target_id` | 실행기에 등록된 자원 별칭. 영문 소문자로 시작하며 소문자·숫자·`-`, 최대 63자입니다. |
| `operation` | `guest.check`, `runtime.install`, `database.configure` |
| `parameters` | 작업별로 정의한 필드만 받습니다. 알 수 없는 필드·다른 작업의 변수·JSON 중복 키는 거부합니다. |

| operation | parameters | 처리 |
|---|---|---|
| `guest.check` | 생략 또는 `{}` | runtime·database 용도 VM 모두 공통 guest 검사 가능 |
| `runtime.install` | 선택적 `wait_timeout_seconds`: 정수 30–600, 생략 시 300초 | 기존 K3s·Cilium 스크립트의 `WAIT_TIMEOUT`으로 전달 |
| `database.configure` | 필수 `mode`, `nodes`, `placements`; 실행에는 `profile_id` 필요 | 승인된 Patroni HA profile을 실행. standalone은 차단 |

`command`, `playbook`, `extra_vars`, SSH 키·파일 경로, 클라우드 자격증명은 HTTP 입력에 없습니다. runtime 작업 제한 시간은 운영자 target 설정의 `timeout_seconds`(기본 1200초), DB는 승인 profile의 `timeout_seconds`입니다. 모두 30–1800초 범위입니다. `wait_timeout_seconds`는 그 안에서 사용하는 개별 준비 상태 대기 값으로, 전체 제한 시간을 늘리지 않습니다.

런타임 요청 예시입니다.

```json
{
  "request_id": "runtime-001",
  "target_id": "demo-openstack",
  "operation": "runtime.install",
  "parameters": {"wait_timeout_seconds": 180}
}
```

### B-3. 입력 검사와 실행 가능 여부

`validate` 응답의 `valid: true`는 입력과 등록 정보의 매핑이 유효하다는 뜻입니다. `execution_supported`는 해당 실행 코드의 존재 여부이며, SSH 연결·자원 소유권·guest 준비를 실시간으로 확인했다는 뜻이 아닙니다. inventory는 키 경로와 SSH 설정을 제거한 요약이므로 그대로 설치에 사용하는 파일이 아닙니다.

| 반환 항목 | 의미 |
|---|---|
| `operation`, `target_id` | 검사한 작업과 대상 |
| `execution_supported`, `blockers` | 현재 실행 지원 여부와 차단 사유 |
| `inventory` | 그룹·호스트 별칭·관리 IP·Provider·배치 위치·자원 ID |
| `variables` | 기존 플레이북에 전달되는 변수 또는 DB 담당자 전달용 매핑 |

현재 runtime은 Ubuntu 22.04/24.04의 amd64 단일 노드입니다. K3s `v1.34.11+k3s1`, Cilium `1.20.2`를 사용합니다. ARM64 runtime과 다중 노드 설치는 지원으로 표시하지 않습니다.

## C. Provider Result → Inventory → Variables

### C-1. 자원 정보의 출처

상위 API는 의도한 프로젝트의 권한으로 자원을 생성·조회한 후 운영자 등록 정보와 연결합니다. 실행기는 등록된 파일을 읽으며 Provider에 다시 조회하지 않습니다. target 등록·변경은 현재 운영자 설정 단계이고 동적 등록 endpoint는 없습니다. target 목록 변경은 실행기를 정상 종료한 뒤 다시 시작해 반영합니다. Provider 응답 파일은 실행 시 읽으므로 변경 시 기존 요청 ID와 충돌할 수 있습니다.

| 출처 | 사용하는 필드 | 내부 매핑·검사 |
|---|---|---|
| AWS/GCP `node_descriptor` | `target_id`, `provider_kind`, `resource_id`, `addresses.private`, `location`, `transport_ref`, `architecture` | target·사설 IP·지역/zone을 보존. `x86_64`→`amd64`. SSM/IAP 대상과 자원 ID 일치 검사 |
| OpenStack `GET /api/v1/servers/{id}` | `id`, `project_id`, `status`, `addresses[].network/address/version` | 등록한 VM·프로젝트와 일치하고 `ACTIVE`여야 합니다. 지정 관리망의 IPv4가 정확히 하나여야 합니다. |
| 운영자 target 설정 | 관리망 이름, placement, architecture, initialization, SSH 참조 | Controller 응답에 없는 접속·초기화 정보를 보완합니다. |

OpenStack 생성 응답의 `resource_id/status: accepted`는 사용할 수 없습니다. 상세 조회의 `id/status: ACTIVE`가 필요합니다. 관리망 선택에는 `addresses[].network`의 실제 이름/키를 사용하며 네트워크 UUID라고 가정하지 않습니다. 주소가 없거나 여러 개이면 임의로 첫 주소를 선택하지 않습니다. 관리 주소는 RFC1918 IPv4만 허용합니다.

필드 대조는 등록 정보와 응답의 일치 검사입니다. 자원 소유권을 독립적으로 조회하거나 VM의 현재 상태를 보장하지는 않습니다. 상위 API가 최신 응답과 프로젝트 접근 권한을 책임집니다.

### C-2. 운영자 등록 파일

[전체 등록 예제](../../examples/ansible/api-targets.json)에 AWS/GCP runtime, OpenStack runtime, 별도 DB VM을 구분했습니다. 아래는 OpenStack runtime 항목입니다.

```json
{
  "server_file": "/secure/railshot/openstack/demo-openstack/server.json",
  "resource_id": "example-runtime-vm",
  "project_id": "example-project",
  "management_network": "management",
  "placement": "onprem-a",
  "architecture": "amd64",
  "initialization": "cloud-init",
  "purpose": "runtime",
  "ssh": {
    "user": "railshot-operator",
    "identity_file": "/secure/railshot/identity_ed25519",
    "known_hosts_file": "/secure/railshot/known_hosts"
  },
  "timeout_seconds": 1200
}
```

이 객체는 `version: 1`, `targets` 맵의 `demo-openstack` 항목으로 저장합니다. VM ID·프로젝트·주소·경로는 설명용이며 실제 값으로 교체해야 합니다. SSH 포트는 OpenStack에서 생략 시 22번입니다. DB VM은 다른 자원 ID·IP를 등록하고 `purpose: database`를 사용합니다. 이 값은 운영자의 용도 지정이며 VM에 K3s가 없는지 탐지하는 기능은 아닙니다.

AWS 운영자가 CLI로 직접 생성·조회한 자원은 `execution_driver: "aws-cli"`를 명시할 수 있습니다. AWS에만 허용하며 기존 resource/region/SSM 일치 검사를 적용합니다. Terraform 실행기의 target/provenance 검사는 변경하지 않습니다.

### C-3. 실제 Ansible 매핑

| 입력·설정 | inventory / extra-vars | 읽는 구현 |
|---|---|---|
| VM 별칭·사설 IPv4 | `k3s_server.hosts.<id>`, `ansible_host` | `guest.yml`, `runtime.yml` |
| 운영자 SSH 참조 | `ansible_user`, `ansible_port`, 개인키·known_hosts 설정 | 실행기에만 존재하는 실제 inventory |
| 요청·대상·자원 ID | `railshot_request_id`, `railshot_target_id`, `railshot_resource_id` | 실행 요청·준비 확인 기록의 대상 바인딩 |
| architecture·initialization | `railshot_expected_arch`, `railshot_initialization` | 실제 guest와 요청 비교, cloud-init 완료 확인 |
| VM IP·별칭 | `railshot_node_ip`, `k3s_api_host`, `k3s_node_name` | NIC 주소·runtime identity 확인 |
| 팀 버전 정책 | `k3s_version` | 기존 설치 버전 검사 |
| `parameters.wait_timeout_seconds` | `railshot_wait_timeout_seconds` → `WAIT_TIMEOUT` | K3s·Cilium 준비 상태 대기 |
| 실행기가 만든 값 | `railshot_nonce`, `railshot_receipt_path` | 요청과 실행 단계가 일치하는 준비 확인 기록 검사 |

실제 inventory와 변수는 비공개 임시 JSON 파일로 만들고 `ansible-playbook -i inventory.json ... --extra-vars @vars.json`에 전달합니다. API 호출자가 파일명이나 명령 문자열을 조립하지 않습니다. Ansible의 [inventory·그룹 모델](https://docs.ansible.com/projects/ansible/latest/inventory_guide/intro_inventory.html)과 [JSON/YAML 변수 파일 입력](https://docs.ansible.com/projects/ansible/latest/playbook_guide/playbooks_variables.html#vars-from-a-json-or-yaml-file)을 사용합니다.

## D. Execution / Status

### D-1. 실행 순서

1. 호출자 인증과 입력 형식을 확인합니다.
2. 등록 target에서 Provider 결과와 접속 설정을 읽고 실행 요청을 만듭니다. DB는 profile에 승인된 모든 노드와 TLS/Vault/SSH 참조의 내용 해시도 바인딩합니다.
3. `validate`는 비밀을 제거한 요약을 반환하고 끝납니다. DB HA 최소 배치나 profile이 없으면 차단 사유를 반환합니다.
4. 실행 작업은 입력 해시와 `queued` 상태를 저장한 뒤 `202`를 반환합니다.
5. 단일 worker가 CLI와 공유하는 제한 시간·잠금·내구 기록 함수를 실행합니다. runtime은 guest/runtime 플레이북, DB는 팀 `playbooks/site.yml`을 고정 wrapper로 실행합니다. 별도 CLI subprocess를 중첩하지 않습니다.
6. 요청·노드·단계·nonce가 일치하는 준비 확인 기록을 검사하고 결과를 저장합니다. 호출자는 `Location` 경로를 조회합니다.

접수와 완료를 분리하고 상태 자원의 위치와 조회 간격을 반환하는 [비동기 요청·응답 패턴](https://learn.microsoft.com/en-us/azure/architecture/patterns/async-request-reply)을 적용했습니다. 상태 조회 자체가 `200`이어도 본문의 `status`가 실패일 수 있습니다.

| 상태 | 의미 |
|---|---|
| `queued`, `running` | 접수·실행 중 |
| `succeeded` | 해당 guest/runtime/database 단계의 준비 확인 기록 검증 완료 |
| `failed`, `blocked` | 실패 또는 실행 전제 미충족 |
| `unknown` | 실행 후 결과를 확정할 수 없어 운영자 확인 필요 |

`database.configure` 결과에는 `database_ready`를 추가하며 native playbook 종료 코드 0과 요청·전체 target 목록·nonce가 같은 receipt를 모두 확인한 경우에만 true입니다. DB의 `guest_ready`는 팀 HA preflight를 통과했다는 의미이고 runtime용 CPU/RAM 검사를 대신하지 않습니다. DB 작업에서 `runtime_ready`는 false입니다. `application_ready`와 `public_http_verified`는 항상 false입니다. K3s 설치 성공을 앱·DB·공개 HTTPS 성공으로 바꾸어 표시하지 않습니다. guest는 Ubuntu·systemd·CPU 2개 이상·RAM 1800MiB 이상·swap 비활성·root 여유 10GiB·NIC 주소·초기화 완료를 확인합니다.

### D-2. 중복 요청과 오류

동일한 요청 ID와 동일한 서버 측 확장 입력이면 기존 작업·결과를 반환합니다. `parameters`, Provider 결과, 접속 설정, 제한 시간으로 만든 내부 요청이 달라지면 `409 REQUEST_ID_CONFLICT`입니다. 한 번에 한 작업을 접수하며 다른 작업이 실행 중이면 `409 EXECUTOR_BUSY`입니다. 모든 선택 target와 physical resource를 정렬 잠그며 runtime 작업과도 같은 잠금을 사용합니다. 검증된 SSM/IAP 참조와 AWS ARN/인스턴스 ID를 정규화하므로 등록 별칭으로 잠금을 우회할 수 없습니다. 이전 ARN 형식의 unknown 기록에도 같은 정규화를 적용하고 physical identity가 없는 과거 unknown 기록은 보수적으로 새 실행을 차단합니다. 재시작 후 결과가 없거나 unknown인 작업과 자원이 하나라도 겹치면 새 ID도 차단합니다. 무제한 대기열이나 자동 재시도는 없습니다.

| HTTP / 코드 | 처리 방법 |
|---|---|
| `400 INVALID_JOB_REQUEST` | 필드·타입·작업별 변수·DB 배치 수를 수정합니다. |
| `400 TARGET_PURPOSE_MISMATCH` | DB VM에 runtime 설치를 요청했거나 DB 목록에 runtime VM을 넣었는지 확인합니다. |
| `401` / `403` | Bearer 인증 또는 localhost Host/Origin 조건을 확인합니다. |
| `404 TARGET_NOT_REGISTERED` | 승인된 target을 등록한 뒤 호출합니다. |
| `409 REQUEST_ID_CONFLICT` | 같은 ID로 입력을 바꾸지 않습니다. |
| `409 TARGET_RECONCILE_REQUIRED` | 이전 미확정 작업의 실제 결과와 준비 확인 기록을 확인합니다. |
| `501 DATABASE_STANDALONE_UNSUPPORTED` | 단일 DB 설치기는 연결되어 있지 않습니다. |
| `501 DATABASE_HA_TOPOLOGY_REQUIRED` / `DATABASE_PROFILE_REQUIRED` | HA 최소 배치와 승인 profile이 필요합니다. |
| `400 DATABASE_PROFILE_NOT_REGISTERED` / `DATABASE_PROFILE_MISMATCH` | 승인된 profile ID와 정확한 역할·배치를 사용합니다. |
| `503 DATABASE_PROFILE_INVALID` | 운영자가 비공개 profile·TLS·Vault 참조를 점검합니다. |
| `503 TARGET_CONFIGURATION_INVALID` | 등록 파일·자원/프로젝트·관리 주소와 OpenStack ACTIVE 상태를 확인합니다. 자동 재시도를 지시하는 응답이 아닙니다. |

HTTP 오류는 `{"error":{"code":"INVALID_JOB_REQUEST"}}` 형식입니다. 접수된 작업의 `error`는 `code`, `outcome_unknown`을 가지며 원문 예외·비밀·SSH 파일 경로는 반환하지 않습니다. API가 중단되면 기록과 일치하는 native 완료 결과가 있을 때만 복구하고, 없으면 `unknown`으로 남깁니다. 시간 초과가 원격 설치 중단을 보장하지 않습니다.

## E. Database 실행 계약 — K3s 밖의 별도 HA 클러스터

### E-1. 구성과 접속

정본은 DB 담당 브랜치의 [README](https://github.com/Jasmin-Softbank/Railshot/blob/feature/multicloud-db-hwagyun/README.md)입니다. 준비된 별도 VM에 **PostgreSQL·Patroni·etcd·HAProxy로 구성한 단일 HA 클러스터**를 설치하며 여러 거점에 배치할 수 있습니다. 거점마다 독립된 standby cluster를 만드는 구조가 아닙니다. DB VM은 K3s control-plane이나 worker에 가입하지 않고, `database.configure`는 K3s를 설치하지 않습니다. 앱 K3s와 DB 클러스터의 설치·재생성 수명주기를 분리합니다.

앞서 제안한 PostgreSQL 전용 VM 1대·DCS 0개 구성은 단순화 논의를 위한 이전 제안입니다. 팀의 현재 HA 구현이나 운영 기본값을 대체하지 않으며 standalone 실행은 계속 차단됩니다. 아래 3 VM 예시는 DB/DCS 역할을 겸해 연결부를 검증하기 위한 최소 배치입니다. 실제 운영 개수·장애 도메인·proxy 이중화는 별도로 결정합니다.

```mermaid
flowchart LR
    A["유저 앱 Pod<br>실행 K3s"] -->|"사설 경로 · TCP 5432"| P["별도 DB 클러스터<br>HAProxy"]
    P --> D["PostgreSQL · Patroni<br>DB VM 2대 이상"]
    D --- E["etcd DCS<br>홀수 3개 이상"]
    S["앱 namespace Secret<br>DB 접속 자격"] -.-> A
    D --> V["영속 데이터 볼륨<br>VM 재생성과 분리"]
    D -.->|"담당 구현에서 구성"| B["별도 백업 저장소<br>복구 시험 필요"]
    O["Ansible 실행기"] -.->|"사설 SSH · 팀 HA playbook"| D
```

위 그림은 연결 구조입니다. 이번 API는 승인 profile로 팀 HA 설치 playbook을 실행합니다. VM·볼륨·사설 네트워크 준비와 앱 Secret·접속 정책 구성은 별도 선행 작업입니다.

| 항목 | 기본 설계 |
|---|---|
| DB 서버 | 별도 VM의 PostgreSQL·Patroni·etcd·HAProxy. K3s 설치·노드 가입 없음 |
| 데이터 | 데이터 볼륨과 백업의 보존·복구 정책을 DB 담당 구현에서 확정 |
| DB 통신 | 앱에서 DB의 사설 주소로 TCP 5432. 공인 EIP·ALB를 DB 접속 주소로 사용하지 않음 |
| 환경 간 접속 | 운영자가 준비한 VPC/LAN 등의 route·복귀 경로 필요. Ansible이 관리 네트워크를 새로 생성하지 않음 |
| 접근 제어 | DB의 listen 주소·방화벽·`pg_hba.conf`를 제한하고 앱 전용 계정 사용 |
| 비밀 전달 | 앱 namespace의 Secret으로 전달. Git·HTTP 요청·작업 로그에 DB 비밀번호를 저장하지 않음 |

PostgreSQL의 [listen 주소·기본 5432 포트](https://www.postgresql.org/docs/current/runtime-config-connection.html)와 [클라이언트 주소·DB·계정별 접근 규칙](https://www.postgresql.org/docs/current/auth-pg-hba-conf.html)을 기준으로 설계합니다. 외부 DB가 관찰하는 원본 주소는 CNI의 SNAT 여부에 따라 노드 IP 또는 Pod IP일 수 있으므로, 실제 출발 주소와 복귀 경로를 확인한 뒤 허용 범위를 정해야 합니다.

현재 팀의 `deployment/cilium/network-policy.json.template`은 **Ingress만** 정의합니다. 앱→외부 DB의 Egress 허용 정책이나 DB 방화벽이 구현됐다고 볼 수 없습니다. Pod CIDR `10.42.0.0/16`, Service CIDR `10.43.0.0/16`과 LAN/VPC/VPN 대역 중복도 실제 환경에서 확인해야 합니다.

현재 [CD 인계 경로](../../gitops/README.md)도 DB·앱 Secret 주입·외부 egress를 지원 범위에서 제외하며, 생성하는 NetworkPolicy는 egress를 차단합니다. 이미지 pull Secret 참조 지원과 DB 자격 주입은 다른 기능입니다. 따라서 위 앱 접속 구조는 기본 설계이며 기존 자동 배포만으로 DB 자격과 TCP 5432 접속 정책까지 적용되지 않습니다. DB 사용 앱을 연결할 때 해당 계약을 담당자와 함께 확장해야 합니다.

### E-2. 입력 검증과 이전 standalone 예시

[DB 검증 요청 예제](../../examples/ansible/database-validate.json)는 이전 standalone 제안의 **차단 응답 확인용**입니다. 실제 HA 실행 입력은 E-3을 사용합니다.

```json
{
  "request_id": "db-plan-001",
  "target_id": "db-onprem",
  "operation": "database.configure",
  "parameters": {
    "mode": "standalone",
    "nodes": [{"target_id": "db-onprem", "roles": ["database"]}],
    "placements": [{
      "provider": "openstack", "site": "onprem-a",
      "database_nodes": 1, "dcs_voters": 0
    }]
  }
}
```

| 입력 | 매핑·검사 |
|---|---|
| `mode` | `standalone`은 설계·매핑 검증만, `patroni`는 승인 HA profile 실행을 지원합니다. |
| `nodes[].target_id` | 모두 `purpose: database`로 등록한 별도 VM. 요청의 대표 target도 목록에 포함해야 합니다. |
| `nodes[].roles` | `database`, `dcs`, `proxy`. 역할·VM·관리 IP 중복을 거부하고 DB와 proxy를 같은 VM에 둘 수 없습니다. |
| `placements[]` | 실제 provider/site별 역할 수와 `database_nodes`, `dcs_voters`, 선택적 `proxy_nodes`(생략 0)가 일치해야 합니다. |
| `inventory` | `database`, `dcs`, `proxy`의 비밀 없는 요약. 실행기는 이를 팀의 `db_nodes`, `etcd_nodes`, `proxy_nodes`로 매핑합니다. |
| `variables.railshot_database` | `mode`, `port: 5432`, `placements`의 공개 요약. 실제 cluster·TLS·비밀번호 변수는 운영자 profile에서만 구성합니다. |

standalone은 DB 1개·DCS/proxy 0개를 검사합니다. Patroni 실행에는 DB 2대 이상·홀수 etcd 3대 이상·proxy 1대 이상을 요구합니다. 입력 검증만으로 실제 복제·장애 도메인·장애 전환을 입증하지 않습니다. 회의의 AWS 3개·온프렘 2개 예시는 배치를 변수로 전달하려는 설명으로 해석하며 기본 노드 수로 고정하지 않습니다.

위 standalone 예시는 `DATABASE_STANDALONE_UNSUPPORTED`로 차단됩니다. [HA 입력 규격](deployment-inputs.md)을 만족하는 승인 profile만 `execution_supported: true`로 표시합니다. 이는 파일 참조와 입력의 일치 확인이며 실제 설치 결과가 아닙니다.

### E-3. 승인된 HA profile

등록 파일에 `"database_profiles": {"ha-demo": "/secure/railshot/ha-demo.json"}`을 추가합니다. 이 값은 운영자만 설정하며 HTTP는 `parameters.profile_id: "ha-demo"`만 지정합니다. profile 파일은 매번 읽고 내용 변경을 요청 해시에 반영합니다.

HTTP `parameters`의 최소 검증 배치 예시입니다. 운영 기본 VM 수를 고정하지 않습니다. 각 target는 별도의 실제 VM이어야 하며 모두 `purpose: database`로 등록합니다.

```json
{
  "profile_id": "ha-demo", "mode": "patroni",
  "nodes": [
    {"target_id": "db1", "roles": ["database", "dcs"]},
    {"target_id": "db2", "roles": ["database", "dcs"]},
    {"target_id": "db3", "roles": ["dcs", "proxy"]}
  ],
  "placements": [{"provider": "aws", "site": "ap-northeast-2", "database_nodes": 2, "dcs_voters": 3, "proxy_nodes": 1}]
}
```

profile의 필수 필드는 `parameters`(위에서 profile_id만 제외), `cluster_name`, `client_cidrs`(승인 RFC1918 CIDR 목록), `certificates`, `vault_file`, `vault_password_file`, `timeout_seconds`입니다. HTTP topology와 profile topology가 정확히 일치해야 합니다. 역할·노드 목록 순서만 달라도 같은 배치로 비교합니다.

`certificates`는 target_id별 실제 인증서/개인키 절대경로 맵입니다. Jinja 경로 템플릿이나 임의 Ansible 변수는 받지 않습니다. 역할을 겸하면 필요한 키의 합집합을 제공합니다.

| 역할 | 필수 certificate 참조 키 |
|---|---|
| dcs | `etcd_ca_src`, `etcd_cert_src`, `etcd_key_src` |
| database | `etcd_ca_src`, `patroni_etcd_cert_src`, `patroni_etcd_key_src`, `patroni_api_ca_src`, `patroni_api_cert_src`, `patroni_api_key_src`, `postgres_tls_ca_src`, `postgres_tls_cert_src`, `postgres_tls_key_src` |
| proxy | `patroni_api_ca_src` |

profile·인증서·키·Vault·Vault password·SSH 참조는 모두 실행기 소유 0600 파일로 준비합니다. Vault는 Ansible Vault 암호화 파일이며 복호화 결과는 `vault_postgres_password`, `vault_replication_password`, `vault_patroni_api_password` 세 문자열(각 16자 이상)만 허용합니다. 키·비밀번호·파일 경로는 HTTP 요약이나 작업 결과에 포함하지 않습니다. 인증서 SAN/CA·유효기간과 실제 연결은 운영자가 준비하고 팀 native TLS 검사로 확인합니다.

선택적으로 profile에 `backup_enabled: true`, `pgbackrest_repo_path: "/mnt/pgbackrest"`, `retention_full: 1`을 함께 지정하면 팀 백업 역할을 실행합니다. 볼륨 준비·mount는 별도 선행 작업이고 이 API가 새 디스크를 만들거나 포맷하지 않습니다. 기본은 backup 비활성입니다. 복원은 별도 승인된 격리 대상에서 팀 `playbooks/restore.yml`로 검증하며 이 API에 임의 restore/playbook 입력을 추가하지 않습니다.

접수 시 profile·TLS·Vault·SSH 파일 내용 해시를 요청에 묶습니다. 실행 직전 다시 대조하고 비공개 임시 사본으로 실행하므로 접수 후 파일이 바뀌면 `DATABASE_PROFILE_CHANGED`로 차단합니다. timeout·Ansible 실패·receipt 부재는 원격 변경 여부를 확인할 수 없는 `unknown`입니다. native 상태를 대조한 운영자만 격리된 후속 검증/복구를 결정하며 자동 재실행하지 않습니다.

etcd는 Patroni가 사용하는 v3 JSON gateway를 명시적으로 활성화합니다. gRPC `etcdctl endpoint health`와 별도로 mTLS HTTP `/v3/cluster/member/list`의 성공 응답과 전체 member 이름을 확인합니다. 초기화된 etcd의 설정 차이는 기존 유지보수 guard가 차단하므로, 운영자 검토와 스냅샷 후 member별 재시작·quorum 확인이 필요합니다. unknown 이후 수동 재개가 승인되면 원본 기록을 보존하고 모든 기존 worker·터널의 종료 및 실제 상태 대조 증거를 남긴 뒤 별도 검증 state 디렉터리와 새 요청 ID를 사용합니다. 이는 자동 재시도나 기존 실패 기록의 삭제 기능이 아닙니다.

## F. Integration / Verification

### F-1. 회의 요구사항과 이번 반영

근거는 [2026-10-01 Slack 허들 전문](https://softbankhackathon2026.slack.com/files/USLACKBOT/F0C63236BL1/___________________)의 자동 전사입니다. 원문 MD와 해시를 연구 자료에 보존했으며 이번에 오디오를 대조한 것은 아닙니다.

| 원문 위치 | 확인한 요구 | 반영 |
|---|---|---|
| 2:45:09 이후 | DB/Patroni 설치와 Kubernetes 배치 논의 | DB를 별도 VM으로 설명하고 standalone 미지원과 승인 HA 실행 구분 |
| 2:49:49–2:50:07 | Web/MCP 요청이 Ansible을 호출하고 변수 전달 필요 | 작업별 HTTP 입력→검증→inventory·extra-vars 매핑 |
| 2:50:14–2:51:07 | Provider별 배치·개수를 전달할 명세 요청 | nodes의 역할과 provider/site별 placements를 대조 |
| 2:53:19–2:53:42 | DB 담당 범위 확인 | 담당 플레이북을 새로 대신 구현하지 않고 연결 경계 보존 |
| 최신 사용자 지시와 DB 담당 README | DB는 K3s와 별도 클러스터이며 원본 HA 구현이 기준 | 단일 Patroni HA 클러스터의 다중 거점 배치·K3s 설치 없음·검증용 개수와 운영 배치 구분 |

현재 상위 API의 자동 자원 등록·배포 전체 오케스트레이션까지 연결된 것은 아닙니다. 이번 변경은 승인된 자원을 Ansible 입력으로 변환하는 연결부, 내부 HTTP 계약, 기존 runtime 실행과 승인 DB HA 실행을 대상으로 합니다.

### F-2. 브랜치·시험·병합

최초 계약 브랜치는 `feature/ansible-integration-contract`, HA 실행 연결은 `feature/database-execution-20261002`, 기준은 `integration/team-assembly-20261002`입니다. 별도 worktree에서 작업하며 팀원 구현과 개인 작업 공간의 미커밋 변경을 보존합니다. PR로 검토한 뒤 integration에 merge commit으로 반영하고, squash·rebase·force push는 사용하지 않습니다.

```bash
python3 -m unittest discover -s infrastructure/ansible -p 'test_*.py'
```

검사는 OpenStack 상세 응답의 ID·프로젝트·관리 주소, 잘못된 변수 거부, HTTP 검증/접수/조회, 변수의 공유 실행 함수 전달, 중복 방지, DB 배치·profile 일치·용도 분리·모든 physical target 잠금·unknown 결과 차단을 다룹니다. 실제 로컬 Vault 암복호화와 자식 프로세스 timeout 정리, 선행 Ansible 실패 시 receipt 차단도 검사합니다. HTTP 시험은 localhost 서버를 실제로 띄우며 원격 Ansible 실행만 모의 처리합니다. Ansible inventory parser와 `guest.yml`, `runtime.yml` syntax-check도 별도로 확인합니다.

이 검사는 실제 OpenStack/CSP VM 설치나 DB 서비스 가동의 증거가 아닙니다. 실제 대상에서는 사설 경로·SSH 호스트키·guest 상태·패키지 접근·runtime 준비를 확인해야 하고, DB 플레이북 연결 후에는 앱에서 인증된 DB 연결과 백업 복구를 별도로 검증해야 합니다.

### F-3. 실행기 설정과 기존 CLI

등록 파일·Provider 응답 파일·Bearer 파일은 실행기 소유 0600 파일로 관리합니다. 개인키는 group/other 권한 없이, known_hosts는 group/other 쓰기 권한 없이 준비하고 신뢰된 경로로 호스트키를 확인합니다. 실제 토큰·키는 예제에 포함하지 않습니다.

```bash
python3 infrastructure/ansible/api.py \
  --targets-file /secure/railshot/ansible-targets.json \
  --token-file /secure/railshot/ansible-api-token \
  --state-dir /secure/railshot/ansible-jobs \
  --listen 127.0.0.1 --port 4180
```

POST는 `application/json`, 1–8192바이트의 Content-Length 본문을 사용합니다. query string·후행 `/`·CORS·동적 등록·취소·삭제는 지원하지 않습니다. HTTP 상세 헤더·오류는 OpenAPI를 기준으로 합니다.

기존 직접 CLI는 [내부 요청 Schema](../../contracts/ansible-request.schema.json)와 [runtime 예제](../../examples/ansible/runtime-single-node.json)를 유지합니다. `--validate-only`는 입력 검사만 수행합니다. 기존 `patroni.install` CLI는 계속 차단합니다. 승인 profile을 사용하는 HA 실행은 HTTP `database.configure`로만 접수합니다.

```bash
python3 infrastructure/ansible/run.py \
  --request examples/ansible/runtime-single-node.json --validate-only
```

OpenStack은 실행기에서 사설 주소까지 운영자가 준비한 경로로 SSH에 도달해야 합니다. WireGuard는 지원 선택지에서 제거했습니다. AWS SSM·GCP IAP도 게스트의 SSH를 운반하는 관리 터널이며 SSH를 제거하는 방식이 아닙니다. `StrictHostKeyChecking=yes`를 유지하고 임의 ProxyCommand·ProxyJump·agent 전달은 차단합니다. 키 내용·클라우드 토큰을 Ansible 변수로 전달하지 않습니다.

일반 SIGTERM/SIGINT 종료는 새 접수를 닫고 현재 worker의 자체 제한 시간과 Ansible·SSM/IAP 정리까지 기다립니다. 서비스 관리자나 컨테이너의 종료 유예 시간은 `timeout_seconds`에 터널별 정리 여유(최대 5초씩)를 더한 값 이상으로 설정해야 합니다. SIGKILL·호스트 장애 시 자식 프로세스 정리는 보장하지 않습니다. 남은 intent는 재시작 시 `unknown`으로 처리하고 운영자가 실제 노드 상태를 대조하기 전 자동 재실행하지 않습니다.
