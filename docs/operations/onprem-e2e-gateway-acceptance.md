# 온프레미스 연결 검증용 gateway 실행 결과

2026-10-10 한국 시간에 work-vm에서 수행한 로컬 검증 결과다. OpenStack 명령 성공과 공개 UDP 도달성을 이 문서의 통과로 추정하지 않는다.

## 실행 자원

- 독립 Docker Compose project: `railshot-onprem-e2e`.
- API: `railshot/api:onprem-e2e`, 약 404MB, UID/GID 1000, capability 없음.
- Gateway: `railshot/personal-gateway:production-test`, 약 202MB, 별도 network namespace, `NET_ADMIN`만 추가.
- Dashboard: 약 94MB. HTTPS proxy와 인증된 시험 메일 receiver를 E2E overlay로 추가했다.
- Gateway 사설 RPC: `172.29.253.2:4184`. 동적 Docker 주소 풀을 `172.29.253.128/25`로 제한해 API가 gateway 주소를 먼저 받는 문제를 수정했다.
- HTTPS 시험 설치 URL: `https://e2e.railshot.test:18443`. Docker에서는 TLS 서비스 alias로 해석하고 원격 호스트에서는 승인된 SSH 역포워드와 hosts 항목으로 연결한다.
- 시험 CA 공개 인증서 SHA256: `832c97011c888f994bbd11a8f755d4b9f679b76707cbb43662d2b70f875dd0ca`. 개인키를 원격 호스트로 복사하지 않는다.
- 기존 observability 3개와 proxmox-ctrl-mcp 컨테이너는 계속 실행된 상태를 확인했다.

## 실제 통과한 검사

| 검사 | 결과 |
| --- | --- |
| 실제 API namespace에서 동일 HTTPS manifest, installer, artifact 사전검사 | ready=true, 세 파일의 SHA256/크기 검증 성공 |
| HTTPS 시험 CA 검증 | work-vm localhost:8443 health 성공 |
| 계정 가입 → 인증 메일 파일 → 검증 → 로그인 → 소유권 → 30분 토큰 | 계정 실행기 setup 18개 통과 |
| API와 gateway network namespace 분리 | 서로 다른 namespace inode 확인 |
| API capability 및 WireGuard 소유 여부 | capability=0, API 안에 railshotwg 없음 |
| 별도 임시 peer의 실제 kernel WireGuard | Docker 사설망의 native UDP handshake 확인 |
| 인증된 RPC 및 제한된 CONNECT | 관리 TCP 2222 byte stream 성공, 잘못된 generation/port/bearer 거부 |
| Gateway 재시작 복원 | gateway 컨테이너만 재시작 후 동일 peer/route 복원 및 byte stream 성공 |
| 임시 peer 제거 | RPC remove 이후 CONNECT 404, 임시 peer 컨테이너 제거 |
| Python 회귀 | 34개 통과, 수백 번 fsync 풀순환 2개 제외 |

재시작 검사 최초의 60초 대기 한도는 실제 WireGuard 재키 교환 주기보다 짧았다. 일반 클라이언트 재키 교환을 기다리도록 재시작 후 handshake 대기 한도를 180초로 보완했고 성공을 확인했다. 서버 키나 peer 키를 재발급하지 않았다.

실제 계정의 생성 암호는 실행기 RAM에서만 사용했다. 세션·CSRF·등록 토큰은 `/tmp/railshot-onprem-e2e-account-output/account-state.json`의 UID/GID 1000, 0600 파일로 저장했다. 토큰과 메일 본문을 채팅·로그·Git에 기록하지 않았다. 설치 직전에 실행기의 `refresh`를 수행해 30분 토큰을 다시 발급할 수 있다.

## 원격 연결 진단 경로

