# Platform release verification bootstrap

`platform-publish.yml`의 `deploy=true` 실행은 선언 push 뒤 `verify` job까지 통과해야 완료된다. 검증 순서는 정확한 `deployment/platform` revision의 Argo `Synced/Healthy`, 해당 Deployment가 소유한 Ready Pod의 게시 digest, GitHub-hosted runner에서 `https://railshot.io/healthz`의 `200 ok\n`과 `/api/v1/targets`의 정상 JSON이다. 검증 실패·시간 초과는 workflow 실패다. 선언 push 자체를 되돌리거나 자동 rollback하지 않는다. `publish=true, deploy=false`는 이미지 게시만 수행한다.

이 모듈은 최초 운영자 bootstrap용이다. 기존 control Terraform과 **다른 state**로 신규 IAM role, inline policy, 고정 SSM Command document 세 리소스만 관리한다. EC2, 기존 IAM role, 기존 GitHub OIDC provider는 변경하지 않는다. 기존 `registry.tf`의 railshot-apps pull-token 전달 역할은 재사용하지 않는다. 초기 계정·OIDC provider·SSM agent·K3s·Argo·registry 및 public edge 준비 뒤 플랫폼 release마다 수동 SSH/SSM 작업이 필요하지 않다.

## 고정 대상과 권한

| 항목 | 값 |
| --- | --- |
| AWS account / region | `721622471953` / `ap-northeast-2` |
| control instance | `i-033ae2db907fde68e` |
| 새 role | `railshot-platform-verifier` |
| 기존 OIDC provider | `arn:aws:iam::721622471953:oidc-provider/token.actions.githubusercontent.com` |
| 초기 trusted ref | `refs/heads/integration/team-assembly-20261002` 하나 |
| audience | `sts.amazonaws.com` |
| exact subject | `repo:Jasmin-Softbank@335003159/Railshot@1400202256:ref:refs/heads/integration/team-assembly-20261002` |
| SSM document | `Railshot-VerifyPlatform`, 실행 시 version과 SHA256 모두 고정 |
| public origin | `https://railshot.io`만, redirect·proxy 사용 안 함 |

