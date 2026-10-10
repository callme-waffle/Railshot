#!/usr/bin/env bash
# Opt-in live regression; creates and removes ONLY its own transient namespace.
# Run after a separately authorized installation: sudo bash "$0" --run
set +x
set -Eeuo pipefail
[[ $# == 1 && $1 == --run ]] || {
  echo 'Usage: sudo bash test-control-cilium.sh --run (isolated or explicitly authorized control node)' >&2
  exit 2
}
[[ $(uname -s) == Linux && $(id -u) == 0 ]] || exit 2
# shellcheck source=../../deployment/scripts/common.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/../../deployment/scripts/common.sh"
[[ -f /etc/railshot/node-role && $(cat /etc/railshot/node-role) == control ]] || die 'Expected control node role'
[[ -s $KUBECONFIG && -x /usr/local/bin/k3s ]] || die 'Local K3s admin configuration required'
for tool in python3 curl flock; do command -v "$tool" >/dev/null || die "Missing $tool"; done
exec 9>/run/railshot-control.lock
flock -n 9 || die 'Control installation or another regression is running'
exec 8>/run/railshot-deployment.lock
flock -n 8 || die 'Another runtime operation is running'
[[ $(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}') == https://127.0.0.1:6443 ]] || die 'Expected local K3s API'

# This is a platform-node smoke, not cross-node/Docker isolation acceptance.
kubectl wait --for=condition=Ready nodes --all --timeout="$WAIT_TIMEOUT"
# Reuse the installer guard and its unique server selection, including when
# the approved build agent appears first in the API NodeList.
identity=$(python3 - "$ROOT_DIR/cilium" <<'PY'
import ipaddress, json, sys
sys.path.insert(0, sys.argv[1])
from preflight import check_cluster, check_host
node = check_cluster(*check_host('control'), profile='control')
assert any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"]), "Node is not Ready"
address = next(a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP")
assert ipaddress.ip_address(address).version == 4
print(node["metadata"]["name"], address)
PY
)
read -r node node_ip <<< "$identity"
[[ $(kubectl -n default get service kubernetes -o jsonpath='{.spec.clusterIP}') == 10.53.0.1 ]] || die 'Wrong control service CIDR'
kubectl get ciliumnode "$node" -o json | python3 -c '
import ipaddress, json, sys
cidrs = json.load(sys.stdin)["spec"]["ipam"]["podCIDRs"]
assert cidrs and all(ipaddress.ip_network(c).subnet_of(ipaddress.ip_network("10.52.0.0/16")) for c in cidrs), "Wrong Cilium allocation"
'
kubectl -n kube-system rollout status daemonset/cilium --timeout="$WAIT_TIMEOUT"
kubectl -n kube-system rollout status deployment/cilium-operator --timeout="$WAIT_TIMEOUT"
kubectl -n kube-system exec daemonset/cilium -c cilium-agent -- cilium-dbg status --brief
kubectl -n kube-system rollout status deployment/coredns --timeout="$WAIT_TIMEOUT"
argo=$(kubectl -n argocd get deployment,statefulset -o name)
[[ $argo == *statefulset.apps/argocd-application-controller* && $argo == *deployment.apps/argocd-server* ]] || die 'Expected Argo installation'
while IFS= read -r resource; do
  kubectl -n argocd rollout status "$resource" --timeout="$WAIT_TIMEOUT"
done <<< "$argo"

namespace='' namespace_uid=''
cleanup_namespace() {
  [[ -n $namespace ]] || return 0
  local current_uid
  current_uid=$(kubectl get namespace "$namespace" --ignore-not-found -o jsonpath='{.metadata.uid}') || return 1
  [[ -z $current_uid ]] || {
    [[ $current_uid == "$namespace_uid" ]] || { log 'Namespace ownership changed; refusing cleanup'; return 1; }
    kubectl delete namespace "$namespace" --wait=true --timeout=90s || return 1
  }
  namespace=''
}
finish() {
  local result=$?
  trap - EXIT
  (( result == 0 )) || diagnostics
  cleanup_namespace || result=1
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
created=$(kubectl create -f - -o 'jsonpath={.metadata.name}{" "}{.metadata.uid}' <<'JSON'
{"apiVersion":"v1","kind":"Namespace","metadata":{"generateName":"railshot-cilium-check-"}}
JSON
)
read -r namespace namespace_uid <<< "$created"
WORKLOAD_NAMESPACE=$namespace
log "Testing in owned namespace $namespace"

# Reuse the version policy; no package installation or tool download.
python3 - "$ROOT_DIR/airgap/versions.json" "$namespace" "$node" <<'PY' | kubectl -n "$namespace" apply -f -
import json, sys
policy = json.load(open(sys.argv[1]))
ns = sys.argv[2]
web = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "web", "labels": {"app": "web"}},
       "spec": {"automountServiceAccountToken": False, "containers": [{"name": "web", "image": policy["sample_image"],
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
                "readinessProbe": {"httpGet": {"path": "/", "port": 80}, "periodSeconds": 2},
                "securityContext": {"allowPrivilegeEscalation": False, "seccompProfile": {"type": "RuntimeDefault"}}}]}}
items = [web,
    {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "web"}, "spec": {"type": "NodePort", "externalTrafficPolicy": "Local",
     "selector": {"app": "web"}, "ports": [{"port": 80, "targetPort": 80}]}},
    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "probe"},
     "rules": [{"apiGroups": [""], "resources": ["pods"], "resourceNames": ["web"], "verbs": ["get"]}]},
    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "probe"},
     "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "probe"},
     "subjects": [{"kind": "ServiceAccount", "name": "default", "namespace": ns}]}]
