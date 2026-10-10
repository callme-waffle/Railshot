## 요약

> 온프레미스 계정·등록·암호화 vault·격리 게이트웨이 연결과 실제 OpenStack 조회 검증을 개발 기준 `dev/v1.0.0`에 제안합니다.
> 기존 경로를 유지한 레거시 보존 및 브랜치 지침을 함께 정리하며, 공개 native UDP와 배포 인수는 후속 검증으로 남깁니다.

### #주요기능

- 계정·소유권: 이메일 확인, 로그인·로그아웃, 비밀번호 재설정, 계정별 타깃 격리, CSRF·Host/Origin·요청 제한을 추가합니다. 기존 익명 소유 환경은 자동 이전하지 않습니다.
- 등록·설치: 30분 일회용 등록 토큰과 교체·재사용 차단, 자격 없는 짧은 설치 스크립트, 고정 배포본 SHA-256 확인, API와 원격 클라이언트의 동일 HTTPS endpoint 확인을 연결합니다.
- 서버 지원 vault: 전용 OpenStack 제어 계정과 제한된 Application Credential을 만들고 RSA-OAEP/AES-GCM vault로 저장합니다. 감싼 데이터 키 등록, 서버 지원 잠금 해제, 재시도·replay 차단과 임시 평문 정리를 구현하며 legacy 요청 호환을 유지합니다.
- 격리 게이트웨이: API와 WireGuard 게이트웨이를 별도 네트워크 namespace로 배치하고 인증된 내부 RPC 및 제한된 SSH CONNECT를 사용합니다. Compose·Kubernetes·플랫폼 렌더 및 preflight에 같은 경계를 반영합니다.
- 안전한 메일 설정: UID 소유 `0600` 정규 파일에서 메일 토큰을 읽고 symlink·hardlink·과대 입력을 거부합니다. 토큰과 비공개 경로 환경 변수를 자식 프로세스에서 제거하고 대시보드 vault 경로의 Bearer 전달을 보존합니다.
- 실검증 도구: 비공개 상태·메일 수신기·테스트 HTTPS proxy·계정 E2E runner 및 게이트웨이 재시작 검사를 추가합니다. 출력은 검사 개수와 제한된 상태로 한정합니다.
- 지정 VM 설치·운영: 기존 지정 VM에서 nested KVM과 고정 DevStack 설치를 검증했습니다. 비밀번호 노출 경로를 다루는 설치 도구, RabbitMQ stdin 정의 입력, absent-only Nova rootwrap 경로 보완과 승인된 loopback 진단 UDP relay의 독립 서비스 운영을 기록합니다.
- 레거시·브랜치 관리: baseline `645e28a`와 기존 경로를 보존하는 담당 변경 및 브랜치/PR 지침을 통합합니다. 원본 blob·mode 보존 검증을 완료했습니다.
- CI 변경 범위: API 관련 Docker stage의 실제 root COPY 의존성을 변경 경로 판정에 반영해 `connection-api` 메일 검사 파일의 API 빌드·HTTP 검사 누락을 수정합니다. 기존 동적 COPY 커버리지 검사를 유지하고 relay·probe·레거시 경계를 확인합니다.
- PR 범위: base `dev/v1.0.0` (`645e28aefa7f3d3996c57ea6853c6e2f67c5bcc4`), head `refactor/onprem-integration`. 기능 작업 39개 커밋의 마지막은 `96e6f3c4258d1c6527a4090d7a205882b67922f5`이며 이후 레거시·지침·게시 문서 커밋도 포함합니다. 최종 head SHA는 게시 직전 확정합니다.

### #Endpoints

