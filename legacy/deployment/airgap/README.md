# RailShot Airgap Bundle

Airgap(외부 인터넷과 분리된 환경)에서는 **사전에 전달한 로컬 artifact(설치 파일)와 컨테이너 이미지**로 K3s·Cilium·workload(실행할 앱)를 구성합니다. Provider(인프라 제공 계층)의 자원 생성이나 방화벽 설정은 수행하지 않습니다.

## 지원 범위와 전제조건

- 대상은 기존 Runtime과 같은 전용 단일 Linux 노드입니다. Linux·systemd(서비스 관리 도구)·Python 3.10 이상·root(관리자) 권한과 기본 OS 도구가 미리 준비되어 있어야 합니다.
- 실제 검증은 Ubuntu 24.04 arm64에서 수행했습니다. amd64 artifact 선택 경로는 있으나 실제 설치는 검증하지 않았습니다. [최종 검증 기록](../scripts/tests/results/AIRGAP-VALIDATION-2026-10-02.md)에 실행 결과와 한계를 기록했습니다.
- 번들 준비 도구는 **인터넷에 연결된 동일 CPU 구조의 Linux 및 실행 중인 K3s/containerd(컨테이너 실행 도구)**가 필요합니다. 준비 도구가 K3s를 새로 설치하거나 Provider VM을 만들지는 않습니다.
- OS 패키지, SELinux 정책, private registry(비공개 이미지 저장소) 자격정보, 앱의 DB/API 의존성은 이 bundle에 자동으로 포함되지 않습니다. Ubuntu/Debian 전제조건을 준비한 후 사용합니다.
- loopback(노드 내부 연결), NIC(네트워크 장치), 내부 Kubernetes 네트워크와 기본 route(패킷 이동 경로)는 계속 필요합니다.

**Internet-independent(외부 인터넷 없이 로컬/내부 artifact로 배포 가능)**를 목표로 합니다. 중앙 시스템과 노드 사이의 통신조차 없이 원격 배포할 수 있는 **Network-independent(모든 네트워크와 무관한 배포)**를 주장하지 않습니다. 최초 전달에는 USB·파일 복사·SSH 등 실제 전달 수단이 필요합니다.

## Online / Offline / Partial Network

| 모드 | 네트워크 검사와 선택 | 파일·이미지 처리 |
| --- | --- | --- |
| `auto` — 기본값입니다. | 필요한 endpoint(접속 지점)를 병렬 검사합니다. 모두 가능하면 online, 실패하고 유효한 bundle이 있으면 airgap을 선택합니다. | bundle 경로가 있으면 검증 후 로컬 파일도 재사용합니다. 둘 다 없으면 JSON 오류를 반환합니다. |
| `online` | 필수 artifact·registry 경로 접근을 요구합니다. | bundle을 지정하면 검증된 로컬 파일을 재사용할 수 있습니다. 이 모드에서는 airgap으로 자동 전환하지 않습니다. |
| `offline` | 외부 capability(통신 가능 항목) 검사를 생략합니다. | bundle만 사용하고 앱·검사·Cilium 이미지의 pull policy(다운로드 규칙)를 `Never`로 지정합니다. 외부 확인 URL과 공개 URL hook도 생략합니다. |

Partial Network(일부 통신만 되는 환경)에서는 필요한 항목과 선택 항목을 구분합니다. nginx 샘플에는 GHCR 접근이 필수이지 않습니다. Cloudflare 접속 실패도 K3s/Cilium/앱 성공을 막지 않습니다. 불명확한 항목은 `null`로 반환하며, 검사하지 않은 것을 `false` 또는 `true`라고 단정하지 않습니다.

`auto`가 처음 online을 선택한 뒤 명확한 artifact/이미지 접근 오류가 발생한 경우에도, 이미 검증한 bundle이 있으면 airgap으로 한 번 다시 시도합니다. 임의의 readiness(앱 준비 상태) 실패나 잘못된 포트는 fallback(대체 경로 선택)으로 숨기지 않습니다. Helm 설치가 중간 상태로 남으면 재시도만으로 복구되지 않을 수 있습니다.

## 네트워크 검사