for name in ("allowed", "denied"):
    items.append({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "labels": {"client": name}},
        "spec": {"automountServiceAccountToken": name == "allowed", "activeDeadlineSeconds": 1200,
            "containers": [{"name": "probe", "image": policy["health_image"], "command": ["sh", "-c", "sleep 1200"],
                "resources": {"requests": {"cpu": "5m", "memory": "8Mi"}, "limits": {"cpu": "100m", "memory": "32Mi"}},
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}}]}})
for item in items:
    if item['kind'] == 'Pod':
        item['spec']['affinity'] = {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
            'nodeSelectorTerms': [{'matchFields': [{'key': 'metadata.name', 'operator': 'In', 'values': [sys.argv[3]]}]}]}}}
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
PY
kubectl -n "$namespace" wait pod --all --for=condition=Ready --timeout="$WAIT_TIMEOUT"
service_url="http://web.$namespace.svc.cluster.local"
http_ok() {
  kubectl -n "$namespace" exec "$1" -- curl --noproxy '*' --fail --silent --show-error \
    --connect-timeout 2 --max-time 3 --output /dev/null "$service_url"
}
http_blocked() {
  # Only an HTTP connection timeout is a denial; exec/Pod/DNS errors are failures.
  kubectl -n "$namespace" exec "$1" -- sh -c '
    curl --noproxy "*" --fail --silent --connect-timeout 2 --max-time 3 --output /dev/null "$1"
    test "$?" -eq 28
  ' probe "$service_url" 2>/dev/null
}
await_check() {
  local deadline=$((SECONDS + 90))
  until "$@"; do
    (( SECONDS < deadline )) || die "Timed out: $*"
    sleep 2
  done
}
for client in allowed denied; do await_check http_ok "$client"; done
log 'PASS: both clients resolve cluster DNS and reach Service HTTP'
kubectl -n "$namespace" exec allowed -- sh -ec '
  sa=/var/run/secrets/kubernetes.io/serviceaccount
  printf "header = \"Authorization: Bearer %s\"\n" "$(cat "$sa/token")" |
    curl --config - --noproxy "*" --fail --silent --show-error --connect-timeout 3 --max-time 10 \
      --cacert "$sa/ca.crt" --output /dev/null "https://kubernetes.default.svc/api/v1/namespaces/$(cat "$sa/namespace")/pods/web"
'
log 'PASS: Pod ServiceAccount-authenticated Kubernetes API request with CA verification'
node_port=$(kubectl -n "$namespace" get service web -o jsonpath='{.spec.ports[0].nodePort}')
await_check curl --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 3 --output /dev/null "http://$node_ip:$node_port"
log 'PASS: host -> InternalIP:NodePort, externalTrafficPolicy=Local (external gateway path remains manual)'
# Use the already-proven Service address for policy checks so a DNS timeout
# cannot masquerade as policy denial.
service_url="http://$(kubectl -n "$namespace" get service web -o jsonpath='{.spec.clusterIP}')"
for client in allowed denied; do await_check http_ok "$client"; done
kubectl -n "$namespace" apply -f - <<'JSON'
{"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy","metadata":{"name":"web-ingress"},"spec":{"podSelector":{"matchLabels":{"app":"web"}},"policyTypes":["Ingress"],"ingress":[]}}
JSON
await_check http_blocked allowed
await_check http_blocked denied
kubectl -n "$namespace" apply -f - <<'JSON'
{"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy","metadata":{"name":"web-ingress"},"spec":{"podSelector":{"matchLabels":{"app":"web"}},"policyTypes":["Ingress"],"ingress":[{"from":[{"podSelector":{"matchLabels":{"client":"allowed"}}}],"ports":[{"protocol":"TCP","port":80}]}]}}
JSON
await_check http_ok allowed
await_check http_blocked denied
log 'PASS: ingress deny for both proven clients, then label-selected allow with other client still denied'
cleanup_namespace

# Snapshot only, not a capacity verdict; compare against the pre-change baseline.
kubectl get --raw "/api/v1/nodes/$node/proxy/stats/summary" | python3 -c '
import json, sys
node = json.load(sys.stdin)["node"]
memory = node["memory"]
print(json.dumps({"node": node["nodeName"], "measured_at": memory["time"], "working_set_bytes": memory["workingSetBytes"], "available_bytes": memory["availableBytes"]}))
'
log 'PASS: Cilium/CoreDNS/Argo rollouts and platform-node regression; cross-node/Docker isolation, Argo registered-cluster API, ALB/WireGuard and sustained memory headroom require runbook acceptance'
