# 브랜치 관리 및 게시 점검 — 2026-10-10

## 작업 명세

사용자가 승인한 대상 저장소는 `callme-waffle/Railshot`이며 origin URL은 `https://github.com/callme-waffle/Railshot`이다. 이번 작업의 개발 기준은 `dev/v1.0.0`, 통합 PR의 head는 `refactor/onprem-integration`이다. 원격 `main`에 변경을 게시하거나 PR을 병합하는 작업은 포함하지 않는다.

| 항목 | 승인된 기준 및 범위 |
| --- | --- |
| 개발 기준 | `dev/v1.0.0`을 `645e28aefa7f3d3996c57ea6853c6e2f67c5bcc4`에서 생성 |
| 통합 브랜치 이름 | `codex/onprem-integration`을 `refactor/onprem-integration`으로 변경 |
| 통합 시작 HEAD | `96e6f3c4258d1c6527a4090d7a205882b67922f5` |
| 레거시 보존 기준 | 사용자 확정 baseline `645e28a`; 기존 경로를 유지하며 담당 변경을 통합 |
| PR 방향 | base `dev/v1.0.0` ← head `refactor/onprem-integration` |
| 원격 게시 | 사용자가 실제 push와 PR 생성을 승인함; 성공 여부는 원격 증거로 확인 |

브랜치 정책과 레거시 보존 문서는 각각 담당 작업에서 작성하고, 관리 세션이 완료 커밋을 통합한다. 이 문서는 인증 점검과 게시 계획 및 결과를 기록한다. 최종 통합 HEAD는 담당 커밋 통합 후 다시 확정해야 한다.

## 작업 계획

1. 담당 작업의 완료 커밋과 변경 범위를 확인하고 통합 브랜치에 반영한다. baseline과 기존 경로 보존 여부를 점검한다.
2. `dev/v1.0.0`의 SHA와 통합 HEAD를 확정한다. PR의 base/head와 전체 diff를 검토한다.
3. 승인된 저장소에 사용할 기존 인증 수단을 확인한다. 값은 로그·채팅·명령 인자에 출력하지 않는다. 자격 파일 전체 덤프, 무관한 서비스·개인 로그·VM 파일 탐색을 하지 않는다.
4. 사용할 수 있는 인증 경로가 있으면 값을 메모리 또는 비공개 stdin 경로로만 전달하여 push `--dry-run`을 검사한다. 인증이 없거나 권한이 거부되면 실제 게시를 완료로 기록하지 않는다.
5. 관리 세션이 두 브랜치를 실제 게시하고 원격 ref를 확인한다. PR 본문에는 변경 범위, 확정 SHA, 실행한 검증, 남은 제약을 기록한다.
6. 관리 세션이 PR을 생성하고 URL 및 base/head를 조회하여 확인한다. PR 생성과 병합을 구분한다.

## 최초 인증 점검 checkpoint의 검증 게이트

이 표는 App 쓰기 권한 복구와 원격 개발 기준 생성 이전의 점검 결과다. 후속 상태는 아래 원형 Git 이력 보존 게시 경로에 기록한다.

| 게이트 | 통과 조건 | 현재 기록 |
| --- | --- | --- |
| 로컬 개발 기준 | `dev/v1.0.0`이 승인 baseline SHA와 동일 | 확인됨: `645e28aefa7f3d3996c57ea6853c6e2f67c5bcc4` |
| 로컬 브랜치 이름 | 통합 작업 브랜치가 `refactor/onprem-integration` | 확인됨: 점검 당시 HEAD `96e6f3c4258d1c6527a4090d7a205882b67922f5` |
| 담당 변경 통합 | 완료 커밋 통합 및 baseline·경로 검토 | 관리 세션 후속 확인 필요 |
| 로컬 변경 검증 | 최종 diff와 담당 범위에 필요한 검증 통과 | 최종 통합 후 확인 필요 |
| 게시 인증 | 해당 저장소 Git push 또는 connector 쓰기 권한을 실제 사용 가능 | 통과하지 못함 |
| 원격 ref | 원격 개발 기준과 통합 HEAD가 확정 로컬 SHA와 동일 | 실제 게시 후 확인 필요 |
| PR | base/head 및 URL을 생성 결과와 조회 결과로 확인 | 생성 확인 없음 |

