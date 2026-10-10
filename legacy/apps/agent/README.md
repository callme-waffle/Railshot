# RailShot 서비스 내부 에이전트

Node 22 이상에서 실행합니다. 이 패키지의 stdio MCP 서버는 RailShot 제품 API의 조회와 공개 GitHub 저장소 배포 요청을 도구로 제공합니다. 별도 HTTP 대화 에이전트는 모델의 배포 제안을 승인 토큰으로 확인한 뒤 실행합니다. 외부 AI가 MCP 서버를 직접 등록해 `deploy_repository`를 호출하면 이 HTTP 승인 절차를 거치지 않고 즉시 제품 API에 배포 요청을 보냅니다. 이 경우 사용자 확인은 MCP 호스트에서 설정해야 합니다. API의 공통 입력 검증·멱등성·상태 기록을 사용하며, MCP가 인프라에 직접 접근하지 않습니다.

## 설정과 실행

저장소 루트에서 `npm ci --ignore-scripts` 후 다음 환경변수를 설정합니다.

| 변수 | 의미 |
| --- | --- |
| `OPENAI_API_KEY` | 서버에서만 사용하는 모델 API 키 |
| `RAILSHOT_AGENT_MODEL` | Responses API에서 사용할 모델명 |
| `RAILSHOT_AGENT_TOKEN` | 에이전트 HTTP 호출용 32자 이상 비밀 토큰. 승인 토큰 서명에도 사용 |
| `RAILSHOT_API_URL` | 제품 API 원점. 기본 `http://127.0.0.1:4173` |
| `RAILSHOT_API_TOKEN` 또는 `RAILSHOT_API_TOKEN_FILE` | 제품 API의 내부 Bearer 토큰. 둘 중 하나만 설정 |
| `RAILSHOT_AGENT_SESSION_DIR` | 선택 사항. 제품 API 세션 쿠키의 사용자 전용 저장 디렉터리. 기본 `~/.local/state/railshot-agent`; 절대 경로와 소유자 전용 권한 필요 |
| `RAILSHOT_AGENT_PORT` | 로컬 포트. 기본 `4184` |

`npm run start --workspace @railshot/agent`로 서비스, `npm run mcp --workspace @railshot/agent`로 독립 stdio MCP 도구 서버를 시작합니다. HTTP 서비스는 `127.0.0.1`에만 바인딩하고 브라우저 Origin 요청을 거부합니다. 대시보드에서 사용할 때는 사용자 인증을 제공하는 서버 프록시가 필요합니다. 토큰은 브라우저에 전달하지 마세요.

로컬 stdio MCP 진입점은 `apps/agent/src/mcp.js`입니다. 원격 Streamable HTTP MCP 진입점은 `apps/agent/src/remote-mcp.js`입니다. `apps/api/Dockerfile`의 `mcp` 이미지는 기본적으로 stdio를 실행하며 원격 배포에서는 명령을 `node src/remote-mcp.js`로 바꿉니다. 제품 API의 구형 `/api/deploy`는 CLI 호환 경로로 남아 있지만 MCP 배포에는 사용하지 않습니다.

## 로컬 프로젝트 배포 MCP

로컬 폴더·ZIP을 배포하려면 사용자 컴퓨터에서 **`apps/agent/src/mcp-local.js`**를 실행합니다.
기존 도구에 `deploy_local_project` 하나를 추가하는 별도 stdio 진입점입니다.
원격 `/mcp`와 기존 서버용 진입점에는 로컬 파일 경로 도구가 노출되지 않습니다.
저장소 루트에서 의존성을 설치해야 합니다(`npm ci --ignore-scripts`). `apps/agent`만 복사하면
공유 파일 패키징 코드를 사용할 수 없습니다. Node 22 이상을 사용합니다.

```sh
codex mcp add railshot-local --env RAILSHOT_API_URL=https://railshot.io -- node /absolute/path/to/Railshot/apps/agent/src/mcp-local.js
```

도구 호출 예시:

```json
{
  "path": "/home/user/my-app",
  "app": "my-app",
  "target_id": "<list_targets에서 선택한 ID>",
  "idempotency_key": "local-my-app-attempt-1"
}
```

- 도구가 지정된 폴더 또는 ZIP을 읽어 기존 `/api/v1/deployments` 업로드 API로 직접 보냅니다.
  GitHub에 올릴 필요가 없고 소스 내용은 모델에 반환하지 않습니다. 접수 결과에는 파일 수·ZIP 크기·SHA-256만 추가합니다.
- 기존 CLI 패키징과 API ZIP 검사를 재사용합니다. `.git`, `node_modules` 등은 제외합니다.
  `.env`, 키 파일, 심볼릭 링크 등은 자동 삭제하지 않고 **전송 전에 거부**하므로 제거한 사본을 지정하세요.
  ZIP 입력도 검증한 파일만 다시 묶어 제외 항목이 실제 전송 바이트에 남지 않게 합니다.
