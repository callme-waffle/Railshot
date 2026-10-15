# kubeadm 공유 스토리지 운영 계약

첫 `kubeadm-1c2w` 프로파일은 AWS Regional EFS, GCP Regional Filestore, OpenStack의 기존 CephFS를 사용한다. 하나의 공급자·계정/프로젝트·리전 안의 compute 두 대가 서로 다른 AZ에 있어도 같은 PVC 데이터를 사용할 수 있어야 한다. VM 생성, Ceph 신규 구축, 기존 Local Path/EBS/Cinder PVC의 데이터 이전은 이 작업에 포함하지 않는다. 기존 운영 경로·자원은 수정하지 않는다.

## 입력과 실행 책임

`runtime.storage.binding`의 `cluster_id`, `provider`, `region`, `account_id`는 승인된 `target`과 정확히 일치해야 한다. `inventory.workers`는 두 개이며 `id`, `resource_id`, `private_ipv4`, `availability_zone`을 제공한다. 전체 kubeadm 프로파일의 GCP 자원 ID는 `projects/PROJECT/zones/ZONE/instances/NAME`, AWS는 계정·리전이 명시된 canonical EC2 ARN을 사용한다. storage 모듈 단독 adapter는 raw EC2 ID 또는 GCP Compute API self link도 처리하지만 전체 프로파일 입력은 core의 canonical 형식으로 통일한다.

[공급자별 입력 조각](../../infrastructure/ansible/examples/storage/)을 전체 kubeadm 요청의 `runtime.storage`에 결합한다. 예제의 주소·계정·자원 ID는 실행할 환경의 값으로 바꾼다.

- AWS/GCP `mode: create`는 준비된 VM과 별도의 전용 Terraform state에서 공유 백엔드를 생성한다. `mode: reuse`는 API로 조회한 기존 자원의 범위·소유권·준비 상태·접근 설정을 검사하며 자원을 가져오거나 변경하지 않는다.
- `state_dir`은 실행 사용자 소유의 `0700` 영구 디렉터리다. `binding.json`, Terraform state, plan, 관측 결과를 보존한다. VM Terraform state와 공유하지 않는다. AWS/GCP 계정 인증은 실행 사용자 소유의 `0600` `credentials_file`을 참조한다.
- `prepare()`의 Terraform 출력만으로 성공을 기록하지 않는다. AWS EFS 및 GCP Filestore API 관측 후에만 `storage_backend_ready`가 참이다. OpenStack 설정만으로는 백엔드 준비를 증명하지 않는다.
- 고정 `kubeadm-storage.yml`은 control의 `/etc/kubernetes/admin.conf`를 사용하여 CSI를 설치한다. CSI rollout과 같은 PVC의 compute A 쓰기 → A Pod 삭제 확인 → compute B 읽기를 실제로 통과한 뒤에만 `storage_csi_ready`, `storage_pvc_ready`, `storage_ready`를 기록한다.
- 구문 검사·모의 테스트·Helm 렌더링은 준비 상태를 반환하지 않는다. 실제 클라우드와 클러스터 검증은 추후 제공 환경에서 수행한다.

## AWS Regional EFS

입력은 VPC ID, 두 compute의 subnet ID 매핑, CSI credential Secret 이름이다. 생성 모듈은 실제 EC2 주소·subnet·AZ·VPC를 검사하고, 암호화된 Regional EFS와 AZ당 mount target 하나를 생성한다. NFS TCP 2049 ingress는 두 compute의 사설 `/32` 주소로 제한한다. 같은 AZ의 두 compute는 mount target 하나를 공유한다. 재사용 EFS는 `managed-by=railshot`, `railshot-cluster=CLUSTER` 태그와 지정 계정, 암호화·Regional·available 상태, 각 worker AZ의 available mount target 및 동일 제한을 만족해야 한다.