|Method|Path|기능설명|
|---|---|---|
|POST|`/api/v1/accounts`|이메일 확인을 요구하는 계정 가입 요청|
|POST|`/api/v1/accounts/verifications`|30분 일회용 이메일 확인 토큰 소비|
|POST|`/api/v1/logins`|확인된 계정 로그인 및 쿠키·CSRF 발급|
|GET|`/api/v1/logins`|현재 계정 로그인·CSRF 조회|
|DELETE|`/api/v1/logins`|계정 로그인 폐기|
|POST|`/api/v1/accounts/resets`|계정 존재를 노출하지 않는 재설정 요청|
|POST|`/api/v1/accounts/password`|일회용 토큰으로 비밀번호 변경 및 기존 세션 폐기|
|POST|`/api/v1/targets`|로그인 계정이 소유하는 OpenStack 환경 생성|
|GET|`/api/v1/targets?scope=owned&provider=openstack`|본인 소유 환경 목록 조회|
|GET|`/api/v1/targets/{id}`|소유 환경 조회 및 다른 계정 접근 차단|
|POST|`/api/v1/targets/{id}/enrollments`|30분 일회용 등록 토큰·스크립트 URL·해시 발급|
|GET|`/api/v1/installers/personal`|자격 정보 없는 짧은 Bash 설치 스크립트|
|POST|`/api/v1/enrollments/{id}/claims`|일회용 Bearer 등록과 별도 지속 클라이언트 자격 발급|
|POST|`/api/v1/targets/{id}/vaults`|클라이언트 Bearer로 감싼 데이터 키·프로젝트 결속 등록|
|GET|`/api/v1/keys/current`|서버 vault 공개키와 키 ID 조회|
|GET|`/api/v1/readiness?scope=personal&phase=connection`|연결 선행 조건 확인|
|GET|`/api/v1/readiness?scope=personal`|실행환경·CI를 포함한 전체 운영 설정 확인|
|POST|`/api/v1/targets/{id}/heartbeats`|기존 클라이언트 상태 경로와 연결 상태 반영|
|GET|`/api/v1/targets/{id}/instances`|터널 SSH·서버 지원 자격으로 실제 OpenStack 서버 목록 조회|

### #테스트_결과

#### case#1) 계정·등록 실제 HTTP 검증

- 목적: 이메일 확인, 로그인, 소유권 및 등록 계약을 실제 API에서 확인합니다.
- 진행과정: 2026-10-10 KST work-vm Compose API -> 별도 Node runner `setup` -> 두 계정 격리·CSRF·토큰 교체·30분 만료·스크립트 해시 확인
- 결과: 기존 실행 기록의 `setup` 18개 통과. 이번 문서 작업에서 재실행한 결과는 아닙니다.

#### case#2) 설치 직전 등록 갱신

- 목적: OpenStack 설치 뒤 만료되지 않은 새 일회용 등록 토큰을 확보합니다.
- 진행과정: 2026-10-10 02:17 KST 기존 계정·타깃 -> `refresh` -> 이전 토큰 거부·새 만료·해시 확인 -> 비공개 상태 원자적 교체
- 결과: 기존 실행 기록의 `refresh` 5개 통과. 상태 파일 UID/GID `1000:1000`, `0600` 유지.

#### case#3) 실제 원격 클라이언트·백엔드 OpenStack 조회

- 목적: 등록, WireGuard·forced SSH, 서버 지원 vault와 계정 격리를 실제 서비스로 확인합니다.
- 진행과정: 지정 VM 설치 종료 -> 원격 클라이언트 설치 -> 02:23:02 KST `check` -> owner 서버 목록·다른 계정 접근·소비된 등록 토큰 재사용 검사
- 결과: 기존 실행 기록의 `check` 6개 통과. 서버 목록 `200`, `server_count=0`, `connection_ready=true`, `deployable=false`, 다른 계정 조회 `404`, 등록 재사용 `401`.

#### case#4) 진단 relay 서비스 전환 후 재연결

- 목적: 진단 연결이 Codex 터미널 종료와 분리되어 복구되는지 확인합니다.
- 진행과정: 비특권 loopback relay 서비스 전환 -> 원격 자동 재연결·새 WireGuard handshake -> 02:31:24 KST 실제 `check` 반복
- 결과: 기존 실행 기록의 6개 재통과. 실제 WireGuard 암호화·SSH·OpenStack 조회 증거이며 공개 native UDP 도달성은 미검증입니다.

#### case#5) vault·메일·게이트웨이 회귀 검사

- 목적: 암호화 호환, 재시도·replay, 비공개 메일 파일과 격리 RPC 경계를 확인합니다.
- 진행과정: 기존 통합 기록 -> 클라이언트 지원 vault 4개 및 관련 134개 -> 계정 메일·API 8개 -> gateway 상태·격리 24개 및 서비스·메일 8개
- 결과: 기록된 범위 통과. Provider 대역을 사용하는 단위 검사이며 외부 클라우드 연결 증거와 구분합니다. 해당 rerun에서 제외한 장시간 filesystem-cycle 2개는 통과로 집계하지 않습니다.

