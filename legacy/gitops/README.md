# CI → Argo CD 인계

`handoff.py`는 검증된 CI 게시 결과로 검토용 선언을 만들고, `argo.py`는 운영자가 등록한 Argo CD에서 그 선언의 sync와 결과를 확인합니다. `bridge.py`는 이 두 구현을 제품 백엔드에서 호출하는 비공개 실행 진입점입니다. 팀원 runtime bootstrap으로 준비한 AWS/GCP single-node K3s를 재사용하며, 클러스터 설치나 CI runner 등록을 수행하지 않습니다. 사용자 소스 저장소와 Argo가 읽는 배포 선언 저장소는 별개입니다. Argo Application은 config 저장소의 앱별 경로를 읽으므로 고객마다 원본 소스 저장소를 새로 등록할 필요는 없습니다.

```sh
python gitops/handoff.py /private/published /private/target.json /private/handoff-review
```

입력은 신뢰된 GitHub CI artifact 채널에서 받은 `images.json`, `railshot.yaml`, `verdict.json`, `manifest.json`, `handoff.json`입니다. `handoff.json`은 **version 2**여야 하며 run/producer attempt/bundle artifact ID와 `registry` 접근 검증 결과를 출력 receipt에 유지합니다. registry 결과의 `images_sha256`은 `images.json` 해시와 같아야 합니다. v1은 private pull 계약을 표현하지 못하므로 거부합니다. 게시 artifact 자체의 ID는 receipt 본문에 없으며 API가 GitHub 응답에서 별도로 검증합니다. 이 CLI는 GitHub 조회를 수행하지 않으므로 신뢰된 게시 채널에서 확인한 파일만 전달해야 합니다. 파일 해시는 무결성 검사이며 서명이 아닙니다. 임의 업로드 artifact를 신뢰하지 않습니다. 원래 이미지 tar는 이 선언 생성 단계에서 재빌드하거나 실행하지 않습니다.

target JSON은 운영자가 제공합니다. 필수 필드는 `id`, `namespace`, `argocd_namespace`, 제한된 AppProject `project`, `architecture: amd64`, `repo_url`, Kubernetes API `cluster_server`, config repo `path`, 그 경로를 검토한 Git commit SHA `revision`, 할당한 `node_port`, 실제 라우팅 원본 `ingress_cidrs`, CPU(m)/memory(Mi) requests·limits `resources`입니다. 예시는 `test_handoff.py`의 target을 참고하세요. CI S/M/L을 운영 리소스 값으로 임의 변환하지 않습니다.

Private 이미지는 target에 `"image_pull_secret": {"namespace": "tenant-demo", "name": "ghcr-pull"}`을 추가합니다. namespace/name은 게시 receipt의 참조와 정확히 같고 namespace는 `target.namespace`와 같아야 합니다. 그러면 Deployment에 `imagePullSecrets: [{name: ghcr-pull}]`을 생성합니다. 이 참조는 설치할 Secret의 위치와 이름이며 Secret 존재나 pull 성공의 증거가 아닙니다. 실제 `kubernetes.io/dockerconfigjson` Secret 설치와 자격 갱신은 CD 담당자가 수행합니다. Secret bytes, registry token, Docker 인증 설정, kubeconfig는 입력 artifact나 생성 파일에 넣지 않습니다.

생성 파일은 Deployment/Service/NetworkPolicy를 묶은 `workload.json`, 별도 `application.json`, `receipt.json`입니다. receipt의 `documents`는 두 선언의 canonical JSON hash를 보존합니다. **workload.json만** 지정한 config repo path에 넣고 commit한 뒤 그 SHA로 target.revision을 갱신해 Application을 다시 생성합니다. Application과 receipt는 workload 디렉터리 밖에 둡니다. Application 이름은 target·namespace·app을 결합하므로 AWS/GCP에 같은 앱을 배포해도 충돌하지 않습니다. 자동 sync·prune·force·namespace 생성은 켜지 않습니다.

