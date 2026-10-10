# Administrator Terraform tools

Run these CLIs from the repository root. They operate on administrator-registered targets and private files outside the checkout. `provision.py` supports saved-plan AWS/GCP/Azure provisioning, target/state-owner binding, source/plan hash checks, a target lock, budget reservations and maintenance checks. It does not install Kubernetes or expose an HTTP API. An applied `node_descriptor` remains `readiness: unverified` until guest/runtime checks produce their own receipts.

`specsheet.py` normalizes descriptor fields for a report. It is not the Ansible request adapter or a ReadyTarget promotion service. The dedicated `terraform/ci` module has a different descriptor and is not an app-host target for this CLI.

## AWS billing snapshot

`costs.py` imports an administrator-captured [Cost Explorer GetCostAndUsage](https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html) response together with its exact request and the registered account ID. Keep the envelope and actual query observation time in the private operations directory:

```json
{
  "account_id": "123456789012",
  "request": {
    "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-02"},
    "Granularity": "DAILY",
    "Metrics": ["UnblendedCost"],
    "Filter": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": ["123456789012"]}}
  },
  "response": {"ResultsByTime": "REPLACE_WITH_THE_UNMODIFIED_API_RESPONSE"}
}
```

The example response is a placeholder, not an importable zero-cost fixture. `response` must contain the actual response object. `DAILY` and `MONTHLY` queries are supported; start must be the first day of the usage month and end must cover the observation date. End is exclusive. Service/tag filters, grouping, incomplete pagination, missing totals, missing estimate status, mismatched accounts and gaps/overlaps are rejected. The CLI makes no AWS calls and cannot authenticate the provenance of an operator-supplied file. `query_verified` means the captured account/filter/response contract matches, not that the CLI independently queried AWS.

```sh
python3 infrastructure/providers/terraform_tools/costs.py --db /absolute/private/billing.sqlite3 import \
  --provider aws --scope aws/123456789012/2026-10 --period 2026-10 \
  --source-scope 123456789012 --observed-at 2026-10-02T02:00:00Z \
  /absolute/private/aws-billing.json
python3 infrastructure/providers/terraform_tools/costs.py --db /absolute/private/billing.sqlite3 report \
  --scope aws/123456789012/2026-10
```

An explicit API amount of zero is accepted; absent/empty data is not zero. Any `Estimated: true` result stays `basis: reported_estimate`. The account-wide total is not attributed to one VM. An administrator must separately review the incremental quote and conservative `unreported_cost`; reported data can lag usage. Existing reservations remain held until provider completion is reconciled and explicitly released. Missing, stale, wrong-account, wrong-currency, previous-month or over-limit billing blocks apply.

## Registered app-host input

`budget.py` provides the server-side AWS refresh used before a product plan. It takes
`--input /private/input.json --evidence-dir /private/evidence --output /private/output.json`.
Input is `{ "version": 1, "targets": [registered_target] }`; stdout is the public cost
summary and the private output contains refreshed targets plus evidence hashes.
It authenticates the account with STS, retrieves complete account-wide Cost Explorer
pages and current EC2/gp3/public-IPv4 Price List results, then imports the observation
through `costs.py`. The existing ledger must already exist and bind the account.
Missing observations, changed policy, ambiguous prices and incomplete pagination fail closed.

Quotes preserve the operator's monthly limit and unreported-cost allowance, include
existing held reservations, and expire after two hours. Compute uses the configured
guest-stop duration; retained storage is estimated through month end. Optional
`retained_storage_hours: 24` is only an explicit synthetic-test cleanup assumption,
not an automatic deletion feature. The guest timer and estimates are not spending caps;
retained disks and EIPs continue to require cleanup. GCP automatic collection is not
implemented. Only apply reserves money; plan refresh never releases existing holds.

Target JSON retains this existing shape; use a real private ledger and reviewed quote times, not the example values as an approval:

