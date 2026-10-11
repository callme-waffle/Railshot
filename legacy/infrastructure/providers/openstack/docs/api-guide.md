# OpenStack REST Controller API 사용 안내

버전 0.1.0 · 작성일 2026-10-01

이 문서는 현재 구현된 FastAPI 컨트롤러를 호출하는 방법을 설명합니다. 기본 주소는 `http://127.0.0.1:8000`이며, API는 HTTP로 요청과 응답을 주고받는 인터페이스입니다. 생성·삭제·전원 제어는 요청 접수와 실제 완료를 구분합니다.

## 1. 실행 환경과 인증

설정 대상은 외부 OpenStack의 nate2402 프로젝트에 있는 가상머신에 설치한 **내부 DevStack의 demo 프로젝트**입니다. 외부 프로젝트의 인스턴스를 관리하는 설정이 아닙니다. 내부 서비스는 사설망 주소이므로 준비된 실행 명령이 SSH(암호화된 원격 접속) 터널과 로컬 API를 함께 실행합니다.

```sh
cd /Users/waffle/workspace/competition/2026-softbank/dev && uv run --frozen python scripts/run_local.py
```

실행 후 `http://127.0.0.1:8000/docs`에서 대화형 문서를 볼 수 있습니다. 종료할 때 Ctrl+C를 누르면 이 명령이 시작한 API와 SSH 터널이 함께 종료됩니다. 이미 8000 포트를 쓰는 프로그램은 먼저 별도로 확인해 주세요. 실행을 위해 10.26.2.180에 도달 가능한 네트워크가 필요합니다.

### API 호출자 토큰

`dev/.env`의 `CP_API_TOKEN`을 사용합니다. 이것은 Keystone 비밀번호가 아니라 이 컨트롤러에 접근하는 토큰입니다. 문서에는 실제 비밀 값을 싣지 않습니다. 아래 명령은 dev 폴더에서 실행하며, 토큰을 화면에 출력하지 않고 현재 셸 변수에 담습니다.

```sh
export API_TOKEN="$(.venv/bin/python -c 'from dotenv import dotenv_values; print(dotenv_values(".env")["CP_API_TOKEN"])')"
export API_BASE='http://127.0.0.1:8000'
curl -sS "$API_BASE/health/ready" -H "Authorization: Bearer $API_TOKEN"
```

모든 업무 요청에 `Authorization: Bearer <토큰>` 헤더가 필요합니다. 인증이 없거나 잘못되면401입니다. `/health/live`는 인증이 필요하지 않습니다. 현재 로컬 설정은 개발용 문서를 켰으므로 `/docs`와 `/openapi.json`도 인증 없이 열리지만, 실제 업무 API에는 토큰이 필요합니다.

<!-- pagebreak -->
## 2. 경로 목록과 공통 규칙

| 메서드 | 경로 | 성공 응답 |
|---|---|---|
| GET | /health/live | 200 프로세스 상태 |
| GET | /health/ready | 200 서비스 준비 상태 |
| GET | /api/v1/servers | 200 서버 목록 |
| POST | /api/v1/servers | 202 생성 접수 |
| GET | /api/v1/servers/{server_id} | 200 서버 상세 |
| DELETE | /api/v1/servers/{server_id} | 202 삭제 접수 / 204 이미 없음 |
| POST | /api/v1/servers/{server_id}/actions | 202 전원 작업 접수 |
| GET | /api/v1/images | 200 이미지 목록 |
| GET | /api/v1/flavors | 200 서버 사양 목록 |
| GET | /api/v1/networks | 200 기존 네트워크 목록 |

### 상태와 요청 추적

- 성공 본문은 JSON 형식입니다.204 응답에는 본문이 없습니다.
- 모든 응답의 `X-Request-ID` 헤더는 요청 추적용 고유 식별자입니다. 작업 ID나 중복 방지 키가 아닙니다.
- 202 응답의 `Location` 헤더는 상태를 조회할 서버 상세 경로입니다.
- 202는 OpenStack이 요청을 접수했다는 뜻이며 생성·삭제·전원 변경 완료를 보장하지 않습니다.
- 상태 문자열은 OpenStack이 제공하는 값을 유지합니다. 알 수 없는 상태를 오류로 단정하지 마세요.
- 요청 본문의 알 수 없는 필드는422로 거부합니다. 프로젝트·클라우드 접속 정보는 요청에 넣지 않습니다.

### 목록 페이지 처리

