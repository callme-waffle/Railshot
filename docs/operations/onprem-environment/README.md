# 온프레미스 환경 설정 입력

값은 채팅이나 PR에 넣지 않고 실행기 소유의 저장소 밖 디렉터리에 입력합니다.
예시의 `REPLACE_...`와 example.com은 반드시 실제 승인된 값으로 교체합니다.
디렉터리는 0700, JSON·키·공개키 파일도 0600으로 만들며 기존 다른 사용자
소유 파일의 권한을 바꾸어 읽지 않습니다. 예시는 연결 검증 결과가 아닙니다.

| 입력 | 준비할 내용 |
|---|---|
| 계정 | Railshot 사용자 ID와 등록 계정 ID, OpenStack project ID/region |
| 기존 고객 연결 | personal 설정 파일, 등록 target snapshot, 기존 WireGuard gateway와 vault 공개키/암호문 메타데이터 참조 |
| 직접 운영자 연결 | remote_config_file 대신 credentials_file. 프로젝트에 제한된 Application Credential JSON; 임시 평문은 기존 /dev/shm 실행기만 사용 |
| VM | Ubuntu amd64 cloud image UUID, kubeadm/PG flavor UUID, 노드별 AZ. 최소 kubeadm 2 CPU/2GiB/20GB |
| 신규망 | 겹치지 않는 사설 CIDR, 외부 provider network ID. VIP용 .10과 VM DHCP .16 이후를 사용 |
| 관리망 | 기존 관리 network ID와 실행기 관리 CIDR. 각 guest의 보조 NIC에 실행기가 실제로 SSH 접근할 수 있어야 함 |
| SSH | 비 root sudo 사용자, guest 전용 개인키와 대응 공개키 파일. 신규 VM host key는 소유 확인된 Nova console 공개키로 등록; 선택적 기존 known_hosts는 독립 검증된 값 |
| CephFS | FSID, MON 주소, 파일시스템·subvolume group, CSI 사용자 자격의 private file 참조 |
| 공개 접근 | Octavia, Barbican TLS container, 프로젝트 범위 edge credential, DNS zone 권한, Named Tunnel UUID/credential 파일 |
| 앱 | GitOps repo/context, CI source repo/tenant, GHCR 읽기 자격 참조, 공통 kubeadm dynamic route 설정 |
| PostgreSQL | 별도 VM flavor, 공통 standalone runtime 설정, 백업 저장소 endpoint/bucket/prefix/credential reference, TLS와 보존 정책 |

1. 각 `.json.example`을 실행기의 private 디렉터리로 복사하고 실제 식별자와 파일 참조를 입력합니다. secret 원문을 예시 파일에 커밋하지 않습니다.
2. `profile.json`의 remote_config_file을 선택하면 기존 `createPersonalAdapter.execute`를 통해 고객 측 vault/WireGuard/forced SSH 경계를 유지합니다. 인증 정보가 중앙의 일반 CLI로 복사되지 않습니다.
3. remote native API 연결 성공은 guest SSH 연결 성공과 다릅니다. 현재 guest 실행기는 명시한 관리망 NIC로 직접 접근합니다. gateway가 고객 관리 호스트에만 도달할 수 있다면 guest 관리 경로도 준비해야 합니다. 기존 gateway 권한을 넓히거나 임의 SSH forwarding을 켜지 않습니다.
4. `validate_config`는 누락 필드·오류 코드만 반환합니다. 아래 오프라인 검사는 API를 호출하지 않습니다.

```python
import json
from infrastructure.providers.environments.onprem import private_file, validate_config
profile = private_file('/etc/railshot/private/onprem/profile.json')
print(json.dumps(validate_config(profile, check_files=True)))
```

5. 공통 connection/catalog/plan 검사로 프로젝트 scope·이미지·flavor·provider network를 확인한 뒤 승인된 환경 apply를 사용합니다.

`PermissionError`가 발생했던 uid 1001 소유 credential 경로는 실행 사용자와
배포된 자격 전달 경로의 정합성을 먼저 해결해야 하는 live 선행조건입니다.
chmod/chown 또는 소유권 우회로 해결하지 않습니다.

공통 kubeadm multi-app route와 standalone PostgreSQL runtime은 별도 통합
작업입니다. 해당 모듈/설정이 없는 환경은 실행 가능으로 표시하지 않습니다.
모의 API 응답, syntax 검사, 설정 파일 존재만으로 live 지원 완료를 표시하지
않습니다. 환경 전체 삭제는 app/route/DB revocation과 데이터 보존 정책을
확인하는 공통 teardown이 필요합니다. 실제 해제 receipt 후 네이티브 리소스를
역순 삭제하고 API 목록에서 사라졌는지 확인합니다. CephFS 데이터는 보존하고,
PostgreSQL VM root disk의 데이터 보존 계약이 확정되기 전에는 PG VM과 연결망을
의도적으로 보존한 리소스로 명시합니다. PG VM은 SHUTOFF를 확인하고 보존합니다.
PG root disk, 데이터·관리 port, security group/rules, keypair, router/subnet/network는
남습니다. 별도 CephFS 영속 데이터와 DB 백업도 삭제하지 않습니다. 보존 자원의
스토리지·네트워크 비용은 계속 발생할 수 있습니다. VM 시작·중지는 소유권과 실제 전원 상태를
확인하며, 시작만으로 애플리케이션 준비 완료를 표시하지 않습니다.