```json
{
  "schema_version": "v1",
  "provider_kind": "aws",
  "target_id": "customer-aws-demo",
  "execution_driver": "terraform",
  "profile": {"kind": "app_cluster", "allowed_sizes": ["t3.large"]},
  "variables": {
    "target_id": "customer-aws-demo",
    "account_id": "123456789012",
    "region": "ap-northeast-2",
    "name": "customer-aws-demo",
    "instance_type": "t3.large",
    "ami_id": "REPLACE_WITH_REVIEWED_UBUNTU_AMI",
    "vpc_id": "REPLACE_WITH_EXISTING_VPC",
    "subnet_id": "REPLACE_WITH_EXISTING_SUBNET",
    "http_enabled": false,
    "https_enabled": false,
    "allocate_eip": false,
    "create_ci_plan_role": false,
    "additional_security_group_ids": [],
    "operator_ssh_public_key": "REPLACE_WITH_OPENSSH_PUBLIC_KEY",
    "initialize_empty_data_disk": true
  },
  "budget": {
    "ledger_path": "/absolute/private/billing.sqlite3",
    "scope": "aws/123456789012/2026-10",
    "currency": "USD",
    "incremental_cost": "REVIEWED_QUOTE",
    "limit": "REVIEWED_LIMIT",
    "unreported_cost": "REVIEWED_METERING_GAP",
    "quoted_at": "REPLACE_WITH_ACTUAL_QUOTE_TIME",
    "expires_at": "REPLACE_WITH_QUOTE_EXPIRY"
  }
}
```

WireGuard variables are rejected before state creation or Terraform execution. Provider-local app ingress and a separately verified management route replace new cross-cloud WireGuard setup. A Terraform `forget` plan retains the remote object while ending state ownership; it requires the existing maintenance receipt and explicit ownership/cleanup handoff, and is not proof of live tunnel shutdown.

The executor injects `owner_ref` with the exact external state path. Use `initialize_empty_data_disk: true` only for a reviewed new empty module-created disk. The public SSH key is configuration, not a private credential. Never put SSH private keys, registry tokens or WireGuard private keys in target JSON or Terraform state.

```sh
python3 infrastructure/providers/terraform_tools/provision.py plan \
  --target /absolute/private/customer-aws.json --state-root /absolute/private/terraform
python3 infrastructure/providers/terraform_tools/provision.py apply \
  --target /absolute/private/customer-aws.json --state-root /absolute/private/terraform \
  --plan-sha256 REVIEWED_PLAN_SHA256
```

Review the saved plan before apply. Changes requiring drain/backup also require the existing `--maintenance` receipt. An attempted apply with an unknown outcome is not automatically retried. Source or target changes require a new plan. Cloud permissions, quota, connectivity, actual disk preparation and runtime readiness still require live verification.

Offline contracts:

```sh
python3 -m unittest discover -s infrastructure/providers/terraform_tools -p 'test_*.py'
```

Terraform subprocesses in executor tests are mocked; these tests do not prove successful cloud provisioning.

## Database nodes and host-key enrollment

For AWS/GCP DB nodes, use `profile.kind: database_cluster` and `variables.purpose: database`. The same saved-plan, owner, budget and maintenance checks apply. Each target still creates one host. Runtime hosts retain the default `app_cluster` / `runtime` pair. DB hosts mount the retained disk at `/var/lib/postgresql`; Terraform never reports a configured database or runtime. Match the DB playbook data directory to that mount and verify it in the guest.

Both modules accept `database_ingress` and `database_egress` lists of `{port, cidr}` objects. Only TCP 5432, 2379, 2380 and 8008 with RFC1918 IPv4 /16-/32 ranges are supported. Prefer exact peer /32 rules; these inputs neither create routes nor establish reachability. Ingress requires a database-purpose host; runtime hosts may use egress to their DB proxy. DB purpose suppresses public web ingress. Reuse one environment network: AWS uses its existing `vpc_id` / `subnet_id`; GCP uses the optional paired `existing_network_name` / `existing_subnetwork_name`. Existing network ownership stays outside each new DB target.

`access.py --descriptor /private/node.json --output /private/new-known-hosts` enrolls a new VM's Ed25519 key through authenticated provider APIs. The descriptor and output directory must be private and owned by the executor; the output must not exist. AWS binds STS account, EC2 ID and private IP, waits for SSM, then reads the public host key after cloud-init. GCP binds its immutable instance ID and private IP before and after reading the bootstrap's public serial marker. Polling is bounded by `--timeout-seconds` (default 600). The file is created exclusively with mode 0600; no SSH keyscan or accept-new trust is used. Missing bootstrap markers, changed identities or provider failures block enrollment. This is a host-key observation, not runtime or database readiness.
