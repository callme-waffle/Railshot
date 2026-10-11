# CI와 이미지 게시

## Railshot 저장소의 PR 검사

사용자 앱을 게시하는 아래 템플릿과 별도로, [Railshot CI](../.github/workflows/railshot-ci.yml)는 PR과 `integration/**` push에서 플랫폼 통합본을 검사한다. GitHub가 workflow를 인식하는 `.github/workflows/`에는 실행 연결만 두고 검사 구현은 `ci/`와 기존 담당 경로에서 재사용한다.

| 검사 | 실행 범위 |
|---|---|
| Python / OpenStack contracts | 기존 CI·Ansible·runtime·GitOps·provider 검사와 OpenAPI. observability는 실제 promtool 설정 검사와 loopback HTTP 200/503/redirect probe를 포함. 모델·외부 API는 mock |
| HTTP publication and browser E2E | 실제 HTTP ZIP 업로드 → Python 게시 산출물 → API 읽기. GitHub·registry는 mock. Chromium은 실제 dashboard 선택·검토·탐색을 실행 |
| Terraform validation | 모든 현재 모듈의 fmt/init/validate 및 기존 계약 검사. credentials/backend/apply 없음 |
| Database Ansible contracts | 팀 DB playbook 9개의 native syntax/lint, 실제 localhost 입력·템플릿 생성 15개, 기존 guest/runtime syntax. DB 서비스·복제·장애 전환 실행은 아님 |
| Linux amd64 runtime E2E and cleanup | 일회성 GitHub runner에 실제 K3s/Cilium 설치 → Pod·서비스·HTTP 검증 → 소유 자원과 클러스터 정리 |
| Railshot CI gate | 위 검사가 모두 성공해야 통과하는 고정 이름의 합산 검사 |

PR 검사에는 cloud·모델·private registry 자격을 제공하지 않는다. 브라우저 검사는 현재 선택·검토 UI가 요청을 아직 전송하지 않는다는 사실도 확인한다. 통과를 제품 UI에서 AWS/GCP로 자동 배포한 결과로 해석하지 않는다. Vercel preview는 별도 연동이다.

실패 브라우저 trace·스크린샷과 runtime 요청·단계 결과·정리 영수증은 run/attempt별 artifact로 7일 보존한다. runtime은 `finally`와 workflow `always()`에서 정리하며, 정리 실패도 gate를 실패시킨다. [E2E 배포 해제 경로](../docs/integration/e2e-teardown.md)에 재시도와 실제 클라우드 자원 정리 범위를 기록했다.

로컬 Python 의존은 `python -m pip install -r ci/requirements-test.txt`, 브라우저 검사는 [ci/browser](browser/README.md)를 따른다. runtime wrapper는 기존 개발·운영 노드에서 실행하지 못하도록 hosted runner·소유권을 검사한다.

`workflows/railshot-deploy.yml`은 private apps 저장소에서 사용할 workflow 템플릿이다. `loop`와 `release` 두 job만 포함한다. 업로드 원본 → baseline gate → 필요한 adapter/fixer 수정 → 전체 gate → 검증한 이미지 bundle → GHCR 게시를 담당한다. UI·Provider API·클러스터 설치·CD·DB·공개 ingress는 각 담당 영역에서 연결한다. 공통 설계와 역할은 루트 README를 따른다.

## 실행 경계