앱 하나의 지원 범위는 단일 HTTP 서비스와 선택적 PostgreSQL 연결, 명시한 route·health endpoint, immutable image, linux/amd64입니다. `/health` 같은 절대 경로는 허용하며 prefix를 제거하거나 다시 쓰지 않습니다. `http` receipt의 `route`, `health_path`, `container_port`, `node_port`를 edge 연결에 사용합니다. ALB health check는 `health_path`를 그대로 사용하고 앱 요청 경로도 그대로 전달합니다. query·fragment·percent escaping·상위 경로 이동은 이 초기 계약에서 차단합니다. 임의 앱 secret·외부 egress·다중 서비스·arm64는 지원하지 않습니다. 여러 앱은 각각의 review 디렉터리와 Application으로 처리합니다. 같은 클러스터의 앱들은 서로 다른 NodePort를 할당해야 합니다.

Public registry는 `anonymous_manifest_read`와 null Secret 참조, private registry는 `authenticated_manifest_read`와 위 Secret 참조를 요구합니다. 이 결과는 CI에서 digest manifest에 접근한 증거이며 대상 노드에서 image layers를 pull한 증거가 아닙니다. renderer는 registry나 클러스터에 접속하지 않습니다. NetworkPolicy는 ingress CIDR/서비스 포트만 허용하고 egress를 차단합니다. NodePort는 `externalTrafficPolicy: Local`이며 **ALB target node에 실제 Pod가 있어야 합니다**. Cilium의 source IP 관측/정책 적용과 등록된 클라우드의 SG·라우팅은 실제 환경에서 함께 검증합니다.

## PostgreSQL 연결과 마이그레이션

게시된 workload가 `resources.postgres`를 요청하면 target에 다음 참조를 함께 등록해야 합니다. DB 생성·사용자 권한·TLS 인증서·아래 Secret 설치와 재조회는 환경 생성 실행자가 먼저 완료합니다. renderer는 이름과 사설 IP만 저장하며 Secret 값이나 연결 URL을 Git에 쓰지 않습니다.

```json
"database": {
  "host": "10.20.0.10",
  "port": 5432,
  "runtime_secret": "demo-runtime",
  "migration_secret": "demo-migration",
  "ca_secret": "demo-ca"
}
```

세 Secret은 workload namespace 안의 서로 다른 이름이어야 합니다. runtime Secret의 `DATABASE_URL`은 DML 전용 사용자, migration Secret의 `MIGRATION_DATABASE_URL`은 스키마 소유자 URL을 담습니다. 두 URL 모두 `sslmode=verify-full&sslrootcert=/etc/railshot/db/ca.crt`를 사용하고 인증서는 DB proxy IP SAN을 포함해야 합니다. CA Secret의 `ca.crt`만 해당 경로에 읽기 전용으로 마운트합니다. Deployment에는 runtime URL만 주입하고, 같은 이미지의 migration Job에는 migration URL을 `MIGRATION_DATABASE_URL`과 호환용 `DATABASE_URL`에 주입합니다. CI 임시 PostgreSQL과 실제 Patroni PostgreSQL의 major version은 16으로 맞춥니다.

배포 순서는 일반 Argo Sync wave의 NetworkPolicy `-2` → migration Job `-1` → Deployment `0`입니다. 사전 namespace 기본 차단 정책과 함께 사용하며 DB IP `/32`의 TCP 5432만 나갈 수 있습니다. 연결은 숫자 IP를 사용하므로 DNS 허용이 필요하지 않습니다. migration은 동일한 검증 이미지·보안 제한·CA를 사용하고, `backoffLimit: 0`, `restartPolicy: Never`, 300초 제한으로 실행합니다. Job이 실패하면 다음 wave로 진행하지 않습니다. Argo의 현재 revision 완료와 정확한 Job sync 결과가 있어야 `deployed: true`, `migration.state: succeeded`가 됩니다. 실제 SQL 응답·권한·TLS 연결 성공은 환경 E2E에서 별도로 확인합니다.