서버·이미지·사양·네트워크 목록에 공통 적용됩니다. `limit` 기본값은20, 허용 범위는1~100입니다. `marker`는 직전 응답의 `next_marker`를 그대로 전달합니다. 알 수 없는 목록 쿼리와 중복 쿼리는422입니다.

```json
{"items": [], "next_marker": null}
```

`next_marker`가 null이면 추가 페이지가 없습니다. marker를 다른 프로젝트나 다른 종류의 목록에 재사용하지 않습니다. 조회 도중 자원이 바뀌면 고정된 목록 스냅샷은 보장하지 않습니다.

```sh
curl -sS "$API_BASE/api/v1/servers?limit=20" \
  -H "Authorization: Bearer $API_TOKEN"
```

<!-- pagebreak -->
## 3. 인스턴스 생성

`POST /api/v1/servers` · 인증 필요 · 성공202

요청은 인스턴스 한 개를 생성합니다. 먼저 이미지·사양·네트워크 목록에서 사용할 실제 ID를 확인합니다. 아래 ID는 설명을 위한 예시이므로 실제 조회 결과로 바꾸어야 합니다.

| 필드 | 형식 | 규칙 |
|---|---|---|
| name | 문자열 | 필수. 양끝 공백 제거 후1~255자 |
| image_id | 문자열 | 필수. 접근 가능한 활성 이미지 ID |
| flavor_id | 문자열 | 필수. 사용할 기존 사양 ID |
| network_ids | 문자열 배열 | 필수. 기존 네트워크 ID 최소1개, 중복 금지 |

```json
{
  "name": "web-01",
  "image_id": "image-id",
  "flavor_id": "flavor-id",
  "network_ids": ["network-id"]
}
```

```sh
curl -i -X POST "$API_BASE/api/v1/servers" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"web-01","image_id":"image-id","flavor_id":"flavor-id","network_ids":["network-id"]}'
```

접수 응답 예시입니다. `Location: /api/v1/servers/server-id`도 함께 반환합니다.

```json
{
  "resource_id": "server-id",
  "action": "create",
  "status": "accepted",
  "request_id": "9cd16c11-e380-4c48-8b97-d9dbdfbdf200"
}
```

이름이 같다고 중복 요청이 차단되지는 않습니다. 생성 응답이 시간 초과되거나 연결이 끊겼다면 먼저 자원 상태를 확인하세요. 무조건 재전송하면 중복 인스턴스가 만들어질 수 있습니다. 생성 시 관리자 비밀번호를 반환하지 않습니다.

이미지·사양·네트워크가 유효하지 않으면422, 권한 부족은403, 상태 충돌이나 확인된 할당량 초과는409입니다. 외부 통신 오류의 공통 규칙은7절을 참고하세요.

<!-- pagebreak -->
## 4. 인스턴스 목록과 상세 조회

`GET /api/v1/servers`와 `GET /api/v1/servers/{server_id}` · 인증 필요 · 성공200

목록은 `items`와 `next_marker`로 감싸고, 상세는 서버 객체를 직접 반환합니다. 현재 인증 프로젝트의 서버만 대상으로 합니다. 다른 프로젝트의 서버나 존재하지 않는 서버를 상세 조회하면404입니다.

| 서버 필드 | 형식 | 의미 |
|---|---|---|
| id | 문자열 | 서버 식별자 |
| name | 문자열 | 서버 이름 |
| project_id | 문자열 | 소유 프로젝트 ID |
| status | 문자열 | 공급자 상태. 예: BUILD, ACTIVE, SHUTOFF, ERROR |
| addresses | 배열 | 네트워크별 주소. 아직 없으면 빈 배열 |

addresses의 각 항목은 `network`(네트워크 이름), `address`(주소 문자열), `version`(4 또는6)입니다.

```json
{
  "id": "server-id",
  "name": "web-01",
  "project_id": "project-id",
  "status": "ACTIVE",
  "addresses": [
    {"network": "private", "address": "10.0.0.16", "version": 4}
  ]
}
```

```sh
curl -sS "$API_BASE/api/v1/servers/server-id" \
  -H "Authorization: Bearer $API_TOKEN"
```

### 완료 확인 방법

생성 후에는 상세 조회를 일정 간격으로 반복하여 BUILD에서 ACTIVE 또는 ERROR 등으로 바뀌는지 확인합니다. 요청마다 서버 식별자와 실패 원인을 보관하세요. 클라이언트는 자체 대기 제한 시간을 두어 무한 대기를 피해야 합니다.

ACTIVE는 가상머신 활성 상태입니다. 운영체제 초기 설정 완료, 컨테이너 설치 완료, 웹 서비스 접속 성공을 뜻하지 않습니다. 현재 API는 애플리케이션 설치나 외부 접속 주소 할당을 수행하지 않습니다.