- `loop`: 임대한 전용 CI worker에서 실행한다. 관리자가 고정한 `PLATFORM_REF`, 입력 경로, 영속 `RAILSHOT_RUN_ROOT`, 설치 네트워크와 SDK 인증 경로를 검사한다. 부분 gate 성공이나 SDK의 자체 보고로 게시를 허용하지 않는다.
- `release`: 보호된 `railshot-release` environment의 hosted worker에서 실행한다. producer가 반환한 bundle artifact ID로 다운로드하고 `bundle.py publish`로 검증한 이미지만 게시한다. 사용자 source를 다시 빌드하거나 실행하지 않는다.
- GHCR prefix·visibility·게시 자격은 trusted release 설정이다. 업로드와 모델 출력에서 받지 않는다. 기본 private 모드는 전용 pull 자격과 운영자가 정한 namespace·Secret 이름이 있어야 게시하며, 별도 임시 인증 설정으로 각 digest의 manifest를 조회한다. 명시적 public 모드는 이미 public인 package를 빈 인증 설정으로 조회한다. 어느 모드도 package 공개 설정을 변경하지 않는다.
- CI worker 등록, 인증 준비와 target runtime 준비가 완료됐다는 뜻은 아니다. 현재 템플릿의 로컬 테스트는 실제 GitHub workflow 실행·registry 게시·배포 성공의 증거가 아니다.

## 단계별 재실행과 AI 책임

단계 이름·담당자·완료 조건·재실행 방식·조사 순서는 [stages.json](scripts/contract/stages.json)에 둔다. 기존 `execution.py`가 이 계약을 읽고 gate 결과와 AI 입력에 같은 이름을 사용한다. 오류 문구마다 새 정규식이나 실행 스크립트를 추가하지 않는다. 이 계약의 `repair`는 역할 구분이며 실제 파일 수정 권한은 기존 `repair_scope`·경로 정책·호스트의 제안 검증이 결정한다.

| 단계 | 실행 위치 | 재실행과 AI 개입 |
|---|---|---|
| 소스 준비 | `loop.py` → 기존 `intake.py` | 소스 가져오기와 기본 패키징을 별도 checkpoint로 저장. 완료된 checkpoint는 다시 실행하지 않음 |
| 컨테이너 구성 | 기존 `native_packaging.py`, 필요시 adapter | 지원하는 기본 구성부터 적용. AI는 관측한 구성 누락과 관련 파일부터 조사 |
| 정책·spec 검사 | `gate.py` L0/L1 | 읽기 검사 반복. 실패한 정책·필드와 허용된 패키징 수정만 전달 |
| 이미지 빌드 | `gate.py` L2 | 같은 run·전체 소스·spec·네트워크 설정·gate 구현에 묶인 build 결과와 실제 이미지 ID가 일치할 때만 재사용 |
| 이미지 실행 검사 | `gate.py` L3 | 정확한 이미지 ID로 격리된 실행 검사를 다시 수행. 빌드 재사용은 실행 검사 생략을 뜻하지 않음 |
| bundle 내보내기 | 기존 `bundle.py export` | 기존 bundle의 전체 해시·소스·spec·verdict가 같으면 재사용. 불완전하거나 다른 결과는 덮어쓰지 않음 |
| 이미지 게시 | Actions `release` → 기존 publisher | 검증한 archive를 게시. 기존 journal과 원격 digest 확인 유지. AI가 게시 자격이나 이미지를 결정하지 않음 |
| GitOps·Argo | API → 기존 `bridge.py` / `argo.py` | push 완료·sync 요청 완료를 각각 저장. 같은 배포의 apply 재요청은 완료 이후부터 진행. 이미 검증된 Argo revision은 다시 sync하지 않음 |
| 외부 관측 | 기존 CD observer | Pod·HTTP 상태를 읽어 확인. 모델의 성공 보고로 배포 완료 처리하지 않음 |

선택적 Q/L4도 동일한 단계 계약을 사용한다. Q는 기존대로 자동 소스 수정 대상이 아니며, L4는 패키징 범위다. 인프라·자격·결과 불확실 오류의 중단 정책도 유지한다. 처음 보는 오류 문구라도 알려진 실패 단계와 근거를 전달할 수 있다. 이를 새로운 정규식으로 먼저 분류해야 하는 것은 아니다.

