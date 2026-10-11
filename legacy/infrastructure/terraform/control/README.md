# 기존 운영 노드 Terraform 복원

원본 Jasmin commit `2b4b462`의 `infra/terraform/control/main.tf`와 provider lock을 재사용해 **기존** `railshot-control-poc`를 관리한다. 유저 AWS/GCP 앱 노드나 격리된 CI builder를 생성·대체하는 모듈이 아니다. 기존 resource 주소와 IAM role/profile/policy, 기본 보안 그룹을 유지한다.

현재 복원 대상은 계정 `721622471953`, region `ap-northeast-2`, instance `i-033ae2db907fde68e`, primary ENI `eni-09a70375ba8bfc234`다. 이미지와 subnet은 원본 state에 기록된 정확한 값을 private inputs로 전달한다. 새 default subnet을 선택하거나 AMI를 자동 갱신하지 않는다. `prevent_destroy`로 운영 인스턴스 교체·삭제를 차단한다.

기존 cloud-init은 옛 Ansible/accounts 경로를 참조한다. 이 복원에서는 그 파일을 복제하거나 user-data를 재계산하지 않는다. `ignore_changes = [user_data, user_data_base64]`로 기존 값을 보존하고, 현재 운영 K3s/Argo 설정은 별도 관리 작업으로 수행한다. 다른 기존 resource drift를 자동으로 승인하지 않는다.

기존 `AmazonSSMManagedInstanceCore` 및 Codex 인증 Parameter 조회를 유지한다. 추가한 registry 연결은 운영 역할에 `/railshot/registry/ghcr/pull_token` 한 경로의 조회만 허용하고, 다른 Parameter 조회와 경로·이력 조회는 거부한다. `registry.tf`의 GitHub OIDC 역할은 railshot-apps의 고유 조직·저장소 ID와 `railshot-release` 환경에 결합되어 이 경로의 `PutParameter`만 허용한다. 토큰 값은 Terraform 입력이나 state에 넣지 않고 전용 workflow가 SecureString으로 전달한다.

`enable_product_executor`는 기본 `false`다. 관리자 bootstrap에서 명시적으로 켜면 기존 control role에 고정 `product-executor-policy.json`을 `railshot-product-executor` inline policy로 추가하고 이 control 인스턴스의 IMDSv2 hop limit만 1에서 `product_metadata_hop_limit`(기본 2)로 변경한다. 실측한 추가 라우팅 홉이 있으면 3까지 허용하며, opt-in을 끄면 입력값과 관계없이 1을 유지한다. role/profile/trust 및 기존 SSM 정책은 보존한다. 정책은 계정·리전·VPC·AMI·subnet·고객 instance profile을 고정하고, 새 `ProjectOwner=railshot-product`/`Target` 생성 태그를 가진 리소스의 provisioning과 고정 SSM 명령·세션, Pricing/CE 읽기만 허용한다. 기존 리소스에 소유 태그를 붙여 권한을 얻거나 IAM/IMDS 설정을 변경할 권한은 제품 API에 없다. 검토된 문서는 AWS ValidatePolicy findings 0개와 합성 resource/context에 대한 IAM simulation 57개를 통과했으며, 실제 신규 인스턴스 생성 성공이나 정책 적용을 입증하는 결과는 아니다. 이 JSON은 compact 7,686 bytes로 단일 managed policy 한도를 넘지만 현재 inline 정책과 합친 8,316 bytes는 role inline aggregate 한도 이내다. 추가 권한은 같은 inline 문서에 임의로 합치지 않는다.

이 opt-in을 적용하기 전에 bootstrap 운영자가 `deployment/manifests/product-metadata.yaml`의 정확한 CCNP UID·`specs`·Cilium 적용 상태 및 대표 Pod의 실제 metadata 정책 거부를 검증해야 한다. 그 다음 control saved plan을 적용하고 API의 IMDSv2/STS role identity 및 나머지 Pod의 거부를 다시 확인한다. hop limit 1에서의 단순 timeout은 Cilium 거부 증거가 아니다. 정책은 Argo 전체, `railshot-system`에서 API app과 `railshot-product` SA가 **동시에** 일치하지 않는 Pod, CoreDNS/local-path provisioner의 metadata IPv4/IPv6만 차단한다. metadata 이외 통신의 default-deny를 새로 켜지 않으며 build runner/customer namespace는 선택하지 않는다. host root와 hostNetwork Cilium 구성 요소는 신뢰하는 운영 영역으로 남는다. CCNP는 관리자 bootstrap 소유로 유지하며 Argo AppProject의 cluster 권한을 넓히지 않는다. API는 개인 AWS 자격이나 정적 Secret 대신 native IMDS credential 갱신 경로를 사용한다. 2026-10-03 control에 정책을 적용하고 실제 API IMDSv2/STS 역할과 대표 4개 Pod의 Cilium policy drop을 검증했다. 이 노드에서는 hop 2 응답이 ENI에 도달했지만 Pod에 도착하지 않았고, hop 3에서 인증이 성공했다. credential 만료 후 갱신과 신규 앱 E2E는 별도 검증 대상이다.

