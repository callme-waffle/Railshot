# 환경 제어평면 v1 명세 및 구현 계획

승인 범위는 AWS 서울, GCP 서울, NCP 민간 VPC 한국 및 온프레미스 OpenStack이다. 공통 기반 이후 네 공급자는 병행 구현한다. 사용자 소유 계정과 Railshot 관리 계정, 공유 및 앱 전용 환경을 지원한다. 구성은 kubeadm control-plane 1대/worker 2대, 영속 볼륨과 공개 접근, 선택적인 별도 단일 PostgreSQL VM 및 백업/복구다. Patroni HA는 후속 태스크다.

## 공통 계약

`infrastructure/providers/environments/contract.py`가 포트 계약이다. 공급자는 동일 디렉터리의 `aws.py`, `gcp.py`, `ncp.py`, `onprem.py`에서 `Adapter(config)`를 제공한다. config는 서버 운영자가 등록한 계정의 private `adapter` 객체다. HTTP 입력으로 파일 경로, 명령, credential 원문을 받지 않는다.

메서드는 `connection(context)`, `catalog(context)`, `plan(spec, context)`, `apply(spec, context)`, `describe(context)`, `operate(operation, context)`, `delete(context)`다. context는 `environment_id`, `owner_id`, `account_id`, `credential_ref`, `policy_revision`, `state_dir`, `operation_id`, `spec`을 포함한다. `state_dir`은 해당 환경 전용 private 경로다.

spec은 version=1, name, provider, account_id, region, scope(shared/dedicated), runtime={kind:kubeadm,control_planes:1,workers:2}, database={mode:none/standalone}이며 dedicated에는 application_id가 필수다. 네트워크/이미지/인스턴스 사양/실행 권한은 운영자 계정 프로파일로 제한한다.

`apply`/`describe`는 status(succeeded/blocked/unknown), readiness(infrastructure/cluster/storage/public_access/database/applications의 boolean), resources 목록, 선택적으로 `application_config_path`를 반환한다. 모든 readiness는 실제 관측 근거다. DB none일 때 database만 생략 가능하다. 자원 목록이 있거나 설정이 렌더링되었다는 이유로 ready 처리하지 않는다. 성공하지 않은 외부 호출의 원문을 오류에 포함하지 않고 `ProviderError(code,status,outcome_unknown)`를 사용한다.

`application_config_path`는 기존 `createApplicationAdapter`가 소비하는 private 환경 등록 파일이다. 공통 Node bridge는 이를 읽어 기존 배포/로그/수명주기 실행기에 연결한다. 동적 경로는 API로 노출하지 않는다. DB 실행기는 `infrastructure/ansible/standalone.py`의 고정 CLI를 호출하며 secret은 private spec/ref 파일로 전달한다.

## 실행과 보존

계정 등록부는 owner_id와 계정 소유 방식(owned/managed), 안정적인 credential_ref, policy_revision, 허용 리전 및 adapter config를 가진다. credential_ref는 세션 credential bytes의 hash가 아니다. credentials 회전은 같은 ref 아래에서 가능하고 정책/계정 바인딩 변경은 실행 전 차단한다.

환경 상태와 작업은 private 디스크에 원자적으로 저장한다. 프로세스 간 잠금, 소유권 확인, 요청 fingerprint, idempotency key, 실행 전 durable dispatch marker를 사용한다. 중단된 mutation은 자동 재실행하지 않고 unknown으로 남긴다. describe/reconcile이 native 상태를 조회하고 readiness를 회복해야 한다. 환경 삭제는 계정/소유권을 다시 검증하고 공급자가 잔여 자원 부재를 확인해야 완료된다.

## 진행 순서와 완료 기준

1. 계약/계정/환경/작업 저장소와 안전한 Python CLI 구현.
2. Node bridge, 인증된 API 및 기존 앱 어댑터에 연결.
3. 웹 환경 생성/선택 및 MCP 도구 제공.
4. 각 플랫폼 PR의 실호출 구현을 합쳐 계약/회귀 검사 수행.
5. 운영자 인증 접근, 테스트 계정/권한, DNS 및 비용 상한 확보 후 실환경 인수.

로컬 계약 검사와 실환경 인수를 따로 기록한다. 실환경 인수는 자원 생성, 실제 3노드 준비, PVC 데이터, 앱 HTTPS, PostgreSQL 백업 복원 SQL 확인 및 자원 정리를 포함한다. 온프레미스 인증 파일 접근 문제는 실환경 인수의 선행 해결 항목이다. 미실행 또는 관측하지 못한 항목을 성공으로 표시하지 않는다.