2026-10-02 저장소 OIDC 설정 조회에서 `use_default=true`, `use_immutable_subject=true`, 위 `sub_claim_prefix`를 확인했다. IAM 조건은 지원되는 `aud`와 `sub`의 `StringEquals`만 사용한다. `repository_id`라는 custom IAM condition key는 사용하지 않는다. 별도로 workflow admission이 저장소 이름과 `GITHUB_REPOSITORY_ID=1400202256`, 정확한 trusted ref를 검사한다. IAM 역할은 해당 ref의 신뢰된 workflow에 대한 권한이므로 그 ref의 변경 권한도 제한해야 한다. [GitHub AWS OIDC 계약](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws), [immutable subject 형식](https://docs.github.com/en/actions/reference/security/oidc), [AWS trust 조건](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html).

`ssm:SendCommand`는 위 document ARN과 control instance ARN에만 허용한다. `ssm:GetCommandInvocation`은 AWS가 resource 수준 제한을 제공하지 않아 `Resource:"*"`인 상태·출력 조회 권한이 필요하다. 실행 코드는 자신이 방금 받은 command ID와 고정 instance만 조회한다. `StartSession`, `AWS-RunShellScript` 직접 실행, document 수정, Secret/Parameter 조회, EC2 변경, IAM 변경 권한은 부여하지 않는다. [AWS Run Command 권한](https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command-setting-up.html), [SSM IAM actions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_awssystemsmanager.html).

Document 본문에 `deployment/scripts/verify-platform.py`를 고정하여 포함한다. parameter는 40자리 revision과 두 64자리 digest뿐이며 `allowedPattern`과 `interpolationType:ENV_VAR`를 함께 사용한다. shell command/path/URL은 parameter로 받지 않는다. 최신 SSM agent의 환경 변수 보간 지원이 필요하며 지원하지 않으면 `set -u`로 실패한다. Code는 SSM agent의 root context에서 control의 K3s context로 고정 Application, 두 Deployment 및 그 Pod/ReplicaSet을 **조회만** 한다. Secret 조회나 `apply`, `patch`, `exec`는 없다. 원격 출력은 revision, digest, Pod 이름·UID, 상태와 고정 오류 코드뿐이다. [SSM parameter 보간](https://docs.aws.amazon.com/systems-manager/latest/userguide/documents-syntax-data-elements-parameters.html).

## 최초 설치와 같은 경로의 재실행

저장소 root에서 기존 승인된 운영 AWS 자격으로 실행한다. 아래 native Terraform entrypoint는 control 모듈 plan과 섞지 않으며 `-target`을 사용하지 않는다. 자격 값은 변수/state에 넣지 않는다.

```sh
umask 077
verification_private="$HOME/.local/share/railshot/platform-verification"
mkdir -p "$verification_private"
export TF_DATA_DIR="$verification_private/.terraform"
terraform -chdir=infrastructure/terraform/platform-verification init -input=false -lockfile=readonly \
  -backend-config="path=$verification_private/terraform.tfstate"
terraform -chdir=infrastructure/terraform/platform-verification plan -input=false \
  -out="$verification_private/reviewed.tfplan"
terraform -chdir=infrastructure/terraform/platform-verification show "$verification_private/reviewed.tfplan"
# 초기 plan은 새 document, role, inline policy만 생성해야 한다. 검토한 같은 plan을 적용한다.
terraform -chdir=infrastructure/terraform/platform-verification apply "$verification_private/reviewed.tfplan"
terraform -chdir=infrastructure/terraform/platform-verification output -json workflow_variables \
  > "$verification_private/workflow-variables.json"
python3 - "$verification_private/workflow-variables.json" <<'PY'
import json, subprocess, sys
for name, value in json.load(open(sys.argv[1])).items():
    subprocess.run(['gh', 'variable', 'set', name, '--repo', 'Jasmin-Softbank/Railshot',
                    '--body', str(value)], check=True)
PY
```

최초 설치 시 control의 SSM agent 온라인 상태와 ENV_VAR 지원, `/usr/local/bin/k3s kubectl` 조회, 기존 `railshot-platform` Application 및 public DNS/TLS를 확인한다. OIDC `sub` 설정은 아래 읽기 명령으로 재확인하며 모듈 값과 다르면 광역 wildcard로 우회하지 않는다.

```sh
gh api repos/Jasmin-Softbank/Railshot/actions/oidc/customization/sub
```

검증 코드가 바뀌면 같은 모듈·state에서 새 saved plan을 적용하고 네 개 workflow 변수의 document version/hash를 갱신한다. 기존 검증 문서는 기본 버전 변경에 관계없이 pin한 버전으로 실행된다. 일반 앱/API 이미지 게시만으로는 이 bootstrap을 반복할 필요가 없다. trusted ref를 main으로 옮길 때는 검토한 `-var='trusted_ref=refs/heads/main'` plan과 `RAILSHOT_PLATFORM_VERIFY_REF`를 함께 갱신한다. 초기 trust에 main이나 `integration/*` wildcard를 추가하지 않는다.

workflow는 버전·해시를 고정한 SSM 문서로 짧은 읽기 검사를 수행한다. 완료된 검사가 대기 상태를 반환하면 10초 후 다시 조회하며, 응답이 불확실한 요청은 재전송하지 않는다. 이전 revision 처리, API 준비, 준비 재시도, 일반 동기화 대기를 구분해 Actions에 표시한다. 확정된 Argo 실패는 즉시 실패하고, 성공 조건은 기존과 같은 revision·Pod digest·Ready·공개 HTTPS다. 각 SSM 문서는 최대 90초, 전체 검증은 준비 Job의 15분과 Pod 교체 여유 2분을 합한 최대 17분이다. 이 값은 최대 대기 한도이며 정상 준비 완료 후 첫 검사에서 바로 끝난다. 검증 역할 세션은 30분, workflow job은 25분이다.

API 교체 요청은 진행 중 작업을 중단하지 않고 다음 큐 작업의 시작과 새 변경 요청을 먼저 보류한다. 진행 중 작업이 끝나면 교체하며, 준비 프로세스가 사라지면 마지막 조회로부터 2분 뒤 보류를 해제한다. 준비 로그는 대기 사유와 제한된 오류 코드만 출력하며 토큰·응답 본문은 출력하지 않는다. 실패해도 검증기는 Kubernetes를 변경하지 않으며 SSH, 고객 runner, release GitHub token으로 관리자 경로에 진입하는 fallback은 없다. `platform-verification-<source SHA>` artifact가 실제 검증 결과이고, Terraform validate나 로컬 mock 검사는 배포 성공 증거가 아니다.