## 최초 인증 점검 checkpoint 결과

이 절의 실패와 미완료 상태는 당시 점검 결과이며, 이후 App 권한 설정과 개발 기준 게시 성공을 취소하거나 현재 실패로 판정하는 기록이 아니다.

관리 세션에서 `codex`와 `waffle` 사용자 경로의 Git push dry-run은 사용자명 인증 입력 경로가 없어 실패했다. `gh`도 설치되지 않았다. GitHub connector의 repository 조회는 연결 사용자에게 `admin`/`push` 권한이 있다고 표시했지만, `dev/v1.0.0` 생성 요청은 `403 Resource not accessible by integration`으로 거부됐다. 조회 응답의 사용자 권한만으로 connector의 실제 쓰기 권한을 확인할 수 없다.

독립 점검에서는 기존 인증 수단의 존재 여부만 확인했다. Git `credential.helper` 설정이 없고, `gh` 실행 파일과 codex 사용자의 기본 `gh` hosts 파일, Git credentials 파일, netrc 파일이 없었다. `GH_TOKEN`, `GITHUB_TOKEN`, `GIT_ASKPASS` 환경 변수도 없었다. `SSH_AUTH_SOCK` 변수는 존재했으나 현재 세션에서 SSH 에이전트에 접근할 수 없었다. 키나 토큰 값은 읽어 출력하지 않았다.

사용 가능한 기존 인증 경로를 확보하지 못했으므로 이 독립 점검에서는 추가 dry-run이나 외부 쓰기를 실행하지 않았다. 이 checkpoint에서는 로컬 브랜치 상태만 확인됐으며 원격 push와 PR 생성은 완료되지 않았다.

## 최초 checkpoint의 남은 입력 및 게시 결과 기록

해당 저장소에 실제 Git push가 가능한 승인된 인증 세션, 또는 저장소 쓰기 권한이 정상 동작하는 GitHub connector 연결이 필요하다. 인증 경로 복구 후 관리 세션에서 다음 증거를 기록한다.

- 최종 통합 커밋 SHA 및 포함한 담당 커밋
- `dev/v1.0.0`과 `refactor/onprem-integration`의 push 결과 및 원격 ref SHA
- PR URL, base, head, PR 생성 당시 head SHA
- 최종 변경 검증 결과와 실행하지 못한 검사

이 항목에 실제 결과가 기록되기 전에는 원격 게시나 PR 생성을 완료로 취급하지 않는다.

## 후속 게시 경로 — 원형 Git 이력 보존

사용자가 GitHub App 쓰기 권한을 설정한 뒤 관리 세션에서 `dev/v1.0.0`을 승인 baseline `645e28a`에 실제 생성했다. 기존 Git 인증 경로는 여전히 확보되지 않았다. Connector의 commit 생성 도구는 기존 커밋의 author/time 원형을 지정할 수 없어 해당 도구로 기능 39개 커밋을 다시 만들면 SHA가 달라진다. 따라서 이력 보존을 위한 일회성 Git bundle importer를 준비했다. 원격 개발 기준 생성과 통합 브랜치 게시·PR 생성은 각각 별도 결과다.

