# 단일 PostgreSQL 환경 구현 명세와 계획

승인 범위는 독립 VM의 PostgreSQL 16, private CIDR, TLS/SCRAM, 전용 데이터 디스크, 외부 S3/GCS pgBackRest 저장소, 백업과 명시 백업 복구, 앱별 제한 role 연결이다. Patroni/HA 경로는 유지한다. 환경 생성과 동일하게 실제 호출 결과가 없는 readiness는 false다.

## 완료 기준

- `infrastructure/ansible/standalone.py`의 `run(action, descriptor, config, context)`는 운영자 private 파일과 정규화된 소유 VM만 수용한다.
- AWS SSM/GCP IAP/온프레미스·NCP pinned SSH를 기존 forwarding/inventory로 실행한다.
- PostgreSQL 설치는 실제 mount/device 일치, 환경/VM ownership marker, TLS와 private HBA를 확인한다.
- install/status/verify는 SQL server identity·TLS 및 pgBackRest repository 상태를 확인한다. backup은 외부 저장소 완료 항목과 데이터 proof를 반환한다.
- restore는 내부 애플리케이션 중지 gate와 별도 운영자 승인, 정확한 backup_id가 필요하다. 기존 데이터 디렉터리는 보존하며, 복구 후 PostgreSQL 시작·SQL proof·system identifier 확인 전 성공하지 않는다.
- 기존 application_database.py를 재사용해 앱별 migration/runtime role과 TLS 로그인 검증을 제공한다.
- 비밀값 없는 양식과 offline validation, meaningful native/controller tests와 Ansible syntax/lint를 제공한다. 실환경은 인증·비용 승인 후 별도 검증한다.

## 구현 단계

1. private config·descriptor·typed action·journal 계약과 단일 호스트 Ansible runner.
2. 고정 guest installer/verification, pgBackRest 외부 저장소와 restore 보존 정책.
3. 앱 binding 연결, input template, negative readiness와 명령 경계 검증.
4. AWS/onprem/NCP adapter 통합 및 공통 API restore gate 조율.

## 근거와 제약

