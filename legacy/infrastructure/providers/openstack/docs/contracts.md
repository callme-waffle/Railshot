# 계층별 메서드와 데이터 전달 규약

설계 계약 v0.2 · 2026-10-01. 초기 서버 중심 기능을 병렬 구현하기 위한 내부 계약입니다. 상위 Provider Interface의 명세는 아직 미정이므로 HTTP 경로와 본문은 연결 초안입니다. 이 규약에 따른 초기 코드는 src에 구현했습니다. 모의 공급자 기반 HTTP 시험과 실제 OpenStack 검증은 구분하며, 결과는 validation.md에 기록합니다.

## 적용 범위와 용어

초기 범위는 서버 목록·상세·생성·삭제·시작·정지·일반 재부팅, 이미지·사양·기존 네트워크 조회, 인증·상태 확인입니다. 외부 IP·네트워크 생성·볼륨·부하 분산·배포 자동화·영구 작업 이력·중복 요청 방지는 후속 범위로 유지합니다.

상위 Provider Interface는 다른 구성요소와의 공통 호출 규약입니다. 내부 Provider Port는 업무 로직이 공급자를 호출하는 Python 규약입니다. Adapter는 그 규약을 OpenStack 호출로 구현하는 연결 코드입니다. SDK는 외부 서비스 호출용 개발 라이브러리입니다.

## 계층별 책임

| 위치 | 책임 | 전달 형식 |
|---|---|---|
| api | HTTP 입력 검증, 내부 모델 변환, 서비스 호출, 응답·오류 변환 | JSON ↔ Pydantic 요청/응답 모델 ↔ 도메인 모델 |
| application | 프로젝트·연결 대상 검증, 업무 입력 검증, 참조 확인과 작업 순서 | 도메인 모델과 공통 예외 |
| ports | 공급자 메서드의 입력·반환 타입 정의 | Python Protocol |
| infrastructure/openstack | 인증·서비스 검색·실제 호출, 외부 객체와 예외 변환 | 도메인 모델 ↔ SDK 객체 |
| domain | 자원 모델·요청 값·결과·공통 오류 | 표준 라이브러리의 불변 dataclass |
| bootstrap/config/auth | 설정, 인증, 요청별 객체 생성·연결·정리 | 검증된 설정과 인증 컨텍스트 |

api는 application과 domain을 사용합니다. application은 domain과 ports만 사용합니다. infrastructure는 ports를 구현하고 domain을 사용합니다. domain과 ports는 FastAPI/Pydantic/openstacksdk에 의존하지 않습니다. 모든 구현을 아는 조립 코드는 bootstrap에 한정합니다.

초기 메서드는 동기 def로 통일합니다. FastAPI 동기 라우트에서 호출하고, 연결은 요청마다 생성하여 요청 종료 시 정리합니다. 요청마다 연결 객체를 만들더라도 HTTP 인증 검증 실패 시에는 OpenStack 네트워크 호출이 발생하지 않아야 합니다.

## 공통 데이터 정의

다음은 domain/models.py에 옮길 Python 타입 명세입니다. dataclass의 타입 표시는 런타임 검증을 대신하지 않으며, 아래 유효성 규칙을 application에서 검사합니다.

```python
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

T = TypeVar("T")
ServerAction = Literal["start", "stop", "reboot"]
ResourceAction = Literal["create", "delete", "start", "stop", "reboot"]


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    principal_id: str
    project_id: str
    provider_id: str


@dataclass(frozen=True)
class PageRequest:
    limit: int = 20
    marker: str | None = None


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_marker: str | None


@dataclass(frozen=True)
class Address:
    network: str
    address: str
    version: Literal[4, 6]


@dataclass(frozen=True)
class Server:
    id: str
    name: str
    project_id: str
    status: str
    addresses: tuple[Address, ...]


@dataclass(frozen=True)
class Image:
    id: str
    name: str
    status: str


@dataclass(frozen=True)
class Flavor:
    id: str
    name: str
    vcpus: int
    ram_mb: int
    disk_gb: int


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    status: str
    shared: bool


@dataclass(frozen=True)
class CreateServerSpec:
    name: str
    image_id: str
    flavor_id: str
    network_ids: tuple[str, ...]


@dataclass(frozen=True)
class Accepted:
    resource_id: str
    action: ResourceAction


@dataclass(frozen=True)
class DeleteResult:
    resource_id: str
    already_absent: bool
```