Job 이름은 이미지·명령·DB 참조의 해시에 고정되며 완료된 Job을 보존합니다. 같은 배포 ID 재호출은 기존 bridge의 읽기 전용 관측만 수행하므로 migration을 다시 시작하지 않습니다. 이미지나 migration 명령이 바뀌면 새 이름의 Job을 먼저 실행하고 다음 wave에서 앱을 갱신합니다. 이전 Job은 삭제·재생성하지 않으며 `argocd.argoproj.io/compare-options: IgnoreExtraneous`로 전체 sync 상태에서만 제외합니다. [Argo compare option](https://argo-cd.readthedocs.io/en/stable/user-guide/compare-options/)은 health를 제외하지 않으므로 실패한 이전 Job은 성공 판정을 계속 차단합니다. 현재 revision의 정확한 Job 성공과 Deployment 이미지 digest를 별도로 확인하며, 다른 namespace·앱·종류이거나 Argo가 해당 Application 소유로 확인하지 않은 추가 리소스도 차단합니다.

보존 Job은 운영자가 정리할 때까지 남습니다. 현재 API의 기본 100-operation 보관 한도가 전체 요청 수를 제한하며, 자동 prune·force·Job 삭제·DB 삭제는 수행하지 않습니다. 완료 Job이 남아 있는 동안 같은 이미지·명령·DB 참조로 롤백해도 그 migration은 재실행되지 않으므로 앱의 migration은 멱등적이고 롤백 호환성을 유지해야 합니다. AppProject와 namespace Role에는 DB migration이 있는 경우에만 `batch/Job`을 추가합니다.

이전 Job의 완료는 해당 resource의 `health.status: Healthy`로 확인합니다. 현재 고정한 Argo CD 3.5.3 설치 선언은 `argocd-cmd-params-cm`의 `controller.resource.health.persist: "true"`를 설정합니다. [Argo 3.0 이후 기본값](https://argo-cd.readthedocs.io/en/stable/operator-manual/upgrading/2.14-3.0/#health-status-in-the-application-cr)은 resource health를 Application CR 밖에 저장하므로, 기존 Argo를 재사용할 때에도 owner가 같은 설정과 controller rollout·readback을 확인해야 합니다. 이전 Job의 health가 없거나 Progressing/Degraded이면 재배포 성공을 표시하지 않습니다. 전체 Application health 검사와 현재 revision의 정확한 Job·Deployment 검사도 유지합니다.

## Argo 연결과 실행

기존 workload namespace, 해당 namespace에 제한된 runtime ServiceAccount/RBAC, private pull Secret, Argo 설치와 config repository 접근 권한을 운영자가 먼저 준비합니다. 운영 kubeconfig는 비공개 파일로 관리하고 `KUBECONFIG`로 선택합니다. 이 도구는 토큰이나 kubeconfig bytes를 명령 인수로 받지 않습니다.

```sh
# 지정 repo/server/namespace와 세 workload 종류만 허용하는 Project 선언 생성
python gitops/argo.py project /private/review-aws /private/review-gcp > /private/projects.json
kubectl --context railshot-control apply --server-side -f /private/projects.json

# 한 target의 scoped bearerToken + tlsClientConfig JSON만 stdin으로 전달
# config에는 insecure:false와 caData가 필요하며 exec/plugin/admin 자격 생성은 지원하지 않음
python gitops/argo.py register-cluster /private/review-aws --context railshot-control < /private/aws-argocd-auth.json
python gitops/argo.py register-cluster /private/review-gcp --context railshot-control < /private/gcp-argocd-auth.json

python gitops/argo.py sync /private/review-aws /private/review-gcp \
  --context railshot-control --repo /private/config-checkout --timeout 600
python gitops/argo.py verify /private/review-aws /private/review-gcp \
  --context railshot-control --repo /private/config-checkout --timeout 0
```

Project 생성은 선언 출력만 수행합니다. 이미 운영 중인 Project를 갱신할 때는 현재 앱 전체를 포함해 검토합니다. `register-cluster`는 project·namespace를 제한한 네이티브 Argo cluster Secret을 생성하고 다시 읽어 일치를 확인합니다. Secret의 namespace 목록을 축소해 기존 앱을 분리하지 않으며, 갱신 시 그 target의 모든 기존 namespace를 포함해야 합니다. 자격은 subprocess stdin으로만 전달하고 native stderr/stdout을 로그로 출력하지 않습니다. 인증 JSON은 repo·artifact에 넣지 않으며 scoped token 만료와 갱신은 운영자가 관리합니다. Secret 등록 성공은 cluster 연결 성공을 뜻하지 않습니다.

`sync`/`verify`는 live AppProject가 제한된 repo·runtime server·namespace와 Deployment/Service/NetworkPolicy 및 필요한 migration Job만 허용하는지 먼저 확인합니다. config checkout의 origin 및 `SHA:path/workload.json`을 receipt와 비교하고, 해당 Git 디렉터리에 다른 파일이 있으면 차단합니다. `sync`는 검토한 Application을 server-side apply한 뒤 같은 SHA의 네이티브 Argo operation을 요청합니다. 이미 같은 revision의 operation이 있으면 새 요청을 보내지 않고 관측합니다. `verify`는 읽기 전용입니다. 여러 앱 중 일부만 완료되면 앱별 결과와 `incomplete`를 반환합니다.

`rendered_for_review`와 `deployed: false`는 검토용 선언 생성 상태입니다. Argo CLI의 `deployed: true`는 live Application의 소유자·source·target이 일치하고 관측 revision이 고정 SHA이며, operation `Succeeded`, `Synced`, `Healthy`, 예상 리소스 및 이미지 목록이 모두 확인된 상태입니다. Argo 3에서 개별 resource health가 생략되는 경우 aggregate Application health를 사용합니다. 이는 외부 접속 완료와 구분해 `public_verified: false`, `url: null`로 반환합니다. 대상 노드의 실제 Pod imageID·Ready는 별도 E2E에서 확인해야 합니다. `imagePullPolicy: Always`도 캐시된 layers는 재사용할 수 있습니다. 제품 bridge는 이 Argo 관측 뒤 아래 HTTPS 검사를 수행합니다. Patroni 생성과 SQL·TLS 검증은 환경 생성 실행자가 수행합니다.

## 제품 백엔드 실행 연결

`apps/api/src/cd.js`의 `createCdAdapter({configPath, loadPublished})`는 비동기 `deployPublished` 함수를 반환합니다. `loadPublished`는 GitHub의 run/attempt/artifact ID와 source/target/tenant를 재검증해 원본 게시 파일 5개를 가져오는 서버 함수입니다. 클라이언트가 올린 파일이나 URL을 이 입력으로 사용하지 않습니다. Python 실행 환경에는 기존 CI와 같은 PyYAML/jsonschema가 필요하며, 서버의 `RAILSHOT_CD_CONFIG`는 아래 비공개 설정 파일을 가리킵니다.

```json
{
  "version": 1,
  "state_dir": "/private/railshot/cd-jobs",
  "repository": "/private/railshot/config-checkout",
  "branch": "deployments",
  "context": "railshot-control",
  "targets": {
    "k3s-aws": {
      "app": "demo",
      "tenant": "team",
      "target": {
        "id": "k3s-aws",
        "namespace": "tenant-demo",
        "argocd_namespace": "argocd",
        "project": "railshot",
        "architecture": "amd64",
        "repo_url": "https://github.com/example/config.git",
        "cluster_server": "https://192.0.2.1:6443",
        "path": "targets/k3s-aws/demo",
        "node_port": 30080,
        "ingress_cidrs": ["10.20.0.0/24"],
        "resources": {
          "requests": {"cpu": "100m", "memory": "128Mi"},
          "limits": {"cpu": "500m", "memory": "256Mi"}
        }
      },
      "public_http": {
        "url": "https://demo.example.com/health",
        "expected_json": {"status": "ready"}
      }
    }
  }
}
```

설정 파일은 실행 사용자 소유 0600, 상태 디렉터리는 0700이어야 합니다. `target`은 위 기존 handoff 계약을 그대로 사용하며 `revision`은 bridge가 생성한 Git commit으로 채웁니다. Private registry는 기존 `image_pull_secret` 참조도 target에 등록합니다. 전용 config checkout은 지정 branch에서 깨끗하고 원격과 일치해야 합니다. Git push 자격, 커밋 작성자, kubeconfig, 제한된 AppProject, namespace/pull Secret과 Argo cluster 등록은 운영자가 준비합니다. 기존 고정 앱 설정은 그대로 사용할 수 있습니다. 환경 등록 helper가 아래 `edge.prepare`를 호출하면 신규 앱의 NodePort·hostname·공개 route를 할당해 같은 CD 계약으로 연결합니다. namespace·cluster·CI 등록은 환경 등록 담당의 책임입니다.

고정 명령 `python3 gitops/bridge.py --config /private/railshot/cd.json`에 다음 필드만 stdin JSON으로 전달합니다: `action: apply|observe`, `deployment_id`, `target_id`, 서버 시작 때 읽은 설정의 `config_sha256`, 재검증한 `publication`, 그리고 `files` 객체의 파일명별 base64 원본 5개. 설정이 바뀌면 실행 전에 차단하므로 진행 중인 배포가 새 환경으로 향하지 않습니다. 설정 변경 적용은 서버를 다시 시작해 새 요청에서 수행합니다. 앱·대상·원본 해시를 대조한 뒤 기존 `handoff.render`로 선언을 만들고, Git commit/push 및 원격 SHA 재조회, 기존 Argo 소유권·revision·image 검증, 마지막으로 등록된 HTTPS health 경로를 검사합니다. HTTP 200과 기대 JSON의 정확한 일치를 모두 요구하고 리다이렉트·환경 프록시를 사용하지 않습니다. 응답 본문과 native stderr는 제품 결과에 포함하지 않습니다. 이 HTTP 검사는 등록된 경로의 응답 증거이며 실제 Pod imageID 관측을 대신하지 않습니다.

stdout은 `{cd: {state, revision, deployed}, public_http: {state, verified_at, url}}`이며 오류 시 안전한 `error: {code, retryable, outcome_unknown}`를 추가합니다. 공개 검증 완료는 `public_http.state: succeeded`로 나타냅니다. bridge는 push 전에 의도를 영속 저장하고 같은 deployment ID의 재호출에서는 읽기 전용 관측만 수행합니다. 결과가 불명확하면 `unknown`을 반환하며 push/sync를 자동 재전송하지 않습니다. Node adapter는 최초 `apply` 뒤 `observe`만 제한 시간 내 polling합니다. 취소·시간 초과 시 Python과 native Git/kubectl을 포함한 프로세스 그룹을 종료하고 `unknown`을 반환합니다. 이미 원격에 접수된 작업이 취소됐다는 뜻은 아닙니다. 같은 ID에 다른 publication/설정을 보내면 충돌로 거부합니다.

```sh
python -m unittest discover -s gitops -p 'test_*.py'
```

테스트는 native kubectl 경계를 모의 실행하고 임시 로컬 bare Git에서 commit/push·고정 SHA·불명확 결과의 재실행 차단을 검사합니다. 별도 localhost HTTP 서버에서 기대 본문과 redirect 거부를 확인합니다. 실제 Argo 설치·cluster 등록·sync·공개 HTTPS 배포를 증명하지 않습니다.

### 신규 앱 주소와 공유 edge 연결

`edge.py`는 AWS 앱에 한해 기존 `service_name.py`와 `infrastructure/terraform/aws-edge`를 재사용합니다. HTTP API가 아니며, 환경 등록 helper가 검증한 provider descriptor와 운영자 profile로 만든 비공개 JSON만 받습니다. 공개 요청의 IP·URL·SG·명령·파일 경로를 직접 전달하지 않습니다. profile wrapper는 `{version:1, cd:{...}, registration:{state_dir,source_repository,pull_secret_file,edge_config_file,expires_at,...}}`이며, 환경 등록 helper가 `registration.edge_config_file`로 준비한 뒤 `envhome/cd.json`의 해당 target row에 `edge.json.reference`를 넣습니다.

운영자 edge 설정 예시(0600, 경로는 실행 host 기준):

```json
{
  "version": 1,
  "state_dir": "/private/railshot/edge-state",
  "terraform_dir": "/private/railshot/edge-module",
  "variables_file": "/private/railshot/edge-inputs.tfvars.json",
  "auto_apply": false
}
```

`terraform_dir`는 **기존 공유 ALB의 backend/state로 초기화한 전용 checkout**입니다. 새 state로 초기화하면 전체 인프라 생성 계획이 나와 차단됩니다. `variables_file`에는 AWS 앱·apex·wildcard/apex 인증서·zone 설정을 보존합니다. 기존 GCP route 또는 `wireguard_*` 입력이 남으면 신규 prepare/plan/apply를 차단합니다. [AWS edge 이전 절차](../infrastructure/terraform/aws-edge/README.md#기존-wireguard-경로의-이전)에 따라 GCP native L7·DNS·Argo 관리 경로를 먼저 인계합니다. 기존 설정의 읽기 전용 관측은 유지합니다. 실행기는 base routes에 이미 적용한 자체 할당과 이번 route만 더하며 기존 route를 수정·삭제하지 않습니다. 운영 state/자격을 API 컨테이너로 이전할 때는 원래 writer를 먼저 멈추고 한 writer와 동일 backend만 유지해야 합니다. 로컬 `flock`은 서로 다른 host의 동시 writer를 조율하지 않습니다.

등록 helper가 만드는 요청 예시:

```json
{
  "target_id": "new-runtime", "tenant": "team", "app": "new-app",
  "environment_id": "environment-resource-id", "namespace": "tenant-team",
  "provider_kind": "aws", "target_private_ip": "10.20.0.10",
  "target_security_group_id": "sg-0123456789abcdef0",
  "health_path": "/health", "expected_json": {"ok": true},
  "expires_at": "2026-10-05T14:59:00Z"
}
```

신규 등록은 `provider_kind: aws`와 전용 `target_security_group_id`가 필요합니다. GCP·온프레 등록을 이 AWS ALB 실행기에 보내지 않습니다. namespace와 앱 이름은 기존 등록 대상과 일치해야 합니다. hostname identity는 `(tenant, app, environment_id)`이며 deployment ID나 image revision이 바뀌어도 유지합니다. 다른 identity의 hostname 충돌은 거부하고 NodePort(30000–32767)·priority(1000–49999)는 기존 값과 모든 영속 예약을 피해 할당합니다. 같은 물리 주소·namespace·앱을 다른 환경이 점유하지 못합니다. 등록 helper는 클러스터의 기존 Service 전체에서도 NodePort 충돌을 확인해야 합니다. 만료일은 소유권과 정리 인수용 기록이며 자동 삭제 예약을 대신하지 않습니다.

```sh
python3 gitops/edge.py prepare --config /private/edge.json \
  --request /private/request.json --out /private/environment/edge.json
```

결과의 `node_port`, `public_http`를 CD target에 넣고, `edge.reference`의 `{config_path, allocation_path, config_sha256}`를 CD row의 `edge`에 넣습니다. 이 reference만 별도 비공개 파일에 저장하면 운영자가 saved plan을 검토·적용할 수 있습니다:

```sh
python3 gitops/edge.py plan --reference /private/reference.json --out /private/plan-receipt.json
python3 gitops/edge.py apply --reference /private/reference.json \
  --plan-sha256 <reviewed-plan-receipt-hash> --out /private/apply-receipt.json
```

계획은 새 route의 target group/attachment/host rule/DNS, 정확한 AWS NodePort SG 생성만 허용합니다. 공유 ALB SG는 기존 규칙을 그대로 보존한 정확한 사설IP/NodePort egress 추가만 허용합니다. 기존 자원 변경·삭제·교체·state 소유권 해제(`forget`)는 차단합니다. provider refresh가 null collection이나 계산된 ALB 연결을 채운 경우에도 해당 자원의 실행 action이 `no-op`이어야 합니다. plan bytes의 SHA256을 대조하고 외부 apply 전에 `applying`을 영속 저장합니다. 중단·부분 실패 시 자동 replan/reapply 없이 같은 route의 실제 상태를 조회합니다. 소유자가 수동 정리를 완료하기 전에는 예약·불명확 상태를 지우지 않습니다.

플랫폼 담당이 실행 host·state·자격·단일 writer를 인수한 설정에서만 `auto_apply:true`를 사용합니다. bridge 최초 apply는 Git/Argo 전달 뒤 saved plan을 만들고 검사·적용합니다. 반복 `observe`는 변경을 수행하지 않습니다. `auto_apply:false`에서는 운영자 saved-plan 적용을 기다리며 URL 성공을 반환하지 않습니다. 설정 내용이 바뀌면 이전 reference가 거부되므로 설정을 바꿔 이미 접수한 배포를 새 대상으로 돌리지 않습니다.

`reserved/planned/applied`는 공개 사이트 성공이 아닙니다. bridge가 정확한 ALB host/priority/target, Route53 alias, target의 `healthy`, 기본 CA 검증을 사용하는 HTTPS health JSON의 정확한 일치, 실제 앱 route의 HTTPS 200을 모두 관측해야 `public_http.state=succeeded`입니다. redirect·환경 proxy를 사용하지 않습니다. 결과는 검증한 health `url`, 앱 `site_url`, deployment/target/app/tenant/environment/source commit/Git revision/image digest/route digest/plan digest/namespace/expiry가 묶인 `receipt`를 포함합니다. 응답 body·자격·native stderr는 반환하지 않습니다. 실패하면 URL은 null이며 이전 성공 receipt를 현재 성공으로 재사용하지 않습니다.

AWS 경로는 private IP+SG로 ALB에서 접근합니다. GCP의 신규 공개 경로는 native L7 담당이 구성·검증하며 이 실행기는 GCP route를 생성하지 않습니다. 기존 GCP WireGuard 경로를 읽어 관측할 수 있다는 사실은 새 경로의 준비 완료를 뜻하지 않습니다. 다른 클라우드 DB 연결도 별도 네트워크 계약입니다.

온프레는 [Named Tunnel → 사설 Octavia HTTPS](../deployment/cloudflared/README.md) 선언을 운영자가 준비·배포합니다. 제품의 신규 앱 등록과 자동 연결은 별도 작업이며 이 AWS executor는 온프레를 받지 않습니다. 기존 Route53/ALB hostname과 Cloudflare hostname의 소유권을 중복하지 않게 인계해야 합니다. Named Tunnel 공개 HTTP 성공은 GCP Argo API 6443 관리 경로의 연결 증거가 아닙니다.

형식 근거: [Terraform saved plan JSON](https://developer.hashicorp.com/terraform/internals/json-format), [AWS target health 조회](https://docs.aws.amazon.com/cli/latest/reference/elbv2/describe-target-health.html). 로컬 검사는 `python -m unittest discover -s gitops -p 'test_*.py'`이며 네이티브 경계 모의 검사와 실제 cloud 검증은 별도 증거입니다.

## 짧은 Argo 고객 토큰 갱신

정책의 `targets`는 배포 성공 앱 목록이 아니라 관리 중인 인증 목록입니다. CI/CD가 실패한 앱도 부분 생성된 리소스를 복구하는 데 인증이 필요할 수 있으므로 자동으로 제거하지 않습니다. 등록 개수 20개 제한은 사용하지 않으며, 정책 문서의 기존 128 KiB 크기 제한과 개별 소유권·namespace 검증을 유지합니다. 갱신은 최대 4개 Secret을 동시에 처리하고 한 대상의 실패와 무관하게 나머지 대상을 처리합니다. 중복 Secret은 실행 전에 거부하며 각 Secret의 쓰기 후 조회와 resourceVersion 검사는 그대로 유지합니다.

이 변경을 반영할 때는 새 API 이미지로 **인증 갱신 CronJob을 먼저 교체하고 실행 결과를 확인한 뒤 API를 교체**합니다. 이전 갱신 이미지는 20개를 초과한 정책을 거부하므로 API만 업데이트하면 안 됩니다. 기존 정책과 Secret을 유지하고, 사용하지 않는 앱은 기존 앱 삭제 절차로 정리합니다. 이 변경은 이미 실패한 등록을 자동 재실행하거나 GCP의 미확정 라우팅 작업을 복구하지 않습니다.

`credentials.py`는 기존 고객 ServiceAccount의 6시간 토큰을 2시간마다 갱신하는 일회성 명령입니다. 새 관리자 자격이나 장기 ServiceAccount Secret을 만들지 않습니다. 운영자가 고객별 기존 SA 이름·UID, 등록 namespace, server, CA SHA-256, audience를 확인한 비밀 없는 정책을 준비합니다. 예시는 다음과 같으며 값은 실제 등록에서 읽어야 합니다.

```json
{"version":1,"targets":[{
  "secret":"railshot-k3s-aws","target_id":"k3s-aws",
  "server":"https://192.0.2.1:6443","project":"railshot-apps",
  "namespaces":["tenant-demo"],
  "service_account":{"namespace":"tenant-demo","name":"railshot-argocd","uid":"12345678-1234-1234-1234-123456789012"},
  "ca_sha256":"REPLACE_WITH_OBSERVED_CA_SHA256",
  "audiences":["https://kubernetes.default.svc.cluster.local","k3s"]
}]}
```

먼저 각 고객 클러스터에 [credentials-customer.yaml](credentials-customer.yaml)의 제한된 Role/RoleBinding을 적용합니다. 기존 `tenant-demo/railshot-argocd`에 **자기 이름의 `serviceaccounts/token` create만** 추가하며 기존 배포 Role을 덮어쓰지 않습니다. 실제 자기 SA의 발급 허용과 다른 SA·namespace의 발급 거부를 확인합니다. 이 권한은 현재 토큰이 유효한 동안 계속 재갱신할 수 있으므로, 6시간은 각 토큰의 요청 TTL이지 운영 종료 기한이 아닙니다.

```sh
python3 gitops/credentials.py render --policy /private/credentials-policy.json \
  --image "${RAILSHOT_API_IMAGE:?Set the published immutable API image}" > /private/credentials.json
kubectl --context railshot-control apply -f /private/credentials.json
# 첫 실행을 기다리지 않고 확인; 같은 CronJob의 다른 실행과 겹치지 않게 한다.
kubectl --context railshot-control -n argocd create job --from=cronjob/railshot-credentials railshot-credentials-initial
```

선언은 기존 API 이미지의 `/app/gitops/credentials.py`, `argocd/ghcr-pull`, 플랫폼 노드를 사용합니다. 운영 SA는 정책에 등록된 Argo Secret 이름만 `get/patch`할 수 있습니다. ConfigMap에는 정책과 projected SA의 CA·token 파일 경로를 참조하는 kubeconfig만 읽기 전용으로 마운트합니다. 실행 코드나 자격 bytes는 넣지 않습니다. `KUBECONFIG`를 명시하여 kubectl이 localhost 기본값으로 연결하지 않도록 합니다. CronJob은 동시 실행을 금지하고, 5분 실행 제한과 재시도 0을 사용합니다. 직접 실행은 `python3 gitops/credentials.py renew --policy ...`이며 kubectl의 현재 운영 context 또는 Pod의 projected SA를 사용합니다.

현재 Secret의 소유권·등록 범위·CA·SA를 확인한 뒤 TokenRequest를 보냅니다. 새 토큰을 원래 CA로 검증한 고객 API에서 SelfSubjectReview의 이름·UID와 각 등록 namespace의 Pod 조회 권한까지 확인한 경우에만 `data.config`의 bearer token을 교체합니다. JSON Patch의 `resourceVersion` test가 동시 변경을 막습니다. 발급·검증 실패 시 기존 값을 보존하고, patch 결과가 불명확하면 읽기 한 번으로 확인하여 `renewed/unchanged/unknown`을 구분합니다. 토큰과 native 오류 원문을 로그에 쓰지 않습니다.

연속 장애로 기존 토큰까지 만료되면 이 경로는 스스로 복구할 수 없습니다. 운영자가 독립 SSM/IAP 경로에서 기존 고객 SA의 짧은 토큰을 다시 발급해 같은 등록 Secret에 넣고 첫 실행을 검증해야 합니다. 운영 종료 시 CronJob을 `suspend: true`로 전환하고, 진행 중인 갱신 Job이 끝났는지 확인한 뒤 각 고객의 `railshot-argocd-renewal` **RoleBinding을 제거**하여 자기 갱신 권한을 회수합니다. 기존 배포 SA·Role과 고객 앱은 제거하지 않습니다. 이미 발급된 토큰은 자체 만료 시각까지 유효합니다.

공식 형식: [Argo Sync phases and waves](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/), [Argo declarative setup](https://argo-cd.readthedocs.io/en/stable/operator-manual/declarative-setup/), [kubectl sync operation](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-kubectl/), [Argo 3 resource health 변경](https://argo-cd.readthedocs.io/en/stable/operator-manual/upgrading/2.14-3.0/#health-status-in-the-application-cr), [Kubernetes private registry Secret](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/), [TokenRequest](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/#tokenrequest-api), [SelfSubjectReview](https://kubernetes.io/docs/reference/access-authn-authz/authentication/#api-access-to-authentication-information-for-a-client).