PostgreSQL은 [TLS 설정](https://www.postgresql.org/docs/16/ssl-tcp.html)과 [SCRAM HBA](https://www.postgresql.org/docs/16/auth-pg-hba-conf.html)를 사용한다. [pgBackRest 설정](https://pgbackrest.org/configuration.html)은 AWS instance role과 GCS instance service account 자동 갱신, S3-compatible endpoint를 지원한다. [복구 명령](https://pgbackrest.org/command.html)은 정확한 backup set과 새 빈 데이터 디렉터리를 사용한다. 백업 목록 검사는 복구 검증과 구분한다.

첫 지원 OS는 Ubuntu 24.04 amd64, PostgreSQL 16이다. 생성된 파일/예시 ID는 비용 또는 restore 승인을 대신하지 않는다. 삭제 시 외부 repository와 보존 데이터 디스크를 지우지 않는다. 기존 HA 경로의 백업/복구 동작은 변경하지 않는다.

## 운영자 입력 및 API 연결

운영자 private 디렉터리에 아래 파일을 준비하고 파일은 0600, 디렉터리는 0700으로 유지한다. `standalone.config_template(directory)`는 빈 설정/복구 승인 양식을 반환한다. 검증은 값·비밀·파일내용을 출력하지 않는다.

```sh
python3 infrastructure/ansible/standalone.py --config /private/operator/postgres-config.json --validate-only
```

|필드|입력과 검증|
|---|---|
|version|정수 1|
|client_cidrs|실제 워커/서비스가 사용하는 RFC1918 CIDR 목록. 전체 인터넷 범위 금지|
|tls.server_name|probe TLS 검증 이름. 앱 접속은 DB private IP를 사용하므로 인증서 IP SAN도 필요|
|tls.ca_file / certificate_file / key_file|운영자가 제공하는 인증서/키의 private 파일 참조|
|probe_password_file|24자 이상 임의 probe 전용 암호가 있는 private 텍스트 파일. 앱 암호와 별개|
|data_device|온프레미스/NCP에서 native descriptor에 device가 없을 때 승인 `/dev/disk/by-id/...`. guest가 실제 mount source와 일치를 검증|
|ssh|native descriptor에 없을 때 user/identity_file/known_hosts_file/port22. AWS/GCP는 provider가 등록된 SSM/IAP host key와 결합|
|backup.type|s3 또는 gcs|
|backup.bucket / prefix|기존 외부 bucket과 prefix. 환경 ID가 자동 추가되어 공유 bucket 내 환경을 분리|
|backup.retention_full|2–30. repository는 환경 삭제 시 제거하지 않음|
|backup.identity|AWS S3 `instance`, NCP/온프레미스 S3-compatible `secret`, GCS `instance` 또는 `service`|
|backup.endpoint / region|S3 DNS endpoint(프로토콜·경로 없음) 및 region. TLS 검증 항상 활성화|
|backup.credentials_file|S3 secret은 `{access_key,secret_key}`, GCS service는 서비스 계정 JSON. 해당 모드만 필요|
|restore_approval_file|`{approval_id,environment_id,backup_id,expires_at}` private 승인. 빈 예시는 유효 승인이 아님|
|timeout_seconds|60–1800초. 각 호출은 bounded process group으로 실행|

AWS DB VM instance profile에는 해당 backup bucket의 지정 prefix List/Get/Put/Delete 및 multipart 작업 권한이 필요하다. 권한을 워커 전체에 주지 않는다. GCP DB VM은 bucket-scoped object 권한과 write 가능한 OAuth scope가 모두 필요하다. NCP/온프레미스 S3 endpoint는 유효한 TLS 인증서와 해당 bucket/prefix 전용 자격을 사용한다. bucket 생성·IAM 변경은 각 플랫폼 adapter의 비용/소유권 gate를 거친 별도 구현이며 PG helper가 자동으로 넓은 권한을 부여하지 않는다.

VM 생성자가 데이터 디스크를 붙이고 `/var/lib/postgresql`에 준비한다. PG installer는 디스크를 포맷하지 않는다. AWS는 EBS NVMe volume ID, GCP는 descriptor의 by-id device, 다른 플랫폼은 native device 또는 승인 참조를 실제 mount source에 대조한다. root filesystem 사용이나 기존 외부 PG 데이터의 자동 채택은 거부한다.

### 단일 PG 호출 계약

- `run('install'|'status'|'verify'|'backup', descriptor, config, context)`.
- `run({action:'restore',backup_id:'YYYYMMDD-HHMMSSF'}, ...)`: 내부 `context.restore_authorized=True`, 정수 `active_applications=0`, 만료되지 않은 복구 승인, 기존 저장된 백업 proof가 모두 필요하다.
- `run({action:'bind',binding_file:'/private/generated-binding.json'}, ...)`: 기존 앱 role installer로 migration/runtime 권한과 TLS 로그인을 확인한다.
- 성공은 `status:'succeeded',ready:True,database_ready:True`; native 실패/nonce 불일치/결과유실은 ready false이며 진행한 mutation은 unknown이다.
- 모호한 mutation은 모든 새 mutation을 차단한다. `status`/`verify`는 remote durable journal의 성공 기록을 nonce-bound 관측과 함께 확인할 때만 완료 기록을 복구한다. guest 작업 자체가 중단된 경우 운영자 조정 전 재실행하지 않는다.

`application_bundle(descriptor, config_file, context)`는 private descriptor/config/context 참조 묶음을 생성한다. 이 경로를 앱 profile의 `ingress.standalone_database`에 넣는다. 애플리케이션 등록은 앱별 DB/owner/runtime ID와 서로 다른 암호를 한번 생성한 후 실제 bind를 수행하고, 기존 Kubernetes Secret/Argo/render 경로에 연결한다. AWS는 공통 authority wrapper가 발급한 현재 STS child 환경으로 SSM을 실행한다. 암호를 API 응답이나 PR에 포함하지 않는다.

### 복구 보존과 인수 기준

복구 전 실제 백업의 논리 크기 + 25%(최소 1GiB) 여유 + `max_wal_size` 두 배가 현재 free space 이하인지 확인한다. 부족하면 서비스 중지 전에 거부한다. 공통 앱 gate 외에 PostgreSQL의 예상하지 않은 client backend가 없어야 한다. 기존 DATA는 같은 mount의 `retained/<operation_id>`로 이동하며 자동 삭제하지 않는다. 새 빈 DATA에 지정 백업만 복구하고 재시작 후 동일 system identifier와 백업에 포함된 SQL token을 비교한다. 실패 시 이전 디렉터리는 남고 자동 롤백/재복구를 하지 않는다.

로컬 unit/계약·Ansible syntax/lint는 실환경 인수를 대신하지 않는다. 실환경에서 초기 설치·private TLS 로그인·실제 object backup 생성·복구 후 앱 SQL·오류/재시작·보존 데이터 비용을 확인해야 전체 지원을 완료했다고 판단할 수 있다.

NCP 직접 SSH는 native descriptor의 public_ipv4와 connect_host가 정확히 같은 global IPv4일 때만 허용한다. private IPv4 HostKeyAlias와 사전 등록 known_hosts를 사용하며, provider의 management /32 ACG가 선행되어야 한다. OpenStack relay는 RFC1918만 허용한다.

게스트 readiness는 운영자 TLS `server_name`뿐 아니라 앱 binding이 실제 사용하는 DB private IP를 `host`로 둔 별도 `psycopg2` 연결도 `sslmode=verify-full`로 검증한다. 인증서에 고정 DB private IP SAN이 없으면 DNS 이름 검증이 성공해도 ready로 처리하지 않는다. 각 연결의 `pg_stat_ssl` 결과와 기존 SQL/백업 저장소 검사를 모두 통과해야 한다.
