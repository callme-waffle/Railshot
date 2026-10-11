# 사용자 진입과 CI 게시 계약

## POST /api/deploy

multipart/form-data와 `x-railshot-request: deploy` (legacy: `x-jasmin-request: deploy`) 헤더를 받는다. `app`과 ZIP `archive`, 폴더 `files`+`paths`, 또는 공개 GitHub `repository_url` 중 정확히 하나를 제공한다. 선택 `target_id`는 서버의 `RAILSHOT_TARGET_ID`와 같아야 한다. 생략하면 서버 설정을 쓴다. target은 준비·배포 성공을 뜻하지 않는 운영자 식별자다.

응답 202는 `{run_id, tenant, app, source_commit, target_id, state: "queued", changes, actions_url}`이며 공개 GitHub 입력이면 별도의 원본 `{source: {type, repository, sha}}`를 추가한다. 원본 repo SHA와 apps repo 등록 `source_commit`은 다르다. 단지 202를 받았다고 CI나 앱 배포가 성공한 것은 아니다.

등록 commit을 만들고 ref를 갱신한 뒤 dispatch의 tenant/app/source_commit/target_id로 넘긴다. 소스가 같으면 새 commit 없이 현재 parent를 source_commit으로 사용한다. 다른 commit이 ref를 앞서 바꾸면 workflow checkout 일치 검사에서 차단된다. 원격 commit이나 dispatch의 불확실한 결과를 자동 재전송하지 않는다.

## GET /api/runs/:run_id

등록된 `.github/workflows/<workflow>`만 허용한다. `steps`에 loop/release의 상태와 `observed_attempt`를 반환한다. `observed_attempt`는 job을 확인한 GitHub attempt의 번호다. 실패 job만 재실행하면 GitHub가 이전 성공 job을 새 ID와 attempt로 복제해 보여줄 수 있으므로 이 값으로 artifact의 생산 attempt를 판단하지 않는다. GitHub completed/success와 별도로 state를 반환한다.

| state | 의미 |
| --- | --- |
| queued/running | CI 대기 또는 실행 중 |
| failed | workflow 또는 필수 job 실패·건너뜀 |
| publication_unverified | Actions는 성공했지만 올바른 게시 artifact를 확인하지 못함 |
| published | 해당 release producer의 artifact와 evidence 파일·run/source/target 연결 확인 |

published 응답의 publication에는 artifact_id/name, run_id, producer_attempt, bundle_artifact_id, source_commit, target_id, tenant, app, 4개 evidence 파일 SHA256와 service→digest images가 들어간다. `publication.producer_attempt`는 검증한 게시 handoff의 실제 생산 attempt이며 `steps.observed_attempt`와 의미가 다르다. `bundle_artifact_id`는 release 재시도에서도 원래 gate 산출물의 ID를 유지한다. 이 API는 `deployed`나 `handed_off` 완료를 만들지 않는다. CD가 아직 소비하지 않았기 때문이다. `url`은 항상 null이며 사용자 화면은 결과 앱 링크를 표시하지 않는다.

attempt 조회는 최대 100 attempt, 각 attempt는 최대 100 jobs다. 그 범위를 넘거나 중복 job이면 불완전한 상태로 판단해 실패한다. 동일 artifact 이름이 두 개이거나 만료·해시·source·target이 다르면 게시 확인을 차단한다. 요청한 run의 최신 release 실패를 옛 artifact로 대체하지 않는다.

## 신뢰 경계

서버의 GitHub token과 target 설정은 local operator가 관리한다. 앱 업로드는 이 설정을 바꾸지 않는다. Provider application credential, Ansible SSH key, admin kubeconfig를 이 API 요청이나 게시 artifact에 넣지 않는다. Provider·Ansible·CD 실행은 각각 담당 인터페이스이며, 현재 API가 자동 호출하지 않는다.

localhost 기본 bind와 Host/Origin 검사는 그대로다. 운영 ALB backend에 연결하려면 인증과 허용 Host/Origin 설정을 따로 구현해야 한다.


### 앱 관리 계획의 비동기 조회

`POST /api/v1/applications/:id/plans`에 `Prefer: respond-async`를 보내면 `202`, `Location`, `Retry-After: 2`와 `status: planning`인 계획을 반환한다. `GET /api/v1/applications/:id/plans/:plan_id`는 같은 세션의 `planning | ready | failed` 상태를 반환한다. 준비된 계획만 기존 승인/실행 요청에 사용할 수 있다. 구 클라이언트의 헤더 없는 POST는 기존 201 응답을 유지한다.

클라우드 조회는 상태 저장의 쓰기 대기열 밖에서 실행한다. 같은 앱·같은 작업의 진행 중 계획은 재사용한다. 서버 재시작은 미완료 계획을 같은 공개 계획 ID로 다시 계산한다. 계산마다 별도의 비공개 실행 ID를 쓰며 실제 변경 작업은 재실행하지 않는다. 계획 확인 중 앱 상태가 바뀌면 `APPLICATION_PLAN_STALE`로 실패한다. 실패 코드와 안전한 안내만 공개하며 자격정보·실행 경로는 공개하지 않는다. 창 닫기는 화면의 조회만 중단한다. 실제 삭제는 별도 confirmation, delete_data, plan_hash, Idempotency-Key와 기존 소유권·공유 자원 검사를 유지한다.

설계 근거: [RFC 7240 respond-async](https://www.rfc-editor.org/rfc/rfc7240#section-4.1), [RFC 9110 202 Accepted](https://www.rfc-editor.org/rfc/rfc9110#section-15.3.3).

플랫폼 교체는 운영자 토큰 전용 `POST /internal/releases/prepare`로 준비한다. 진행 중인 HTTP/백그라운드 작업이 있으면 202를 반환하고 계속 서비스한다. 유휴 상태에서는 배포 대기열과 새 API 접수를 함께 잠그고 200을 반환한다. 이때 새 요청은 처리 전에 `503 PLATFORM_UPDATING`, `outcome_unknown: false`, `Retry-After: 1`을 받는다. 교체가 취소되면 120초 뒤 잠금이 해제된다. 이 경로는 공개 대시보드 프록시에 노출하지 않는다. SIGTERM은 서버와 저장소를 닫은 뒤 프로세스를 종료한다.

Argo PreSync Job은 기존 API가 있는 노드에서 새 API 이미지로 실행되므로 다운로드가 끝나기 전 API를 내리지 않는다. Job은 준비 확인 후에만 Sync를 허용한다. 현재 파드 템플릿 ID가 이미 목표 ID와 같으면 잠그지 않고 완료한다. 대시보드만 배포할 때도 이 비교로 API 작업을 방해하지 않는다. `/readyz`는 저장소/설정 준비 여부를 HTTP 상태로 반환하며 2초 주기로 검사한다. 이미지 준비 Job에는 API 토큰만 제공하고 PVC, Kubernetes 서비스 계정 토큰, 클라우드 자격정보는 마운트하지 않는다.
