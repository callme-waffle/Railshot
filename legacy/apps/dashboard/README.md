# RailShot dashboard

저장소 루트에서 `npm ci --ignore-scripts` 후 `npm start --workspace apps/api`를 실행하고
`http://127.0.0.1:4173`을 연다. 로그인·회원·브라우저 토큰 입력은 없다.
공개 배치의 설정과 실행 범위는 [제품 API 운영 계약](../../docs/api/product.md)을 따른다.

- UI 기준은 `006ad34`의 소스 → 클라우드/온프레미스 → 선택 내용 확인 → 배포 시작이다.
  기존 카드·OpenStack/Proxmox 선택·콘솔 탭을 유지한다. 화면은
  `/api/v1/options`에서 준비 상태를 읽고 소스와 environment/provider만
  deployments API에 보낸다. 등록 대상의 ID·앱 이름 결정과 실행 가능 여부 검사는 서버가 수행한다.
  미연결 provider는 명확히 차단하며 다른 대상에 배포하지 않는다.
- 202의 `Location`과 자원 ID를 확인한 뒤 15초마다 상태를 조회한다. 이미지 게시와
  앱 배포 성공을 구분한다. 배포 성공 및 HTTP 검증 시각이 있을 때만 앱 링크를 표시한다.
- HttpOnly 익명 세션 쿠키로 배포·빌드 이력을 분리한다. 실행 토큰·소스·자격증명은
  localStorage에 저장하지 않는다. 내역은 종류별 최신 접수 순으로 10건씩 서버에서 읽고
  next_marker로 이동한다. 페이지 위치는 현재 탭의 history.state에 유지하며, 최신 내역·
  실행 종류 변경·새 배포 접수는 첫 페이지로 돌아간다. 목록 실패를 빈 이력으로 표시하지 않는다.
- OpenStack 선택 시 사용자 키나 Keystone 인증정보를 받지 않는다. 등록 API가 일회성 토큰을 발급하며
  같은 브라우저 세션의 미완료 등록은 만료 후 재발급할 수 있다. 화면에는 토큰이 포함된
  `curl` 다운로드 명령과 기존 `install.sh`의 파일 다운로드·코드 복사·동반 파일 ZIP 다운로드를 제공한다.
  현재 스크립트는 연계 토큰 입력과 WireGuard 신규 연결을 지원하지 않는다.
- 환경 모니터링은 `/targets`의 접근 가능한 공용·세션 환경과
  `/targets/{id}/observations`를 조회한다. AWS/GCP/OpenStack 필터, 노드 수집 정상·수집/응답 실패·미수집·
  오래된 관측 요약, 지표별 수집 시각을 표시한다. 최대 4개 환경을 동시에 조회하고
  화면이 열려 있을 때 30초마다 갱신한다. CPU·메모리·루트 디스크·네트워크는 대상 노드 전체다.
  미수집/실패/stale은 0으로 바꾸지 않으며 과거 시계열을 합성하지 않는다.
  환경 필터는 아래 선택 실행의 상세·앱 로그를 바꾸지 않는다.
- 제출은 자동 재시도하지 않는다. 배포 재요청에는 기존 Idempotency-Key를 유지한다.
  상태 조회 중지와 페이지 종료는 서버 실행을 취소하지 않는다. unknown 결과는 자동
  재실행하지 않으며 서버가 재조정될 때까지 신규 접수를 차단한다.
- 선택한 provider에 앱 배포 사양이 하나 연결되어 있으면 같은 카드에서 새 환경을 준비한다.
  DB 사양은 PostgreSQL·DCS·proxy 수량을 보여 주며 required DB는 해제할 수 없다.
  소스 이름에서 앱 이름을 정하고 `/plans`로 비용·만료 시각을 확인한 뒤 하나의 `plan_id`로
  환경 준비부터 DB 연결·앱 배포까지 요청한다. 사양이 여러 개이거나 준비되지 않았으면 차단한다.
  계획 불일치·만료·예산 초과는 자원 생성 전에 차단하며 비용은 확인 화면에 표시한다.
- 환경 계획 `/plans`는 최대 10분 기다린다. 앱 중지·재개·삭제 계획은 `Prefer: respond-async`로 접수하고 반환된 계획 주소를 2초마다 조회한다. 경과 시간·실패 이유·다시 확인 버튼을 표시하며, 창을 닫거나 새로고침해도 저장한 계획 ID로 다시 조회한다. 계획 조회 실패는 같은 ID로 재조회하고, 계획 자체가 실패하거나 만료됐을 때만 새 계획을 생성한다. 삭제 실행은 유효한 계획을 확인한 별도 승인 이후에만 접수한다.
  컨테이너 Nginx와 외부 ALB의 timeout은 610초다. 다른 POST는 120초, 조회는 15초다.
