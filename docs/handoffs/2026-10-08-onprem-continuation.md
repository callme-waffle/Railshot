# 온프레미스 원터치 연결 작업 인계 (2026-10-08)

## 목표와 사용자 결정

- REST API를 통해 설치 스크립트 URL 발급, 온프레미스 인프라 목록, WireGuard 전담 게이트웨이를 구현한다.
- 이메일·비밀번호 로그인 계정 ID에 인프라를 귀속한다. 등록용 일회성 토큰 유효기간은 30분이다.
- 설치 클라이언트가 OpenStack 관리자 입력을 받아 전용 제어 계정을 만들고, 제어 자격증명을 클라이언트에 AES-GCM으로 암호화해 저장한다. 명령 시 서버가 필요한 복호화 자료를 제공하며 클라이언트는 실행 직후 평문을 폐기한다.
- 사용자는 서브에이전트별 독립 워크트리와 커밋을 요구했다.
- 실제 검증용으로 console.aolda.net의 로그인된 nate2402 프로젝트에서 단일 VM을 생성하고, 미리 할당된 floating IP 10.26.4.67을 붙인다. VM 인증은 키가 아닌 비밀번호로 설정한다. 연결 주소는 ssh.aolda.net:20505로 안내받았다. VM 위에 단일 노드 OpenStack 테스트 환경을 설치해 E2E를 검증한다.

## 코드 상태

- 원본 /workspace/Railshot의 main은 수정하지 않았다.
- 통합 브랜치 `codex/onprem-integration`, 워크트리 `/tmp/railshot-onprem-integration`.
- 통합 기능 커밋 `a431743`, 문서 경로 수정 `65b3e71` (설치 API `/api/v1/installers/personal`). 이 인계 문서가 후속 커밋이다.
- 하위 브랜치: `codex/onprem-auth` (`225bab9`), `codex/onprem-gateway` (`1630323`), `codex/onprem-client` (`1dd4bf8`, `a7def0b`, `eeaef6b`).
- 구현 범위: 이메일·비밀번호 로그인, 소유 인프라 조회, 30분 등록 토큰, 설치 스크립트, WireGuard 게이트웨이 Pod와 내부 RPC, API 프록시, RSA-OAEP/AES-GCM 자격증명 보관소, 클라이언트 강제 SSH 명령.
- 주요 API: `/api/v1/logins`, `/api/v1/targets?scope=owned&provider=openstack`, `/api/v1/targets/{id}/enrollments`, `/api/v1/installers/personal`, `/api/v1/keys/current`, `/api/v1/targets/{id}/vaults`.
- 계약 문서: `docs/architecture/onprem-connection-contract.md`. 게이트웨이 매니페스트: `deployment/manifests/personal/gateway.k8s.yaml`.

## 확인한 결과와 미완료 항목

- API 계정/연결 33개 통과, 프록시/보관소 10개 통과, 플랫폼 릴리스 21개 중 20개 통과·1개 건너뜀, 게이트웨이 7개 통과.
- 클라이언트 92개 통과·7개 실패. 기존 main에서도 재현된 삭제 소유권 관련 실패가 포함되므로 실패별 원인을 다시 분류해야 한다.
- 실제 WireGuard 핸드셰이크와 OpenStack E2E는 수행하지 못했다. VM 생성이나 floating IP 연결도 아직 하지 않았다.
- 계정 가입 이메일 검증에는 외부 HTTPS 메일 게이트웨이 설정(`RAILSHOT_ACCOUNT_MAIL_URL`, `RAILSHOT_ACCOUNT_MAIL_TOKEN`)이 필요하다. 없으면 가입 API는 503이다.
- 새 v2 보관소 자동 제거/자격증명 폐기는 서버가 승인하는 철회 경로가 구현되기 전까지 fail closed이다.
- `console.aolda.net`과 `ssh.aolda.net`은 210.107.196.189로 확인되었고 콘솔 HTTPS 응답은 200이었다. `ssh.aolda.net:20505`의 TCP 접속은 가능했으나 SSH 핸드셰이크는 연결 초기화로 실패했다.
- 이 대화에서는 Browser 읽기·클릭 도구가 노출되지 않았다. 사용자가 Browser 도구 연결 의사를 밝혔고 Cloud 탭을 링크했지만, 로그인된 콘솔을 실제로 조작하지 못했다. 새 대화에서 브라우저 도구 노출 여부를 먼저 확인한다. 자격증명은 채팅에 요청하지 않는다. 대안은 비공개 `clouds.yaml`/openrc 파일이다.

## 새 세션의 우선 작업

1. 통합 브랜치의 Git 상태, 테스트 결과, 계약 문서를 확인한다.
2. Browser 조작 도구가 있다면 nate2402 프로젝트의 VM 이미지·flavor·네트워크·보안 그룹·floating IP 상태를 확인한다. 없다면 사용자에게 도구 연결 또는 비공개 OpenStack API 파일 제공을 요청한다. 스크린샷만으로 VM 생성 완료를 추정하지 않는다.
3. 단일 VM을 비밀번호 방식으로 생성하고 10.26.4.67을 연결한다. SSH 실접속을 검증한다.
4. VM 자원에 맞는 단일 노드 OpenStack 환경을 설치한다. 설치 자격증명은 채팅이나 Git에 기록하지 않는다.
5. REST API → 등록 스크립트 → 30분 토큰 소비 → WireGuard 핸드셰이크 → 인프라 계정 생성 → 암호화 보관소 → `openstack server list`까지 실제 E2E를 검증하고 발견한 결함을 수정한다.
6. 재현 가능한 테스트와 배포 설정을 정리하고 커밋한다. 테스트 VM과 floating IP 처리는 사용자의 검증 목적과 자원 사용에 맞춰 기록한다.

브라우저 조작이 없던 지난 대화에서 실제 인프라 접근을 했다고 주장하지 말 것. 이전 테스트 명령과 환경 의존성은 커밋 및 각 패키지의 package.json을 참조할 것.
