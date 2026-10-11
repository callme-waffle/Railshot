# GCP 호스트 준비

이 module은 고객용 Ubuntu 24.04 amd64 VM 한 대, 전용 VPC/subnet, 정적 IP, 역할 없는 service account와 별도 데이터 disk를 구성한다. 팀 K3s/Cilium·Ansible·CD·DB 구현은 설치하지 않는다. 운영 CI/CD 서버와 별개이며 현재 역할은 [저장소 README](../../../README.md), 공통 실행·보존 계약은 [Terraform 도구](../../providers/terraform_tools/README.md)를 따른다.

`project_id`, region/zone, target/owner, 정확한 공개 Ubuntu image를 명시한다. image family는 거부한다. 계정·billing·API 활성화와 quota는 module 외부 관리자 준비 사항이며 credential은 입력이나 metadata에 넣지 않는다. `node_repo`, `node_ref`, public bundle, GitOps 입력은 제거했다. 기존 target 설정에서도 해당 입력을 정리한다.

cloud-init은 Ubuntu apt source를 HTTPS로 바꾸고 디스크 도구를 설치한다. `/dev/disk/by-id/google-railshot-data`를 기다려 `/var/lib/rancher`에 mount한 뒤 `host_prepared` marker만 남긴다. `/etc/railshot/host.yml`의 desired node identity는 팀 runtime 인계 정보이며, runtime은 `not_configured`다. Terraform bootstrap 상태는 실제 확인 전 `unverified`로 유지한다.

`initialize_empty_data_disk`는 기본 false다. 새 module-created 빈 디스크를 검토한 경우만 true로 초기화한다. 기존 ext4는 재사용하고, 파티션·알 수 없는 서명·다른 파일시스템·비어 있지 않은 unmounted 경로·다른 UUID는 거부한다. fstab은 UUID를 사용하고 `nofail`을 두지 않는다. 팀 runtime 설치자는 mount-before-start와 기존 node identity/버전 호환성을 별도 검증해야 한다.

데이터 disk의 `prevent_destroy` 및 `deletion_policy = PREVENT`, compute attachment의 비자동 삭제를 유지한다. 같은 zone의 compute 교체도 중단·동시 writer 방지·백업/복구 검토가 필요하다. retained disk는 백업이나 HA가 아니며, disk 확대 후 filesystem 확대도 별도다. 소스 변경은 기존 guest를 재구성하지 않으며 cloud-init metadata 변경이 자동 재실행을 보장하지 않는다.

HTTP/HTTPS ingress는 기본 닫힘이다. IAP SSH도 별도 opt-in이며 `35.235.240.0/20`의 TCP22만 허용하고 OS Login/IAP 권한을 따로 준비한다. host egress는 TCP443 허용 후 나머지를 deny한다. Google metadata DNS/NTP의 플랫폼 예외는 남으므로 tenant 격리는 팀 guest 정책의 책임이다. registry OAuth scope는 기본 없고, opt-in 시 read-only scope만 추가한다. 실제 repository IAM은 별도다. 정적 공인 IP는 egress에 사용되며 정지 후에도 보존 비용이 남는다.

자동 관리용으로 `allow_iap_ssh: true`, `operator_ssh_public_key`에 공개키 한 줄을 지정하면 첫 부팅에 잠긴 암호와 noninteractive sudo를 가진 `railshot-operator`를 만들고 해당 VM의 OS Login을 끈다. 공개키를 지정하지 않으면 기존 OS Login 방식을 유지한다. private key는 입력하지 않는다. IAP 터널 권한과 신뢰한 SSH host key 검증은 별도로 필요하며 `node_descriptor.transport_ref`는 기존 `iap:<project>/<zone>/<name>` 형식이다. 기존 VM의 metadata 변경만으로 사용자·키가 다시 설치되지는 않는다.