- 환경 준비·이미지 게시 API는 기존 CLI/운영 경로에서 유지한다. 앱 배포 화면에
  내부 실행 종류나 target ID 선택을 추가하지 않는다. 콘솔은 확인된 작업·환경 상태를
  표시하며 앱 로그 수집이 미연결이면 그 상태를 표시한다.

UI 단독 개발은 `npm run dev`(Vite 4181), 정적 빌드는 `npm run build`다. API 등록 기능까지
확인하려면 `deployment/compose.yaml`의 Dashboard Nginx와 API를 함께 실행한다. Dashboard
Nginx가 `/api/`와 `/onpremise/install.sh`를 프록시하며 운영자 Bearer는 서버에서만 붙인다.
브라우저 검사는 [ci/browser](../../ci/browser/README.md)를 따른다.

개발 서버의 실제 페이지(`/`)도 같은 프런트엔드를 사용합니다. `npm start --workspace apps/api`로
로컬 API(기본 4173)를 먼저 실행하면 Vite가 `/api/`와 `/onpremise/install.sh` 요청을 전달합니다.
다른 로컬 API 주소는 `RAILSHOT_DEV_API_TARGET=http://127.0.0.1:포트 npm run dev`로 지정합니다.
세션 쿠키는 브라우저가 연 개발 서버 주소에서 유지되며, 운영자 인증정보를 프런트엔드에 추가하지 않습니다.
API가 실행되지 않았거나 이 세션에 배포 기록이 없으면 실제 내역은 표시되지 않습니다.
예시 기록은 `/preview/history.html`에서만 표시합니다.

앱+DB 브라우저 검사는 같은 카드에서 DB 계획 식별자, 필수 DB, DB 없는 사양, 비용 표시,
예산·만료·불일치 차단을 localhost fixture로 확인한다. 실제 클라우드는 호출하지 않는다.

## Repeated app submissions

The new-deployment form resolves the source-derived or explicit app name through `GET /api/v1/applications/resolve` using the selected provider's registered environment. The lookup is server-owned and independent of the visible application inventory page. A successful app owned by the same session opens the existing update preview with the selected GitHub/ZIP/folder source preserved; the app ID, target, namespace and public address stay fixed. The user reviews added/modified/deleted files before starting. Verified identical source can finish unchanged without CI, or be explicitly rebuilt.

Another session's matching name remains an ownership conflict. Different names/environments are separate apps; repository URL alone does not select an app because one repository may have multiple deployments. Stopped/deleted/pending apps are not silently recreated. A ready registration with no successful baseline can retry normal deployment. The explicit app-detail update flow still supports source files whose archive/repository name changes.

An uncertain published CD/HTTP result fences its registered environment, so a GCP route reconciliation does not block a same-named AWS application. Uncertain CI/registration or missing environment bindings keep the conservative name-level fence because CI source paths are shared. The affected environment still requires reconciliation; this does not retry it or alter its resources.

짧은 API 교체 중에는 읽기/비동기 계획 요청과 동일 Idempotency-Key를 가진 앱 작업 요청의 전송을 제한된 횟수로 재시도한다. 서버가 접수 전에 반환한 `PLATFORM_UPDATING`도 같은 요청으로 이어간다. 키 없는 변경 요청이나 앱 상태 충돌은 자동 재전송하지 않는다. 중단 버튼/창 닫기는 재시도 대기도 취소한다.

## Update UI design system