물리적인 Actions job은 `loop`와 `release`를 유지한다. 매 수정마다 별도 job을 띄우면 작업 소스와 이미지 archive 전송이 추가되고 로컬 캐시를 잃는다. 단계 경계는 job 개수가 아니라 **입력 식별·완료 기록·재실행 규칙**으로 구현한다. checkout, Python 준비, artifact 전달, Docker daemon 준비와 GHCR 로그인은 표준 Actions를 사용한다. 동적 수리 루프 안의 빌드·실행은 기존 CLI 실행기가 담당한다.

`loop-<attempt>` artifact에는 `gate-N/L0.json` … `L3.json`(선택한 Q/L4 포함)과 `build.json`이 들어간다. 각 단계 결과는 source hash, run/attempt, outcome, 재사용 여부, 이미지 ID와 구조화된 오류를 기록한다. 이 파일들이 모두 독립적인 crash 복구 checkpoint인 것은 아니다. 기존 SQLite supervisor가 실행 중 죽어 완료가 불확실하면 여전히 reconcile을 요구한다. CD의 `pushing`·`syncing`·`routing` 역시 무조건 재전송하지 않는다. `observe` 요청은 다음 단계를 실행하지 않는다.

### 모델에게 처음 주는 정보

기존 `repair_evidence.py`가 실패 단계와 그 단계의 조사 순서, 실패 발췌 최대 4,000 bytes, 같은 단계의 로그 최대 2개 × 2,000 bytes, 관련 경로 후보 최대 24개, 이전 시도 최대 3개의 결과를 전달한다. 경로 후보는 내용 전체가 아니라 위치·해시·크기 등의 메타데이터다. 원본 evidence 참조와 생략량을 함께 주므로 부족하면 모델이 필요한 파일/로그 범위만 추가로 읽는다. 프로그램 출력과 과거 모델의 원인 추정은 명령이나 검증된 사실로 취급하지 않는다.

AI의 책임은 제한된 근거로 수정안을 제안하는 것이다. 호스트가 수정 범위와 근거를 검사하고 적용한 뒤 공식 gate를 실행한다. 새 JEV 분류 호출·그래프 DB·언어별 오류 사전은 이 경로에 추가하지 않는다. 기존 오류 분류는 중단 정책을 위해 유지하지만 새로운 조사 순서는 그 분류의 정규식 매칭에 의존하지 않는다.

### 최초 구성 1회와 실패 수정 2회

Actions Variable `RAILSHOT_MAX_PACKAGING_ATTEMPTS`와 `RAILSHOT_MAX_REPAIR_ATTEMPTS`는 각각 0~8회를 허용한다. 워크플로 기본값은 adapter 2회, fixer 8회이며 CLI 기본값은 각각 2회다. 같은 오류가 반복돼도 남은 횟수 안에서 수리한다. 두 역할은 상대 역할의 횟수를 빌리지 않는다. `--max-attempts 0`은 모든 SDK 호출을 끈다.

기존 끄기 옵션 `RAILSHOT_MAX_REPAIR_ATTEMPTS=0`은 패키징 값과 관계없이 모든 SDK 호출을 끈다. 이때 모델 인증과 SDK 설치 없이 결정적 검사만 실행한다. 패키징 `0`은 adapter 호출을 끄며 규칙 기반 자동 패키징은 계속 실행한다. 초기 adapter 제안이 거부되면 1회 몫을 소진하므로 fixer로 역할을 바꿔 재호출하지 않는다. fixer의 안전한 재계획은 수정 2회 안에서만 가능하다. 검사를 통과한 입력은 예산이 남아 있어도 모델을 호출하지 않는다.