- 파일 2,000개·소스 100 MB·ZIP 100 MB 제한을 적용합니다. 비밀 파일 이름 검사는
  소스에 하드코딩된 모든 비밀값을 탐지하는 기능은 아닙니다.
- 응답을 잃으면 자동으로 재배포하지 않고 `outcome_unknown=true`를 반환합니다.
  재확인 시 동일한 소스·앱·대상·요청 키를 유지합니다. 멱등성 판정과 CI/CD 실행은 기존 API가 담당합니다.
- 배포 후 `resource_id`로 **같은 railshot-local MCP**의 `get_deployment_progress`,
  `get_app_overview`를 호출합니다. 원격 OAuth/브라우저 세션과 로컬 쿠키 세션은 자동 공유되지 않습니다.
  로컬에서 만든 배포가 기존 웹/OAuth 세션 내역에 자동으로 나타난다고 가정하면 안 됩니다.
- 공개 데모 API는 기존 익명 세션 방식을 사용합니다. 보호된 사설 API라면 기존 운영자 인증 구성이
  필요합니다. 원격 MCP OAuth 토큰을 제품 API의 내부 토큰으로 대신 사용하지 않습니다.

이 기능은 배포 pipeline, 소스 자동 수정, 환경 생성 및 관측 자동 등록을 추가하지 않습니다.
핵심 검사: `node --test apps/agent/test/local-project.test.js`.

## ChatGPT·Codex·Claude 원격 연결

운영 플랫폼에서 dashboard, api, mcp 이미지를 함께 배포하면 Nginx가 `https://railshot.io/mcp`를 MCP 서버로 연결합니다. 공개 OAuth 메타데이터는 `/.well-known/oauth-protected-resource/mcp`와 `/.well-known/oauth-authorization-server`에 있습니다. MCP 서버는 내부 API 토큰을 서버 측에서만 사용합니다.

- ChatGPT에서는 개발자 모드를 켜고 Plugins의 새 MCP 연결에 `https://railshot.io/mcp`를 입력합니다. 인증 방식은 OAuth이며 동적 클라이언트 등록(DCR)을 사용합니다.
- Codex CLI에서는 `codex mcp add railshot --url https://railshot.io/mcp` 후 `codex mcp login railshot --oauth-client-registration dcr`을 실행합니다. Codex 앱 또는 IDE에서는 Streamable HTTP 서버 URL을 추가하고 인증을 시작합니다.
- Claude 웹·Desktop에서는 **Customize → Connectors → Add custom connector**에서 이름 `Railshot`, URL `https://railshot.io/mcp`를 입력합니다. OAuth 인증을 선택하고 **OAuth client는 `Register automatically`**로 설정합니다. 기본으로 제안되는 `Use Claude’s published identity`는 이 서버가 지원하지 않습니다. Request headers에는 내부 API 토큰이나 고정 Bearer 토큰을 넣지 않습니다. Team·Enterprise에서는 조직 소유자가 Organization settings → Connectors에서 먼저 등록하고 각 사용자가 Connect합니다. 대화에서 Connectors 메뉴의 Railshot을 켭니다.
- Claude Code에서 이 저장소를 열면 루트 `.mcp.json`의 원격 서버 설정을 검토·승인한 뒤 `claude mcp login railshot` 또는 대화의 `/mcp`에서 브라우저 인증을 완료합니다. 다른 프로젝트에서는 `claude mcp add --transport http railshot https://railshot.io/mcp`를 실행해 로컬 범위로 추가할 수 있습니다. `claude mcp list`로 연결 상태를 확인합니다. 별도의 Claude 전용 MCP 서버나 사용자 컴퓨터의 `RAILSHOT_API_TOKEN`은 필요하지 않습니다.
- 승인 화면은 **웹 앱을 쓰던 동일한 브라우저**에서 열어 `연결 승인`을 누릅니다. 이때 기존 `railshot_session` 쿠키를 AI 연결에 묶습니다. 웹 방문 기록이 없는 브라우저에서는 새 익명 세션을 발급하며, 이후 그 브라우저에서 Railshot을 열면 AI가 만든 앱을 같은 세션에서 볼 수 있습니다.

원격 AI 연결마다 별도 OAuth Bearer 토큰을 발급하고 서버에서 해당 토큰을 정확히 하나의 API 세션 쿠키에 연결합니다. 인증 코드와 Bearer 토큰은 MCP 프로세스 메모리에만 보관하므로 MCP Pod가 재시작되면 AI에서 OAuth 연결을 다시 승인해야 합니다. 등록된 클라이언트 ID는 내부 API 토큰으로 서명되어 재시작 후에도 재사용할 수 있습니다. 승인된 OAuth Bearer 토큰과 연결된 웹 세션에는 만료 시간을 두지 않습니다. OAuth 승인 코드는 탈취 방지를 위해 10분·일회용으로 유지합니다. 서로 다른 브라우저 세션의 앱은 자동 병합되지 않습니다.

## HTTP 계약

모든 요청은 `Authorization: Bearer <RAILSHOT_AGENT_TOKEN>`을 사용합니다.

