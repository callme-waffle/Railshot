# 온프레미스 계정·등록 API

이 계약은 신규 계정 소유 온프레미스 환경에 적용한다. 기존 `railshot_owner` 쿠키의 환경은 계정으로 자동 이전하지 않는다.

## 계정

| 요청 | 입력 | 결과 |
| --- | --- | --- |
| `POST /api/v1/accounts` | `{email,password}` | `202 {status:"accepted"}`. 이메일 확인 토큰 발송. |
| `POST /api/v1/accounts/verifications` | `{token}` | `200 {status:"verified"}`. 30분 유효한 일회용 토큰. |
| `POST /api/v1/logins` | `{email,password}` | `201 {account_id,email,csrf_token,expires_at}`와 `railshot_account` HttpOnly 쿠키. 확인된 이메일만 로그인 가능. |
| `GET /api/v1/logins` | 없음 | 현재 계정 로그인과 CSRF 토큰. |
| `DELETE /api/v1/logins` | 없음 | 계정 로그인 폐기. |
| `POST /api/v1/accounts/resets` | `{email}` | 존재 여부와 관계없이 `202 {status:"accepted"}`. |
| `POST /api/v1/accounts/password` | `{token,password}` | 30분 유효 일회용 재설정 토큰 사용, 기존 세션 모두 폐기. |

비밀번호는 최소 12자다. 브라우저 변경 요청에는 `X-Railshot-Request: dashboard`를, 로그인된 세션의 변경 요청에는 `X-Railshot-CSRF`를 함께 보낸다. Host·Origin 제한과 IP별 요청 횟수 제한도 적용된다. 계정 세션은 기존 익명 세션과 분리되며 12시간 후 만료된다.

운영 환경에는 `RAILSHOT_ACCOUNT_MAIL_URL`과 `RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE`을 설정한다. 토큰 파일은 절대 경로의 정규 파일이어야 하고 API 프로세스 UID가 소유하며 권한은 정확히 `0600`이어야 한다. 심볼릭 링크·하드 링크는 허용하지 않는다. 내용은 공백 없는 ASCII 1–4096자와 선택적인 마지막 줄바꿈 하나로 제한한다. 단일 노드 검증 배포의 경로는 `/var/lib/railshot/config/personal-mail-token`이며 API UID/GID `1000:1000`으로 설치한다.

기존 `RAILSHOT_ACCOUNT_MAIL_TOKEN` 환경 변수도 지원하지만 파일 설정과 동시에 사용하면 시작을 거부한다. API는 읽은 뒤 토큰과 비공개 파일 경로 환경 변수를 즉시 제거해 자식 CI/CD 프로세스에 상속하지 않고, 메일 발송 closure에만 토큰을 보관한다. 파일은 API 시작 때 읽으므로 토큰을 교체한 뒤 API를 재시작해야 한다. API는 HTTPS 메일 게이트웨이에 `{to,purpose,token}` JSON을 Bearer 인증으로 전송한다. 게이트웨이는 실제 이메일 전송과 메일 본문 내 확인/재설정 링크 작성을 담당한다. 메일 게이트웨이가 없으면 신규 가입은 `503 MAIL_UNAVAILABLE`이다. 격리된 로컬 테스트 네트워크에서만 `RAILSHOT_ACCOUNT_TEST_ALLOW_HTTP=1`로 HTTP를 허용한다. 검증용 수신기는 비공개 파일에 확인/재설정 토큰을 저장하며 실제 이메일을 보내지 않는다.

## 연결 등록