#### case#6) 지정 VM OpenStack 설치 및 건강 확인

- 목적: 기존 VM의 nested KVM과 실제 OpenStack 제어 서비스를 확인합니다.
- 진행과정: KVM CirrOS 부팅 -> 고정 DevStack 안전 설치 -> 정상 종료 -> Keystone·Nova·Glance·Neutron HTTP 확인 -> 제한된 비밀번호 노출 모니터 결과 확인
- 결과: 기존 실행 기록상 설치 exit 0, nova-compute up/enabled, 빈 서버 목록, Neutron agent 2개 alive. 새 outer VM/FIP나 Nova 서버 생성 결과는 없습니다. 모니터는 기록된 argv/log 표본에서 비밀번호 일치 0건입니다.

#### case#7) 레거시 원본 blob 보존 검증

- 목적: 사용자 확정 baseline `645e28a`의 레거시 원본과 기존 경로가 보존되는지 확인합니다.
- 진행과정: 담당 변경 완료 -> baseline 원본 832개와 실행 파일 30개 검증 -> legacy 835개 hash/mode 비교 -> Docker·검색·pytest 격리 및 현재 npm workspace 확인
- 결과: 통합 HEAD `bb838ad`에서 관리 세션의 독립 검증 PASS: 원본 832개 blob·mode 및 실행 파일 30개 보존. 담당 검증의 legacy 835개 hash/mode 동일, `.dockerignore`·`.ignore`·pytest 격리와 현행 npm workspace 3개 확인 결과도 포함합니다. 현행 기능 소스 변경 없음 확인.

#### case#8) 브랜치·문서 형식 및 게시 게이트

- 목적: 사용자 PR 형식과 개발 기준·게시 상태를 확인합니다.
- 진행과정: 명세·계획·검증 게이트 문서 작성 -> 로컬 ref 확인 -> PR 템플릿 섹션 대조 -> diff 공백 검사 -> 인증 경로 존재 여부 확인
- 결과: 개발 기준 SHA와 통합 브랜치 이름 확인. 관리 세션의 AGENTS 및 PR template 사용자 규칙 검증 PASS, `git diff --check` PASS. App 권한 설정 뒤 개발 기준 branch 게시는 성공했습니다. 원형 이력 보존 helper 실행으로 46개 commit을 `0e5aa74f954c3659001b4375e5c9b68be633f19d`에 게시했고 임시 helper branch 삭제 성공. [Draft PR #1](https://github.com/callme-waffle/Railshot/pull/1) 생성 및 본문 첨부 완료. 최초 원격 CI 실패와 후속 로컬 수정 검증은 case#10에 기록합니다.

#### case#9) 남은 인수 범위

- 목적: 연결 성공과 제품 배포 인수의 범위를 명확히 기록합니다.
- 진행과정: 실행된 진단 경로 확인 -> 미실행 인수 항목 분리 -> 후속 공개 endpoint와 배포 검증 입력 확인
- 결과: 공개 native UDP, 고객 Kubernetes 설치, Nova 생성·삭제, 운영 메일 전달 및 production ingress 인수는 미완료입니다. 이번 연결 검증을 해당 항목의 통과로 집계하지 않습니다.

#### case#10) API Docker COPY 의존성 CI 회귀 수정

- 목적: 신규 API 이미지 COPY 파일 변경이 API 빌드·HTTP 검사에서 누락되지 않도록 합니다.
- 진행과정: 원격 run `38054555832`의 scope 검사 13개 중 1개 실패 확인 -> 같은 로컬 13개 검사로 e2e mail 누락 재현 -> 실제 API stage COPY source 경로 판정 보완 -> 기존 Dockerfile 동적 커버리지 및 비복사 relay·probe·레거시 경계 검증
- 결과: 수정 후 로컬 13개 검사 PASS, `git diff --check` PASS. 기존 테스트를 삭제하거나 완화하지 않았습니다. 이 수정 commit의 원격 게시 및 원격 CI 재실행 결과는 아직 검증하지 않았습니다.