- `POST /v1/messages`, `Content-Type: application/json`, 본문 `{"message":"demo 대상의 배포 상태를 확인해 줘"}`: `answered`와 답변·읽기 도구 결과를 반환합니다. 배포가 제안되면 `approval_required`, 설명, `action`, `approval_token`을 반환하고 실행하지 않습니다.
- `POST /v1/approvals`, 본문 `{"approval_token":"..."}`: 제안된 인자를 변경하지 않고 MCP 도구로 배포를 한 번 요청합니다. 토큰은 10분 유효하며 같은 토큰 재호출은 제품 API의 같은 `Idempotency-Key`로 처리됩니다.
- `GET /healthz`: 프로세스 생존만 나타냅니다.

공통 MCP 도구는 `list_options`, `list_targets`, `get_deployment`, `get_deployment_progress`, `get_build`, `deploy_repository`, `get_app_overview`, `get_deployment_evidence`입니다. 배포 도구의 성공은 **요청 접수**이며 배포 성공이 아닙니다. 배포 완료는 `get_deployment`에서 대상 적용 및 공개 HTTP 검증 후 `status=succeeded`와 URL을 확인해야 합니다. 공통 MCP 배포 소스는 공개 GitHub 저장소 URL만 지원하며, 위 로컬 전용 진입점에서만 폴더·ZIP 업로드를 추가합니다. 환경 생성 및 소스 자동 수정은 제공하지 않습니다.

배포 접수 후 반환된 `resource_id`를 `get_deployment_progress.deployment_id`로 전달하면 현재 배포 단계·상태와 `/events`의 `agent_activity`를 함께 조회합니다. `agent_activity`에는 AI의 상태·작업 요약·현재 작업·시도 횟수·시간·변경 파일과 적용 여부·재검증 결과·이전 시도·관측 상태가 담깁니다. `run_attempt`, `events_status`, `events_state`, `activity_cursor`, `deployment_updated`, `agent_activity_updated`도 반환합니다. 다음 조회에서는 직전 `activity_cursor`를 `since`에 그대로 전달하세요. `deployment_updated`이면 단계·상태를, `agent_activity_updated`이면 AI 작업 변화를 대화에 설명합니다. 이전 GitHub Actions 시도나 낮은 revision의 응답은 새 AI 작업으로 표시하지 않습니다. `agent_activity.state=succeeded`는 자동 처리의 결과일 뿐, 배포 성공 여부는 최상위 `status`와 검증된 URL로 별도로 확인해야 합니다.

실패 시 이 도구는 대시보드가 읽는 동일한 `/diagnostics` API에서 해당 앱·대상·소스 커밋·CI 실행에 연결된 검증된 원인과 로그를 최대 4개·각 4,000자로 반환합니다. `poll_after_ms`가 있으면 MCP 클라이언트가 그 간격 뒤 다시 호출해 바뀐 내용만 대화에 표시할 수 있습니다. 이벤트 조회 실패나 갱신 지연은 `events_state`와 `agent_activity.observation`을 확인하고 최신 상태로 단정하지 마세요. MCP 서버 자체는 대화창에 메시지를 먼저 보내거나 사용자가 대화를 닫은 뒤에도 자동으로 갱신할 수 없습니다. 진단이 아직 없거나 조회할 수 없으면 `diagnostics.state=unavailable`이며 실패 원인을 추측하지 마세요.

로컬 stdio MCP는 API 원점별 세션 쿠키를 `RAILSHOT_AGENT_SESSION_DIR`에 저장해 후속 조회와 MCP 프로세스 재시작 시 재전송합니다. 같은 API 원점과 쿠키 디렉터리를 공유하는 로컬 MCP 프로세스는 제품 API에서 같은 세션으로 취급됩니다. 이 쿠키는 해당 세션의 작업 조회 권한을 가지므로 디렉터리와 파일을 다른 OS 사용자가 읽을 수 없게 유지합니다. 원격 HTTP MCP는 이 공유 파일을 사용하지 않습니다. 원격 API에서 서로 다른 쿠키를 사용하는 세션의 배포 ID 조회는 차단되지만, 비공개 localhost의 쿠키 없는 유지보수 요청은 세션 소유권 제한을 적용하지 않습니다. 사용자 컴퓨터에 내부 API Bearer 토큰을 배포하는 것은 별도의 사용자 인증·권한 모델을 대신하지 않습니다.

`npm test --workspace @railshot/agent`는 모델·클라우드 호출 없이 승인 흐름과 실제 MCP stdio 프로토콜 및 제품 API 전달을 확인합니다.

실제 시험 대상에서는 운영자가 연결한 API와 등록된 대상에 대해 `list_targets` → `deploy_repository` → 반환된 `resource_id`로 `get_deployment`를 호출해 접수와 후속 조회를 확인합니다. `deploy_repository`는 실제 배포를 시작하므로 시험용 공개 저장소·앱 이름·대상을 사용하고 MCP 호스트에서 실행을 승인한 뒤 호출하세요. `accepted`는 CI/CD 완료를 의미하지 않습니다.