1. 로그인 후 `POST /api/v1/targets {label,provider:"openstack"}`. 서버가 세션의 `account_id`를 소유자로 기록한다.
2. `GET /api/v1/targets?scope=owned&provider=openstack`에서 본인 환경만 조회한다.
3. `POST /api/v1/targets/{id}/enrollments {}`는 `{id,enrollment_id,target_id,expires_at,token,script_url,script_sha256}`를 한 번 반환한다. 토큰은 256비트 무작위 값이고 30분 후 만료된다. 새 등록 요청은 이전 토큰을 폐기한다. `script_url`에는 토큰이 없다.
4. `GET /api/v1/installers/personal`은 짧은 Bash 설치 스크립트를 응답한다. 사용자는 등록 ID와 토큰을 터미널에 입력한다. 스크립트는 고정된 배포본을 SHA-256으로 검증한 뒤 실행한다.
5. 클라이언트가 `POST /api/v1/enrollments/{id}/claims`에 일회용 토큰을 Bearer로 전달한다. 본문은 `{public_key,client_version,runtime:{ssh_host_key}}`이다. 성공 응답은 `{target_id,generation,client_token,tunnel,vault_public_key,vault_key_id,...}`이다. 클라이언트 토큰은 등록 토큰과 별개다.
6. 클라이언트가 전용 OpenStack 제어 자격을 만든 뒤 `POST /api/v1/targets/{id}/vaults`에 지속 클라이언트 Bearer와 `{generation,project_id,key_id,wrapped_dek}`를 전달한다. 서버는 감싼 데이터 키가 유효한지 확인하고 프로젝트를 환경에 결속한다. 이 엔드포인트는 Application Credential 비밀값을 받지 않는다.

`GET /api/v1/keys/current`는 클라이언트가 사용할 서버 공개키와 키 ID를 반환한다. `GET /api/v1/readiness?scope=personal&phase=connection`은 연결 선행 조건만 확인한다. 기존 `GET /api/v1/readiness?scope=personal`은 실행환경·CI 준비를 포함한 전체 운영 설정 검사다. WireGuard 등록 자체는 `connection_status=ready`를 만들지 않는다. 서버에서 터널 SSH로 `server list`를 성공적으로 실행한 뒤 준비 상태가 된다.

## 실제 HTTP 인수 검증 도구

`apps/api/test/onprem-accounts-e2e.mjs`는 Node 22 이상에서 명시적으로 실행하는 도구다. 두 개의 고유한 검증 계정을 생성하고 이메일 확인·일회용 확인 토큰·로그인·CSRF·환경 목록 및 계정 격리·등록 토큰 교체·30분 유효 기간·스크립트 해시를 실제 API로 확인한다. 폐기된 등록 토큰으로만 claim을 시도하므로 WireGuard peer를 생성하지 않는다. 마지막 유효한 등록 토큰은 클라이언트 설치에 사용한다. 생성한 계정 비밀번호는 프로세스 메모리에서만 사용한다.

설정 파일은 실행 UID 소유의 `0600` 정규 파일로 준비한다. 예시 JSON의 값에는 자격 정보가 없다.

```json
{
  "api_url": "https://e2e.railshot.test:18443",
  "mail_directory": "/private/mail",
  "output_directory": "/private/output",
  "mail_uid": 1000,
  "label": "railshot onprem acceptance"
}
```

`output_directory`는 실행 UID 소유의 `0700` 디렉터리여야 하며 기존 결과 파일을 덮어쓰지 않는다. TLS 검증용 CA는 `NODE_EXTRA_CA_CERTS`에 인증서 경로로만 제공한다. 격리된 로컬 HTTP 테스트에 한해서 JSON의 `test_allow_http:true`를 추가한다.

```bash
node apps/api/test/onprem-accounts-e2e.mjs setup /private/configuration.json
node apps/api/test/onprem-accounts-e2e.mjs refresh /private/configuration.json
node apps/api/test/onprem-accounts-e2e.mjs check /private/configuration.json
```

`setup`은 `account-state.json`에 계정 쿠키·CSRF·타깃 ID·등록 ID·등록 토큰·스크립트 URL과 해시를 `0600`으로 기록한다. `account-report.json`에는 검증 메타데이터만, `personal-install.sh`에는 자격 정보가 없는 고정 설치 스크립트를 기록한다. 이 비공개 출력 디렉터리는 Git 저장소 밖에 둔다. 표준 출력은 성공 상태와 통과한 검사 개수만 표시하며 오류 출력도 단계·오류 코드만 표시한다.