| 항목 | 생성 주체와 규칙 |
|---|---|
| request_id | HTTP 계층에서 요청마다 생성하는 UUID(고유 식별자), 외부 입력으로 덮어쓰지 않음 |
| principal_id | 인증 계층에서 결정한 호출자, 초기에는 단일 운영자 식별자 |
| project_id | 설정에서 확인한 nate2402의 실제 ID. 프로젝트 이름과 구분 |
| provider_id | 설정된 연결 대상의 식별자. 초기 예: openstack-main. 자격증명이나 URL을 포함하지 않음 |
| 자원 ID | 비어 있지 않은 문자열. 사양 ID 등을 UUID 형식으로 강제하지 않음 |
| name | 양끝 공백 제거 후 1~255문자. 정규화된 새 CreateServerSpec을 만들어 전달 |
| network_ids | 최소1개, 중복 금지. ID를 그대로 사용하고 이름 검색으로 대체하지 않음 |
| page | limit은 정수1~100, marker는 null 또는 비어 있지 않은 문자열 |
| status | 초기에는 공급자 원본 상태를 유지. 미래의 공통 상태 도입 시 별도 필드와 매핑 계약 추가 |
| 누락 값 | 주소 없음은 빈 tuple. 외부 선택적 name 누락은 빈 문자열, status 누락은 UNKNOWN. 필수 ID·사양 수치 누락은 외부 응답 오류 |

provider_id와 project_id는 요청 본문에서 받지 않습니다. 컨텍스트에는 호출자 토큰이나 OpenStack 자격증명을 넣지 않습니다. 애플리케이션이 가진 연결 대상·프로젝트와 컨텍스트를 대조하고, Adapter도 실제 인증된 프로젝트가 일치하는지 확인합니다.

페이지 marker는 현재 공급자·프로젝트·자원 종류의 동일 조회에서만 사용합니다. Adapter는 서비스의 페이지 처리 기능을 이용해 최대 limit+1개의 적합한 항목까지만 확인하고, 추가 항목이 있으면 반환 마지막 항목 ID를 next_marker로 설정합니다. 전체 목록을 자동 순회하지 않습니다. 서비스마다 지원 정렬·페이지 방식은 구현 시 확인하며, 동시 자원 변경 중에는 고정된 목록 스냅샷을 보장하지 않습니다.

## HTTP 계층의 메서드

라우트 함수의 ctx와 service는 FastAPI 의존성 주입으로 전달하며 외부 쿼리 인자로 노출하지 않습니다. 표의 메서드명과 내부 모델명을 그대로 사용합니다.

| HTTP 경로 | 라우트 함수 | 서비스 호출 | 정상 응답 |
|---|---|---|---|
| GET /health/live | live | 외부 호출 없음 | 200 HealthResponse |
| GET /health/ready | ready | HealthService.check_ready(ctx) | 200 또는503 |
| GET /api/v1/servers | list_servers | ComputeService.list_servers(ctx, page) | 200 PageResponse[ServerResponse] |
| GET /api/v1/servers/{server_id} | get_server | ComputeService.get_server(ctx, server_id) | 200 ServerResponse |
| POST /api/v1/servers | create_server | ComputeService.create_server(ctx, spec) | 202 AcceptedResponse |
| DELETE /api/v1/servers/{server_id} | delete_server | ComputeService.delete_server(ctx, server_id) | 202 또는204 |
| POST /api/v1/servers/{server_id}/actions | act_server | ComputeService.act_server(ctx, server_id, action) | 202 AcceptedResponse |
| GET /api/v1/images | list_images | CatalogService.list_images(ctx, page) | 200 PageResponse[ImageResponse] |
| GET /api/v1/flavors | list_flavors | CatalogService.list_flavors(ctx, page) | 200 PageResponse[FlavorResponse] |
| GET /api/v1/networks | list_networks | CatalogService.list_networks(ctx, page) | 200 PageResponse[NetworkResponse] |