`evidence.json`의 `agent_budget`은 선언한 전체 상한, `budget_used`는 역할별 supervisor 시도, `sdk_invocations`는 기록으로 확인한 SDK 호출 수다. 워크플로 기본 선언 10은 실제 호출 10회를 뜻하지 않는다. SDK 내부 모델 요청 수와 구분하며 확인할 수 없는 횟수는 null로 남긴다. 기존 GitHub Checks의 `loop.started/completed`에도 예산을 전달한다. `max_invocations`의 0~16을 받는 API를 먼저 배포한 뒤 CI 워크플로와 실행기 참조를 승격한다. 플랫폼 코드나 예산이 바뀌면 기존 run을 덮어쓰거나 강제 resume하지 않고 새 run으로 비교한다.

### 검증 범위와 측정

로컬 테스트는 입력 변경 시 빌드 재사용 거부, 실행 검사 반복, bundle 재사용, 구성/수정 합산 상한, 완료된 loop 재개 시 모델 재호출 방지, Git push/sync 완료 경계 복구를 검증한다. Docker·모델·클라우드 경계는 mock이고 일부 CD 테스트는 임시 로컬 Git remote를 사용한다. 실제 Actions 실행 속도나 토큰 절감률을 증명하는 결과는 아니다.

승격 후 같은 소스와 동일한 모델·실패 조건으로 기존/변경 경로를 비교한다. 확인할 값은 모델 입력 bytes·SDK 토큰·호출 수, `L2.json`의 `reused`, gate별 시간, 총 시간과 최종 성공률이다. 전체 소스가 바뀌는 수정에서는 빌드를 다시 해야 하므로 재사용 효과가 없을 수 있다. 운영 반영에는 이 템플릿의 apps 저장소 적용, 검증한 `PLATFORM_REF` 지정, CD 변경을 포함한 API 이미지 배포가 별도로 필요하다.

## CD에 전달하는 산출물

| 산출물 | 식별과 내용 |
|---|---|
| `release-bundle-<run_attempt>` | `loop.outputs.bundle_id`; 검증한 `images.tar`, spec, verdict와 SHA-256 manifest |
| `published-<run_attempt>` | `release.outputs.published_id`; `images.json`의 원격 digest, 원본 `railshot.yaml`, `verdict.json`, `manifest.json` 및 출처 영수증 `handoff.json` |
| `publish-journal-<run_attempt>` | 같은 run의 게시 복구용 `publish.json`만 보존. 자격·Docker 설정·진단 로그는 포함하지 않음 |
| bundle 연결 | `release.outputs.bundle_id`는 소비한 원본 artifact ID. manifest는 source digest와 spec/verdict/image archive 해시를 보존 |

소비자는 workflow run 및 producer artifact ID를 기준으로 읽어야 한다. 고정 `rendered` alias나 가장 최근 이름으로 대체하지 않는다. 이 산출물은 게시된 이미지와 CI 검사 근거이며 URL·클러스터 상태·배포 성공을 포함하지 않는다. CD 구현과 완료 조건은 CD 담당이 결정한다.

게시 재실행은 같은 run의 journal과 GitHub job 이력을 읽는다. journal은 repository·run·source commit·target·PLATFORM_REF·원본 bundle artifact ID·manifest hash에 묶이며 다른 입력으로 복원할 수 없다. 이전 게시 단계가 명시적으로 건너뛰어진 경우에만 새 push를 허용한다. 게시가 시작됐거나 이력이 불완전하면 확인하지 못한 이미지는 registry에서 읽어 검증하고 다시 push하지 않는다. worker 소실로 journal이 업로드되지 않아도 이 규칙은 유지한다. 원격 이미지를 확인할 수 없으면 불확실 상태로 멈춘다.

bundle과 게시 journal의 보존 기간은 1일이다. bundle이 만료되면 release만 재실행할 수 없으므로 새로운 CI 실행에서 전체 검사를 다시 해야 한다. private manifest 조회 성공은 고객 노드의 pull·Secret 설치·Pod Ready를 뜻하지 않으며, 이 검증은 CD에서 이어간다.

## 보존한 AWS 선택 구현

