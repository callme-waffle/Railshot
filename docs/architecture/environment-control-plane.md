# 환경 제어평면 v1 명세 및 구현 계획

승인 범위는 AWS 서울, GCP 서울, NCP 민간 VPC 한국 및 온프레미스 OpenStack이다. 공통 기반 이후 네 공급자는 병행 구현한다. 사용자 소유 계정과 Railshot 관리 계정, 공유 및 앱 전용 환경을 지원한다. 구성은 kubeadm control-plane 1대/worker 2대, 영속 볼륨과 공개 접근, 선택적인 별도 단일 PostgreSQL VM 및 백업/복구다. Patroni HA는 후속 태스크다.

## 공통 계약

`infrastructure/providers/environments/contract.py`가 포트 계약이다. 공급자는 동일 디렉터리의 `aws.py`, `gcp.py`, `ncp.py`, `onprem.py`에서 `Adapter(config)`를 제공한다. config는 서버 운영자가 등록한 계정의 private `adapter` 객체다. HTTP 입력으로 파일 경로, 명령, credential 원문을 받지 않는다.

메서드는 `connection(context)`, `catalog(context)`, `plan(spec, context)`, `apply(spec, context)`, `describe(context)`, `operate(operation, context)`, `delete(context)`다. context는 `environment_id`, `owner_id`, `account_id`, `credential_ref`, `policy_revision`, `state_dir`, `operation_id`, `spec`을 포함한다. `state_dir`은 해당 환경 전용 private 경로다.

spec은 version=1, name, provider, account_id, region, scope(shared/dedicated), runtime={kind:kubeadm,control_planes:1,workers:2}, database={mode:none/standalone}이며 dedicated에는 application_name가 필수다. 네트워크/이미지/인스턴스 사양/실행 권한은 운영자 계정 프로파일로 제한한다.

`apply`/`describe`는 status(succeeded/blocked/unknown), readiness(infrastructure/cluster/storage/public_access/database/deployment_binding/application_http의 boolean), resources 목록, 선택적으로 `application_config_path`를 반환한다. 모든 readiness는 실제 관측 근거다. 환경 준비는 infrastructure/cluster/storage/public_access/deployment_binding 및 선택 DB를 요구한다. public_access는 LB/DNS/Tunnel 구성의 실제 readback이며 준비 조건이다. application_http는 첫 앱의 실제 HTTPS 관측으로 환경 준비의 선행 조건이 아니다. deployment_binding은 실제 클러스터 검증 결과와 앱 등록 권한을 확인한다. DB none일 때 database만 생략 가능하다. 자원 목록이 있거나 설정이 렌더링되었다는 이유로 ready 처리하지 않는다. 성공하지 않은 외부 호출의 원문을 오류에 포함하지 않고 `ProviderError(code,status,outcome_unknown)`를 사용한다.

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

## 안전한 입력 준비 (실환경 실행 전)

1. 채팅/저장소에 비밀값을 붙이지 않는다. 아래 helper로 **저장소 밖**, 현재 운영 사용자 소유 0700 디렉터리와 0600 계정 등록 파일을 만든다. helper는 기존 파일을 덮어쓰지 않으며 allow_apply=false로 시작한다.

   ```sh
   python3 infrastructure/providers/environments/setup.py init --directory /private/operator/railshot-inputs --provider aws --owner-id account:ACCOUNT_UUID
   python3 infrastructure/providers/environments/setup.py validate --config /private/operator/railshot-inputs/control.json
   ```

2. account owner_id는 서버가 인증한 계정의 `account:<account UUID>` 또는 승인된 MCP/웹 소유 세션 ID다. managed 계정도 명시된 owner_id 한 명에게만 허용한다. 다른 소유자에게 제공하려면 별도 계정 항목/credential_ref와 정책을 등록한다. secret file은 기존 secret manager로 배치하거나 운영자 secure editor에서 작성한다. 프로필에는 그 private file의 절대 참조만 넣는다. 비밀값을 shell 명령 인수로 넣지 않는다.

   웹에 로그인한 뒤 동일 세션에서 `GET /api/v1/logins` 응답의 계정 ID를 확인하고 `account:` 접두사를 붙인다. 임의 문자열은 실제 UI 소유자와 일치하지 않아 계정이 보이지 않는다. 기존 세션 기반 MCP는 서버 운영자가 현재 인증 세션의 소유 ID를 확인해 매핑한다. 세션 cookie/token 자체를 owner_id로 사용하거나 공유하지 않는다.
3. provider PR의 실제 `validate_config`와 예제에 맞춰 adapter 객체를 채운다. offline helper는 누락 필드명과 오류 코드만 출력한다. 파일이 유효하다는 것은 계정 인증, 네트워크 연결, 앱 배포 인수 성공이 아니다. 공급자 구현이 설치되지 않았으면 UNVERIFIED로 실패한다.
4. `RAILSHOT_CONTROL_CONFIG`를 이 파일로 지정한다. 실환경 비용 상한·계정·DNS·인증 접근을 승인한 뒤 정책 revision을 올리고 allow_apply를 활성화한다. 기존 계획은 정책 변경으로 무효화되어 새로 계획해야 한다.