다음 요청·응답 모델은 api/schemas.py에 정의합니다.

| 요청/응답 모델 | 필드와 변환 |
|---|---|
| CreateServerRequest | name, image_id, flavor_id, network_ids: list[str] → CreateServerSpec |
| ServerActionRequest | action: start/stop/reboot → ServerAction |
| ServerResponse 등 자원 응답 | 대응 도메인 모델과 필드 동일. tuple은 JSON 배열로 변환 |
| PageResponse[T] | items: list[T], next_marker: str 또는 null |
| AcceptedResponse | resource_id, action, status는 accepted 고정, request_id |
| HealthResponse | status는 ok 고정 |
| ErrorResponse | 아래 공통 오류의 error 객체 |

본문의 알 수 없는 필드는422로 거부합니다. 목록 쿼리는 limit/marker만 허용하며, 알려지지 않은 쿼리도422로 거부합니다. 경로 ID와 쿼리 타입을 검증합니다. live를 제외한 업무 경로에 Authorization: Bearer 인증을 요구합니다. 기본적으로 문서 경로는 비활성화합니다. 개발용 docs_enabled=true를 명시하면 /docs·/openapi.json·/docs/oauth2-redirect는 인증 없이 제공하고, 업무 API에는 계속 인증을 요구합니다. 누락·불일치는401이며 WWW-Authenticate: Bearer를 반환합니다. 인증 실패가 업무 입력 검증보다 우선하고, 외부 호출이 없어야 합니다.

모든 응답에 X-Request-ID를 포함합니다.202에는 Location: /api/v1/servers/{resource_id}를 넣습니다. DELETE의 already_absent=true는204/본문 없음, false는 action=delete의202로 변환합니다. 내부 Accepted에는 HTTP 상태 코드·Location·request_id를 중복 저장하지 않습니다.

## 애플리케이션과 공급자 메서드

공급자 규약의 파일은 ports/compute.py, ports/catalog.py, ports/health.py입니다. 아래 블록은 domain/models.py의 타입을 import한 뒤 사용합니다.

```python
from typing import Protocol


class ComputeProvider(Protocol):
    def list_servers(self, ctx: RequestContext, page: PageRequest) -> Page[Server]: ...
    def get_server(self, ctx: RequestContext, server_id: str) -> Server: ...
    def create_server(self, ctx: RequestContext, spec: CreateServerSpec) -> Accepted: ...
    def delete_server(self, ctx: RequestContext, server_id: str) -> DeleteResult: ...
    def act_server(self, ctx: RequestContext, server_id: str, action: ServerAction) -> Accepted: ...


class CatalogProvider(Protocol):
    def list_images(self, ctx: RequestContext, page: PageRequest) -> Page[Image]: ...
    def list_flavors(self, ctx: RequestContext, page: PageRequest) -> Page[Flavor]: ...
    def list_networks(self, ctx: RequestContext, page: PageRequest) -> Page[Network]: ...
    def validate_create_references(self, ctx: RequestContext, spec: CreateServerSpec) -> None: ...


class HealthProvider(Protocol):
    def check_ready(self, ctx: RequestContext) -> bool: ...
```

| 서비스 생성자 | 공개 메서드 | 정책 |
|---|---|---|
| ComputeService(compute: ComputeProvider, catalog: CatalogProvider, project_id: str, provider_id: str) | ComputeProvider와 동일한5개 메서드·인자·반환 타입 | 컨텍스트·입력 검증, 생성 참조 검증, 실행 순서 |
| CatalogService(catalog: CatalogProvider, project_id: str, provider_id: str) | CatalogProvider의 list_images/list_flavors/list_networks와 동일 | 컨텍스트·페이지 입력 검증 |
| HealthService(health: HealthProvider, project_id: str, provider_id: str) | check_ready(ctx: RequestContext) -> bool | 컨텍스트 검증, 예상된 외부 오류를 false로 변환 |

