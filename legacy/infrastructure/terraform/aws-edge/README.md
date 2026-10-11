# AWS 공유 앱 진입점

`Route53 Alias → 공유 ALB HTTPS443 → AWS 고객 노드 사설 IP:NodePort`를 구성한다. AWS K3s 한 노드를 여러 앱이 재사용하며 앱마다 namespace/Application과 NodePort를 별도로 할당한다. 이 모듈은 등록된 NodePort·호스트와 전용 target SG를 연결한다. WireGuard 설정 생성·GCP 경로 등록은 제공하지 않는다. 과거 `wireguard_*` 변수는 nonempty 값을 거부하는 폐기 입력 검사만 남으며 새 설치의 선택 항목이 아니다. GCP는 native L7 진입점, 온프레는 [Named Tunnel → 사설 Octavia HTTPS](../../../deployment/cloudflared/README.md)를 별도로 준비한다.

[gitops/edge.py](../../../gitops/edge.py)는 환경 등록에서 할당한 **AWS** 앱 route를 기존 routes에 추가하고 saved plan을 검사·적용한다. [실행·인수 계약](../../../gitops/README.md#신규-앱-주소와-공유-edge-연결)을 따르며 적용만으로 공개 HTTP 성공을 주장하지 않는다. Terraform **1.7 이상**이 필요하며 CI는 **1.7.5**로 검증한다. 기존 운영 state에는 아래 이전 절차를 먼저 적용한다.

## 입력 예시

아래 IP/ID는 형식 예제다. 실제 노드 descriptor, 서비스 NodePort와 관리자 조회 결과로 바꾼다. `base_domain`은 구매·위임이 끝난 도메인으로 지정하며, 이 모듈이 도메인 등록을 수행하지는 않는다.

```hcl
name       = "railshot-edge"
account_id = "123456789012"
region     = "ap-northeast-2"
vpc_id     = "vpc-0123456789abcdef0"
public_subnet_ids = ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]
zone_id        = null # 새 public zone 생성; 기존 zone이면 실제 Z... ID
base_domain    = "railshot.io"
certificate_arn = null # 기존 regional ACM ARN도 가능
http_redirect   = true

routes = {
  demo-a1b2c3 = {
    host                     = "demo-a1b2c3.railshot.io"
    provider_kind            = "aws"
    target_private_ip        = "172.31.10.20"
    node_port                = 30080
    health_path              = "/healthz"
    priority                 = 100
    target_security_group_id = "sg-0123456789abcdef1"
  }
}
```

앱 key·host는 서비스명+안정적인 hash 등 호출자가 정한 값을 사용한다. `routes`는 1–50개를 받고 host와 listener priority는 중복될 수 없다. 한 고객 노드에 앱을 더 배포할 때 새 VM 대신 별도 NodePort와 route entry를 추가한다. 같은 IP/포트의 별칭을 등록해도 AWS SG 규칙을 중복 생성하지 않는다.

ALB는 서로 다른 두 AZ의 지정 VPC subnet을 확인한다. 운영자는 각 subnet의 실제 IGW 경로와 유효 route table을 확인한다. HTTPS 기본 응답은 404이며 등록된 host만 전달한다. `http_redirect`가 true일 때만 80을 열어 HTTPS로 redirect한다. `web_client_cidrs` 기본값은 공개 웹 `0.0.0.0/0`이며 관리 API 공개 여부를 대신 승인하지 않는다.

`zone_id`가 null이거나 생략되면 `base_domain`의 **public Route53 zone**을 만든다. 기존 ID를 넣으면 새 zone을 만들지 않고 같은 이름의 public zone인지 확인해 재사용한다. 신규 생성 전 같은 도메인의 zone이 이미 있는지 관리자가 확인한다. 관리하는 zone에 `prevent_destroy`를 적용했으므로 생성 후에도 입력을 null로 유지한다. 출력 ID를 다시 입력에 넣으면 관리 대상 제거가 되므로 그렇게 전환하지 않는다.

새 도메인은 private backend/입력을 준비한 후 먼저 `terraform plan -target=aws_route53_zone.app -out=<private-zone-plan>`으로 zone만 계획하고, 그 saved plan을 검토·적용한다. 출력 `zone_id`와 `name_servers`를 확인해 registrar에서 정확한 NS로 위임한다. 그 다음 **target 옵션 없이 전체 plan을 새로 만들고** 검토·적용해 ACM/ALB를 구성한다. 부분 apply는 NS 위임을 준비하는 단계이며 edge 전체 적용이나 앱 공개 완료가 아니다.

`certificate_arn`이 null이면 `*.base_domain` ACM 인증서, DNS 검증 record와 검증 대기를 만든다. Route host는 기본적으로 base domain 바로 아래 한 label만 허용한다. `railshot.io` 같은 apex route는 같은 region/account에서 발급·DNS 검증을 마친 `apex_certificate_arn`을 명시해야 한다. 모듈은 기존 wildcard/default 인증서를 교체하지 않고 추가 SNI 인증서만 연결한다. Apex 인증서와 DNS 검증 receipt는 운영자가 보관하고, 기존 검증 CNAME을 재사용할 때 중복 Terraform 소유자를 만들지 않는다. 기존 ARN을 쓰면 같은 region/account와 모든 host coverage를 운영자가 검증한다. 도메인의 실제 NS 위임이 끝나지 않으면 ACM DNS 검증이 완료되지 않는다. DNS 등록과 TLS 설정은 앱 준비 완료 증거가 아니다.

## 보안 그룹

ALB SG와 inline ingress/egress는 이 모듈만 소유한다. egress는 각 target /32와 NodePort에 제한한다. AWS 고객 노드에는 별도 **규칙 전용 SG**를 붙이고 그 ID를 route에 넣는다. 이 모듈은 ALB SG에서 해당 NodePort로 들어오는 규칙만 추가한다. 해당 SG에 다른 writer의 inline ingress를 섞지 않는다. 고객 호스트 기본 SG와 직접 공개 HTTP를 대체하는 별도 그룹이다. 공개 IP, metadata IP, IPv6 및 RFC6598 target은 거부한다.

## 기존 WireGuard 경로의 이전

**코드에서 신규 생성을 제거한 상태와 운영 WireGuard 종료는 별개다.** 기존 GCP 앱 트래픽과 Argo의 Kubernetes API 접근(TCP 6443)이 WireGuard에 의존한다면 먼저 대체 경로를 완성한다. 이 변경은 native GCP L7, Cloudflare 터널, DNS 전환, 운영 설정 변경 또는 WireGuard 종료를 실행하지 않는다. 기존 경로와 새 경로를 병행 검증한다.

1. 기존 단일 Terraform writer를 멈추고 backend/state의 lineage·serial, private tfvars, 적용한 Git commit, 자원 ID와 route key를 비공개 인수 자료로 보관한다. AWS ALB·listener·인증서·zone·AWS route key는 그대로 유지한다. 새 state로 초기화하거나 기존 자원을 재생성하지 않는다.
2. GCP native L7에서 같은 앱·revision의 HTTPS와 health를 검증한다. 운영 Argo가 사용하는 GCP API 6443의 별도 관리 경로와 인증서·권한·sync/observe도 검증한다. Named Tunnel → Octavia는 온프레 앱의 공개 경로이며 이 관리 경로를 자동으로 대체하지 않는다. 두 조건이 모두 확인되기 전에는 WireGuard 서비스를 멈추거나 방화벽/route/EIP를 제거하지 않는다.
3. 각 GCP hostname의 DNS 소유권을 새 경로 담당에게 명시적으로 인계하고 트래픽을 전환한다. 기존 `aws_route53_record.app["<gcp-route-key>"]`가 같은 이름의 새 record를 삭제하지 않도록 **state에서 해당 DNS record만 비파괴로 인계**하고 새 소유자의 import/readback을 확인한다. 정확한 주소 목록을 검토하고, state 백업 후 `terraform state rm -dry-run '<exact-record-address>'`로 대상을 확인한 뒤 별도 승인된 state 인계를 실행한다. 광범위한 module/state 삭제는 사용하지 않는다. rollback용 이전 state는 보관하되 두 writer를 동시에 실행하지 않는다.
4. 외부 HTTPS와 Argo 관리 경로 전환이 확인되면 private tfvars에서 기존 GCP route와 모든 `wireguard_*` 입력을 제거한다. 영속 allocation에 GCP row가 있으면 해당 `applied` 예약도 소유자 기록을 남기고 별도 인계한다. Terraform `routes`는 AWS만 허용하고, GitOps `prepare/plan/apply`는 GCP route나 WireGuard 입력이 남은 base 설정을 거부한다. 기존 설정의 읽기 전용 관측은 가능하다.
5. 같은 backend에서 Terraform 1.7.5로 전체 saved plan을 만든다. `removed` 블록은 아래 자원의 모든 기존 instance에 **`forget`(state 소유권만 해제)**을 계획하며 실제 자원을 파괴하지 않는다. AWS ALB·listener·인증서·zone·AWS route에 destroy/replace가 없어야 한다. 제거 대상 GCP target group/attachment/host rule만 별도로 검토한다. 이 이전 계획은 신규 앱 실행기의 허용 범위 밖이므로 `gitops/edge.py`가 `forget`·삭제를 차단한다. 운영자가 별도로 검토한 계획으로 인계한다.
6. state에서 해제한 객체는 여전히 운영 중일 수 있다. 자원 ID·담당자·만료/정리 조건을 남기고, 다른 소비자가 없는지 확인한 뒤 별도 정리 변경으로 실제 객체를 제거한다. EIP가 운영 노드의 다른 접속 경로로도 쓰이면 유지한다. 그 후에만 guest 서비스/키, GCP 방화벽, AWS forwarding/UDP 규칙과 route를 정리하고 필요 시 원래 instance 소유 모듈에서 `source_dest_check`를 복원한다. 서비스 정지·규칙 삭제·EIP 해제의 각각의 readback이 있어야 종료 완료로 기록한다.

`removed { lifecycle { destroy = false } }`로 보존하는 기존 주소:

- `aws_eip.wireguard`, `aws_eip_association.wireguard`
- `aws_security_group_rule.wireguard_in`, `aws_security_group_rule.wireguard_out`
- `aws_security_group_rule.forward_from_alb`, `aws_route.gcp`

이 선언은 신규 state에 WireGuard 자원을 만들지 않는다. 기존 자원은 명시적인 별도 정리 전까지 **운영 종료 미완료**이며, state에서 사라졌다는 사실만으로 해제·비용 종료를 주장하지 않는다. [Terraform removed 블록](https://developer.hashicorp.com/terraform/language/block/removed)의 `destroy=false` 의미를 따른다.

## 검증과 출력

```sh
python3 -m unittest discover -s infrastructure/terraform/aws-edge -p 'test_*.py'
terraform -chdir=infrastructure/terraform/aws-edge fmt -check
```

오프라인 테스트는 임시 source-only Terraform console로 AWS 전용 변수 제한과 여러 앱의 SG 중복 제거를 확인한다. 실제 provider 검증은 별도 임시 복사본에서 locked provider로 `init -backend=false -lockfile=readonly`, `validate`를 사용한다. 이 검사들은 클라우드 plan/apply, DNS 위임, 인증서 발급 또는 앱 실행을 증명하지 않는다.

출력은 `zone_id`, `name_servers`, `app_urls`, `target_group_arns`, `alb_dns_name`, `alb_security_group_id`, `certificate_arn`이다. `readiness`는 계속 `configured-references-only; runtime and public HTTP unverified`다. 실제 인수는 각 target health, DNS/TLS/host routing, NodePort health path와 외부 HTTPS를 각각 확인한다. ALB health check는 target IP와 포트를 Host로 사용하므로 지정 경로가 기본 virtual host에서도 응답해야 한다. 공유 ALB의 두 AZ가 각 단일 고객 노드의 HA를 보장하지는 않는다.

근거: [ALB IP target 제약](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-target-groups.html), [Terraform IP target attachment](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb_target_group_attachment), [Route53 Alias](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-to-elb-load-balancer.html).
