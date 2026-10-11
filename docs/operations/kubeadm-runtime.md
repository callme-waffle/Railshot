# 준비된 서버의 kubeadm 런타임 자동화

이번 프로파일 `kubeadm-1c2w`는 AWS·GCP·OpenStack 중 한 공급자, 한 계정/프로젝트,
한 리전에 준비된 Ubuntu 22.04/24.04 amd64 서버 세 대를 사용한다.
control-plane 한 대와 compute 두 대만 지원하며 AZ는 서로 달라도 된다.
VM 생성과 관리 호스트 변경, 기존 K3s의 제자리 전환, HA 및 업그레이드는 수행하지 않는다.
기존 단일 VM K3s 프로파일은 명시적으로 그대로 유지된다.

## 구성과 버전

Kubernetes/kubeadm/kubelet/kubectl 1.36.5, containerd 2.3.6, runc 1.3.4,
Cilium 1.20.2, Helm 3.21.4를 고정한다. kube-proxy와 CoreDNS는 kubeadm 기본 addon을
사용한다. control-plane의 `NoSchedule` taint를 유지하고 compute만 앱을 수용한다.

버전 선택 근거는 [Kubernetes 1.36 지원 기간](https://kubernetes.io/releases/1.36/),
[Cilium Kubernetes 호환 목록](https://docs.cilium.io/en/stable/network/kubernetes/requirements/),
[kubeadm Cilium 설치](https://docs.cilium.io/en/stable/installation/k8s-install-kubeadm/),
[container runtime 구성](https://kubernetes.io/docs/setup/production-environment/container-runtimes/)이다.
패키지는 서명된 Kubernetes v1.36 저장소의 정확한 `1.36.5-1.1` 버전으로 설치하고 hold한다.
containerd·runc·Helm은 고정 upstream 배포와 SHA256을 확인한다.
버전 변경은 일반 재실행이 아니라 새 검증과 명시적 업그레이드 작업으로 다룬다.

## 사전 조건과 입력

서버는 systemd, 최소 2 vCPU·2 GB RAM·10 GiB 가용 root 디스크, swap 비활성화,
서로 다른 RFC1918 IPv4 주소, sudo 비대화식 권한과 검증된 SSH host key가 필요하다.
클러스터의 API 6443, 노드 kubelet 10250, Cilium VXLAN 8472/UDP 및 노드 간 통신,
공유 스토리지 연결, 외부 패키지·이미지 다운로드 경로는 운영자가 준비한다.
Ansible 설치기는 방화벽을 임의로 열지 않는다. Pod/Service CIDR는 서로 겹치지 않아야 하며
노드 주소를 포함할 수 없다. 기존 VPC 전체 CIDR와의 중복도 운영자가 사전에 확인한다.

`infrastructure/ansible/examples/kubeadm/{aws,gcp,openstack}.json`은 운영자 명세의 뼈대다.
저장소의 `examples/storage`와 공개 접근 문서에서 공급자별 storage/edge 설정을 완성한다.
`runtime.storage`는 설정 객체이며 `runtime.edge`는 private operator JSON 파일의 절대 경로다.
SSH·cloud·Ceph credential bytes는 공개 HTTP 요청과 preview에 넣지 않는다.

명세의 각 member에는 노드 이름, `control_plane` 또는 `workers` 역할, 실제 AZ,
SSH 참조, 공급자 snapshot 참조를 지정한다.

* AWS descriptor의 EC2 ARN으로 계정·리전을 확인하고 location.zone과 AZ를 비교한다.
* GCP descriptor의 project/zone/instance 경로와 IAP 참조를 비교하며 region에는 zone의 리전을 쓴다.
* OpenStack Controller GET ServerResponse의 ACTIVE·project·management IP·실제 AZ·region을 비교한다.
  Controller의 `region_name` 설정이 필요하다. 이전 snapshot에 AZ/region이 없으면 새 응답을 수집해야 한다.
  Nova가 AZ를 제공하지 않는 환경은 이 프로파일에서 추정값으로 대체하지 않는다.

Snapshot은 운영자가 적절한 프로젝트 권한으로 수집한 자원 바인딩이며 독립적인 실시간
cloud 소유권 조회가 아니다. 실제 guest IP·아키텍처·노드 이름은 SSH/클러스터 검사로 다시 확인한다.
명세·snapshot·key 파일은 실행자 소유의 private 파일로 두고 Git에 저장하지 않는다.

## 실행 경로

```sh
python3 infrastructure/ansible/run.py --cluster-file /etc/railshot/operator/cluster.json \
  --request-id prepared.aws.001 --validate-only
python3 infrastructure/ansible/run.py --cluster-file /etc/railshot/operator/cluster.json \
  --request-id prepared.aws.001 --state-dir /var/lib/railshot/operator/ansible
```

validate-only는 계약/참조 설정을 검사하며 SSH·cloud apply와 준비 상태 기록은 하지 않는다.
실제 설치는 모든 guest를 먼저 검사하고 containerd·Kubernetes 준비, control 초기화,
두 compute 가입, Cilium/CoreDNS·네트워크 검증, 공유 backend·CSI·PVC 이동 검증,
LB/DNS/인증서 연결 순서로 진행한다. 세 노드의 SSM/IAP 터널은 addon 완료까지 각각 유지하고
종료 시 각각 정리한다. 온프레미스는 등록된 SSH/relay 경로를 사용한다.

기존 localhost API의 운영자 registry에서는 다음처럼 cluster를 선택한다.

```json
{
  "version": 1,
  "targets": {
    "prepared-aws": {
      "profile": "kubeadm-1c2w",
      "cluster_file": "/etc/railshot/operator/cluster.json",
      "purpose": "runtime"
    }
  }
}
```

공개 job 입력은 기존 `runtime.install`, target_id, request_id와 wait_timeout_seconds만 받는다.
HTTP caller는 노드·playbook·storage·edge·credential 경로를 바꿀 수 없다.
`guest.check` 단독은 새 cluster 프로파일에 제공하지 않는다. 준비 검사는 전체 runtime 실행에 포함된다.
기존 environment registration은 이 registry와 admin.conf 기반 kubectl을 사용할 수 있다.
새 프로파일의 registration에는 `settings.edge_config_file`을 지정하지 않는다.
이 필드는 control IP를 단일 backend로 보내는 기존 edge allocator를 선택하므로 mutation 전에 거부한다.
등록할 앱의 NodePort, 공개 HTTPS hostname과 health path는 설치 명세의 고정 초기 route와 정확히 일치해야 한다.
명세의 path prefix 밖으로 route를 확장하거나 동적 앱 route를 추가하는 작업은 이번 범위에 포함되지 않는다.
고정 route의 실제 입력 hash는 registration identity에도 반영한다.

## 결과와 재실행

`guest_ready`는 세 guest 모두 통과했을 때, `runtime_ready`는 요청된 정확히 세 노드의
이름·IP·역할·amd64·버전·Ready 및 Cilium/CoreDNS/kube-proxy rollout과 실제 네트워크 probe가
통과했을 때만 기록한다. probe는 두 compute 사이 Pod·Service 통신, CoreDNS 조회,
NetworkPolicy 허용·차단을 확인하고 UID precondition으로 전용 namespace를 정리한다.

`storage_backend_ready`와 `storage_csi_ready`, `storage_pvc_ready`를 구분한다.
`storage_ready`는 compute A에서 쓴 동일 PVC 데이터를 compute B에서 읽었을 때만 참이다.
OpenStack Ceph는 실행 전 cloud 조회 대신 이 원격 검증에서 backend 상태를 확정한다.
`edge_ready`는 공개 접근 자원의 설정 완료다. 앱을 배포하거나 검증하지 않으면
`application_ready` 및 `public_http_verified`는 false로 남는다.
로컬 모의 테스트 통과는 실제 환경의 준비 상태 증거가 아니다.

동일 request ID와 동일 입력은 저장된 결과를 재생하고 원격 작업을 반복하지 않는다.
다른 입력에 같은 ID를 재사용하면 거부한다. 새 ID의 동일 설정 재실행은 소유권 marker,
구성·버전·멤버와 실제 kubeadm 설정·cluster UID·Cilium·containerd를 확인한 뒤 진행한다.
Ansible가 정상 종료한 부분 실패는 이미 소유한 일치하는 구성으로 새 ID를 통해 재실행할 수 있다.
서버 초기화 도중 pki만 남은 상태와 다른 버전/네트워크/멤버 변경은 자동으로 복구하지 않는다.
실행 중단·timeout·손실된 readiness receipt·cloud 변경 응답 손실은 outcome_unknown으로 남겨
같은 자원이나 alias 대상의 후속 요청을 차단한다. 운영자가 실제 자원과 private state를 점검하고
명시적으로 조정한 뒤 다시 시작해야 한다. private 기록을 삭제하는 것으로 안전을 증명할 수 없다.

join.yml은 worker의 always 처리로 삭제하고 token은 정상 종료·실패 정리 시 삭제한다.
강제 중단으로 정리가 실행되지 못하면 30분 TTL이 만료되며 해당 요청은 unknown으로 유지한다.
공유 데이터·PVC·backend 삭제는 일반 설치 재실행의 동작에 포함되지 않는다.

## 로컬 검증과 향후 인수

이번 작업에서는 구문/lint, 입력 바인딩과 실패·재생·터널 종료 계약, 템플릿·provider 모의
검사를 수행한다. 실제 서버 설치, AZ 분산 데이터 이동, provider LB health·DNS·인증서·HTTPS,
worker 중단 후 앱 복구 및 공유 파일시스템에서 SQLite 사용의 호환성은 향후 제공되는 환경에서
검증한다. HA backup/restore, 무중단 업그레이드, 여러 topology는 첫 버전의 완료 조건에 포함되지 않는다.
신규 CI 도입은 보류한다.