`scripts/codebuild.py`, `scripts/codebuild_release.py`, `workflows/codebuild-release.yml`은 별도의 관리자용 CodeBuild/ECR publisher다. 승인한 bundle을 private S3로 전달하고 고정 trusted source로 게시한다. 제품의 기본 registry/driver로 강제하지 않는다. 공통 `ci/scripts/gate/bundle.py`를 재사용하며 작업 dispatch 불확실성을 journal로 보존한다. 테스트는 AWS CLI 경계를 mock하며 실제 클라우드 호출을 하지 않는다.

## 로컬 검증

필요한 Python 환경에는 PyYAML, jsonschema, 기존 고정 SDK가 있어야 한다. SDK contract 테스트는 SDK 객체를 mock하며 모델을 호출하지 않는다.

```sh
python -m unittest discover -s ci/scripts -p 'test_*.py'
python -m unittest discover -s ci/scripts/gate -p 'test_*.py'
python -m unittest discover -s ci/scripts/loop -p 'test_*.py'
python -m unittest discover -s ci/scripts/runner -p 'test_*.py'
```

[현재 CI 게시 계약](../docs/api/ci-publication.md)과 [과거 snapshot 검증 기록](https://github.com/Jasmin-Softbank/Jasmin/blob/9e13c7c2e50b003c146852ccf22c9e9399271cf7/platform/scenarios/ci-validation.md)을 구분한다. 개인 Argo/CNPG/KEDA renderer·CD observer·reachability job은 제거했다. 과거의 해당 테스트 결과는 현재 실행 범위를 증명하지 않는다.

## 통합 입력과 게시 계약

source_commit/target_id를 workflow 입력으로 받아 checkout 및 운영자 target과 일치하는지 검사한다. 게시 ZIP은 `images.json`, `railshot.yaml`, `verdict.json`, `manifest.json`, `handoff.json`의 flat 구조다. 기존 네 evidence 파일은 byte 그대로 유지하며 handoff.json이 source/run/attempt/target과 파일 해시를 연결한다. API는 실제 producer artifact ID로 읽고 published 상태만 표시한다. 자세한 계약은 [CI publication](../docs/api/ci-publication.md)을 따른다.

운영자 변수 `RAILSHOT_TARGET_IDS`에 `["k3s-aws","k3s-gcp"]`처럼 허용할 target을 JSON 배열로 등록할 수 있다. 이 변수가 없을 때만 기존 `RAILSHOT_TARGET_ID` 한 개를 사용한다. 빈 배열·중복·잘못된 ID는 차단하며, loop와 release가 동일한 검증 함수를 호출한다. target 선택으로 registry·게시 자격·pull Secret 정책을 바꿀 수는 없다.

## Private pull 자격 배송

`workflows/railshot-pull-credential.yml`은 `Jasmin-Softbank/railshot-apps`의 보호된 `railshot-release` 환경에서 수동 실행하는 별도 workflow다. 같은 환경의 `GHCR_PULL_TOKEN`을 GitHub OIDC로 AWS 계정 `721622471953`, 서울 리전의 `/railshot/registry/ghcr/pull_token`에 `SecureString`으로 전달한다. 운영자 변수는 `GHCR_PULL_SYNC_ROLE_ARN`, `GHCR_PULL_USERNAME`이며 사용자가 role·region·parameter 경로를 입력할 수 없다. OIDC role은 해당 저장소·environment subject만 신뢰하고, 이 parameter의 `ssm:PutParameter`만 허용해야 한다. workflow도 동일한 session policy를 적용하며 SSM 읽기 권한은 갖지 않는다.

토큰은 environment secret → 0600 임시 JSON → AWS CLI 파일 입력으로만 전달하고, 성공·실패·시간 초과 모두 파일을 삭제한다. 토큰을 CLI 인수·로그·artifact에 넣지 않는다. 운영 노드는 별도 IAM으로 이 parameter 하나의 `GetParameter`만 허용받아 대상 namespace의 pull Secret을 설치한다. 이 workflow의 성공은 SSM 저장 접수이며 실제 노드의 private image pull은 별도로 검증한다.

현재 관리 중인 GHCR 자격은 classic PAT의 `read:packages` 권한만 가지며 기록된 만료일은 **2026-10-09**다. 만료 전에 보호된 GitHub secret을 교체하고 이 workflow를 다시 실행한 뒤 대상 Secret과 실제 pull을 확인한다. SSM parameter의 설명은 만료 메타데이터이며 토큰을 자동 갱신하지 않는다.

## 고객 앱 CI의 기본 실행량

기본 경로는 L0(변경·비밀 경계) → L1(실행 설정) → L2(이미지 빌드) → L3(실제 기동·HTTP)다. 이미지가 게시되면 CD가 같은 digest를 클러스터에 적용하고 외부 HTTPS를 확인한다. 성공 URL은 대시보드의 앱 목록과 상세에 표시한다. 별도 lint/type/unit Q와 취약점 스캔 L4는 기본 배포에서 실행하지 않는다. `--layers L0,L1,L2,L4,L3` 또는 `--layers L0,L1,Q,L2,L4,L3`를 명시하면 추가 검사를 실행하며, 이전 bundle도 계속 검증한다.

GitHub와 ZIP 모두 접수한 소스에 동일한 자동 패키징을 적용한다. 루트 `index.html`이 있는 완성된 HTML/CSS/JavaScript 사이트는 파일 구조를 보존하여 비특권 Nginx 컨테이너로 감싼다. 표준 단일 Vite 앱은 lockfile과 기존 build 명령을 보존한다. 기존 Dockerfile은 마지막 stage에 명시된 단일 `EXPOSE` 포트로 명세만 생성하며, 기존 컨테이너 기동 제약과 HTTP 검사는 계속 적용한다. 포트가 불명확하거나 서버 코드·미빌드 프레임워크가 섞인 앱을 정적 사이트로 오인하지 않는다. 사용자 명세와 앱 소스는 덮어쓰지 않는다. 기본 SDK 호출은 최초 구성 1회와 실패 수정 최대 2회로 총 3회이며 `RAILSHOT_MAX_REPAIR_ATTEMPTS=0`으로 모두 끌 수 있다. 초기 구성 몫을 사용하지 않아도 수정은 최대 2회다. 최초 게이트가 통과하면 Agent를 호출하지 않는다. 첫 패키징은 packaging 범위로 수행하며, 실제 L2 빌드·L3 기동/HTTP 실패 뒤에만 source 범위로 소스를 수정한다. workflow 기본 `REPAIR_SCOPE=source`이며 명시적인 `packaging` 설정은 유지한다. [배포 준비 스킬](scripts/agents/skills/prepare-deployment/SKILL.md)은 두 역할의 실제 SDK instructions에 합성되고 예시는 필요할 때 읽는다. 제안된 생성·수정·삭제는 기존 경로·파일 수·바이트 제한과 보호 규칙을 적용한 뒤 전체 게이트를 재실행한다. 동일 실패 반복, 인증·인프라 문제, 실행 결과 불명확 상태에는 추가 호출하지 않는다. 옵션은 실행 시작 시 고정되므로 이전 0회 실행의 재시도가 아니라 새 실행에서 적용해야 한다.

Memos의 Go 서버와 영구 `/var/opt/memos` 저장소는 정적 Vite 앱 조건에 해당하지 않는다. CI 축소는 영구 볼륨 지원을 추가하지 않으며, 임시 디스크로 대체하여 배포 성공으로 처리해서는 안 된다.

앱 컨테이너의 루트 파일시스템은 CI와 GitOps에서 쓰기를 허용한다. 비루트 UID, capability 제한, 자격 증명 분리와 네트워크 격리는 유지한다. L3 시작 직후 IP가 없으면 컨테이너 상태와 로그를 수리 입력으로 남긴다.