서비스는 생성자로 받은 객체만 사용하고 HTTP·환경변수·SDK에 접근하지 않습니다. 컨텍스트 불일치는 Forbidden, 잘못된 ID/페이지/명령은 InvalidInput을 발생시킵니다. 생성은 입력 정규화·검증 → catalog.validate_create_references → compute.create_server 순서로 진행합니다. 모든 조회·변경의 자원 접근 제한은 Provider 계약에도 포함하므로 Adapter를 직접 시험할 때도 동일하게 적용합니다.

validate_create_references는 이미지가 존재하며 ACTIVE인지, 사양이 존재하고 사용 가능한지, 각 네트워크에 접근 가능한지 확인합니다. 참조가 없거나 이미지 상태가 부적합하면 InvalidInput, 정책상 거부가 명확하면 Forbidden입니다. 공유 이미지·네트워크를 무조건 다른 프로젝트 자원으로 거부하지 않습니다. 검증 이후 자원 삭제·할당량 변화가 가능하므로 생성 시점의 외부 실패도 처리합니다.

서버 목록은 인증된 프로젝트로 조회하며 all_projects를 사용하지 않습니다. 잘못된 소속 항목이 섞이면 UpstreamFailure로 전체 요청을 실패시켜 다른 프로젝트 정보를 반환하지 않습니다. 상세·변경에서 다른 프로젝트의 존재가 확인되면 NotFound로 감춥니다. 삭제 대상이 실제로 없으면 DeleteResult(already_absent=True), 다른 프로젝트의 서버임이 확인되면 NotFound입니다. 존재 여부를 알 수 없는 인증 실패·통신 실패를204로 바꾸지 않습니다.

start/stop/reboot는 외부 공급자의 상태 판정을 따릅니다. 로컬 상태만 보고 이미 완료되었다고 판단하지 않고 공급자에게 요청합니다. reboot는 일반 재부팅인 soft만 지원합니다. SDK에서 액션 성공 응답에 본문이 없더라도 Adapter는 요청한 ID/action으로 Accepted를 생성합니다.

## OpenStack 연결 구현과 조립

| 클래스 | 생성자 | 구현하는 규약 |
|---|---|---|
| OpenStackComputeAdapter | (connection, project_id: str, provider_id: str) | ComputeProvider 전체 |
| OpenStackCatalogAdapter | (connection, project_id: str, provider_id: str) | CatalogProvider 전체 |
| OpenStackHealthAdapter | (connection, project_id: str, provider_id: str) | HealthProvider 전체 |

connection은 infrastructure 내부의 openstack.connection.Connection이며 상위 계층으로 반환하지 않습니다. 연결 생성은 infrastructure/openstack/connection.py의 open_connection(settings) 컨텍스트 관리자가 담당하고, bootstrap이 요청 생명주기에 맞게 열고 닫습니다. 실제/모의 공급자 선택은 bootstrap만 담당합니다.

| 조립 함수 | 반환/책임 |
|---|---|
| create_app(settings, providers=None) | FastAPI 앱. 테스트에서는 모의 ProviderBundle을 주입 |
| get_context | RequestContext. 인증 결과·고정 설정·생성된 요청 ID 결합 |
| get_providers | ProviderBundle(compute, catalog, health). 실제 연결은 요청 단위로 정리 |
| get_compute_service | ComputeService |
| get_catalog_service | CatalogService |
| get_health_service | HealthService |

ProviderBundle은 bootstrap.py 소유이며3개의 공급자 타입을 필드로 갖습니다. 테스트용 bundle에는 실제 connection이 없어도 됩니다. settings는 config.py 소유이며 provider_id/project_id/인증 방식/리전/interface/인증서/호출 시간 제한을 검증합니다. 비밀 설정은 연결 생성 단계에만 전달하고 domain으로 내보내지 않습니다.

