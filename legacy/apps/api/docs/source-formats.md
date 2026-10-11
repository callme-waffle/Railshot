# 해커톤 PoC의 배포 입력 형식

아래는 원본 담당 브랜치 2026-10-01의 소스 지원 검토다. 통합본의 현재 입력·게시 경계는 [API 계약](interface.md)과 [CI 계약](../../../docs/api/ci-publication.md)을 따른다. 옛 AWS 배포·원격 경로 언급은 당시 검토 맥락이며 현행 배포 구현을 뜻하지 않는다.

판단 시점: 2026-10-01. **현재 기능**은 입력을 받아 기존 워크플로로 보낼 수 있다는 뜻이다. 모든 앱의 실제 AWS 배포 성공을 뜻하지 않는다. 실배포에는 앱이 Linux/amd64 컨테이너로 빌드되고, 비루트 사용자로 실행되며, HTTP 포트와 2xx/3xx 건강 확인 경로를 제공해야 한다. 실제 Actions·클라우드 연동은 별도 검증이 필요하다.

판단 근거: 이 저장소의 `apps/api/src/server.js`·`apps/api/src/github.js`, `ci/workflows/railshot-deploy.yml`, `ci/scripts/contract/stack-contract.md`·`catalog.yaml`, `ci/scripts/poc/intake.py`, `ci/scripts/agents/adapter/INSTRUCTION.md`, `ci/scripts/gate/gate.py`. 워크플로는 소스를 검사·수정한 뒤 Dockerfile로 이미지를 빌드한다. 빌드가 끝난 이미지 URI를 직접 받아 배포하는 경로는 없다.

| 입력 형식 | 판단 | 현재 조건 또는 필요한 작업 |
|---|---|---|
| ZIP 안의 웹 앱 소스 | **현재 기능으로 배포 가능** | 대시보드·CLI·MCP에서 수집 가능. 압축 해제 후 100 MB/2,000파일 제한과 컨테이너·HTTP 계약을 통과해야 한다. |
| 로컬 폴더의 웹 앱 소스 | **현재 기능으로 배포 가능** | 대시보드는 폴더 파일을 직접 업로드한다. CLI·MCP는 내부에서 ZIP으로 전송한다. 이후 경로는 ZIP과 같다. |
| Python/Node.js 웹 앱 소스 | **현재 기능으로 배포 가능** | 허용된 Python/Node 베이스 이미지가 있다. 기존 Dockerfile·`.railshot/railshot.yaml` (legacy: `.jasmin/jasmin.yaml`)이 적합하거나 adapter가 생성하고 gate를 통과해야 한다. 언어만으로 성공이 보장되지는 않는다. |
| 정적 HTML/CSS/JS | **현재 기능으로 배포 가능** | `nginx-unprivileged` 등 허용 이미지로 HTTP 서빙하는 Dockerfile과 spec을 만들 수 있어야 한다. |
| Go/Rust 웹 서버 소스 | **현재 기능으로 배포 가능** | Go/Rust 빌드 이미지와 distroless 최종 이미지가 허용된다. 단, 실제 앱에 맞는 Dockerfile·HTTP 동작 검증이 필요하며 자동 어댑터의 성공은 미검증이다. |
| `.tar.gz` 안의 소스 트리 | **추가 구현 시 배포 가능** | 현재 업로드 API는 ZIP/폴더와 공개 GitHub URL을 받는다. tar 파서, 경로 탈출·심볼릭 링크·용량 검사 후 공통 파일 목록으로 변환하면 기존 워크플로를 사용할 수 있다. |
| 공개 GitHub 저장소 기본 URL | **현재 기능으로 배포 가능** | 기본 브랜치의 커밋 SHA를 고정해 ZIP을 받고 공통 파일 목록으로 검사한다. 같은 앱 이름으로 다시 요청하면 변경된 파일을 반영한다. 공개 저장소만 지원하며 앱의 실제 빌드 성공은 별도다. |
| 비공개 GitHub 저장소·브랜치/하위 폴더 지정·push 자동 재배포 | **추가 구현 시 배포 가능** | 소스 저장소용 인증, ref/경로 선택, webhook 또는 GitHub App 설치, 권한 검증이 필요하다. |
| 실행 가능한 `.jar` 파일 단독 | **추가 구현 시 배포 가능** | 현재 API는 단독 JAR를 받지 않는다. 허용된 `eclipse-temurin:21-jre` 이미지에 JAR를 넣는 Dockerfile과 실행 명령·포트·건강 경로를 구성해야 한다. JAR를 폴더/ZIP에 Dockerfile·spec과 함께 넣으면 현재 경로로도 시도할 수 있다. |
| Linux/amd64 단일 실행 파일 | **추가 구현 시 배포 가능** | 단독 파일 입력과 최소 Dockerfile/spec 생성을 추가한다. 실행 파일이 비루트·읽기 전용 파일 시스템·HTTP 계약을 만족해야 한다. |
| Maven/Gradle Java 소스 | **배포 가능성 있으나 난이도가 요구됨** | 현재 허용 목록은 JRE만 포함하고 JDK 빌드 이미지가 없다. 빌드 이미지/규칙, 잠금·재현성, 캐시, 게이트 검증을 추가해야 한다. |
| `.war` 파일 | **배포 가능성 있으나 난이도가 요구됨** | Servlet 컨테이너 이미지·설정이 현재 허용 목록에 없고, 비루트 실행·HTTP 건강 확인까지 설계해야 한다. |
| Docker Compose 프로젝트 | **배포 가능성 있으나 난이도가 요구됨** | Compose 파일을 그대로 실행하지 않는다. 서비스별 Dockerfile과 Railshot spec으로 변환하고, 볼륨·네트워크·DB 의존성을 계약 범위에 맞춰야 한다. |
| 기존 Docker/OCI 이미지 URI | **배포 가능성 있으나 난이도가 요구됨** | 현재 Actions는 소스를 받아 `docker buildx build`를 실행한다. 이미지 직접 배포에는 digest 고정, 레지스트리 인증, 이미지 검사, build/loop 우회 경로, GitOps 렌더링 변경이 필요하다. |
| `docker save`/OCI 이미지 tarball | **배포 가능성 있으나 난이도가 요구됨** | 소스용 `.tar.gz`와 다르다. 이미지 import→검사→레지스트리 push 경로가 필요하고 현재 100 MB 업로드 제한과도 맞지 않을 수 있다. |
| APK/IPA/Windows·macOS 데스크톱 실행 파일 | **현 해커톤 아키텍처로 배포 불가능** | 모바일/데스크톱 배포 대상이 아니다. 현 플랫폼은 Linux/amd64 HTTP 컨테이너를 k3s에 배포한다. |
| HTTP 서비스가 전혀 없는 배치 작업 또는 GPU·root·privileged 필수 앱 | **현 해커톤 계약으로 배포 불가능** | 계약은 적어도 하나의 HTTP route·health를 요구하고 GPU, root/privileged 워크로드를 지원하지 않는다. |

**우선순위 제안:** 폴더/ZIP/공개 GitHub URL 경로로 실제 Python/Node 앱 1개씩 end-to-end 검증한다. 다음으로 `.tar.gz`를 추가한다. JAR 단독 입력은 데모 수요가 있으면 작은 확장으로 진행한다. Docker/OCI 이미지 직접 배포는 현재 `source → Dockerfile build` 경로와 달라 별도 마일스톤으로 다룬다.
