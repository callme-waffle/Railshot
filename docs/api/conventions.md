# Railshot REST API 컨벤션

2026-10-02 · 로컬 개발 기준 · **현행 제품 계약은 [제품 API](product.md)와 [OpenAPI](product.openapi.json)를 따른다.** 기존 내부 실행 계약은 제품 경계에서 변환한다.

## 1. 기준 구현과 적용 범위

화균 님의 **OpenStack REST Controller** 형식을 기준으로 한다. [통합 출처](../integration/source-map.json)는 원본을 `Jasmin/feature/poc-onprem-hwagyun@e308749cc78408c3d933ea76aa06ab982045450b`, 담당자를 `hwagyun`으로 기록한다. 확인한 통합본은 `f35ffda3825681f292a62bde3db4da0f4b35009c`이다. 별도 `feature/multicloud-db-hwagyun@67d19ef`는 Ansible 배포 변수 계약이며 REST 응답 형식의 출처가 아니다.

| 근거 | 재사용하는 규칙 |
|---|---|
| [router.py](../../infrastructure/providers/openstack/src/control_plane/api/router.py) | `/api/v1`, 복수형 자원, HTTP 메서드, 목록 페이지, `202 + Location` |
| [schemas.py](../../infrastructure/providers/openstack/src/control_plane/api/schemas.py) | 요청/응답 모델 분리, 엄격한 입력, 직접 자원 응답, 접수·목록·오류 형식 |
| [auth.py](../../infrastructure/providers/openstack/src/control_plane/auth.py) | 서버 생성 요청 ID, 응답 `X-Request-ID`. OpenStack의 사용자 인증 모델을 제품에 복제하지 않음 |
| [error_handlers.py](../../infrastructure/providers/openstack/src/control_plane/api/error_handlers.py) | 공통 오류 객체, 안전한 메시지, `405` 허용 메서드. 내부 운영자 모드의 `401` 인증 안내 |
| [OpenAPI](../../infrastructure/providers/openstack/docs/openapi.json), [HTTP 테스트](../../infrastructure/providers/openstack/tests/unit/api/test_http.py) | 선언과 실제 HTTP 응답을 함께 확인 |

위 구현의 HTTP 형식을 신규 제품 API에 적용한다. Node 서비스를 FastAPI로 바꾸거나 Python 계층 구조·Protocol을 그대로 복제할 필요는 없다. 기존 Ansible·OpenStack 내부 계약은 담당 구현을 유지하고 제품 API 경계에서 변환한다.

공유 workspace와 기존 해커톤 대시보드는 익명 쿠키 세션을 유지한다. 새 온프레미스 인프라 등록은 [계정·연결 API](onprem-accounts.md)의 이메일·비밀번호 로그인 계정 ID에 귀속한다. 기존 개인 환경의 장기 소유자 쿠키와 복구키는 [개인 환경 계약](personal-environments.md)에 따라 기존 대상에만 적용되며 새 계정으로 자동 이전되지 않는다. 운영자 지정 공용 대상은 공유한다. 운영자만 target·profile·실행 도구·자격을 설정하며, 내부 서비스 인증과 Host/Origin 검사는 사용자 로그인과 별개다.

## 2. REST 원칙·HTTP 표준·로컬 이름 규칙