Keystone의 서비스 카탈로그를 사용하고 인증된 project_id를 기대값과 대조합니다. TLS(통신 암호화 및 접속 대상 확인) 인증서 검증을 활성화합니다. 생성된 관리자 암호, SDK 객체, 원문 응답·헤더는 반환하지 않습니다. 요청별 로그에는 요청 ID, 주체, 프로젝트, 연결 대상, 작업, 자원 ID, 소요 시간과 외부 요청 ID만 필요한 범위로 기록합니다.

네트워크 호출의 연결 시간 제한은5초, 읽기 시간 제한은20초를 초기값으로 사용합니다. ready는 각 호출에 더 짧은 제한을 적용하고 인증 및 필수 서비스의 최소 읽기를 수행합니다. 연결/읽기 제한을 합산한 값은 요청 전체의 엄밀한 상한이 아니므로, 종전의 ready 전체5초 보장은 검증 전 확정하지 않습니다. 실제 응답 지연 측정과 전체 마감시간 구현 여부는 실행 기반 단계에서 확정합니다.

초기에는 변경 작업을 자동 재시도하지 않습니다. SDK와 인증 라이브러리의 전송 재시도 설정도 확인하며, 안전한 인증 갱신과 결과 불명확한 변경 요청의 재전송을 구분합니다.

## 오류 전달 규약

Adapter가 외부 예외를 domain/errors.py의 ControlPlaneError 계열로 변환하고, application은 별도 정책이 필요한 경우 외에는 그대로 전달합니다. api/error_handlers.py만 HTTP 상태로 변환합니다. HTTP 라우팅404/405와 요청 검증 오류도 동일한 JSON 외형을 사용합니다.

ControlPlaneError의 공개 데이터는 code: str, message: str, retryable: bool=False, outcome_unknown: bool=False입니다. code는 아래 하위 예외별 상수이며, message는 공개 가능한 정형 문구를 사용합니다. 원래 예외는 예외 연결로 내부에 보존하되 원문을 응답/로그에 그대로 출력하지 않습니다.

| 하위 예외 / code | HTTP | 판정 |
|---|---|---|
| InvalidInput / INVALID_INPUT | 422 | 형식·업무 입력·생성 참조 오류 |
| Unauthenticated / UNAUTHENTICATED | 401 | 제어 API 호출자 인증 실패 |
| Forbidden / FORBIDDEN | 403 | 컨텍스트 불일치 또는 허가되지 않은 작업 |
| NotFound / NOT_FOUND | 404 | 서버 없음 또는 접근 범위 밖 |
| MethodNotAllowed / METHOD_NOT_ALLOWED | 405 | 지원하지 않는 HTTP 메서드. Allow 헤더 유지 |
| Conflict / CONFLICT | 409 | 서버 상태 충돌 |
| QuotaExceeded / QUOTA_EXCEEDED | 409 | 명확하게 식별한 할당량 초과 |
| RateLimited / RATE_LIMITED | 429 | 외부 요청 빈도 제한 |
| UpstreamFailure / UPSTREAM_FAILURE | 502 | 외부 인증 설정 실패·잘못된 응답·미분류 외부 오류 |
| UpstreamUnavailable / UPSTREAM_UNAVAILABLE | 503 | 외부 연결 불가·필수 서비스 미제공 |
| UpstreamTimeout / UPSTREAM_TIMEOUT | 504 | 외부 호출 시간 초과 |
| InternalFailure / INTERNAL_ERROR | 500 | 예상하지 못한 내부 오류 |

외부403을 일괄 할당량 초과로 해석하지 않습니다. 외부 인증 자격증명 오류를 호출자401로 바꾸지 않습니다. ready의 예상된 외부 오류는503/UPSTREAM_UNAVAILABLE로 단순화하고, 내부 프로그래밍 오류는500으로 유지합니다.

