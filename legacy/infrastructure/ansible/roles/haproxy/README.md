# HAProxy 역할

별도 `proxy_nodes`에서 PostgreSQL 쓰기 접속을 중계합니다. `private_ip:5432`를 수신하며, 각 `db_nodes`의 Patroni `/primary` 응답이 200인 노드만 선택합니다. 전환 시 중단된 서버로 연결된 세션을 종료하므로 애플리케이션의 재접속·트랜잭션 재시도 처리가 필요합니다.

필수 입력: `private_ip`, `patroni_api_ca_src`, `vault_patroni_api_password`. API 사용자 이름은 `patroni`입니다. 각 DB 노드의 API 인증서에는 `private_ip` IP SAN 외에 `inventory_hostname` DNS SAN이 필요합니다. HAProxy는 사설 IP로 연결하면서 `verifyhost`로 해당 이름과 인증기관을 검사합니다. 이름을 주소로 변환하는 DNS 설정은 이 검사에 필요하지 않습니다.

상태 확인만 `check-ssl`을 통해 8008 포트의 TLS(암호화 전송)를 사용합니다. 실제 데이터베이스 연결에는 `ssl` 옵션을 설정하지 않아 PostgreSQL의 TLS 협상과 인증서를 그대로 전달합니다. 클라이언트는 데이터베이스 인증서에 포함된 서비스 이름으로 서버 인증서를 검증해야 합니다.

인증 헤더가 포함된 설정은 root 및 haproxy 그룹만 읽을 수 있고 변경 내용은 로그에 출력하지 않습니다. 배포 전 `haproxy -c`로 설정을 검사합니다. 리스너 확인은 백엔드 쓰기 성공 검증을 대체하지 않으므로 전체 검증 플레이에서 실제 DB 연결을 추가 확인해야 합니다. 여러 중계 서버의 단일 접속 주소 구성은 외부 부하 분산 장치 또는 별도 네트워크 설계가 필요합니다.

근거: https://docs.haproxy.org/2.8/configuration.html (`check-ssl`, `verifyhost`, `http-check send`)