REST는 아키텍처 스타일이며 경로의 복수형·대시·밑줄·버전 prefix를 강제하는 단일 명명 표준은 없다. [Fielding 원문 5.1](https://ics.uci.edu/~fielding/pubs/dissertation/rest_arch_style.htm#sec_5_1)은 클라이언트/서버 분리, 무상태 요청, 캐시 여부, 일관된 인터페이스와 계층을 설명한다. 일관된 인터페이스에는 자원 식별, 표현을 통한 조작, 자체 설명 메시지, 하이퍼미디어를 통한 상태 전이가 포함된다. **경로 이름만 바꿔서 REST 전체를 충족했다고 판단하지 않는다.**

[RFC 3986 §2.3](https://www.rfc-editor.org/rfc/rfc3986.html#section-2.3)은 `-`와 `_`를 모두 URI에서 사용할 수 있는 비예약 문자로 정의한다. Railshot에서는 사용자 요청에 따라 **대시·밑줄로 내부 용어를 조합하지 않고, 제품 자원을 나타내는 짧은 명사의 복수형**을 경로에 쓴다. 이것과 `/api/v1`은 팀 규칙이며 RFC의 의무로 표현하지 않는다.

HTTP 메서드·상태는 [RFC 9110 §9·§15](https://www.rfc-editor.org/rfc/rfc9110.html#name-methods), PATCH는 [RFC 5789 §2](https://www.rfc-editor.org/rfc/rfc5789.html#section-2)를 따른다. 아래 접수·목록·오류 JSON 외형은 화균 님 구현을 재사용한 제품 규약이며 HTTP 표준이 그 JSON 필드를 강제하는 것은 아니다.

- 신규 업무 API prefix는 `/api/v1`이다. 경로는 `targets`, `builds`, `deployments`, `profiles`, `plans`, `environments`처럼 **소문자 단일 명사의 복수형**으로 정한다. 실행기 이름·처리 단계를 이어 붙인 이름을 구분자나 camelCase만 바꿔 채택하지 않는다. 실제 종속 자원이 생길 때만 `/resources/{id}/children` 관계를 사용한다.
- 목록·생성은 `/resources`, 상세는 `/resources/{id}`이다. `/createDeployment`, `/getStatus`, `/deploy` 같은 동사 경로를 새로 만들지 않는다. `Content-Type`, `X-Request-ID`, `Retry-After`, `Idempotency-Key` 같은 HTTP 헤더명과 이미 발급된 불투명 자원 ID는 경로 이름 규칙으로 변조하지 않는다.
- `GET`은 조회만, `POST`는 자원 생성·작업 접수, `PATCH`는 허용 필드 부분 수정, `PUT`은 전체 교체, `DELETE`는 삭제다. 수정·삭제 요구와 실행 계약이 없으면 메서드를 추가하지 않는다. GET 본문에 실행 인자를 넣지 않는다.
- `GET`은 안전한 조회, `PUT/DELETE`는 의도한 효과에 대한 멱등성을 유지한다. 삭제 반복 시 202 다음 204처럼 응답이 달라도 멱등성이 깨지는 것은 아니다. `POST/PATCH`를 자동으로 멱등하다고 가정하지 않는다. PATCH를 도입할 때는 지원 patch media type·원자적 적용·동시 변경 조건도 계약에 포함한다.
- 기존 OpenStack 전원 제어처럼 CRUD로 표현하기 어려운 작업은 `POST /resources/{id}/actions`와 허용된 `action` 값을 사용한다. 이 예외를 일반 배포 생성이나 임의 명령 실행 통로로 확대하지 않는다.
- JSON 필드와 query parameter는 `snake_case`, 오류 코드는 `UPPER_SNAKE_CASE`다. 자원 자신의 식별자는 `id`, 다른 자원 참조는 `target_id`, `plan_id`처럼 쓴다. ID는 비어 있지 않은 불투명 문자열이며 UUID로 일괄 제한하지 않는다.
- 제품 상태는 소문자로 정의한다. 공급자 원본 `ACTIVE`, `BUILD` 등을 바꾸거나 제품 성공 상태로 치환하지 않는다. 시간은 UTC 문자열(`2026-10-02T09:00:00Z`)로 반환한다.
- 일반 본문은 `application/json`, 소스 업로드는 기존 `multipart/form-data`를 사용한다. ZIP·파일을 JSON base64로 다시 감싸지 않는다.
- 요청의 알 수 없는 필드, 중복 단일 필드, 알 수 없는 query와 중복 query는 `422 INVALID_INPUT`이다. 스키마에서 반복을 선언한 multipart `files`만 예외다. JSON의 잘못된 문법은 `400 INVALID_INPUT`, 지원하지 않는 Content-Type은 `415 UNSUPPORTED_MEDIA_TYPE`, 업로드 한도 초과는 `413 PAYLOAD_TOO_LARGE`로 정한다. 마지막 세 매핑은 제품 API에 추가하는 규칙이다.
- CI의 기존 tenant 값, Provider account/project, target·profile·자격은 서버 설정으로 결정한다. 실행 요청 본문으로 이 값을 바꾸거나 임의 Provider URL·명령·자격·로컬 경로를 지정하지 못한다. 별도 connections 자원은 OpenStack 콘솔 링크·접속 ID·암호화할 비밀번호만 보관하며 실행 설정이나 서버의 외부 접속에 사용하지 않는다. 이름·파일 경로·업로드 크기의 기존 검증 한도를 재사용한다.

각 요청은 대상 자원·필요 입력을 스스로 제공한다. 이전 화면에서 선택한 target을 서버의 숨은 대화 상태로 추측하지 않는다. 배포·계획·job 기록을 자원으로 영속 저장하는 것은 이 요청 독립성과 구분한다. 제품 API의 동적 응답은 [RFC 9111의 no-store](https://www.rfc-editor.org/rfc/rfc9111.html#section-5.2.2.5)에 따라 `Cache-Control: no-store`로 캐시 정책을 명시한다. 공개 정적 Dashboard 자산의 캐시는 별도 배치 규약이다.

클라이언트는 202의 `Location`을 상태 조회 경로로 사용한다. 현재 설계의 연결 정보는 이 Location과 기존 `actions_url` 범위다. 상태별 허용 작업·링크 관계와 표현 형식까지 정의한 하이퍼미디어 계약은 아직 없으므로 이 문서는 **REST 원칙에 맞춘 자원 중심 HTTP 계약**으로 설명하며 완전한 REST 적합성 검증을 주장하지 않는다.

## 3. 성공 응답

상세는 자원 객체를 직접 반환한다. 공통 `{success, data, message}` 포장을 추가하지 않는다.

```json
{
  "id": "dep001",
  "app": "demo-web",
  "target_id": "demo-aws",
  "status": "running",
  "stage": "ci"
}
```

목록은 아래 형식을 사용한다. `limit` 기본 20, 범위 1–100, `marker`는 이전 응답의 `next_marker`다. marker를 다른 자원·조회 조건에 재사용하지 않는다. 처음 target 하나만 지원하더라도 같은 형식으로 반환하고 다음 페이지가 없으면 null을 쓴다. 필터·정렬은 실제 필요와 허용 목록을 정의한 뒤 추가하며 고정 스냅샷을 보장한다고 쓰지 않는다.

```json
{"items": [], "next_marker": null}
```

비동기 생성은 **화균 님의 `AcceptedResponse`를 그대로 따른다.** 접수 시 `202`, `Location`에 조회 경로, 본문에 `resource_id/action/status/request_id`를 반환한다. 제품 API에서는 `Retry-After: 2`도 제공한다. `status=accepted`는 HTTP 접수 결과이고 작업의 `queued/running/succeeded`는 이후 상세 조회에서 읽는다.

```http
HTTP/1.1 202 Accepted
Location: /api/v1/deployments/dep001
Retry-After: 2
X-Request-ID: 9cd16c11-e380-4c48-8b97-d9dbdfbdf200
Content-Type: application/json
```

```json
{
  "resource_id": "dep001",
  "action": "create",
  "status": "accepted",
  "request_id": "9cd16c11-e380-4c48-8b97-d9dbdfbdf200"
}
```

`201 Created`는 요청한 자원이 동기적으로 생성·저장됐을 때 사용한다. 예를 들어 환경 계획을 완성해 저장하면 `201 + Location`과 계획 객체를 반환한다. 계획 생성은 VM 준비 완료를 뜻하지 않는다. 비동기 작업을 접수만 했으면 `202`다. `204`에는 본문을 넣지 않는다. [HTTP 201·202·204 의미](https://www.rfc-editor.org/rfc/rfc9110.html#name-successful-2xx)를 따른다.

## 4. 요청 추적·중복 요청·재시도

| 식별자 | 용도와 생성 주체 |
|---|---|
| `X-Request-ID`, 응답의 `request_id` | HTTP 서버가 **요청마다 새 UUID**를 생성. 성공·오류·404·405에도 제공. 호출자의 헤더로 덮어쓰지 않음 |
| 자원 `id` / 접수 `resource_id` | 동일 자원을 조회할 때 유지. 제품 deployment ID와 GitHub run ID는 다른 자원 |
| `Idempotency-Key` | 클라이언트가 동일한 생성 의도를 재접수할 때 재사용. 인증·자원 ID·HTTP 추적 ID를 대신하지 않음 |
| 기존 Ansible `request_id` | 내부 API의 영속 job 식별자/중복 방지 값. 제품 HTTP `request_id`와 의미가 다르므로 제품 기록의 `ansible_job_id`로 매핑 |

제품 `POST /api/v1/deployments`와 `POST /api/v1/environments`에는 `Idempotency-Key`를 요구한다. 1–128자의 영문·숫자·`.`·`_`·`-`만 허용하고 익명 세션과 자원 종류별로 관리한다. 같은 키·같은 정규화 입력이면 같은 자원을, 다른 입력이면 `409 IDEMPOTENCY_CONFLICT`를 반환한다. 기존 자원이 queued/running이면 같은 `resource_id`의 202 접수 형식, succeeded/failed/blocked/unknown이면 같은 자원 객체의 200 형식과 Location을 반환한다. unknown을 다시 접수하거나 실행하지 않는다. 실행 worker가 종료된 unknown은 60초 뒤 전역 실행 슬롯을 반납하지만, 기록·중복 방지 키·저장 한도는 유지하고 같은 앱 또는 미확정 환경의 변경은 계속 차단한다. HTTP `request_id`는 매번 새 값이다.

재시작 후에도 키·소스 snapshot·입력 digest·외부 실행 식별자를 복구해야 한다. 원문 multipart boundary/ZIP 시각을 입력 의미로 비교하지 않는다. 미완료/unknown 기록을 자동 삭제하지 않는다. 배포와 업데이트는 기존 operations의 queue.sequence 순서대로 단일 worker가 처리한다. queue.enqueued_at은 소스 보관 완료, queue.started_at은 실행 claim 완료, queue.released_at과 release_reason=unknown_timeout은 실행 슬롯 반납을 뜻한다. 성공·실패 확정이나 외부 실행 취소를 뜻하지 않는다. 신형 queue가 있고 아직 claim되지 않은 배포만 재시작 후 자동 실행하며, 이미 시작한 배포는 unknown으로 복구해 재제출하지 않는다. 오래 대기한 계획과 업데이트 기준 revision은 실행 직전에 다시 검사한다. legacy builds의 실제 run_id 응답 및 환경·수명주기·resume 계약은 유지하며, 이 경로는 슬롯이 사용 중이면 접수 전 409를 반환한다.

기존 CI 제출과 OpenStack 생성에는 영속 중복 방지 계약이 없다. v1 CI 표현을 붙이는 것만으로 이를 지원한다고 쓰지 않는다. `X-Request-ID`를 넣었다고 자동 재전송이 안전해지지 않는다. 결과가 불확실하면 먼저 자원을 관측한다. `outcome_unknown=true`이면 `retryable=false`이며 변경 작업을 자동 재실행하지 않는다.

## 5. 오류 응답과 상태 코드

화균 님의 `ErrorResponse` 다섯 필드를 그대로 사용한다. 별도 `retry_policy`나 문자열 `error`를 신규 v1에 섞지 않는다. 작업의 실패 단계는 자원 응답의 `stage`에 둔다.

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

| HTTP | code / 처리 |
|---|---|
| 400 | `INVALID_INPUT`: 본문 문법 오류 |
| 401 | `UNAUTHENTICATED`: 별도 내부 운영자 모드의 Bearer 실패. `WWW-Authenticate: Bearer`. 공개 workspace의 사용자 로그인 응답이 아님 |
| 403 | `FORBIDDEN`: 허용되지 않은 Host·Origin 등 요청 경계 위반 |
| 404 | `NOT_FOUND`: 자원·경로 없음. 이 workspace에 기록되지 않은 외부 CI run도 조회하지 않음 |
| 405 | `METHOD_NOT_ALLOWED`: `Allow` 헤더 유지 |
| 409 | `CONFLICT`, `QUOTA_EXCEEDED`; 제품 추가 `IDEMPOTENCY_CONFLICT`, `EXECUTOR_BUSY`, `CAPABILITY_UNAVAILABLE` |
| 413 / 415 | `PAYLOAD_TOO_LARGE` / `UNSUPPORTED_MEDIA_TYPE` |
| 422 | `INVALID_INPUT`: 필드·타입·범위·참조 검증 실패 |
| 429 | `RATE_LIMITED`: 실제 요청 빈도 제한. 단일 worker가 사용 중이면 409이며 429와 구분 |
| 500 | `INTERNAL_ERROR`: 예상하지 못한 내부 오류 |
| 502 / 503 / 504 | `UPSTREAM_FAILURE` / `UPSTREAM_UNAVAILABLE` / `UPSTREAM_TIMEOUT` |

HTTP 조회가 성공했지만 작업이 실패한 경우 `GET`은 200이고 자원의 `status=failed`다. 조회 자체가 실패한 경우 위 오류 HTTP 상태를 사용한다. 알려진 미지원 기능은 기능 목록과 계획의 `blockers`로 표시하고 실행 접수를 차단한다. 제품 환경의 DB 실행은 미지원이며, 별도 내부 Ansible의 승인 HA 실행·standalone 차단은 [Ansible 계약](ansible.md)을 따른다.

`retryable`은 동일 요청 재호출의 안전 여부이며 성공 보장이 아니다. 같은 idempotency key의 제품 접수 재조회와 내부 Provider 부작용 재실행을 구분한다. 원문 validation 오류·SDK exception·stdout·토큰·개인키를 응답이나 로그에 내보내지 않는다.

## 6. 적용·호환·검증

구현된 제품 경로와 필드는 [제품 API](product.md)·[OpenAPI](product.openapi.json)가 정본이고, [CI 백엔드 설계](ci-backend-design.md)는 책임 경계와 감사 이력을 설명한다. `/api/deploy`, `/api/runs/{run_id}`, `/healthz`는 기존 호출자용으로 유지한다. legacy 업로드 POST를 HTTP redirect하지 않고 내부 입력 검사·실행·게시 검증과 영속 접수·run binding을 재사용한다.

라우트·요청/응답 모델·OpenAPI·HTTP 계약 테스트를 같은 변경에서 맞춘다. 본문뿐 아니라 `Location`, `X-Request-ID`, `Retry-After`, `Cache-Control` 응답 헤더도 OpenAPI에 표현한다. 기존 OpenStack snapshot에는 일부 런타임 헤더 선언이 없으므로 이를 이미 완비한 예제로 설명하지 않는다. REST 규약을 만들기 위해 아직 없는 모든 CRUD나 범용 작업 API를 추가하지 않는다. 제안·실행 코드·로컬 시험·실제 배포 결과를 구분한다.

구현 검증은 화균 님의 기존 테스트 패턴을 재사용한다: 정상 상태/본문/Location, 잘못된 입력의 외부 호출 0회, Host·Origin 및 미등록 대상·run 차단, 모든 오류의 동일 envelope와 X-Request-ID, 목록 범위·중복 query 거부, 결과 불확실 시 재실행 금지. 제품 idempotency는 저장 실패·중복 접수·재시작 복구까지 확인한다. 실제 실행 근거는 해당 소스·계약 테스트와 배포별 결과이며, 이 규약 문서나 모의 시험만으로 클라우드 E2E 완료를 주장하지 않는다.

큐 구현은 단일 API replica와 기존 SQLite transaction을 전제로 한다. Redis 등 별도 인프라는 추가하지 않는다. [SQLite transaction의 단일 writer](https://www.sqlite.org/lang_transaction.html), [BullMQ timeout의 취소 경계](https://docs.bullmq.io/patterns/timeout-jobs), [idempotent job 조건](https://docs.bullmq.io/patterns/idempotent-jobs)을 참고했다. 살아 있는 worker를 Promise timeout만으로 분리하여 다음 writer를 시작하지 않는다. 다중 API replica가 필요할 때 broker와 fencing을 함께 도입한다.