각 curl 검사는 기본 3초의 연결·전체 제한을 가지며 병렬 실행합니다. `runtime.preflight_timeout_seconds`는 1~10초로 설정할 수 있습니다. capability 검사는 설치 전체의 다운로드·상태 확인 제한과 별개입니다.

| 항목 | 검사 대상·의미 |
| --- | --- |
| `dns` | 필요한 hostname(호스트 이름)의 실제 해석 결과와 DNS 실패 코드를 종합합니다. 검사하지 않으면 `null`입니다. |
| `https_443` | TLS(암호화 연결)를 검증한 TCP 443 응답이 있는지 확인합니다. |
| `k3s_source` | 승인된 K3s tag(버전 이름)의 공식 `install.sh` 주소입니다. `get.k3s.io`의 움직이는 최신 스크립트를 사용하지 않습니다. |
| `github` | 승인된 K3s release binary(실행 파일) 주소와 redirect(다른 주소로 전달되는 경로)를 확인합니다. |
| `cilium_cli_source` / `cilium_chart` | 승인된 Cilium CLI와 chart(설치 정의) 주소입니다. |
| `quay` / `docker_hub` | Cilium·K3s 이미지와 검사 이미지에 필요한 registry의 `/v2/` 응답입니다. |
| `registry_k8s` / `ghcr` | registry.k8s.io는 후속 요구사항에 따라 필수 경로로 검사합니다. GHCR은 해당 workload를 쓰면 필수 항목입니다. |
| `workload_registry` | 다른 workload registry를 사용하는 경우 HTTPS 접속을 확인합니다. |
| `cloudflare_tunnel` | 요청 시 edge의 TCP/TLS 7844 경로를 검사합니다. QUIC(UDP 기반 연결)·토큰·실제 connector 연결 성공을 의미하지 않습니다. |

Registry의 HTTP 401은 인증 challenge(자격정보 요청)를 받았다는 뜻으로, endpoint reachability(접속 가능성)를 인정합니다. **이미지별 다운로드 권한·tag 존재·레지스트리 CDN(파일 전달 서버) 접근까지 보장하지 않습니다.** 실제 다운로드/배포에서 추가 오류가 발생할 수 있습니다.

`runtime.endpoint_overrides`는 시험용 접속 지점 또는 내부 mirror(복제 저장소)의 reachability 진단에 사용할 수 있습니다. **artifact 다운로드 URL이나 containerd registry 설정을 바꾸지는 않습니다.** HTTPS만 허용하고 자격정보·query·fragment는 거부합니다. Runtime은 ufw/nftables(노드 방화벽), 외부 공유기, Security Group/VPC Firewall을 변경하지 않습니다.

## Bundle 준비와 검증

저장소 루트에서 실행합니다. `input.json`은 기존 `0.1` 또는 새 `0.2` 배포 입력이며 nginx 샘플 입력으로 시작할 수 있습니다.

```bash
# 인터넷에 연결된 Linux 준비 노드에서 실행합니다.
sudo ./deployment/airgap/prepare-bundle.sh \
  --input deployment/scripts/tests/fixtures/aws.json \
  --output /var/tmp/railshot-bundle \
  --bundle-version 2026-10-02.1 \
  --image nginx:1.28.1-alpine

# 내용·버전·CPU 구조·이미지 목록과 checksum(파일 무결성 값)을 확인합니다.
sudo ./deployment/airgap/verify-bundle.sh \
  --bundle /var/tmp/railshot-bundle \
  --input deployment/scripts/tests/fixtures/aws.json
```

필수 K3s binary·공식 system image archive(시스템 이미지 묶음)·버전 고정 installer(설치 스크립트)·Cilium CLI·로컬 chart·Cilium 이미지 3개·curl 검사 이미지·nginx 샘플 이미지·요청 workload를 포함합니다. `--image`로 업데이트용/추가 앱 이미지를 포함할 수 있습니다. 준비용 containerd namespace(분리된 이미지 공간)를 사용하며 기존 `k8s.io` 클러스터 이미지를 제거하지 않습니다.

준비 결과는 새 디렉터리에 원자적으로 게시합니다. 같은 출력 경로·버전·이미지 목록이 이미 검증 가능하면 재사용합니다. 새 이미지 또는 버전은 **새 경로**에서 준비합니다. 바이너리/이미지 archive는 Git에 올리지 않습니다. `airgap/bundles/`와 관련 binary archive는 `.gitignore`로 제외했습니다.