같은 opt-in의 edge 권한은 별도 customer-managed `railshot-product-edge` policy와 attachment로 선언한다. `product-edge-policy.json`은 compact **5,557 chars**로 단일 managed policy 한도 6,144 이내다. 2026-10-03에 SHA-256 `fb401be9f21ee946592cd56f1e2991359df0d60785bca03ddf36decb7a118dfb`인 이 문서의 AWS ValidatePolicy findings 0개를 확인했다. 이는 정책 적용이나 실제 삭제 성공의 증거가 아니다. 기존 control Terraform의 `file("${path.module}/product-edge-policy.json")`가 이 소스를 읽으므로, 검토된 control 정책 배포에서 해당 managed-policy version과 attachment의 실제 상태를 별도로 확인해야 한다. 제품 API에는 IAM policy 생성·attachment 권한을 주지 않는다.

앱 stop/delete를 위해 정확한 HTTPS listener 아래의 **child rule**에만 DeleteRule을 허용하고, 같은 account/region의 `rsapp-*` target group에 DeleteTargetGroup/DeregisterTargets를 허용한다. 기존 4개 bootstrap target group에는 이 두 작업과 RegisterTargets/ModifyTargetGroupAttributes를 명시적으로 거부한다. 공유 LB·listener·VM·VPC·SG·hosted zone 삭제 권한은 추가하지 않는다. SG 규칙 추가/제거는 기존 ALB SG 및 등록 target SG의 정확한 ID와 VPC로 제한하며, 새 target SG는 기존 ProjectOwner/Target 태그와 VPC 조건을 모두 만족해야 한다. Route53은 기존 zone과 생성된 12자리 suffix 이름 패턴의 A record에 CREATE/DELETE만 허용한다. UPSERT는 허용하지 않는다. 기존 CreateRoute는 등록 route table의 새 route 생성 권한으로 유지한다.