| 입력 종류 | 필요한 내용 | 전달 방식 |
|---|---|---|
| 공통 계정 | 소유자 ID, owned/managed, 공급자 계정/프로젝트, credential_ref, 리전, 허용 작업 | private control.json (비밀값 없음) |
| AWS 인증 | assume-role ARN/external ID 정책 또는 workload role, 임시 credential 파일 참조 | 역할 위임·private credential reference |
| GCP 인증 | 프로젝트 번호/ID, WIF audience/provider와 서비스 계정, ADC 파일 참조 | WIF/ADC private file |
| NCP 인증 | 민간 VPC 계정·Sub Account access key와 secret key | secret manager가 작성한 private auth JSON |
| 온프레미스 인증 | project/region, application credential 또는 기존 vault/gateway 연결 | 고객측 vault 또는 private OpenStack auth JSON |
| SSH | 로그인 사용자, private key/known_hosts 참조, 신뢰한 host key 및 관리 접속 경로 | 0600 파일; host key를 provider 실제 자원과 검증 |
| 네트워크·컴퓨트 | 리전/AZ, CIDR, 이미지, 크기, 관리 CIDR, 허용 비용 상한 | 공급자별 비밀값 없는 operator profile |
| 스토리지·공개 접근 | 공유 스토리지, LB/DNS zone/도메인/인증서, 필요시 tunnel | profile 및 private DNS/token refs |
| PostgreSQL | 별도 데이터 디스크, DB 비밀값 파일, VM 외부 백업 bucket/권한, 보존 기간 | private standalone config와 secret refs |
| CI 등록 | GitHub 저장소 Actions variables 쓰기 권한 | `ingress.ci_credentials_file`이 가리키는 0600 JSON `{token:...}`; 실제 값은 secret manager/secure editor |
| 기본 지표 | 기존 수집기 주소·수명·접근 경로·API registrar 또는 local product registry | `ingress.observability_config_file` private 기존 collector profile |

등록된 계정은 `GET /api/v1/tenancies`로 조회한다. `POST /api/v1/clusters`는 계획을 만들고 `POST .../{id}/operations (action=apply)`가 멱등 키와 durable queued intent를 저장한 뒤 실행한다. `GET .../{id}`는 작업과 준비 상태를, `POST .../{id}/operations (action=reconcile)`은 실제 상태를 조회한다. `DELETE .../{id}`는 앱/진행 작업이 있으면 차단한다. 기존 앱 API·MCP·웹 대상 선택은 환경 ID를 사용한다.

## 병렬 구현용 계약 확정

- 환경 ID는 `env-` + SHA-256 앞 24자리, 총 28자의 소문자/숫자/하이픈이다. 기본 VM 이름에 역할 접미사를 붙여도 기존 길이 제한을 유지한다.
- `operate` 입력은 `{action:start|stop|backup|restore|verify, backup_id?:...}`다. 지원하지 않는 action은 외부 호출 전에 `OPERATION_UNSUPPORTED`로 차단한다. stop은 `status:succeeded,state:stopped,stopped_verified:true`를 반환해야 하며 환경 상태 stopped와 작업 성공을 따로 기록한다. start는 stopped에서 실행 가능하다. describe는 stopped 결과도 같은 근거로 반환한다.
- 삭제 성공은 `status:succeeded,deleted:true,residuals:[]`다. 의도적으로 보존한 DB/PV/백업은 `retained:[{kind,id,status}]`로 구분하며 residuals에 섞지 않는다. 잔여 미삭제 자원이 있으면 완료로 처리하지 않는다. 앱/진행 작업 참조가 있으면 제품 계층에서 삭제를 차단하고 mutation fence가 신규 앱 접수를 막는다.
- `application_config_path`가 있어도 native infrastructure/cluster/storage/DB 검증이 없으면 배포 대상이 되지 않는다. Node가 기존 application factory의 CI tenant/등록 대상/자동 배포 경로를 검증한 뒤 private `confirm_binding`으로 동일 config hash를 확인한다. 첫 앱 HTTP는 별도로 관측한다.
- 공급자 모듈은 offline `validate_config(config)`와 `config_template(directory)`도 제공한다. validator 반환은 `{valid,missing:[field names],errors:[safe codes],verification:configuration_only}`이며 외부 호출 금지다. template 반환은 `{adapter:<실제 필드 skeleton>,files:{basename.json:<비밀값 없는 JSON skeleton>},notes:[입력 설명]}`이다. helper가 private 파일을 만들고 누락 값은 운영자가 안전한 도구에서 채운다. 공급자 스키마가 설치되지 않았으면 template 성공을 주장하지 않는다.
- 프로세스 시작 때 durable queued 작업만 저장된 동일 키/본문으로 재개한다. native dispatch marker가 running인 작업은 자동 재실행하지 않는다. describe가 `ownership_verified:true,mutation_in_progress:false`로 부분 자원의 소유권과 진행 작업 부재를 확인하면 blocked 상태에서 삭제할 수 있다. 추가로 `no_resources:true`를 확인한 경우에만 planned로 돌아가 새 키로 생성할 수 있다. 이 필드는 로컬 설정이나 journal만 보고 추정해서는 안 된다.
- DB restore는 제품 저장소의 해당 환경 앱이 모두 stopped/deleted이며 진행 작업이 없을 때만 내부 승인 근거를 생성한다. HTTP 요청의 승인 boolean은 받지 않는다. 재시작 queued restore도 제품 guard를 다시 거친다. DB helper는 별도의 환경/backup/유효기간에 묶인 private 운영자 승인도 검증한다.
- AWS 앱 등록/공개경로/수명주기는 `environment_action.py`의 고정 action으로 실행한다. private `aws_authority_file`의 stable role/ref/policy를 확인한 뒤 매 명령 fresh STS를 받아 격리 child 환경으로 전달한다. 임시 session bytes는 클러스터 identity hash가 아니다. 로그와 GitOps는 등록된 Argo 토큰/TLS 경로를 사용한다.
- CI 변수 전체 read-modify-write는 저장소별 flock으로 직렬화한다. 여러 API/operator replica는 동일한 `RAILSHOT_CI_LOCK_DIR` 공유 볼륨을 사용하고, `RAILSHOT_TARGET_BINDINGS`는 이 writer가 관리한다. 외부 writer와 분산 CAS를 제공한다고 간주하지 않는다.
- kubeadm의 기본 지표는 등록된 수집기/exporter와 실제 Cilium 3노드 관측을 연결한다. 이 경로는 Kubernetes API의 익명 인증 설정을 바꾸지 않으며 legacy native anonymous healthz 지표는 미설정으로 남는다. 클러스터 준비 상태는 installer의 인증된 native readback으로 확인한다. 앱 로그·실제 revision/HTTP 검증은 기존 앱 실행기의 별도 관측이다.

