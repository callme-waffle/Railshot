# 사용자 API · CLI

홍진기의 `feature/fe-mcp-jingi@6840d1387798d375234bbf97919210eec96709a3`를 바탕으로 통합했다. 정적 화면은 `../dashboard`, Node 서버·CLI는 이 package에 있다. MCP 도구 서버는 `../agent`에 있다. API는 기본적으로 `127.0.0.1:4173`만 수신하며 localhost Host/Origin 검사를 유지한다. 컨테이너 내부 접근에는 아래 명시적 설정과 운영자 Bearer 인증을 사용한다. 사용자 로그인·유저별 target 인가·공개 API는 구현하지 않았다.

## 로컬 시작

Node 22 이상에서 `npm ci --ignore-scripts`, `npm start`를 실행한다. 이 명령은 SDK나 cloud를 실행하지 않는다. 설정이 없으면 화면과 `/healthz`만 사용할 수 있고 제출은 503이다. `npm test`는 실제 GitHub/cloud 없이 mock GitHub와 로컬 HTTP를 검사한다.

실제 CI 제출에는 운영자가 process 환경으로 다음을 설정해야 한다. `.env`를 자동으로 읽지 않는다. 로컬 파일을 사용한다면 직접 `node --env-file=.env src/server.js`로 실행하며 파일은 Git에 추가하지 않는다.

| 변수 | 의미 |
| --- | --- |
| `GITHUB_TOKEN` | 기존 업로드용 apps repository의 Contents·Actions 권한과 SDK 진행 조회용 Checks 읽기 권한을 사용한다. 새 토큰은 필요하지 않으며 응답에 포함하지 않음 |
| `GITHUB_OWNER`, `GITHUB_REPO` | 기본 `Jasmin-Softbank`, `railshot-apps`; 통합 platform source repo와 별도 |
| `GITHUB_REF`, `GITHUB_WORKFLOW` | 기본 `main`, `railshot-deploy.yml` |
| `RAILSHOT_TENANT` | 소문자·숫자 1–20자. 기본 `demo` |
| `RAILSHOT_TARGET_ID` | 필수 운영자 target ID. CI 저장소의 같은 이름 변수와 일치해야 함 |
| `RAILSHOT_TARGET_IDS` | 선택 CI 대상 ID의 쉼표 목록. 기본 target을 포함해야 하며 생략 시 기본 target만 허용 |
| `RAILSHOT_TARGET_PROVIDER` | 기본 target의 명시적 provider. 플랫폼 기본값은 `aws` |
| `RAILSHOT_PROVIDER_TARGETS` | 선택 JSON provider→target 매핑. 예: `{"openstack":"k3s-openstack"}`. 해당 ID의 CI 허용 및 CD 등록이 모두 있어야 선택 가능 |
| `RAILSHOT_API_URL` | CLI와 `../agent` MCP의 API 주소. 기본 `http://127.0.0.1:4173` |

`npm run cli -- deploy <폴더 또는 ZIP 또는 공개 GitHub URL> --app my-app --target aws-demo`, `npm run cli -- status <run_id>`가 같은 HTTP API를 사용한다. target 생략 시 API의 운영자 설정을 사용한다. 임의 target은 거부한다. 이 제출 명령은 실제 설정이 있을 때 GitHub에 소스를 등록하고 CI를 실행하므로 로컬 검증 과정에서는 실행하지 않는다.

provider 매핑은 기존 AWS 기본 대상을 덮어쓰지 않고 추가한다. 서로 다른 provider에 같은 ID를 지정할 수 없다. 대상 등록 후 플랫폼 저장소의 같은 이름 Actions 변수를 설정하면 [플랫폼 릴리스](../../docs/architecture/container-deployment.md)가 매핑과 기본값을 포함한 `RAILSHOT_TARGET_IDS`를 계속 렌더링한다. 매핑 설정은 인프라 생성이나 배포 성공의 증거가 아니다. OpenStack의 기존 서버 등록 범위는 [런타임 등록 계약](../../docs/api/runtime-registration.md)을 따른다.

MCP는 `npm run mcp --workspace @railshot/agent`로 실행한다. `deploy_repository`는 `/api/v1/deployments`를 호출하고 `get_deployment`는 접수된 배포의 실제 상태를 조회한다.

## 컨테이너와 내부 접근

저장소 루트에서 `docker build -f apps/api/Dockerfile --target api -t railshot-api .`와 `--target mcp -t railshot-mcp`로 두 이미지를 만든다. `mcp` target은 `../agent`의 도구 서버를 패키징한다. 두 이미지는 Node 22와 lockfile의 production 의존성을 사용하고 UID 1000으로 실행한다. `NODE_IMAGE` build argument에는 검토한 base image digest를 전달할 수 있다. 소스 코드, Terraform, SSH 키, Docker socket을 추가로 mount할 필요가 없다.