<!-- pagebreak -->
## 5. 전원 제어와 삭제

### 전원 제어

`POST /api/v1/servers/{server_id}/actions` · 인증 필요 · 성공202

| action | 의미 | 이후 관찰할 상태 |
|---|---|---|
| start | 시작 요청 | ACTIVE |
| stop | 정지 요청 | SHUTOFF |
| reboot | 일반 재부팅 요청 | 상태 변화만으로 재부팅 완료를 확정할 수 없음 |

```json
{"action": "stop"}
```

```sh
curl -i -X POST "$API_BASE/api/v1/servers/server-id/actions" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"action":"stop"}'
```

응답은 생성과 같은 접수 형식이며 `action`만 start/stop/reboot로 달라집니다. 강제 재부팅은 지원하지 않습니다. 서버가 없으면404, 현재 상태에서 작업할 수 없으면409, 허용하지 않는 action은422입니다.

### 삭제

`DELETE /api/v1/servers/{server_id}` · 인증 필요

```sh
curl -i -X DELETE "$API_BASE/api/v1/servers/server-id" \
  -H "Authorization: Bearer $API_TOKEN"
```

- 삭제가 접수되면202이며 `action`은 delete입니다.
- 서버가 이미 없으면204이며 본문은 없습니다.
- 접수 후 상세 조회가404가 되는지 확인해야 실제 소멸을 판단할 수 있습니다.
- 다른 프로젝트 서버의 존재가 확인되면404입니다. 통신·인증 실패를204로 처리하지 않습니다.
- 삭제와 동시에 전체 네트워크·공유 자원을 정리하지 않습니다. 사용자가 의도한 서버 ID만 지정하세요.

<!-- pagebreak -->
## 6. 이미지와 사양과 네트워크 조회

모두 인증이 필요하며 성공200입니다. 응답 외형은 `items`와 `next_marker`입니다. 자원 이름 대신 반환된 ID를 생성 요청에 사용하세요.

| 경로 | 항목의 필드 | 설명 |
|---|---|---|
| /api/v1/images | id, name, status | 현재 인증 범위에서 조회 가능한 이미지 |
| /api/v1/flavors | id, name, vcpus, ram_mb, disk_gb | 기존 서버 사양. 메모리는 MB, 디스크는 GB |
| /api/v1/networks | id, name, status, shared | 접근 가능한 기존 네트워크. shared는 공유 여부 |

```sh
curl -sS "$API_BASE/api/v1/images?limit=20" \
  -H "Authorization: Bearer $API_TOKEN"
curl -sS "$API_BASE/api/v1/flavors?limit=20" \
  -H "Authorization: Bearer $API_TOKEN"
curl -sS "$API_BASE/api/v1/networks?limit=20" \
  -H "Authorization: Bearer $API_TOKEN"
```

사양 목록 응답 예시입니다.

```json
{
  "items": [
    {
      "id": "flavor-id",
      "name": "small",
      "vcpus": 2,
      "ram_mb": 2048,
      "disk_gb": 20
    }
  ],
  "next_marker": null
}
```

vcpus는 가상 CPU 개수, MB와 GB는 메모리·디스크 크기를 표시하는 단위입니다. 값은 공급자가 반환한 사양 수치를 사용합니다. 이미지는 목록에 보이더라도 활성 상태가 아니면 생성 참조 검증에서 거부될 수 있습니다. 공유 이미지·네트워크는 프로젝트 소유자가 달라도 정책상 사용할 수 있습니다.

### 상태 확인

`GET /health/live`는 외부 호출 없이 프로세스 상태를 확인합니다. `GET /health/ready`는 인증과 필수 서비스 접근을 확인하며 정상일 때 `{"status":"ok"}`를 반환합니다. 외부 접근에 실패하면503입니다. ready는 개별 연결·읽기 제한을 적용하며 요청 전체의 엄밀한 시간 상한은 보장하지 않습니다.

<!-- pagebreak -->
## 7. 오류 응답과 재시도

오류 본문의 공통 형식입니다. 입력 검증의 원문이나 OpenStack 자격증명은 반환하지 않습니다.

```json
{
  "error": {
    "code": "UPSTREAM_TIMEOUT",
    "message": "외부 서비스 응답 시간이 초과되었습니다.",
    "request_id": "9cd16c11-e380-4c48-8b97-d9dbdfbdf200",
    "retryable": false,
    "outcome_unknown": true
  }
}
```

