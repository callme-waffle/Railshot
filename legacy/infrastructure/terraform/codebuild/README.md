# Trusted CodeBuild publisher

This module creates a private administrator PoC publisher: **one existing gate-verified service named `web`**, an immutable ECR repository, a private encrypted S3 artifact bucket (three-day retention), a three-day CloudWatch log group and a narrow CodeBuild role. It does not run the uploaded app, rebuild its image, invoke a model, mutate GitOps, or contact Kubernetes. CodeBuild is limited to one concurrent `BUILD_GENERAL1_SMALL` build, 15 minutes execution and 10 minutes queued time.

The platform GitHub commit must already contain `ci/workflows/codebuild-release.yml` and `ci/scripts/codebuild_release.py`. Terraform pins that full 40-character commit; the build also checks `CODEBUILD_RESOLVED_SOURCE_VERSION`. The managed `aws/codebuild/standard:7.0` environment and distro Skopeo package are version families, not content-digest-pinned images. They are not claimed to be byte-reproducible. CodeBuild privileged mode is disabled; the shared bundle publisher uses Skopeo and never starts the image. [AWS build source/override semantics](https://docs.aws.amazon.com/codebuild/latest/APIReference/API_StartBuild.html).

The role can read `bundles/*`, write `receipts/*`, write the one log group and push/pull only the registered ECR repository. ECR authentication requires `ecr:GetAuthorizationToken` on `*`; this does not grant access to other repositories. No EC2, IAM, SSM parameter, model, GitHub secret or Kubernetes permission is granted. [AWS service-role guidance](https://docs.aws.amazon.com/codebuild/latest/userguide/setting-up-service-role.html).

From the repository root, put reviewed values in a **private file outside the checkout**:

```sh
terraform -chdir=infra/terraform/codebuild init -backend=false
terraform -chdir=infra/terraform/codebuild validate
terraform -chdir=infra/terraform/codebuild plan -state=/private/path/codebuild.tfstate -var-file=/private/path/codebuild.tfvars.json -out=/private/path/codebuild.tfplan
terraform -chdir=infra/terraform/codebuild show /private/path/codebuild.tfplan
# Administrator reviews resources, IAM, retained costs and the exact source commit first.
terraform -chdir=infra/terraform/codebuild apply -state=/private/path/codebuild.tfstate /private/path/codebuild.tfplan
terraform -chdir=infra/terraform/codebuild output -state=/private/path/codebuild.tfstate -json codebuild_descriptor > /private/path/codebuild.json
```

Use independent private Terraform state/backend for this module; do not apply over the app/control module state. Required inputs: `account_id`, `platform_ref`; optional `region` (Seoul default) and `name`. No credential values belong in tfvars, user data, environment overrides or the source archive. S3/ECR storage and logs can outlive a build and incur cost; CodeBuild completion is not resource cleanup. Preserve receipts before intentional cleanup. No resource has been applied merely because local validation passed.

After real resources exist, the administrator explicitly binds the manifest they reviewed:

```sh
python3 ci/scripts/codebuild.py start --config /private/path/codebuild.json --root /private/path/publish-jobs --job-id UUID --bundle /private/path/validated-bundle --approve-manifest-sha256 REVIEWED_MANIFEST_SHA256
python3 ci/scripts/codebuild.py reconcile --config /private/path/codebuild.json --root /private/path/publish-jobs --job-id UUID
```

`start` persists intent before each external write and records the native build ID. It accepts no buildspec/role/registry override from the bundle. `reconcile` only reads that build and its bound receipt; it does not submit another build. An uncertain dispatch or failed/stopped build may have published an image and remains `UNKNOWN` until an operator reconciles it. The AWS five-minute idempotency window is not durable application idempotency. A successful receipt means the exact images were published to ECR; `deployment_status` stays `NOT_RUN`. This CLI approval is **not** the product Allow service, which needs a separately authorized adapter connection.

Local verification: `python3 -m unittest discover -s ci -p test_codebuild.py`. It mocks AWS/registry calls and is not evidence of a live build. Current cloud execution status must come from a native Build ID and receipt, not this README.
