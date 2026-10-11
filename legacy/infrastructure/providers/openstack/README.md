# OpenStack REST API Controller

Python FastAPI로 구현한 단일 프로젝트 범위의 OpenStack 제어 API입니다. 초기 범위는 서버 생성·조회·삭제·전원 제어와 이미지·사양·기존 네트워크 조회입니다. 상위 Provider Interface 명세가 확정되면 HTTP 변환 계층에서 맞출 수 있도록 내부 호출 규약을 분리했습니다.

코드 개발과 Git 이력은 이 dev 디렉토리에서 관리합니다. 초기 계획은 상위 control-plane 디렉토리, 구현 기준 계약은 [docs/contracts.md](docs/contracts.md)에 있습니다. 실제 내부 DevStack의 demo 프로젝트에 대한 인증·상태·목록 읽기를 확인했습니다. 실제 자원 생성·삭제 시험은 수행하지 않았습니다.

## 현재 준비된 환경 실행

```sh
uv run --frozen python scripts/run_local.py
```

이 명령은 로컬 API와 기존 SSH 접속을 이용하는 터널을 함께 실행합니다. 접속 정보는 Git에서 제외된 .env와 .local에 준비했습니다. 대상은 외부 nate2402 프로젝트에 설치된 내부 DevStack의 demo 프로젝트입니다. API 문서는 [Markdown 안내](docs/api-guide.md), [PDF 안내](output/pdf/openstack-api-guide.pdf), [OpenAPI 명세](docs/openapi.json)를 참고하세요.

## 설치 및 검사

Python 3.12와 uv(파이썬 실행 환경·의존성 관리 도구)를 사용합니다. uv.lock으로 검증한 의존성 버전을 고정합니다.

```sh
uv sync --python 3.12 --frozen
uv run --frozen ruff check src tests scripts
uv run --frozen ruff format --check src tests scripts
uv run --frozen mypy
uv run --frozen pytest -q
```

전체 시험에는 127.0.0.1의 임시 포트로 실제 HTTP 요청을 보내는 시험이 포함됩니다. 로컬 포트를 열 수 없는 환경에서는 해당 시험이 실패하며, 아래 명령으로 나머지 시험만 실행할 수 있습니다. 제외한 시험은 통과로 간주하지 않습니다.

```sh
uv run --frozen pytest -q -k 'not real_loopback_http_calls'
```

## OpenStack 없이 로컬 실행

```sh
uv run --frozen uvicorn tests.demo_app:create_app --factory --host 127.0.0.1 --port 8000
```

이 실행 방식은 테스트 전용 모의 공급자를 사용하며 실제 OpenStack에 접속하지 않습니다. 토큰은 local-demo-token, 프로젝트는 demo-project입니다. 서버 생성·전원·삭제 결과는 메모리에서 즉시 반영되며, 재시작하면 모두 사라집니다. 이 토큰과 실행 방식은 실제 서비스에 사용하지 않습니다.

다른 터미널에서 읽기 전용 확인을 실행합니다.

```sh
CP_API_TOKEN=local-demo-token uv run --frozen python scripts/check_api.py
```

생성 요청 예시입니다. 세 인스턴스가 필요하면 이름을 달리해 세 번 요청합니다. 일괄 생성의 원자성은 제공하지 않습니다.

```sh
curl -i http://127.0.0.1:8000/api/v1/servers \
  -H 'Authorization: Bearer local-demo-token' \
  -H 'Content-Type: application/json' \
  -d '{"name":"web-01","image_id":"image-test","flavor_id":"flavor-test","network_ids":["network-test"]}'
```

개발용 API 화면은 http://127.0.0.1:8000/docs 입니다. Authorize에 위 토큰을 입력합니다. 화면과 명세는 개발 옵션을 켠 경우에만 인증 없이 제공하며, 자원 API는 항상 토큰 인증을 요구합니다.

## 실제 OpenStack 연결 설정

```sh
cp .env.example .env
# .env를 실제 환경에 맞게 수정한 뒤 실행합니다.
uv run --frozen uvicorn control_plane.main:create_app --factory --env-file .env --host 127.0.0.1 --port 8000
```

- CP_PROJECT_ID에는 제어할 OpenStack의 실제 프로젝트 ID를 넣습니다. 준비된 로컬 설정은 내부 DevStack의 demo ID이며, 외부 nate2402의 ID와 다릅니다.
- Keystone 비밀번호 방식과 애플리케이션 자격증명 방식 중 하나만 설정합니다.
- CP_API_TOKEN은 이 제어 API의 호출자 토큰이며, Keystone 자격증명과 다릅니다.
- 서비스 주소는 Keystone에서 검색하고, 연결 대상의 프로젝트를 매 요청 검증합니다. OS_* 환경변수나 clouds.yaml을 자동으로 읽지 않습니다.
- 인증서 검증을 유지합니다. 자체 인증기관을 쓰면 CP_CA_CERT를 설정합니다.
- CP_DOCS_ENABLED의 기본값은 false입니다. 배포 전에 인증 주소·리전·인터페이스 및 Glance 접근을 확인해야 합니다.

.env는 Git에서 제외됩니다. 실제 비밀 값은 로그나 문서에 기록하지 않습니다. 실제 환경 시험은 읽기 확인 후 지정된 시험 범위에서 수행하고, 이번 실행에서 생성한 자원 ID만 정리해야 합니다.

## API와 응답

| 메서드 | 경로 | 결과 |
|---|---|---|
| GET | /health/live | 프로세스 상태, 인증 불필요 |
| GET | /health/ready | 인증 및 필수 서비스 읽기 확인 |
| GET / POST | /api/v1/servers | 목록 / 생성 접수 |
| GET / DELETE | /api/v1/servers/{id} | 상세 / 삭제 접수 또는 이미 없음 |
| POST | /api/v1/servers/{id}/actions | start, stop, reboot(SOFT) |
| GET | /api/v1/images | 이미지 목록 |
| GET | /api/v1/flavors | 서버 사양 목록 |
| GET | /api/v1/networks | 기존 네트워크 목록 |

생성·전원·삭제의202는 처리 접수이며 완료 보장이 아닙니다. 자원 상세로 상태를 확인합니다. 삭제 대상이 없으면204입니다. 요청 ID는 응답 헤더 X-Request-ID로 반환합니다. 목록은 limit(1~100), marker로 페이지를 조회합니다.

## 구현 경계와 한계

- api → application → ports 호출 규약을 사용하고, infrastructure/openstack이 규약을 구현합니다. domain에는 공통 모델과 오류만 있습니다.
- 공통 모델·규약을 선행 작성한 뒤 HTTP·업무·OpenStack 계층을 서브에이전트별로 병렬 구현했습니다.
- 영구 작업 이력·중복 방지 키·자동 변경 재시도는 구현 범위가 아닙니다. 생성 결과가 불명확하면 outcome_unknown=true로 반환하고 자동 재전송하지 않습니다.
- 외부 IP 할당, 새 네트워크·보안 그룹 구성, 볼륨, 부하 분산, 서비스 설치는 후속 기능입니다.
- ready는 호출별 짧은 시간 제한을 적용합니다. 전체 요청의 엄밀한 마감시간을 보장하지 않습니다.
- 인스턴스 활성 상태는 서비스 접속 가능이나 재부팅 완료를 증명하지 않습니다.
- 단일 운영자 토큰을 사용합니다. 여러 사용자·프로젝트 권한 관리는 후속 범위입니다.

검증 결과와 실제 환경 미검증 항목은 [검증 기록](docs/validation.md)에 기록합니다.