Bundle의 실제 형식은 다음과 같습니다. 이 JSON은 필드 설명을 위한 **축약 예시**이며 실행용 전체 manifest(구성 목록)가 아닙니다. [실제 생성한 전체 manifest](../scripts/tests/results/airgap/bundle-manifest.json)를 별도로 보존합니다.

```json
{
  "schema_version": "1",
  "bundle_version": "2026-10-02.1",
  "platform": "linux/arm64",
  "runtime": {
    "k3s_version": "v1.34.11+k3s1",
    "cilium_version": "1.20.2",
    "cilium_cli_version": "v0.20.1"
  },
  "files": {
    "k3s": {"path": "artifacts/k3s-arm64", "sha256": "272f45b9efc69d0bbdb7042156156c6903087829a5003d4593af0ad2d08d76d4", "size": 70451362}
  },
  "images": [
    {
      "reference": "quay.io/cilium/cilium@sha256:2939231d0d3e3ebddcd80fffa168b7ddcc78fdf0dc864d1c8c126ff523c54f01",
      "digest": "sha256:2939231d0d3e3ebddcd80fffa168b7ddcc78fdf0dc864d1c8c126ff523c54f01",
      "archive": "image_5",
      "role": "cilium_agent"
    }
  ]
}
```

`bundle-manifest.json`에는 모든 artifact의 크기·SHA256과 이미지 reference(이미지 이름)·digest·역할·archive 대응을 기록합니다. `manifest.sha256`에는 manifest 자체의 SHA256을 기록합니다. K3s binary/system archive와 Cilium CLI archive는 준비 시 upstream(공식 배포처)의 checksum도 비교합니다. 추가 OCI(표준 이미지 형식) archive의 index와 blob(이미지 구성 데이터) digest도 검증합니다.

Checksum은 손상 검출 수단이며 **서명이나 출처 인증을 대체하지 않습니다.** 최초 Onboarding(노드 등록·준비) 때 신뢰한 manifest SHA256을 별도 경로로 전달하고 `--bundle-sha256` 또는 `runtime.bundle_sha256`으로 고정하는 방법을 권장합니다. Manifest와 checksum 파일을 모두 바꾼 공격을 자체 checksum만으로 막았다고 주장하지 않습니다. 전달 디렉터리는 신뢰할 수 있는 관리자가 관리해야 합니다.

## 전달·preload·deploy

준비한 bundle 디렉터리 전체를 실행 노드에 전달합니다. 예시의 `/var/lib/railshot-deployment/bundle`은 전달 계층이 정하는 경로이며 Runtime이 SSH 복사를 수행하지 않습니다.

```bash
# 대상 전용 Linux 노드 안에서 실행합니다.
sudo ./deployment/scripts/deploy.sh --mode auto \
  --bundle /var/lib/railshot-deployment/bundle \
  --input input.json > result.json 2> deploy.log

# 완전 offline 실행입니다.
sudo ./deployment/airgap/install-offline.sh \
  --bundle /var/lib/railshot-deployment/bundle --input input.json

# 준비 작업에서 반환한 실제 64자리 digest를 사용합니다.
sudo ./deployment/airgap/verify-bundle.sh \
  --bundle /var/lib/railshot-deployment/bundle \
  --manifest-sha256 "$TRUSTED_MANIFEST_SHA256"
```