1. 관리 세션이 담당 커밋을 모두 통합한 후 최종 HEAD를 확정하고 baseline을 prerequisite로 하는 `refactor/onprem-integration` Git bundle을 생성한다. bundle SHA-256도 별도로 계산한다.
2. 승인 baseline에서 임시 원격 `chore/publish-onprem-integration`을 생성한다. 그 브랜치에만 `.github/workflows/publish-onprem-integration.yml`과 `.github/publication/onprem-integration.bundle.b64` 두 파일을 API로 게시한다. helper commit은 통합 브랜치 또는 PR의 commit 이력에 포함하지 않는다.
3. workflow는 해당 저장소·임시 branch의 push에만 실행되며 `contents: write` 권한만 사용한다. Normal clone의 `dev/v1.0.0` HEAD가 baseline인지 확인하고, event commit에서 bundle을 읽어 base64 decode·SHA-256·`git bundle verify`를 검사한다.
4. bundle이 선언한 통합 ref와 fetch한 ref가 모두 확정 HEAD인지, baseline이 최종 HEAD의 조상인지 확인한다. 원격 통합 branch가 있으면 기존 HEAD가 새 HEAD의 조상인지 먼저 확인한다. force 옵션 없이 Git push하고 원격 ref가 확정 HEAD와 동일한지 다시 조회한다.
5. GitHub token은 workflow 환경의 `github.token`으로만 전달한다. credential helper 코드가 환경 값을 읽어 Git 내부 파이프에 응답하며, 토큰을 명령 인자·파일 내용·workflow 로그에 기록하지 않는다. shell tracing은 꺼 둔다.
6. workflow가 성공한 원격 ref와 원형 SHA를 확인한 후 임시 helper branch의 HEAD가 event commit과 정확히 일치할 때만 해당 branch를 삭제한다. 삭제에는 해당 ref·commit에 한정한 lease를 사용해 확인 이후 remote 변경도 보호하고, 삭제 후 ref가 없는지 재확인한다. 불일치 시 삭제하지 않고 실패하며 알려지지 않은 branch를 삭제하지 않는다. 관리 세션이 PR을 만들고 base/head 및 최종 SHA를 확인한다.

| 후속 게이트 | 확인 방법 | 준비 시점 상태 |
| --- | --- | --- |
| 원격 개발 기준 | 관리 세션의 create-branch 성공 및 baseline 확인 | 생성 성공 보고; 최종 원격 조회는 관리 세션 담당 |
| helper 구문 | Bash syntax와 YAML parse·branch/permission 검사 | 준비 코드 검사 통과 |
| bundle 원형 보존 | baseline prerequisite·declared ref·fetch HEAD 비교 | baseline prerequisite bundle의 로컬 verify·fetch가 기준 SHA와 원형 checkpoint HEAD를 보존함; 실제 최종 bundle은 통합 완료 후 생성 |
| workflow 쓰기 권한 | 임시 branch의 workflow commit API 결과 | 아직 실행하지 않음; scope 부족 시 실제 `403`을 기록하고 연결 권한 입력 필요 |
| Actions 실행 | run 성공과 원격 통합 HEAD 확인 | 아직 실행하지 않음 |
| cleanup·PR | workflow의 일치·lease 보호 삭제와 관리 세션 PR URL 조회 | 아직 실행하지 않음 |

워크플로는 GitHub Actions 실행 허용과 branch 쓰기 정책의 영향을 받는다. 기대하지 않은 원격 분기, SHA 불일치, bundle 손상 또는 권한 거부는 실패로 끝내고 force push로 우회하지 않는다. 준비 코드의 성공을 실제 원격 게시 성공으로 취급하지 않는다.

## 기록 시점과 최종 공개 확인

이 문서는 공개 준비 시점의 점검·계획 기록이다. App 쓰기 권한 복구 후 원격 `dev/v1.0.0` 생성은 성공했고, 원형 이력 importer의 준비 코드와 로컬 bundle 검증도 완료했다. 실제 helper 실행, 통합 branch 공개 및 PR 생성은 이 문서 확정 시점에는 미실행이다.

관리 세션은 통합 HEAD `bb838ad`에서 baseline 원본 832개 blob·mode와 실행 파일 30개의 보존을 독립 검증하여 PASS를 확인했다. 현행 기능 소스 변경 없음, AGENTS 및 PR template 사용자 규칙 검증 PASS, `git diff --check` PASS도 확인했다. 이 문서의 후속 보완 commit은 공개 직전 확정 HEAD에 포함한다.

최종 GitHub 공개 결과는 실제 PR 본문 및 관리 세션 최종 응답의 원격 SHA·PR URL로 확인한다. 아직 기록되지 않은 실제 공개 결과를 이 준비 문서가 완료로 주장하지 않는다.