IAM은 SG 포트/CIDR·target IP·listener hostname/priority까지 제한하지 못한다. 따라서 `gitops/application_cleanup.py`의 앱 소유권, 정확한 리소스 ID, Terraform lineage, saved-plan/CAS 검증과 삭제 후 조회가 권한 정책과 함께 적용되어야 한다. 조건 키와 ARN 형식은 [ELBv2 권한 표](https://docs.aws.amazon.com/service-authorization/latest/reference/list_elbv2.html), [EC2 권한 표](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html), [Route53 조건 문서](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/specifying-conditions-route53.html)를 기준으로 한다.

GCP 권한은 이 AWS 정책에서 변경하지 않는다. 실제 API executor가 사용하는 GCP principal·project와 OAuth access scope를 확인하고, 그 identity의 effective IAM 권한으로 앱 NEG/endpoint·backend·health check·인증서/DNS authorization/map entry의 삭제/재생성, 공유 URL map·방화벽의 제한된 갱신, 삭제 후 list/readback이 가능한지 운영자가 검증해야 한다. 저장된 역할 이름이나 로컬 mock 테스트만으로 실제 권한을 추정하지 않는다. 이 변경은 새로운 GCP 자격 증명, 역할 바인딩 또는 임의의 operator config를 만들지 않는다.

## State와 기대 plan

원본 snapshot:

```text
/Users/mango/workspace/softbank2026 hackerthon/Jasmin/infra/terraform/control/terraform.tfstate
```

새 authoritative state:

```text
/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/terraform.tfstate
```

원본 serial 7, lineage `56131c6f-03c0-a9ab-5fe1-ba591b8bf7f8`를 0600으로 복사했다. 원본은 보존용 snapshot이며 이후 apply는 **새 경로 한 곳**에서만 수행한다. 원본과 복사본에 각각 apply하면 동일 자원을 두 state가 관리하게 되므로 원본 경로를 실행 대상으로 사용하지 않는다. Private 디렉터리 권한은 0700, state·tfvars·plan·로그 권한은 0600이다.

최초 복원 plan은 다음 두 변경으로 적용했다. 이후 `connectivity.tf`는 운영 노드에서 유저 AWS 노드의 6443 접근, `registry.tf`는 위의 제한된 OIDC 전달 권한을 추가했다. 각 단계는 새 saved plan으로 별도 검토했다.

1. `aws_security_group.edge` 한 개 생성. 인라인 ingress/egress 없이 만들고 규칙은 별도 `aws-edge` 모듈이 소유한다.
2. `aws_instance.control`을 제자리에서 수정. `source_dest_check: true → false`, 보안 그룹 목록에 위 edge SG 추가.

최초 복원 시 기존 base SG `sg-01a71e8be9a585b5a`의 ingress/egress·설명, AMI, user-data, instance profile을 유지했다. IAM 변경은 위의 registry 경로와 전용 OIDC 역할에 한정한다. 새 SG 출력만 `aws-edge.wireguard_security_group_id`로 넘긴다. Base SG를 edge 입력으로 사용하면 inline/외부 rule 소유권이 충돌한다.

## 전용 build agent 연결

선택적 `build_worker_security_group_id`는 검토한 build SG만 허용한다. 기본값 null은 기존 정책이다. 값을 지정하면 control ingress에 TCP6443과 Cilium UDP8472/TCP4240/ICMP echo(type8/code0), control egress에 같은 Cilium 3개 경로를 추가한다. CI 모듈의 `control_security_group_id`가 반대편 ingress/egress를 선언한다. 기존 SG 설명과 resource 주소는 보존하며 인터넷 ingress, SSH, etcd, kubelet 포트는 열지 않는다. Edge SG와 기존 고객 API 규칙은 별도 소유 그대로다.

2026-10-02 승인된 가입 작업은 control base SG `sg-01a71e8be9a585b5a`와 build SG `sg-06bee6c9cb9c1bb74` 사이 14개 규칙을 EC2 API로 추가하고 rule ID와 전후 snapshot을 private `product-release-20261002/build-readiness`에 기록했다. 기존 inline Terraform 선언으로 apply하면 이 규칙이 삭제될 수 있으므로 두 모듈의 peer 입력과 최신 선언으로 saved plan을 검토해야 한다. 현재 작업에서 Terraform apply는 수행하지 않았다. CI 인스턴스의 public-IP replacement drift를 해결하기 전 전체 apply는 금지하며, 기존 volume과 2026-10-05 14:59 UTC STOP 기한을 보존한다.

## 관리자 명령

저장소 root에서 아래처럼 정확한 backend 경로와 private tfvars를 지정한다. 파일들은 복원 작업으로 작성되며 예산·권한·plan 검토는 호출자 책임이다. 이 모듈은 공통 app-host executor의 budget reservation 경로에 포함되지 않는다.

```sh
TF_DATA_DIR=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/.terraform \
terraform -chdir=infrastructure/terraform/control init -input=false -lockfile=readonly \
  -backend-config=path=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/terraform.tfstate

TF_DATA_DIR=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/.terraform \
terraform -chdir=infrastructure/terraform/control plan -input=false \
  -var-file=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/inputs.tfvars.json \
  -out=/Users/mango/.local/share/railshot/cloud-e2e-20261002/control-terraform/reviewed.tfplan
```

`review-summary.json`에는 변경 resource·action·필드 이름만, `reviewed-plan.json`과 native 로그에는 상세 검토 자료를 비공개로 보존한다. 기대 범위를 벗어난 변경이나 replacement가 있으면 apply하지 않고 먼저 원인을 확인한다. 검토한 saved plan의 apply는 root 운영 작업자가 별도로 수행한다. 2026-10-02 복원 및 제한된 후속 연결 apply는 완료했으며, 새 변경에도 plan 검토가 필요하다.

출력은 기존 `instance_id`, `auth_parameter_name`, `connect`와 추가된 `private_ip`, `primary_network_interface_id`, `edge_security_group_id`, `source_dest_check`다. 이 값은 WireGuard handshake, route, Kubernetes/Argo 또는 외부 앱 준비 완료 증거가 아니다.

## 기존 앱 노드의 실행 권한

`registered_runtime_instance_ids`는 앱 등록에 인계된 기존 runtime EC2 ID 목록이다. 기본값은 빈 목록이며 `enable_product_executor=true`일 때만 이 목록에 SSM StartSession 권한을 부여한다. 기존 노드에 `ProjectOwner` 태그를 덧씌워 신규 생성 자원으로 취급하지 않는다. Session document 권한은 기존 고정 port-forwarding 문서 정책을 재사용하며 SSH host key와 전용 사용자 검증을 유지한다.

2026-10-03 운영 점검에서 API의 IMDSv2 자격 조회가 실패했고 실행자 opt-in이 적용되지 않았음을 확인했다. metadata 사전 검사에서는 `crictl inspectp` 옵션을 Pod ID 앞에 전달해야 했다. 수정 뒤 Argo·dashboard·CoreDNS·local-path의 실제 Cilium policy drop을 확인했다. 이 사전 검사만으로 IAM 활성화 또는 앱 E2E 완료를 주장하지 않는다.

2026-10-03 실제 API 역할로 SSM 연결과 AWS edge 무변경 plan을 확인했다. SSM document 조건은 AWS 공식 예시의 `BoolIfExists`를 사용한다. `Bool`은 EC2 resource 평가에 키가 없는 요청을 거부했다. AWS provider가 사용하는 `DescribeListenerAttributes` 읽기도 추가했다. 변경 정책은 Access Analyzer findings 0, 권한 허용·거부 시뮬레이션 12개를 통과했다. 기존 VM·문서·리전 범위는 그대로다.