OpenStack 설치가 오래 걸리면 실제 클라이언트 설치 직전에 `refresh`를 실행한다. 기존 계정 세션·타깃을 사용해 새 등록 토큰을 발급하고 이전 토큰 거부·30분 만료·스크립트 해시를 확인한 뒤 `account-state.json`을 `0600`으로 원자적으로 교체한다. 기존 메타데이터 보고서는 보존하고 새로운 `enrollment-report-*.json`을 작성한다. 이미 claim한 타깃에는 새 등록을 발급하지 않으며 API의 `409`를 오류로 기록한다.

클라이언트가 연결된 뒤 `check`는 같은 계정 쿠키로 `connection_status=ready`, `deployable=false`, 소유 목록과 다른 계정의 접근 거부를 확인하고 `GET /api/v1/targets/{id}/instances`를 실제 실행한다. 이 요청은 백엔드가 터널 SSH로 수행한 OpenStack `server list` 결과가 배열인지 확인한다. 빈 배열과 `server_count=0`도 성공한 조회다. 이미 등록에 사용한 토큰으로 다시 claim하면 `401`인지도 검사한다. 표준 출력에는 서버 목록을 포함하지 않고 `connection_ready`와 `server_count`만 추가한다. 검증 결과·서버 수·검증된 UUID 및 제한된 상태는 타임스탬프가 붙은 `connection-report-*.json` 비공개 파일로 기록한다. 서버 이름·네트워크·임의 메타데이터 문자열은 기록하지 않는다. 계정 세션이 12시간 후 만료되면 새 검증 실행이 필요하다.

2026-10-10(Asia/Seoul) work-vm의 Docker Compose API에 `setup`을 실제 실행해 18개 검사를 통과했다. OpenStack 설치 현장은 별도의 지정 원격 VM이다. 검증용 CA로 `https://e2e.railshot.test:18443`를 확인했고, 비공개 메일 수신 볼륨을 읽기 전용으로 마운트한 별도 Node runner를 사용했다. 생성한 계정 비밀번호는 메모리에만 두었으며 표준 출력에는 상태와 검사 개수만 표시했다. 비공개 출력은 work-vm의 `/tmp/railshot-onprem-e2e-account-output/`에 실행 UID/GID `1000:1000`, 디렉터리 `0700`, 파일 `0600`으로 기록했다. 이 결과는 계정·등록 API의 실제 HTTP 검증이며 원격 VM의 WireGuard·OpenStack 명령 검증은 연결 후 별도 `check` 결과로 판단한다.

같은 날 02:17 KST에 OpenStack 설치 정상 종료 후 기존 타깃의 `refresh`를 실제 Compose API에서 실행해 5개 검사를 통과했다. `account-state.json`을 UID/GID `1000:1000`, `0600`으로 원자적으로 갱신했고 새 만료 시각은 02:47:52 KST다. 토큰·쿠키·비밀번호는 출력하지 않았다.

02:23:02 KST에는 원격 클라이언트 설치 후 work-vm의 별도 Node runner에서 실제 `check` 6개를 통과했다. `connection_ready=true`, `connection_status=ready`, `deployable=false`였고 소유 계정의 `GET /api/v1/targets/{id}/instances`는 `200`과 빈 서버 목록(`server_count=0`)을 반환했다. 다른 계정의 타깃·인스턴스 조회는 각각 `404`였고, 02:47:52 만료 이전에 이미 사용한 등록 토큰을 다시 claim한 요청은 `401`이었다. 보고서는 work-vm의 `/tmp/railshot-onprem-e2e-account-output/connection-report-1791566582482.json`에 UID/GID `1000:1000`, `0600`으로 저장했다. 이 검사는 새 클라우드 서버를 생성하지 않았다.

API·gateway는 work-vm의 서로 다른 Docker network namespace에 있고 고객 클라이언트·단일 노드 OpenStack은 지정 원격 VM에 있다. 이 실행에서는 직접 공개 UDP에 도달하지 못해 승인된 SSH 역포워드와 단일 peer UDP 진단 릴레이를 사용했다. 실제 WireGuard와 gateway stream을 통한 SSH 명령·서버 지원 자격 잠금 해제·OpenStack 조회의 성공을 확인했지만 공개 native UDP 도달성이나 Kubernetes 배포의 인수 통과로 집계하지 않는다.