K3s system archive를 공식 경로 `/var/lib/rancher/k3s/agent/images/`에 배치하고, 로컬 binary와 installer를 `INSTALL_K3S_SKIP_DOWNLOAD=true`로 실행합니다. 기존 설정·버전·소유권 검사는 유지합니다. 로컬 binary가 승인된 bundle의 checksum과 다르면 자동으로 덮어쓰지 않습니다. [공식 K3s airgap 방식](https://docs.k3s.io/installation/airgap)을 기준으로 구성했습니다.

K3s API 준비 후 추가 OCI archive를 `k3s ctr -n k8s.io images import --platform <bundle-platform> --digests`로 넣습니다. 각 reference의 digest와 native platform(노드 CPU 구조)의 content(이미지 데이터) 준비 상태를 확인합니다. **동일한 digest의 이미지가 있으면 해당 archive를 다시 import하지 않습니다.** 하나가 없으면 그 이미지 archive만 import합니다. K3s system archive는 같은 내용이면 복사 시각을 유지하며, 단순 재실행에서 서비스를 재시작하지 않습니다. K3s 서비스를 실제 재시작하면 공식 system archive가 다시 읽힐 수 있습니다.

Cilium CLI는 로컬 chart directory를 사용하며 동일한 VXLAN(가상 통신 경로)·kube-proxy(서비스 연결 처리) 설정을 유지합니다. Cilium·앱·검사 이미지는 bundle의 digest로 지정하고 offline에서는 pull policy를 `Never`로 설정합니다. 누락·손상·CPU 구조 불일치는 설치 전에 명확한 JSON 오류로 반환합니다.

이미 실행 중인 K3s에서 preload만 수행하는 명령도 제공합니다. 먼저 `verify-bundle.sh`로 검증한 bundle을 사용합니다.

```bash
sudo ./deployment/airgap/preload-images.sh --bundle /var/lib/railshot-deployment/bundle
sudo ./deployment/scripts/verify.sh --mode offline \
  --bundle /var/lib/railshot-deployment/bundle --input input.json
```

## Bundle lifecycle과 버전 정책

`versions.json`은 **기존에 검증한 Runtime 버전과 Cilium 이미지 digest**의 기준입니다. 요청 버전이 기준과 다르면 `VERSION_NOT_APPROVED`로 거부합니다. 인터넷 연결을 이유로 최신 버전을 설치하지 않습니다.

```text
새 버전 발견 → 새 후보 profile/bundle 준비 → 별도 compatibility 검사
            → 팀 승인·기준 파일 갱신 → 기본 버전 승격
```

최신 버전 탐지는 다음 별도 명령으로 수행합니다. GitHub API 접근 실패는 `null`과 원인으로 표시하고 현재 Runtime 버전은 변경하지 않습니다.

```bash
./deployment/airgap/check-updates.sh
```

`selection_changed=false`를 반환합니다. 배포 중에는 이 명령을 자동 실행하지 않으므로 DeploymentResult의 `update_available`는 `null`입니다. **Latest Version Detection(최신 버전 조회)과 Runtime Version Selection(실제 설치 버전 선택)을 분리합니다.** 새 profile 갱신 자체는 현재 release gate(버전 승인 절차)의 자동 구현이 아니며 팀이 승인해야 합니다.

앱 업데이트를 offline에서 수행하려면 새 이미지를 bundle 준비 단계의 `--image`에 포함하거나 새 bundle을 전달해야 합니다. 컨테이너 이미지뿐 아니라 앱이 실제로 호출하는 API·DB가 외부 인터넷에 의존한다면 앱 전체의 인터넷 독립성까지 보장하지 않습니다.

## Cleanup과 복구

```bash
# 앱만 제거합니다. 노드에 전달한 bundle은 보존합니다.
sudo ./deployment/scripts/cleanup.sh --input input.json

# 전용 노드의 K3s·Cilium·클러스터 전체 데이터를 제거합니다.
sudo ./deployment/scripts/cleanup.sh --input input.json --all --disposable-node
```

제거는 외부 네트워크 검사나 최신 버전 조회를 요구하지 않습니다. Cilium이 이미 비정상인 경우에도 관리 표식과 명시적 전용 노드 옵션을 확인한 전체 제거는 K3s 데이터까지 삭제합니다. 입력 bundle 디렉터리는 자동 삭제하지 않습니다. 전체 제거 후에도 보존한 bundle로 다시 설치할 수 있습니다.

Bundle은 `/var/lib/rancher/k3s`나 `/etc/rancher/k3s` 아래에 저장하지 않습니다. 전체 제거와 함께 사라지는 위치이므로 검증 단계에서 거부합니다. Bundle을 사용한 배포는 이미지 tag를 digest로 고정하므로 `verify`에도 동일한 bundle 입력을 전달해야 합니다.

## 실제 VM 검사

**새로 준비하거나 전체 정리한 전용 Linux VM에서만 실행합니다.** 이 시험 도구는 해당 VM의 별도 nftables table(시험용 방화벽 규칙)을 만들어 외부 DNS/TCP/UDP를 차단하고, 종료 시 그 table만 제거합니다. Mac 호스트 또는 클라우드 방화벽을 바꾸지 않습니다. 시험 도구에는 `nft` 명령이 추가로 필요합니다.

```bash
sudo python3 deployment/scripts/tests/test_airgap_vm.py \
  --bundle /var/lib/railshot-deployment/bundle \
  --results /var/tmp/railshot-airgap-results --disposable-node

# Cold registry pull이 느린 환경에서 시험 입력의 준비 대기만 늘립니다.
# Runtime 기본값은 180초이고 preflight timeout과는 별개입니다.
sudo python3 deployment/scripts/tests/test_airgap_vm.py \
  --bundle /var/lib/railshot-deployment/bundle \
  --results /var/tmp/railshot-airgap-results-longer \
  --runtime-timeout 600 --disposable-node
```

Online·bundle 재사용·registry 실패와 bundle 유무·인터넷 차단 후 최초 설치·손상/checksum/image 누락·반복 offline 배포·cleanup/redeploy·offline update·네트워크 복구·Cloudflare 불가·preflight timeout(대기 제한) 및 CPU 구조 불일치·동일 digest 재import 방지 등 16개 시나리오를 검사합니다. 원본 JSON·stderr·차단 규칙·차단 증거·PASS/FAIL/SKIP 수를 보존하며, 앞 단계 실패로 실행하지 않은 항목은 SKIP으로 기록합니다.

실제 수행 결과와 아직 검증하지 않은 내용은 [검증 기록](../scripts/tests/results/AIRGAP-VALIDATION-2026-10-02.md)에 정리합니다. WireGuard는 지원 경로에서 제거했습니다. QUIC, Cloudflare token/domain/auth(접속 자격정보) 및 실제 CSP 환경은 별도 계약과 검증이 필요합니다.

## Artifact storage와 GitHub Release

해커톤의 앱 이미지 배포 기준은 GHCR(깃허브 컨테이너 이미지 저장소), offline bundle 배포 기준은 GitHub Release asset(버전에 연결한 첨부 파일)입니다. K3s·Cilium 등 공식 시스템 이미지는 공식 registry를 사용하며, 앱 이미지의 GHCR 빌드·push는 CI(빌드·검사 자동화) 담당 영역입니다. 이 Runtime은 GHCR workload 입력을 받을 수 있으나 **현재 실제 Linux 앱 검사는 Docker Hub nginx로 수행했습니다.** GHCR의 접속 검사를 이미지 다운로드·앱 배포 검증으로 표현하지 않습니다.

큰 이미지 archive와 생성 bundle·`airgap/dist/`·`airgap/downloads/`·`.oci` 파일은 Git에서 제외합니다. Git에는 script·README·manifest·checksum·artifact metadata(배포 파일 정보)만 넣습니다. 수 GB 크기나 잦은 갱신이 필요하면 향후 S3/GCS 같은 object storage(파일 저장 서비스)로 전달 계층을 바꿀 수 있습니다. 현재 Runtime은 저장소 API에 직접 의존하지 않습니다.

현재 로컬 arm64 Release 파일은 **747,700,166 bytes(약 713.1 MiB)**입니다. 원래 bundle 디렉터리는 약 803 MiB입니다. amd64 파일 이름·CPU 구조 선택 경로는 구현했지만 **amd64 bundle/Release asset을 실제 생성하거나 설치하지 않았습니다.**

```text
airgap-bundle-v0.1.0                     # Release tag 예시입니다.
├── railshot-airgap-arm64-v0.1.0.tar.zst
├── bundle-manifest-arm64.json
├── artifact-metadata-arm64.json
├── checksums-arm64.txt
└── amd64 파일 4개                        # 실제 amd64 준비·검증 후 추가합니다.
```

CPU 구조별 manifest/checksum 이름을 달리하여 같은 Release에 두 구조를 올릴 때 충돌하지 않게 했습니다. 실제 arm64 [metadata](metadata/v0.1.0/artifact-metadata-arm64.json), [manifest](metadata/v0.1.0/bundle-manifest-arm64.json), [checksum](metadata/v0.1.0/checksums-arm64.txt)을 Git에 보존합니다. Release version(`v0.1.0`)은 bundle version(`2026-10-02.1`) 및 Runtime component version과 별도입니다.

### Pack(로컬 Release 파일 생성)

검증된 bundle과 `zstd` 압축 명령이 필요합니다. 같은 출력 경로의 동일 bundle은 재사용하며, 다른 bundle로 덮어쓰지 않습니다. manifest에 선언한 파일만 압축합니다.

```bash
./deployment/airgap/release-pack.sh \
  --bundle /var/lib/railshot-deployment/bundle \
  --version v0.1.0 --output deployment/airgap/dist/v0.1.0/arm64 \
  --manifest-sha256 "$TRUSTED_MANIFEST_SHA256"
```

### Upload(명시적 draft 업로드)

GitHub CLI `gh`가 필요합니다. 도구는 인증과 해당 repository의 push 권한을 확인합니다. 기존 **draft(미공개 초안) Release만** 사용하고, 파일 이름 충돌 시 덮어쓰지 않습니다. `--create-draft`를 직접 지정한 경우에만 초안을 만들며 정확한 대상 commit SHA와 설명 파일이 필요합니다. 이 도구는 Release를 publish(공개 확정)하지 않습니다.

```bash
./deployment/airgap/release-upload.sh \
  --repo Jasmin-Softbank/Railshot --version v0.1.0 --architecture arm64 \
  --directory deployment/airgap/dist/v0.1.0/arm64

# 승인된 commit과 문서를 준비한 후 초안 생성을 명시할 수 있습니다.
./deployment/airgap/release-upload.sh \
  --repo Jasmin-Softbank/Railshot --version v0.1.0 --architecture arm64 \
  --directory deployment/airgap/dist/v0.1.0/arm64 --create-draft \
  --target "$APPROVED_COMMIT_SHA" --notes-file release-notes.md
```

업로드가 중간에 실패하면 원격 draft 일부 파일이 남을 수 있습니다. 도구는 원격 파일을 자동 삭제하거나 덮어쓰지 않습니다. 원격 상태를 확인하고 새 버전 또는 승인된 수동 복구 절차를 사용해야 합니다. [공식 upload 명령](https://cli.github.com/manual/gh_release_upload)을 사용합니다.

### Download(지정 버전 다운로드·검증)

최초 Onboarding 때 신뢰한 archive SHA256과 manifest SHA256을 별도로 전달합니다. 다운로드한 checksum metadata 자체만 신뢰하지 않습니다. `latest`를 사용하지 않고 정확한 Release version을 받습니다. 새 디렉터리에서 archive checksum·안전한 경로·bundle checksum/digest·CPU 구조를 검사한 뒤 실행 경로로 게시합니다.

```bash
./deployment/airgap/release-download.sh \
  --repo Jasmin-Softbank/Railshot --version v0.1.0 --architecture arm64 \
  --output /var/lib/railshot-deployment/bundle \
  --archive-sha256 "$TRUSTED_ARCHIVE_SHA256" \
  --manifest-sha256 "$TRUSTED_MANIFEST_SHA256"
```

이는 연결된 Onboarding 단계의 도구이며 offline deploy 중에는 호출하지 않습니다. [공식 download 명령](https://cli.github.com/manual/gh_release_download)을 사용합니다. 이번에는 인증·권한 확인, 실제 로컬 파일 생성과 Linux에서 압축 해제·bundle 검증을 수행했습니다. **GitHub Release 실제 생성·업로드·원격 다운로드는 수행하지 않았습니다.** 해당 원격 호출은 자동 테스트에서 mock(모의 응답)으로 검사했습니다.

후속 Release 분리와 16개 Linux 시나리오의 실제 결과는 [최신 Release 검증 기록](../scripts/tests/results/RELEASE-VALIDATION-2026-10-02.md)에 정리했습니다.

새 Railshot 위치의 재검증·첫 실패·최종 통과 결과는 [이관 검증 기록](../scripts/tests/results/MIGRATION-VALIDATION-2026-10-02.md)을 확인하시면 됩니다.