| 변수 | 컨테이너 계약 |
| --- | --- |
| `RAILSHOT_BIND_HOST` | 기본 `127.0.0.1`. 내부 컨테이너 네트워크에서는 명시적으로 `0.0.0.0` |
| `PORT` | API 포트. 기본 `4173` |
| `RAILSHOT_ALLOWED_HOSTS` | 포트를 제외한 정확한 hostname의 쉼표 목록. 예: `localhost,127.0.0.1,railshot-api`. 와일드카드 미지원 |
| `RAILSHOT_ALLOWED_ORIGINS` | 정확한 HTTPS origin의 쉼표 목록. 로컬 개발용 HTTP localhost도 허용. 컨테이너 모드에서 생략하면 Origin이 있는 요청 거부 |
| `RAILSHOT_API_TOKEN_FILE` | 권장: 읽기 전용 Secret 파일. 32–4096자의 공백 없는 ASCII 토큰. API 시작 시 읽고 CLI/MCP는 요청마다 읽음 |
| `RAILSHOT_API_TOKEN` | Secret 환경변수 대안. `_FILE`과 동시에 설정하면 시작/요청 실패 |
| `RAILSHOT_API_URL` | MCP/CLI가 접근하는 내부 API URL. Compose에서는 `http://api:4173`처럼 실제 Service 이름 사용 |

비로컬 bind 또는 비로컬 Host를 허용하면 명시한 Host 목록과 API 토큰이 없을 때 시작을 거부한다. `/api/*`는 해당 토큰을 `Authorization: Bearer ...`로 요구하며 CLI/MCP가 이를 전달한다. `x-railshot-request: deploy`는 계속 필요한 교차 사이트 요청 방어 헤더이며 인증을 대신하지 않는다. Host/Origin은 프록시의 `X-Forwarded-*`를 신뢰하지 않고 실제 요청 헤더로 검사한다. CORS endpoint나 브라우저 토큰 배포는 추가하지 않았다.

`/healthz`는 Host/Origin 검사 후 Bearer 없이 조회한다. 비로컬 모드에서는 `ok`와 `configured`만 반환하며, `configured: true`도 GitHub 권한·CI worker·배포 대상의 실시간 준비 상태를 보장하지 않는다. 이미지의 자체 healthcheck에는 `127.0.0.1`을 Host 목록에 포함해야 한다. Kubernetes HTTP probe도 허용된 Host를 명시한다. 토큰을 교체하면 API를 재시작하고 클라이언트의 파일도 교체한다.

MCP 이미지는 `docker run --rm -i ... railshot-mcp` 또는 Compose의 `run --rm -T mcp`로 시작한다. stdin/stdout JSON-RPC만 제공하며 HTTP 포트·Service·Ingress를 만들지 않는다. Compose는 소유자 전용 `.local/mcp-sessions`에 API 세션 쿠키를 보관한다. `GITHUB_TOKEN`은 API에만 주입한다. API 토큰은 운영자 클라이언트 인증이며 사용자별 로그인·target 인가를 대체하지 않으므로 API는 내부 ClusterIP 또는 루프백 포트로 먼저 배치한다.

제품 API는 기존 source HTML 경로를 유지한다. 배포용 Vite `dist/`는 별도 dashboard 이미지가 제공하며 아직 `/api` 프록시나 UI 제출 연결은 없다. 나중에 프록시를 연결할 때 `/api` 경로를 보존하고 최대 101 MiB multipart 본문·업스트림 처리 시간·HTTPS Origin·사용자 인증을 함께 검토한다.

## 연결한 동작

- 앱 이름은 CI와 같은 3–30자 규칙이다. 영문 소문자로 시작하고 영문 소문자나 숫자로 끝나며 중간에는 하이픈을 쓸 수 있다.
- source 등록 commit SHA와 target ID를 workflow에 전달한다. CI checkout과 source SHA가 다르면 실행을 거부해 concurrent 등록 drift를 드러낸다.
- 상태는 loop/release 두 job을 읽는다. 완료된 재실행에 job이 없으면 이전 attempt의 실제 producer를 찾는다. 최신 실패를 옛 성공으로 대체하지 않는다.
- `published-<producer attempt>`의 유일하고 만료되지 않은 artifact를 고유 ID로 다운로드해 run/source/target과 evidence 파일 해시를 확인한다. 이를 통과해야 state가 `published`다.
- `published`는 이미지 게시 및 CD 자료 생성이다. 앱 적용, Argo 상태 관측, 외부 HTTP와 URL은 이 API가 수행하지 않는다. `url`은 null이다.

[API 계약](docs/interface.md), [소스 형식](docs/source-formats.md), [CI→CD 경계](../../docs/api/ci-publication.md)를 함께 참고한다.

Legacy `JASMIN_TENANT` and `JASMIN_API_URL` remain fallback aliases; `RAILSHOT_*` values take precedence. The server accepts `x-jasmin-request` for older clients. The CLI sends both request headers with the same value for existing servers. Historical `jasmin.yaml` artifacts keep their original hashes. See [naming compatibility](../../docs/api/naming-compatibility.md).

개인 OpenStack 환경의 장기 소유권·등록·상태·동적 대상 배포·삭제 계약은
[개인 환경 API](../../docs/api/personal-environments.md)를 참조하세요.
`RAILSHOT_PERSONAL_CONFIG`는 비공개 운영자 설정 파일을 가리킵니다.
무구성 상태에서는 등록 기록과 복구키를 관리할 수 있지만, 실제 설치 자격·배포는 준비되지 않았다고 응답합니다.
