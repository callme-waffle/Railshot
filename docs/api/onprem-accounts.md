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

운영 환경에는 `RAILSHOT_ACCOUNT_MAIL_URL`과 `RAILSHOT_ACCOUNT_MAIL_TOKEN`을 설정한다. API는 HTTPS 메일 게이트웨이에 `{to,purpose,token}` JSON을 Bearer 인증으로 전송한다. 게이트웨이는 실제 이메일 전송과 메일 본문 내 확인/재설정 링크 작성을 담당한다. 메일 게이트웨이가 없으면 신규 가입은 `503 MAIL_UNAVAILABLE`이다. 로컬 테스트에서만 `RAILSHOT_ACCOUNT_TEST_ALLOW_HTTP=1`로 HTTP를 허용한다.

## 연결 등록

1. 로그인 후 `POST /api/v1/targets {label,provider:"openstack"}`. 서버가 세션의 `account_id`를 소유자로 기록한다.
2. `GET /api/v1/targets?scope=owned&provider=openstack`에서 본인 환경만 조회한다.
3. `POST /api/v1/targets/{id}/enrollments {}`는 `{id,enrollment_id,target_id,expires_at,token,script_url,script_sha256}`를 한 번 반환한다. 토큰은 256비트 무작위 값이고 30분 후 만료된다. 새 등록 요청은 이전 토큰을 폐기한다. `script_url`에는 토큰이 없다.
4. `GET /api/v1/installers/personal`은 짧은 Bash 설치 스크립트를 응답한다. 사용자는 등록 ID와 토큰을 터미널에 입력한다. 스크립트는 고정된 배포본을 SHA-256으로 검증한 뒤 실행한다.
5. 클라이언트가 `POST /api/v1/enrollments/{id}/claims`에 일회용 토큰을 Bearer로 전달한다. 본문은 `{public_key,client_version,runtime:{ssh_host_key}}`이다. 성공 응답은 `{target_id,generation,client_token,tunnel,vault_public_key,vault_key_id,...}`이다. 클라이언트 토큰은 등록 토큰과 별개다.
6. 클라이언트가 전용 OpenStack 제어 자격을 만든 뒤 `POST /api/v1/targets/{id}/vaults`에 지속 클라이언트 Bearer와 `{generation,project_id,key_id,wrapped_dek}`를 전달한다. 서버는 감싼 데이터 키가 유효한지 확인하고 프로젝트를 환경에 결속한다. 이 엔드포인트는 Application Credential 비밀값을 받지 않는다.

`GET /api/v1/keys/current`는 클라이언트가 사용할 서버 공개키와 키 ID를 반환한다. `GET /api/v1/readiness?scope=personal&phase=connection`은 연결 선행 조건만 확인한다. 기존 `GET /api/v1/readiness?scope=personal`은 실행환경·CI 준비를 포함한 전체 운영 설정 검사다. WireGuard 등록 자체는 `connection_status=ready`를 만들지 않는다. 서버에서 터널 SSH로 `server list`를 성공적으로 실행한 뒤 준비 상태가 된다.
