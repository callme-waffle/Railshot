# GCP native HTTPS edge

An independent global `EXTERNAL_MANAGED` HTTPS load balancer fronts one existing
VM's private NodePort through a zonal `GCE_VM_IP_PORT` NEG. HTTP redirects to
HTTPS. The VM, its public IP, VPC/subnet, DNS and WireGuard remain externally
owned. Use a separate private state directory; never apply this configuration
against the VM module's state. Compute API, billing and ADC/IAM must already work.

The module enables Certificate Manager without disabling it on destroy. It
creates a managed certificate using a per-project DNS authorization. Give the
`dns_authorization_record` output to the authoritative DNS owner; retain that
CNAME for renewal. The bootstrap hostname's DNS remains operator-managed. The new global frontend IP
cannot reuse the VM's regional external IP.

For registered `routes`, the existing DNS-authorization resource publishes its
owned CNAME through Railshot's DNS writer and confirms it through Google Public
DNS before Terraform creates the dependent certificate. The application executor
supplies the private DNS config path and Python module path only to that apply;
credentials remain in the existing private token file. Standalone operators adding
routes must supply `RAILSHOT_GCP_CERTIFICATE_DNS_CONFIG` and set `PYTHONPATH` to the
checkout's `gitops` directory. This adds no Terraform resources or CI stages.
Existing authorizations are not recreated by this change; interrupted or failed
certificate issuance still requires observing the existing certificate and DNS.

Before apply, verify the named VM/private IP, its NodePort health, and its
dedicated service account (the firewall applies to every VM using that account).
Add `35.191.0.0/16` and `130.211.0.0/22` through the existing GitOps
`target.ingress_cidrs` input to the workload's actual container port. Preserve
the old source ranges during migration. With `externalTrafficPolicy: Local`,
GFE sources reach NetworkPolicy unchanged; cloud firewall alone is insufficient.

Use Terraform 1.7+ and the locked provider. Copy this directory into a private
operations directory, then run:

```sh
terraform init
terraform test
terraform plan -var-file=terraform.tfvars.example -out=edge.tfplan
terraform show edge.tfplan
# Apply only the reviewed saved plan under the operator's change authorization.
terraform apply edge.tfplan
terraform output -json dns_authorization_record
```

Plan must contain only new edge resources, with no change or deletion of an
existing VM, network, IP, route, AWS object, DNS record or tunnel. State and saved
plans belong in the private operations directory, outside Git. The mock test
checks configuration and the private-IP guard; it cannot establish live health.

Wait for Certificate Manager `ACTIVE` and backend service `HEALTHY`, then test
the new IP before moving the existing hostname:

```sh
gcloud certificate-manager certificates describe railshot-gcp-edge --location=global --project=railshot-poc-20261001
gcloud compute backend-services get-health railshot-gcp-edge --global --project=railshot-poc-20261001
curl --fail --resolve your-app.example.com:443:NEW_GLOBAL_IP https://your-app.example.com/health
```

The backend's 100 requests/second per endpoint is a balancing capacity setting,
not a measured throughput result or request quota. One VM remains a single
failure domain. `readiness` is deliberately unverified until the certificate,
backend health and TLS-verified HTTP response have independent evidence.

Keep AWS ALB and WireGuard until the DNS owner completes cutover and separately
verifies AWS Argo → GCP Kubernetes API6443 management connectivity. This L7 edge
does not replace that management connection. Rolling back public DNS does not
require destroying the new edge.

As checked on 2026-10-03, the first five global forwarding rules cost
$0.025/hour per project (about $0.60/day or $18.25/730 hours), plus regional
traffic processing and Premium internet egress. A forwarding-rule-attached
global IP has no separate IP charge; an unattached reservation may incur one.
The first 100 Certificate Manager certificates per project are free under the
published tier. Existing AWS and VM charges continue during overlap; these
figures are estimates, not billing receipts or a spending cap.

Sources: [LB firewall](https://docs.cloud.google.com/load-balancing/docs/firewall-rules),
[DNS authorization](https://docs.cloud.google.com/certificate-manager/docs/deploy-google-managed-dns-auth),
[LB pricing](https://cloud.google.com/load-balancing/pricing),
[IP pricing](https://cloud.google.com/vpc/network-pricing),
[Certificate Manager pricing](https://cloud.google.com/certificate-manager/pricing).