The update workflow uses pinned [Basecoat 1.0.2](https://basecoatui.com/installation/) Vega Button, Field/Input and Badge components with native disclosure and a bounded review workspace. Basecoat is a framework-independent shadcn-style library; no React migration, remote CDN, font request or extra runtime JS is required.

`node apps/dashboard/sync-design-system.mjs` bundles the official component stylesheet into `styles.css`, scoped to `.update-mode`. Global Tailwind property definitions are preserved; root tokens are rebound to the scope root. The production build checks the bundle against the pinned npm package. This serves the same CSS through the API's existing raw dashboard and Vite without adding backend routes. The MIT notices remain embedded in the distributed CSS.

The app and last verified service are fixed context. Source, review and execution are exclusive stages. The review workspace separates file changes from the execution summary; destructive file removal is explicit but not a large warning banner. Full URLs, commits and expiry timestamps live in details. Expiry disables dispatch and offers re-review. An uncertain start retries the same preview and locks source editing. No-change completion and explicit rebuild retain their server semantics.

## 배포 이력 화면과 선택형 응답 (프런트엔드 우선 구현)

AI의 처리 중 상태·변경 파일·검증 결과를 연결하는 후속 설계는
[대시보드 AI 자동 복구 표시 설계](../../docs/api/dashboard-agent-recovery.md)를 참고합니다.
조회 전용 카드는 기존 events API와 연결됩니다. 질문 제출·응답 이후 재개는 후속 작업입니다.

첨부 디자인 `8_mockup_deploy_history.html`을 기준으로 카드형 목록, 날짜·상태·앱 이름 필터,
접속정보, 배포 이력 표, 단계별 로그 화면을 구성했습니다. 목록의 필터는 현재 서버 페이지에만
적용됩니다. 상세의 이력 표도 현재 불러온 동일 앱·동일 환경 기록만 표시합니다.
`현재` 표시는 앱 API의 검증된 `current_deployment`와 일치할 때만 나타납니다.
기존 앱 관리·소스 다운로드는 카드의 `배포 관리`와 기존 앱 목록에 유지합니다.
오류 없는 배포도 에이전트 활동 이력이 있으면 처리 내역을 표시합니다. 오류가 있는 이력 행은 상태 옆에 오른쪽 화살표를 표시하며, 행을 선택하면 해당 실행의 파이프라인·오류 사유로 수평 전환합니다. 돌아가기는 원래 행으로 초점을 복원합니다. 전환은 240ms이며 키보드로 열 때는 즉시, 동작 줄이기 설정에서는 위치 이동 없이 표시합니다.
수집되지 않은 상태·이미지·주소·단계 로그는 추정하여 채우지 않습니다.

화면과 백엔드의 경계는 `src/deployment-history.js`의 `createHistoryDetail`입니다.
기존 조회 API만 사용하며, 새 응답 API의 경로는 아직 가정하지 않습니다.
API 서버의 변경은 프런트엔드 JS 파일 2개의 정적 제공 경로 추가뿐입니다.
`recovery.load`와 `recovery.submit`이 연결되지 않은 운영 화면에서는 해결 질문을 생성하거나
응답을 보내지 않습니다. 파이프라인의 AI 호출·자동 수정·재시도는 변경하지 않았습니다.

백엔드 확정 후 다음 두 함수를 주입하면 됩니다. 아래는 **프런트엔드 내부 규격 제안**이며
구현된 서버 명세가 아닙니다. 실제 서버 응답의 변환은 이 연결 부분에서 수행합니다.

- `load(deployment, { signal })`: 질문 또는 `null`을 반환합니다. 질문에는 `id`,
  `deployment_id`, `revision`, `expires_at`, `stage` (`build/environment/deploy`),
  `summary` (원인 한 줄 요약), `prompt`, `evidence: [{ label, text }]`, `options`가 필요합니다.
- 각 선택지는 `{ id, label, description?, fields: [...] }`입니다. 후속동작은 하나만 선택합니다.
  각 추가 입력은 `{ id, label, type, required, sensitive?, choices? }`입니다.
  `type`은 `single_select`, `multi_select`, `text`, `text_list`를 지원합니다.
  선택 항목은 `choices: [{ id, label }]`로 제공합니다. 민감한 문장은 `sensitive: true`로 가립니다.
- `submit(answer)`: `{ deployment_id, question_id, revision, option_id, values }`를 받습니다.
  해당 선택지의 값만 전달하며, 접수 응답은 `{ status: 'accepted', question_id, revision }`입니다.
  접수 성공은 배포 재개 성공을 의미하지 않습니다. 같은 질문·버전의 중복 전송은 화면에서 막습니다.
  전송 결과가 불명확하면 자동 재전송하지 않고 상태 재조회를 안내합니다.

필수값·허용 선택지·만료·질문 버전·배포 식별자를 화면에서 확인합니다.
서버에서도 동일한 검증과 권한 검사·중복 방지를 반드시 구현해야 합니다.
응답 입력값은 브라우저 저장소에 저장하지 않으며 화면 이동 시 폐기합니다.
새로고침 이후의 질문 복원·기접수 여부 확인은 서버의 조회 계약이 확정된 뒤 연결해야 합니다.

### 디자인 및 입력 동작 미리보기

`npm run dev` 후 `http://127.0.0.1:4181/preview/history.html`을 여세요.
`auth-service` 카드에서 네 가지 추가 입력과 선택지 전환을 확인할 수 있습니다.
미리보기는 예시 데이터로만 동작하며 실제 배포·응답 전송을 하지 않습니다.
미리보기 파일은 운영 진입점에서 가져오지 않으며 기본 Vite 빌드에 포함되지 않습니다.

검증: `npm run build` 및 기존 `ci/browser` 패키지의 `deployment-history.test.mjs`.
`RAILSHOT_HISTORY_SCREENSHOTS`에 경로를 지정하면 목록·실패 상세의 데스크톱/모바일 이미지를 저장합니다.