## 구현·검증 구분

공통 API/UI/MCP, owner/계정 바인딩, 작업 저장·복구와 기존 앱 어댑터 연결은 공통 PR에 구현한다. 앱 공유 경로는 AWS ALB 두 worker, GCP worker별 NEG, OpenStack Octavia 두 worker와 Named Tunnel을 같은 환경의 소유 상태에서 관리한다. 앱 삭제는 다른 앱과 기본 환경 경로를 보존하며, 환경 삭제는 앱 정리와 Argo/CI 접근 회수 뒤 별도로 수행한다. 미확정 외부 호출은 성공으로 바꾸지 않는다.

로컬 검사는 합성 native transport/문서와 실제 코드의 소유권·멱등성·복구 분기를 검증하며 실환경 인수의 대체물이 아니다. 인증 접근·관리망·DNS·수집기·GitHub writer·비용 상한 입력을 받기 전에는 실제 생성/삭제/DB 복구를 실행하지 않는다. provider별 실호출·단일 PostgreSQL 통합 결과와 AWS/GCP/NCP/OpenStack 실환경 인수 결과는 각 독립 PR에 따로 기록한다.

환경 삭제는 최초 소유권 검증에서 Secret/Project/Namespace UID와 spec, PV Retain 근거를 private journal에 저장한다. 중단 후에는 완료한 접근 회수 단계를 반복하지 않으며 남은 항목은 저장한 UID 또는 실제 부재를 확인한다. edge는 새 native refresh/삭제 계획이 최초 소유 자원 ID의 남은 부분만 포함할 때 재개한다. 교체된 자원이나 새 DNS 레코드는 자동으로 인수하지 않는다. 계정 소유 기록의 SQLite FK는 인증된 로그인에서만 생성하는 영속 `account:<id>` owner row로 유지하며 기존 브라우저 세션 소유 기록과 구분한다.

작업 중 전체 API 회귀 실행은 408 tests / 387 pass / 19 fail / 1 cancelled / 1 skipped로 전체 통과가 아니다. 해당 실행은 구현 도중 시작하여 최종 HEAD 검증으로 간주하지 않는다. 일부 lifecycle timeout, 진단 입력 파일 부재, queue/SIGTERM 문제는 기존 기준에서도 재현되었다. 최종 변경 범위의 계약·소유권·복구·관측 검사 결과와 실환경 미실행 상태를 PR에 별도로 기록한다.

새 VPC의 외부 Argo/수집기 관리 경로는 kubeadm registry의 선택 필드 `management_endpoint`와 `metrics_endpoints`로 지정한다. 전자는 실제 control VM snapshot의 고정 public IPv4와 `https://…:6443`가 일치해야 하고, 후자는 세 node ID 각각의 실제 public IPv4와 완전히 일치해야 한다. 온프레미스는 검증된 관리 SSH 주소를 사용한다. 임의 URL/호스트를 수락하지 않으며 snapshot 일치는 인터넷 도달성 증명이 아니다. 공급자는 명시된 운영자/Argo `/32`에 control API, 수집기 `/32`에 exporter NodePort만 허용해야 한다. Argo TLS CA 및 private IP serverName 검증은 그대로 유지한다. 수집기는 세 node exporter와 control의 kube-state-metrics를 수집하며 기존 제품 `target_node` 지표는 control node 기준, 앱 pod 수는 cluster 기준이다.
