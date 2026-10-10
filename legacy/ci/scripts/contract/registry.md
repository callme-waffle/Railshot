# 검증 이미지와 registry 게시 계약

현재 CI는 gate가 검사한 이미지를 bundle로 고정하고 trusted publisher가 같은 이미지를 게시한다. 제품 기본 registry는 GHCR이며 `bundle.py`의 OCI 게시 함수를 재사용한다. CodeBuild/ECR은 별도 AWS 선택 구현으로 남고 공통 target을 강제하지 않는다.

## 권한과 식별

- 비신뢰 source와 모델 출력은 registry prefix·인증 방식·visibility를 정하지 못한다. 게시 자격은 보호된 release 환경에만 둔다.
- `bundle.py export`는 source digest·spec/verdict hash·이미지 ID를 검사하고 `railshot.yaml`, `verdict.json`, `images.tar`, `manifest.json`을 생성한다. 부분 gate, source 변경, image tag 변경은 게시할 수 없다.
- `publish`는 bundle을 재검증하고 같은 이미지를 게시하며 `images.json`에 확인된 원격 digest를 남긴다. 태그명만으로 이미지 동일성을 판단하지 않는다. 게시 중 결과가 불확실하면 journal로 조정하기 전 재시도하지 않는다.
- GitHub Actions 게시자는 `GITHUB_TOKEN`과 `packages: write`를 사용한다. 사용자 CI에 registry push 자격을 전달하지 않는다. 임시 Docker 인증 설정은 작업 종료 시 제거한다.
- 기본 private workflow는 별도 `read:packages` pull 자격과 대상 namespace·Secret 참조를 요구한다. 게시 후 임시 pull 인증 설정으로 각 digest의 manifest를 조회하고 v2 `handoff.json`에 `authenticated_manifest_read`와 Secret 참조만 남긴다. 명시적 public 모드는 이미 public인 package의 각 digest를 별도 빈 인증 설정으로 조회한다. visibility 입력이 package를 공개로 바꾸지는 않는다.
- hosted release는 `publish-journal-<attempt>`에 `publish.json`만 보존한다. 같은 workflow run에서 복원하며 repository·run ID·source commit·target·platform commit·원본 bundle artifact ID·manifest hash·registry/tag가 모두 같아야 한다. 자격이나 Docker 설정은 artifact에 포함하지 않는다.
- 재실행 시 GitHub job 이력으로 이전 publish 단계의 시작 여부를 확인한다. 명시적으로 skipped인 이전 단계만 새 push를 허용한다. 시작했거나 이력이 불완전하면 journal이 소실돼도 remote readback만 수행하고 재push하지 않는다. 원격의 이미지 ID와 digest를 확인할 수 없으면 `UNKNOWN`을 유지한다. journal의 완료 기록과 입력이 다르거나 원본 bundle artifact가 바뀌어도 중단한다.

## CD 인계

workflow는 producer artifact ID로 bundle을 소비한다. `published-<run_attempt>`에 `images.json`과 변경하지 않은 spec/verdict/manifest를 올리고 원본 bundle artifact ID도 출력한다. 소비자는 workflow run 및 실제 producer artifact ID로 결합하며 stable alias 또는 가장 최근 artifact로 대체하지 않는다.

이 결과는 CI 검사와 이미지 게시 근거다. target별 pull 권한·클러스터 설치·CD 방식·DB·ingress·공개 URL·배포 완료는 담당 영역에서 결정하고 별도로 관측한다. Argo, Gateway, ALB, CNPG나 특정 Secret installer를 공통 앱 계약의 필수 구현으로 정하지 않는다.

## 캐시와 보존

현재 BuildKit의 로컬 named volume을 이용한다. workspace/tenant별 cache 격리와 전체 이미지·bundle 보존/정리 정책은 완료되지 않았다. registry remote cache 전송도 연결되지 않았다. cache miss는 재빌드 대상이며 CI 증거·게시 이미지 동일성·배포 성공을 대체하지 않는다.

release 이미지 수명은 CI VM 수명과 독립이다. workflow의 bundle과 publish journal은 1일 보존한다. 만료된 bundle은 release 재실행에 사용할 수 없으며 새 CI 실행으로 전체 검사를 다시 해야 한다. 현재 실행 대상이 사용하는 digest의 보존·삭제 정책은 별도 소유자와 합의해야 한다. 이 CI 정리가 기존 registry package, VM, 디스크 또는 인증 자료를 삭제하지는 않는다.

## 검증 경계

bundle·publisher·workflow 정책의 로컬 테스트는 실제 GHCR 게시·private pull·cold/warm 성능·고객 배포 성공이 아니다. 실제 게시 기록은 [CI 활성화 기록](../../../docs/integration/ci-activation.md)의 run·artifact·digest에 한정한다. manifest 인증 조회와 고객 노드의 Secret 설치·이미지 pull·Pod Ready는 구분한다. 테스트를 통과시키기 위해 registry/모델/클라우드에 실제 요청하지 않는다.
