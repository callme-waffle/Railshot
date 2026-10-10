# Azure 호스트 준비

이 module은 Ubuntu 24.04 amd64 VM, VNet/subnet, NSG, NIC, 정적 public IP, system-assigned identity와 별도 managed data disk를 구성한다. 팀 Ansible·K3s/Cilium·CD·DB 구현은 설치하지 않는다. 현재 역할은 [저장소 README](../../../README.md), 실행·보존 경계는 [infra README](../../README.md)를 따른다.

등록된 subscription/region/target/owner와 정확한 numeric image version을 명시한다. `latest`는 거부하며 D2s/D4s/D8s_v5 SCSI profile만 지원한다. resource group은 기본 existing이고 create 선택 시 Terraform 삭제 방지를 둔다. 모드 전환은 state 이전 검토가 필요하다. resource-provider registration은 꺼져 있고 VM identity에 RBAC를 부여하지 않는다. executor 인증·Microsoft.Compute/Network 등록·quota·backend는 관리자 준비 사항이다.

cloud-init은 HTTPS apt와 디스크 도구를 준비하고 LUN0 disk를 기다린다. 빈 디스크 초기화는 `initialize_empty_data_disk = true`에서만 허용하며 기본 false다. whole disk/서명/기존 ext4를 검사하고 `/var/lib/rancher`의 fstab 충돌·덮일 기존 데이터·mount UUID를 확인한다. Ansible/GitOps repo 또는 token 입력은 없고 `/etc/railshot/host.yml`의 provider·desired node identity만 남긴다. 성공 marker는 `host_prepared`, `runtime_ready: not_configured`다. Terraform `bootstrap.readiness`는 `unverified`, team runtime은 `not_configured`다.

runtime 담당자는 mount-before-start, 기존 node identity와 버전 호환성, storage/DB writer 및 복구 검사를 따로 연결해야 한다. 이번 소스 변경은 기존 guest에서 프로그램·자격을 삭제하지 않는다. custom-data/image 변경은 다음 Terraform 계획에서 VM 교체를 요구할 수 있으므로 drain·백업·중단 영향을 먼저 검토한다.

NSG inbound는 기본 닫힘이며 SSH/VNet source도 막는다. 명시한 web ports만 허용하고 TLS를 설치하지 않는다. VM API용 공개 SSH key는 SSH 경로를 열지 않으며 private key는 받지 않는다. egress는 HTTPS·DNS·NTP·필수 Azure agent 경로 후 나머지를 deny한다. 이것은 tenant 격리나 domain allowlist가 아니다. transport_ref는 향후 관리 경로 참조이며 command transport를 구현하지 않는다.

| 작업 | 보존과 확인할 영향 |
|---|---|
| guest shutdown / Stopped | compute가 할당된 상태면 요금이 남을 수 있음 |
| deallocate | compute 할당을 해제해도 disk/IP 비용이 남음; 이 root는 start/deallocate 실행기를 제공하지 않음 |
| compute_enabled=false | 검토한 apply가 VM·OS disk·attachment를 삭제하고 data disk/network/IP를 보존; pause가 아님 |
| compute 재생성 | 새 VM/identity와 기존 data disk 연결; ext4 재사용만으로 앱 복구를 입증하지 못함 |
| destroy/rename | data disk와 생성 RG의 prevent_destroy 유지; 소스 블록 제거 또는 외부 API 삭제까지 막지 못함 |

`/var/lib/rancher`의 retained data는 임의 host path·외부 DB·RAM을 보존하지 않는다. filesystem 확대, backup/restore, DB schema 복구는 별도이며 단일 VM에서 HA·무중단을 보장하지 않는다. 기존 VM·disk·state 정리는 이번 소스 변경의 범위가 아니다.

오프라인 확인:

```sh
python3 infra/terraform/azure/tests/test_bootstrap.py
python3 -m unittest discover -s infra/terraform -p test_egress.py
```

검사는 source template을 임시 디렉터리의 Terraform console에서 평가하고 YAML/셸 문법과 임시 apt 파일만 검증한다. 실제 Azure API·state·guest 실행은 없다. live acceptance는 saved-plan 검토, 실제 VM/disk/NSG/identity, cloud-init 결과, mount UUID, 보존/복구 및 팀 runtime 증거를 각각 확인한다.