WireGuard 선택 항목과 출력은 폐기했다. 이전 `wireguard_peer_public_cidrs`에 값이 있으면 Terraform 입력 검사가 실패하므로 검토한 target 설정에서 해당 필드를 제거한다. 이 module은 새 UDP51820 방화벽이나 터널을 만들지 않는다. GCP native L7는 별도 준비 대상이며 이 변경으로 생성되지 않는다.

기존 `google_compute_firewall.wireguard_ingress`와 `wireguard_egress`는 Terraform 1.7 이상의 `removed { lifecycle { destroy = false } }`로 인계한다. 검토한 migration plan을 적용하면 Terraform 관리 대상에서 빠지고 실제 규칙은 남는다. VM·정적 IP·디스크·VPC/subnet 주소는 유지하며 이 소스 변경만으로 live state나 guest를 바꾸지 않는다. 기존 `*-wireguard-in` / `*-wireguard-out` 규칙과 guest 터널은 GCP L7·DNS 전환 및 Argo의 Kubernetes API6443 대체 경로가 검증된 후 별도 운영 작업으로 제거해야 한다. 제거 전 남은 리소스 ID와 소유자를 기록하고, 관리에서 빠졌다는 사실을 삭제 완료로 간주하지 않는다.

`max_run_duration_seconds`는 기본 null이다. 설정하면 매 start마다 지정 기간 후 STOP, `automatic_restart = false`를 적용한다. 작업 drain·절대 예산 cap·disk/IP 삭제는 수행하지 않는다. `desired_status`를 강제하지 않고 `allow_stopping_for_update = false`를 유지한다. cutoff를 다른 reconciler가 다시 시작하지 않도록 운영자가 확인해야 한다. [GCP runtime limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime).

오프라인 확인:

```sh
uv run --python 3.13 --with pyyaml python infrastructure/terraform/gcp/test_bootstrap.py
```

검사는 임시 source-only 디렉터리의 Terraform console, YAML 및 `bash -n`만 사용한다. 계정·provider·state를 읽거나 guest 명령을 실행하지 않는다. 실제 apply는 [관리자 실행 절차](../../providers/terraform_tools/README.md)에 따라 saved plan을 검토한 뒤 별도로 수행한다. 생성 후 project/zone/image/SA/network/disk ID, cloud-init 결과와 실제 mount UUID를 확인해야 하며 team runtime readiness는 별도 증거가 필요하다.

`purpose: database`는 retained disk를 `/var/lib/postgresql`에 mount하고 공개 HTTP/HTTPS ingress를 닫는다. 기본 runtime은 기존 `/var/lib/rancher`를 사용한다. `database_ingress` / `database_egress`는 TCP 5432/2379/2380/8008과 RFC1918 /16-/32 CIDR만 받는다. 앱→proxy, proxy→DB, DB↔etcd 등 실제 peer만 등록하며 방화벽 규칙은 라우팅·도달성 성공을 뜻하지 않는다.

여러 VM이 같은 환경 네트워크를 쓰려면 `existing_network_name`과 `existing_subnetwork_name`을 함께 지정한다. 같은 project/region의 subnet이 지정 network에 속하는지 검사하고 이 module에서 새 VPC/subnet을 만들지 않는다. 두 값을 생략하면 기존 전용 네트워크 생성 방식이다. 기존 Terraform 주소는 moved block으로 보존한다. DB 노드별로 새 VPC를 생성하는 설정을 공통 클러스터 네트워크로 간주하지 않는다.

첫 bootstrap은 SSH Ed25519 **공개키만** `RAILSHOT_SSH_HOST_KEY` marker로 serial port 1에 기록한다. 공통 `access.py`는 인증된 Compute API에서 instance ID/private IP를 대조하고 이 marker를 읽어 0600 known_hosts를 생성한다. SSH keyscan/accept-new는 사용하지 않는다. marker가 없는 기존 VM은 자동으로 신뢰하지 않으며, metadata 변경만으로 bootstrap을 재실행했다고 가정하지 않는다.