지정 원격 VM에서 work-vm의 NAT 주소로 직접 TCP/UDP 도달성이 확인되지 않아 SSH 역포워드용 단일 peer UDP 릴레이를 별도로 준비했다. work-vm은 TCP 127.0.0.1:8444 → UDP 고정 127.0.0.1:51920, 원격 쪽은 UDP 127.0.0.1:51821 → TCP 고정 127.0.0.1:18444를 사용한다. 데이터그램 경계를 길이 프레임으로 보존하고, 다른 UDP 송신자의 응답과 잘못된 프레임 길이를 차단한다. 패킷 내용을 출력하지 않는다.

이 경우 gateway가 클라이언트에 제공하는 endpoint는 시험 전용 `127.0.0.1:51821`이다. 로컬 native probe는 endpoint를 Docker gateway의 실제 UDP 51820으로 별도 지정한다. 릴레이를 통한 원격 WireGuard 성공은 공개 native UDP 인수검증으로 집계하지 않는다.

단일 노드 DevStack 현장 Keystone의 HTTP 때문에 E2E overlay에 `RAILSHOT_PERSONAL_TEST_ALLOW_HTTP=1`을 명시했다. 설치/API/artifact 실제 URL은 HTTPS를 사용했다. CI/runtime은 이 경량 연결 이미지에 구성하지 않았으므로 배포 준비 성공의 증거가 아니다.

## 인계용 시험 릴레이 안정화

실제 원격 클라이언트 설치와 계정 `check` 6개 성공 이후, 부모 세션의 지시에 따라 work-vm 릴레이를 Codex 실행기 세션에서 독립 systemd 서비스로 교체했다. 이 작업은 지정 원격 VM, floating IP, 보안 그룹 및 기존 Docker 서비스를 변경하지 않았다.

- 서비스: `railshot-onprem-e2e-host-relay.service`, enabled 및 active/running 확인.
- 실행 계정: `codex:codex`, 실제 UID 1001, 유효 capability 0.
- 실행 코드: `/usr/local/libexec/railshot-onprem-e2e/udp-relay.py`, root:root 0555.
- unit: `/etc/systemd/system/railshot-onprem-e2e-host-relay.service`, root:root 0644. 저장소 원본은 `deployment/manifests/personal/e2e-host-relay.service`다.
- 원본과 설치 코드 SHA256: `df5449bbbd0f37a45c0adfe6b0682566b4455961b75ef5fa553f7490643b4b19`, 동일함을 대조했다.
- 수신: TCP `127.0.0.1:8444`; 유일한 UDP 목적지: `127.0.0.1:51920`.
- 재시작 정책: `Restart=always`, `RestartSec=2`. 시험 계정의 capability를 추가하지 않으며 host network와 기존 SSH 역포워드를 사용한다.

종료 직전에 기존 PID 3185679의 정확한 argv, UID, 실행 파일, 전담 워크트리 cwd, 8444 listener 소유권 및 코드 해시를 다시 대조했다. pidfd로 그 프로세스에만 SIGTERM을 보냈고 이전 실행기 세션은 종료 코드 143으로 끝났다. 교체 직후 새 MainPID는 3421477이며 해당 argv와 UID, capability 0 및 loopback listener를 다시 확인했다.

교체 후 gateway의 인증된 health probe가 통과했으며 E2E 5개 서비스는 healthy였다. 기존 Grafana·Tempo·Prometheus·proxmox-ctrl-mcp 컨테이너는 계속 실행 중이었다. 부모 세션은 원격 시험 unit에도 `Restart=always`를 적용한 뒤 자동 재시작 1회, 새 WireGuard handshake 및 client/runtime active를 독립적으로 확인했다고 전달했다. 전환 후 실제 REST/OpenStack 재검증 결과는 부모 세션의 전체 E2E 기록에 남긴다.

이 서비스에는 비밀번호, 토큰 또는 OpenStack 자격 정보가 없으며 packet 내용을 출력하지 않는다. 단일 peer의 시험용 SSH 전송 안정화이며 공개 native UDP 인수조건을 추가로 통과했다는 의미는 아니다.