| HTTP | code | 의미 |
|---|---|---|
| 401 | UNAUTHENTICATED | 호출자 토큰 누락·불일치 |
| 403 | FORBIDDEN | 허가되지 않은 작업 또는 범위 불일치 |
| 404 | NOT_FOUND | 자원·경로 없음 또는 접근 대상 밖 |
| 405 | METHOD_NOT_ALLOWED | 지원하지 않는 HTTP 메서드 |
| 409 | CONFLICT / QUOTA_EXCEEDED | 상태 충돌 / 식별된 할당량 초과 |
| 422 | INVALID_INPUT | 요청 필드·값·참조 입력 오류 |
| 429 | RATE_LIMITED | 외부 요청 빈도 제한 |
| 500 | INTERNAL_ERROR | 예상하지 못한 내부 오류 |
| 502 | UPSTREAM_FAILURE | 외부 인증 설정·응답 처리 등 오류 |
| 503 | UPSTREAM_UNAVAILABLE | 외부 연결·필수 서비스 사용 불가 |
| 504 | UPSTREAM_TIMEOUT | 외부 응답 시간 초과 |

retryable은 같은 요청의 재호출이 안전한지에 대한 안내이며, 재시도 성공 보장이 아닙니다. outcome_unknown은 변경 요청의 반영 여부를 확정하지 못했음을 뜻합니다. 변경 요청에서 true가 반환되면 자동 재전송하지 말고 현재 자원을 확인하세요.

문제 조사 시 HTTP 상태, code, request_id, 수행한 메서드·경로를 전달하면 됩니다. 토큰·비밀번호·전체 요청 헤더는 공유하지 마세요. 서버 준비 상태503은 먼저 SSH 경로와 OpenStack 서비스 응답을 확인해야 합니다.

<!-- pagebreak -->
## 8. 사용 시나리오와 검증 범위

### 한 개 생성

이미지·사양·기존 네트워크 선택 후 POST /servers를 한 번 호출합니다. 기본값을 자동 선택하는 기능은 없으므로 호출자가 세 종류의 ID를 모두 채웁니다. 반환된 resource_id로 상세를 조회합니다.

### 세 개 생성

단일 생성 요청을 세 번 호출하고 각 ID와 결과를 따로 저장합니다. count=3 필드는 지원하지 않습니다. 일부만 성공할 수 있으며 자동 전체 취소 기능은 없습니다. 취소를 원하면 성공한 서버 ID만 명시적으로 삭제하고 정리 결과를 확인합니다.

### 지정 사양으로 생성

GET /flavors에서 조건에 맞는 사양을 선택해 flavor_id로 전달합니다. CPU·메모리 숫자를 생성 본문에 직접 넣거나 새로운 사양을 만드는 기능은 없습니다.

### 특정 네트워크 연결

GET /networks로 조회한 기존 네트워크 ID를 network_ids에 넣습니다. 여러 ID도 전달할 수 있으나 실제 허용 여부는 OpenStack 정책을 따릅니다. 새 네트워크·서브넷·외부 IP·고정 내부 주소·보안 규칙 구성은 현재 범위가 아닙니다.

### 읽기 전용 연결 확인

실행 중인 API에 대해 dev 폴더의 보조 스크립트를 사용할 수 있습니다. 이 스크립트는 상태와 목록만 조회하며 자원을 생성하거나 삭제하지 않습니다.

```sh
CP_API_TOKEN="$API_TOKEN" uv run --frozen python scripts/check_api.py
```

이번 설정 검증은 실제 DevStack 인증과 읽기 요청까지만 수행합니다. 생성·삭제·전원 제어의 실제 환경 시험은 수행하지 않았습니다. 기존 단위·계약·모의 통합 시험과 실제 환경 읽기 시험은 서로 구분합니다.

### 파일과 설정

- `.env`: API 토큰 및 OpenStack 연결 정보. Git 제외, 소유자만 읽기·쓰기 가능.
- `.local/`: 기존 SSH 키·확인된 호스트 키·로컬 접속 설정. Git 제외.
- `docs/openapi.json`: 현재 코드에서 생성한 기계 판독용 API 명세.
- `docs/validation.md`: 기존 구현 검증 기록. 실제 환경 추가 검증은 별도 항목으로 기록.

API는 로컬127.0.0.1에서만 수신합니다. 현재 실행 명령은 클라우드 보안 그룹을 변경하거나 서버에 API를 상시 배포하지 않습니다. 작업 이력 저장·중복 요청 방지·서비스 배포 자동화는 후속 기능입니다.
