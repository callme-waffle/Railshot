# etcd 역할

Ubuntu 24.04의 `etcd-server`, `etcd-client` 패키지를 설치합니다. `etcd_nodes` 전체를 한 플레이에서 `strategy: linear`, `any_errors_fatal: true`로 실행해야 합니다. 최초 기동에 `serial: 1`을 사용하지 마십시오. 모든 멤버 기동 작업이 끝난 뒤 건강 상태를 검사합니다. 버전 업그레이드와 멤버 교체는 이 역할의 범위가 아닙니다.

필수 입력: `cluster_name`, 각 노드의 `private_ip`, `etcd_ca_src`, `etcd_cert_src`, `etcd_key_src`. 인증서 원본은 실행 제어 호스트의 파일입니다. 서버 인증서는 노드의 사설 IP를 SAN(인증서의 추가 식별 이름)에 포함하고 `serverAuth`, `clientAuth` 용도를 모두 허용해야 합니다. 서로의 인증서를 신뢰하는 CA를 사용하십시오. 2379는 허가된 데이터베이스·관리 노드에, 2380은 etcd 노드끼리만 허용하십시오.

데이터 경로 기본값은 `/var/lib/etcd/jasmin`입니다. 초기화된 멤버와 새 멤버가 섞이면 중단하며, 기존 데이터·인증서·설정 변경 시 자동 재시작하거나 데이터를 삭제하지 않습니다. 설정 또는 인증서 갱신은 과반수를 유지하는 별도 순차 유지보수로 수행해야 합니다. 일부 멤버만 초기화된 실패도 별도 복구 대상으로 표시합니다. 초기화된 데이터가 있으면 etcd 자체가 최초 생성 옵션을 무시합니다.

검증: 각 멤버에서 인증서 검증을 켠 `etcdctl endpoint health`를 실행합니다. 이 검사는 실제 서비스가 준비된 경우에만 의미가 있으며, 문법 검사 성공은 과반수 상실·복구 시험을 대체하지 않습니다. 확인 모드(`--check`)는 실제 건강 검사를 생략합니다.

근거: https://etcd.io/docs/v3.5/op-guide/configuration/ 및 https://etcd.io/docs/v3.5/op-guide/clustering/