retryable=true는 일시적인 읽기 실패처럼 재호출이 안전한 경우만 사용합니다. 변경 요청을 보낸 후 통신이 끊겨 반영 여부를 확인할 수 없으면 outcome_unknown=true, retryable=false입니다. 모든 예외를 성공값이나 None으로 바꾸지 않습니다.

## 데이터 전달 예시

서버 생성 HTTP 요청 본문입니다. 예시 ID는 실제 환경의 ID로 대체해야 합니다.

```json
{
  "name": "demo-web",
  "image_id": "image-001",
  "flavor_id": "flavor-001",
  "network_ids": ["network-001"]
}
```

api가 network_ids를 tuple로 변환해 CreateServerSpec을 만들고 별도로 생성한 RequestContext와 함께 ComputeService.create_server에 전달합니다. 서비스는 참조 검증 후 같은 spec을 ComputeProvider.create_server에 전달합니다. Adapter는 외부 생성 접수를 확인하면 Accepted(resource_id="server-001", action="create")를 반환합니다.

HTTP202, Location: /api/v1/servers/server-001, X-Request-ID: 9cd16c11-e380-4c48-8b97-d9dbdfbdf200:

```json
{
  "resource_id": "server-001",
  "action": "create",
  "status": "accepted",
  "request_id": "9cd16c11-e380-4c48-8b97-d9dbdfbdf200"
}
```

외부 생성 결과가 불명확한 시간 초과 응답 예시입니다.

```json
{
  "error": {
    "code": "UPSTREAM_TIMEOUT",
    "message": "생성 요청의 반영 여부를 확인할 수 없습니다.",
    "request_id": "9cd16c11-e380-4c48-8b97-d9dbdfbdf200",
    "retryable": false,
    "outcome_unknown": true
  }
}
```

request_id는 HTTP 요청 추적용이며 작업 ID나 중복 방지 키가 아닙니다. 초기에는 영구 작업 ID를 발급하지 않습니다.202는 OpenStack 접수 확인이며 생성 완료가 아닙니다. 이후 상세 조회로 BUILD/ACTIVE/ERROR를 확인하고, 삭제는404로 소멸을 확인합니다. 일반 재부팅은 ACTIVE 상태만으로 완료 여부를 증명하지 못하므로 초기 API는 접수만 보장합니다.

## 병렬 구현과 확장 규칙

주 에이전트가 domain 모델·예외, ports, 서비스 공개 시그니처, 조립 함수와 모의 공급자부터 준비합니다. 그 뒤 A는 api, B는 application, C는 infrastructure/openstack을 병렬 구현합니다. 서비스 구현을 기다리는 A는 같은 시그니처의 모의 서비스를 사용하고, B는 모의 Provider를 사용합니다. 공통 규약 파일은 주 에이전트만 변경합니다.

미래에 NetworkProvider/VolumeProvider/LoadBalancerProvider를 각각 추가하며 ComputeProvider를 거대한 통합 인터페이스로 확대하지 않습니다. 작업 저장소·지원 기능 조회·공급자별 상태 정규화는 필요 시 계약 버전을 올려 도입합니다. 아직 미구현인 기능은 지원하는 것처럼 빈 성공 응답을 반환하지 않습니다.

이 문서의 v0.2는 설계 문서 버전이며 URL의 /api/v1과 다릅니다. 상위 Provider Interface 확정 시 우선 api 변환 규칙을 조정하고, 의미가 달라지는 변경만 domain/ports까지 반영합니다.

검증 항목은 입력 변환, 프로젝트 경계, 참조 검증 순서,202/204 처리, 외부 오류 변환, 결과 불명확 처리, 페이지 경계, 비밀 값 미노출입니다. 코드 구현과 실제 HTTP 호출 검증을 통과하기 전에는 계약의 구현 완료로 보고하지 않습니다.

## 구현 위치

실제 코드와 Git 저장소는 프로젝트의 dev 디렉토리에 있습니다. 초기 계획의 src/control_plane 경로는 dev/src/control_plane을 의미합니다. 구현 및 실행 방법은 [README](../README.md), 검증 내역은 [검증 기록](validation.md)을 참조합니다.