실행기 AWS 인증 파일은 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`와 선택적 `AWS_SESSION_TOKEN`을 갖는 JSON이다. 값은 환경변수로만 해당 하위 프로세스에 전달하며 argv나 템플릿에 기록하지 않는다. 백엔드 생성 권한과 CSI 런타임 권한을 분리한다.

CSI controller credential은 `railshot-storage` namespace의 소유권 라벨 `railshot.io/cluster=CLUSTER`가 있는 Secret을 참조한다. 새 클러스터에서는 `controller_credentials_file`로 control에 사전 배치한 root 소유 `0600` JSON을 참조해 해당 Secret을 생성할 수 있다. 파일 키 이름은 실행기 AWS 인증 파일과 같다. CSI 계정은 EFS 조회와 해당 파일시스템의 access point 생성·조회·삭제 및 드라이버 태그 권한만 허용한다. 앱 PVC마다 access point와 별도 디렉터리를 사용한다. EKS/IRSA를 가정하지 않는다. 키 교체는 별도 운영 절차이며 재실행에서 자동 덮어쓰지 않는다.

[EFS Regional 생성](https://docs.aws.amazon.com/efs/latest/ug/creating-using-create-fs.html), [mount target](https://docs.aws.amazon.com/efs/latest/ug/accessing-fs.html), [EFS CSI 동적 프로비저닝](https://github.com/kubernetes-sigs/aws-efs-csi-driver/tree/master/examples/kubernetes/dynamic_provisioning)

## GCP Regional Filestore

입력은 해당 프로젝트의 사설 network, instance/share 이름, 용량과 생성 모드에서 사용하지 않는 사설 `/26` 범위다. 첫 프로파일은 `REGIONAL`, 1024..10240 GiB, `DIRECT_PEERING`, NFSv3를 사용한다. 리전의 서비스 제공 여부, 용량 quota, reserved range가 다른 subnet·peering·예약 대역과 겹치지 않는지는 실행 환경에서 확인한다. 생성 모듈은 worker의 network/IP/zone을 관측한다. export는 worker `/32` 두 개에 대해서만 `READ_WRITE`와 디렉터리 프로비저닝에 필요한 `NO_ROOT_SQUASH`를 설정한다.

실행기 인증 파일은 지정 프로젝트의 Google 서비스 계정 JSON이다. Terraform ADC와 격리된 gcloud 설정에서 같은 파일을 사용한다. 재사용 인스턴스는 동일 프로젝트·리전·network·share·용량, `managed-by=railshot`, `railshot-cluster=CLUSTER` 라벨, `REGIONAL/READY` 상태, worker 제한 export를 만족해야 한다.

클러스터는 일반 NFS CSI를 사용한다. GKE 전용 Filestore CSI 또는 Workload Identity를 요구하지 않는다. NFS CSI controller와 node는 host network에서 worker IP로 export에 접속한다. worker 간 서비스 network와 Filestore 사설 server 사이의 라우팅·방화벽은 사전 조건이며 실제 PVC 검증으로 확인한다. PV별 디렉터리 이름에 namespace/PVC/PV를 포함한다.

PVC 요청 용량은 NFS 디렉터리에 대한 강제 quota가 아니다. 공유 인스턴스의 총 용량과 사용량을 별도로 관리한다. 공유 백엔드 확장·복구·삭제는 전용 state와 운영 승인 절차로 수행한다.

[Filestore 서비스 계층](https://docs.cloud.google.com/filestore/docs/service-tiers), [IP 주소와 네트워크](https://docs.cloud.google.com/filestore/docs/configuring-ip), [접근 제어](https://docs.cloud.google.com/filestore/docs/access-control), [NFS CSI](https://github.com/kubernetes-csi/csi-driver-nfs)

## OpenStack 기존 CephFS

`mode: reuse`만 지원한다. 사용자 환경에서 제공하는 CephFS FSID, 사설 MON IPv4:3300/6789 목록, filesystem 이름, 전용 subvolume group, provisioner/node Secret 이름을 입력한다. compute와 CSI controller가 MON 및 Ceph MDS/OSD 네트워크에 접근할 수 있어야 한다. 그룹과 CSI 권한은 사용자 Ceph 운영자가 준비한다. 새 Ceph 또는 Manila를 생성하지 않는다.

Secret은 `railshot-storage` namespace에 있으며 같은 클러스터 소유 라벨과 `userID`, `userKey` 키가 필요하다. `secret_files`는 해당 Secret 이름별로 control의 root 소유 `0600` JSON 파일을 참조할 수 있다. 파일이 없으면 기존 Secret을 관측한다. provisioner와 node의 CephX 계정을 분리하고 filesystem/subvolume group 범위로 제한한다. 관리자 키는 요구하지 않는다. Ceph 운영자는 제공한 권한이 요구 작업만 수행하는지 인수 환경에서 검증한다.

Ceph CSI 3.18.1의 지원 Ceph·커널 matrix에 맞는 제공 환경이 필요하다. 파일시스템/권한 검증은 실제 CSI 프로비저닝과 두 compute 마운트 검증으로 확인한다.

[Ceph CSI 지원 범위](https://github.com/ceph/ceph-csi#support-matrix), [CephFS 설치](https://github.com/ceph/ceph-csi/blob/devel/docs/deploy-cephfs.md), [CephFS client 권한](https://docs.ceph.com/en/latest/cephfs/client-auth/)

## 소유권·재실행·데이터 계약

StorageClass 이름은 기존 앱 계약 `railshot-persistent`를 유지한다. default class로 설정하지 않으며 다른 default class가 존재하면 운영자 검토를 요구한다. 기존 class는 클러스터 소유 라벨과 driver/backend parameters, reclaim/binding/expansion/mount 옵션이 일치할 때만 유지한다. 다른 class나 driver를 자동 교체하거나 가져오지 않는다. 앱의 RWO PVC, 단일 replica, Recreate 배포 방식은 유지한다. 공유 스토리지의 RWX 가능성이 앱 다중 writer 허가를 뜻하지 않는다.

앱 PVC reclaim은 `Retain`이다. PVC 삭제가 공유 백엔드나 앱 디렉터리 삭제로 이어지지 않는다. SQLite WAL·NFS/CephFS 잠금·권한·fsync 동작과 앱 데이터 무결성은 실환경 검증 항목이며 코드/렌더 검사만으로 보장하지 않는다.

백엔드 state의 설정 fingerprint가 바뀌면 자동 변경을 멈춘다. Terraform plan에 delete/replace가 있으면 apply하지 않는다. apply 전 `apply-unknown.json`을 기록하고 실제 관측 완료 후에만 삭제한다. 실패·timeout·프로세스 중단 뒤 이 파일이 남으면 state와 실제 자원을 검토해야 한다. 검토 후 attempt marker만 제거하며 state, binding, 파일시스템을 삭제하여 재시도하지 않는다. 자동 rollback 또는 destroy는 없다.

PVC 이동 검증은 `railshot-storage-proof/shared-filesystem-proof`라는 하나의 격리된 1Gi RWO PVC를 반복 사용한다. 이 테스트용 1Gi 요청과 실제 저장한 marker 데이터는 최종 철거까지 유지되며, NFS/EFS에서는 요청 용량이 디렉터리의 강제 quota를 뜻하지 않는다. namespace/PVC의 owner·backend fingerprint, PVC/PV claim UID, driver/backend, ConfigMap의 PVC/PV UID를 검사한다. 실행마다 nonce를 새로 쓰고 각 테스트 Pod의 동일 nonce·노드·PVC·종료 코드를 확인한다. 성공하면 해당 nonce Pod만 삭제하고, 테스트 PVC/PV와 `proof-binding` ConfigMap은 다음 실행에 재사용한다. 재실행마다 EFS access point, NFS 디렉터리, Ceph subvolume이 늘어나지 않는다. 실패한 테스트 Pod는 진단을 위해 남기고 후속 실행을 중지한다. 운영자는 해당 namespace·UID·nonce를 확인해 실패 Pod만 정리한 뒤 재실행한다. 앱 namespace/PVC나 다른 테스트 데이터는 정리 대상이 아니다.

프로파일의 최종 철거에서는 proof PVC/PV의 UID와 CSI `volumeHandle`을 기록하고, 해당 격리 테스트 backing data/access point/subvolume만 공급자별 운영 절차로 정리한다. 애플리케이션 backing data 또는 전체 공유 파일시스템을 삭제하지 않는다.

## 로컬 및 향후 인수 검증

로컬: `PYTHONPATH=infrastructure/ansible python3 -m unittest infrastructure/ansible/test_storage_profile.py`, Ansible syntax/lint, 두 전용 Terraform module의 validate/mock test, `python3 deployment/shared-storage/validate-charts.py --helm /path/to/helm3`로 digest가 일치한 고정 Helm chart의 template 렌더링. chart 다운로드에는 공식 배포 주소의 네트워크 접근이 필요하며 클러스터에는 접속하지 않는다. 신규 CI는 추가하지 않는다.

실환경 인수:

1. 세 노드·공유 backend·credential 범위와 네트워크 선행조건 확인.
2. backend 생성 또는 소유권이 확인된 reuse 실행, CSI rollout 관측.
3. 한 PVC에 compute A가 nonce marker 기록 → A Pod 삭제와 부착 해제 확인 → compute B가 같은 PVC marker 읽기 확인.
4. 동일 요청 replay와 새 요청의 동일 설정 재실행 확인. 새 요청이 같은 proof PVC/PV UID를 재사용하고 새로운 nonce로 검사하는지 확인.
5. 앱 단일 writer 배치·데이터·파일 잠금·권한 확인. worker 중단 및 복구, AZ 사이 접근은 별도 장애 시험.
6. 실제 실행 결과와 실행하지 않은 검사·미검증 항목을 구분해서 기록.
