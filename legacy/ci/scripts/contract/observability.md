# 관측성 규약과 변경 책임 — v1

이 규약은 **실제 관측을 근거로 복구할 수 있는 것**을 목표로 한다. 로그가 있다는 사실, SDK 종료, checkpoint 저장, HTTP 200을 전체 작업 성공으로 바꾸지 않는다. 공통 구현은 `platform/observability.py` 하나다. collector·별도 이벤트 프레임워크·provider별 오류 체계는 추가하지 않는다.

## 근거와 적용 범위

| 원천 규범 | 채택한 의미 | 적용 한계 |
|---|---|---|
| [OpenTelemetry Logs Data Model](https://opentelemetry.io/docs/specs/otel/logs/data-model/) | 사건 시각과 관측 시각, event name, severity, resource/component, correlation의 분리 | local JSON/SQLite 형식을 OTel에 매핑할 수 있게 한다. OTel SDK/exporter/Collector 설치나 distributed trace 구현을 뜻하지 않는다 |
| [OTel exception conventions](https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-logs/) | 예외의 타입과 원인 위치 보존, 오류와 사건 연계 | raw exception message/stack source는 secret을 포함할 수 있다. 타입·errno·함수/파일명/행만 공개 record에 넣는다 |
| [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html) | 안정된 problem 종류와 사람이 읽는 설명 분리; 디버그 내부 정보 노출 방지 | 현재 CLI error는 HTTP Problem Details 응답이라고 부르지 않는다. 미래 API에서 code→type URI, summary→title, event_id→instance를 명시적으로 매핑한다 |
| [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) | 누가/무엇을/언제/어디서 했는지, 상관관계, 입력 정제, 민감정보 제외, 로그 실패 시험 | regex 마스킹만으로 비밀 제거를 보장하지 않는다. 공개 사건은 승인된 구조 필드만 생성한다 |

현재 적용 대상은 local state/loop, Codex·Claude runner, deterministic gate, trusted publisher와 Terraform 관리자 CLI(`platform/infra/provision.py`) 경계다. 제품 API/Allow/SSE/외부 알림은 아직 없다. 그 경로는 구현 시 이 계약을 사용하고 별도 인수 증거를 남긴다.

## 공통 기록

`schema_version`, `event_id`, `event_name`, `occurred_at`, `observed_at`, `component`, `phase`, `outcome`, `severity`, `run_id`, `attempt_id`, `error`, `attributes`를 기록한다.

- local 사건은 생성 시각을 UTC ISO-8601로 기록한다. 외부 객체의 관측은 우리 관측 시각이고 실제 외부 사건 발생 시각은 모르면 `null`이다. 시계 순서로 인과관계를 단정하지 않고 SQLite sequence·operation/attempt ID와 함께 읽는다.
- `run_id`와 `attempt_id`는 실행 식별자다. OTel trace/span이 없으면 임의 UUID를 trace/span ID라고 표기하지 않는다. 외부 workflow ID·SDK session/thread/turn·외부 배포 객체 식별자은 별도의 원천 식별자다.
- 원격 실행을 아직 만들지 않았거나 관측하지 않았으면 ID는 `null`이다. 예시 값·샘플값으로 채우지 않는다.
- `phase`는 관측 범위다. 예를 들어 `sdk.invoke: PASS`는 그 호출의 성공이고, `gate: PASS`는 CI 계약 통과다. SDK `session.finished`와 DB `step.checkpointed`로 `deployment.verified`를 만들어서는 안 된다.
- `error`에는 등록된 `code`, 고정 `summary`, component/phase, outcome, `retry_policy`, `side_effect`, 안전한 `causes`가 있다. 프로그램은 code/enum을 읽고 영어 설명 문자열을 파싱하지 않는다. 임의 예외 문자열을 사용자에게 넘기지 않는다.

| outcome | 사용 조건 |
|---|---|
| RUNNING | 실행 의도를 저장했거나 단계가 진행 중. 완료 증거 아님 |
| PASS | 이 phase가 요구하는 검사를 실제 수행하고 충족한 증거가 있음 |
| FAIL | 실행된 검사 또는 외부 operation이 실패했다고 관측함 |
| BLOCKED | 설정·정책·권한·환경 문제로 필요한 검사를 수행할 수 없음 |
| UNKNOWN | 이미 시작한 외부 작업의 완료/부작용 여부를 확인할 수 없음 |
| NOT_RUN | 실행하지 않음. 관측 부재를 0이나 PASS로 바꾸지 않음 |
| INCOMPLETE | 부분 검사만 수행. release 승격 불가 |

`retry_policy`는 `never`, `after_reconcile`, `after_configuration`, `safe` 중 하나다. 이는 복구 안내이며 자동 retry 명령이 아니다. `side_effect`는 `none`, `possible`, `completed`, `unknown` 중 하나다. 네트워크 timeout·모델 응답 유실·commit 응답 유실은 부작용이 없었다는 증거가 아니다. 실패한 DB migration을 자동 재실행하거나 애플리케이션 rollback을 데이터 복구로 표시하지 않는다.

## 상태·증거·진단의 권위

1. **상태 정본:** `state.sqlite3`에 작업 의도·완료 checkpoint·event를 같은 transaction으로 기록한다. `flock`은 로컬 단일 writer만 보장한다. 분산 lease나 exactly-once 실행 보장이 아니다.
2. **검사 증거:** gate verdict, source/spec digest, immutable image identity와 publisher artifact 식별자을 보존한다. `evidence.json`은 checkpoint에서 생성하는 출력이고 정본을 별도로 갱신하지 않는다.
3. **진단:** failure classifier의 F-code·signature는 설명용 분류다. 실행 exit/check evidence를 대체하지 않는다. SDK 판단과 운영자 해석도 관측 사실과 분리한다.
4. **알 수 없음:** 관측 경로가 끊기면 healthy/성공을 유지해 현재 상태처럼 표시하지 않는다. 이전 성공 기록과 현재 관측 freshness는 별개다.
5. **무결성 한계:** 로컬 hash와 append-only SQLite trigger는 실수·일반 변경 탐지다. 같은 OS 관리자/파일 owner에 대한 서명·변조 불가 감사 저장소가 아니다. 외부 신뢰 경계를 넘을 때는 인증된 transport·접근 제어·독립 저장소가 추가로 필요하다.

## 보안과 저장 실패

- 사건에는 prompt, raw tool args/output, 전체 env, OAuth/API token, kubeconfig, Terraform state, 원본 `.env`, 임의 예외 메시지를 넣지 않는다. 원본 진단이 꼭 필요하면 관리자 전용 저장소에서 접근 권한·보존 기간을 따로 정한다.
- 예외는 `raise OperationError(...) from exc`로 내부 인과관계를 유지한다. 외부 record는 causal type, errno/exit code, 코드 위치만 직렬화한다. 코드 위치는 절대 경로를 제거하고 식별자를 길이 제한·허용 문자로 정규화한다. `<stdin>` 같은 Python 가상 위치도 같은 규칙으로 왕복하며, 원본 경로 복원이 필요한 경우 관리자 전용 증거를 읽는다. cause를 잃는 `except: pass` 또는 `from None`을 복구 분기에서 사용하지 않는다.
- 필수 state/evidence 기록이 실패하면 다음 부작용으로 진행하지 않는다. 디스크 가득 참, SQLite busy/corrupt, SDK event 저장 실패를 실제 오류로 반환한다. 선택적 metrics exporter 장애는 별도지만 현재 그런 exporter는 없다.
- private run root는 checkout·임시 폴더 밖에 두고 관리자만 읽게 한다(디렉터리 0700, 민감 record 0600). SQLite/checkpoint/원본 SDK receipt를 무조건 공개 CI artifact에 올리지 않는다.
- 오래된 작업의 파일을 새 작업 증거로 재사용하지 않는다. 다운로드·관측 출력은 run/job attempt별 새 경로를 쓰고 artifact 이름도 producer attempt와 결합한다.

## 소유권·변경·보존

| 책임 | 소유자/경계 | 승인·검사 |
|---|---|---|
| event/error schema·코드 registry | 플랫폼 관리자/플랫폼 코드 리뷰 | 같은 의미의 새 코드 중복 금지. 필드 삭제·의미 변경은 schema version 증가, 소비자 migration 검토 |
| producer의 사실성 | loop/gate/runner/CD/infra 모듈 담당 | 실제 원천 식별자·phase 범위·비밀 제외 회귀 검사 |
| 수명/권한·retention | 플랫폼 운영자 | 증거 보관 만료는 정책으로 정하고 DB와 원본 artifact를 함께 고려. 자동 삭제 기능은 현재 없음 |
| 완료·release 승격 | 결정론 gate/검증기 | LLM 문구·정규화 event 자체를 승인으로 사용하지 않음 |
| 자격/비용/파괴적 작업 Allow | 관리자·프로젝트 권한 정책 | 아직 제품 API 미구현. CI environment reviewer 설정으로 제품 Allow 구현을 주장하지 않음 |

코드·summary·원인 정보·recovery 정책 변경은 같은 PR에서 테스트와 이 계약의 적용 범위를 갱신한다. 배포 환경별 ID/주소/계정/저장 위치는 trusted 등록 설정으로 주입한다. 지원 프로파일·보안 상한·버전 pin·gate 순서는 이름 있는 공통 정책으로 유지하고 사용자 업로드가 바꿀 수 없게 한다. 설정화가 권한 확대 수단이 되어서는 안 된다.

## 검증 규약

필수 회귀는 cause-chain 보존/비밀 미출력, stable code 분기, UNKNOWN과 FAIL 분리, append-only event, 중복 writer, crash/resume, 기록 실패 차단, 입력/artifact drift, SDK 응답 유실, 잘못된 source/bundle 식별자 및 stale artifact 방지다. mock·정적·실프로세스·실클라우드 결과를 따로 보고한다. SDK mock 또는 YAML 검사를 live SDK/네트워크 E2E PASS로 승격하지 않는다.

현재 보존·삭제 자동화, 다중 worker fencing, 중앙 OTel collector, 제품 API의 Problem Details/SSE, 독립 감사 저장소는 **NOT_IMPLEMENTED**다. 필요 시 같은 event 계약에 연결하며 별도의 병렬 상태 체계를 만들지 않는다.
