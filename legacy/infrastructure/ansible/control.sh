#!/usr/bin/env bash
# Dedicated operator server. User workloads are installed by runtime.yml elsewhere.
set -euo pipefail
[[ $(id -u) == 0 && $(uname -s) == Linux && $(uname -m) == x86_64 ]] || exit 2
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
refuse() { echo "$* See docs/operations/control-cilium-migration.md" >&2; exit 2; }
exec 9>/run/railshot-control.lock
flock -n 9
exec 8>/run/railshot-deployment.lock
flock -n 8
# The operator entrypoint is online and version-pinned; do not inherit installer overrides.
[[ -z $(env | grep -E '^(K3S_|INSTALL_K3S_|CILIUM_|RAILSHOT_BUNDLE|RAILSHOT_OFFLINE|NODE_IP=)' || true) ]] || refuse 'Unset runtime installer overrides.'
role=/etc/railshot/node-role
if [[ -e $role ]]; then
  [[ $(cat "$role") == control ]] || refuse 'Existing non-control node.'
fi
if [[ -e /etc/rancher/k3s || -e /var/lib/rancher/k3s || -e /etc/systemd/system/k3s.service ]] || command -v k3s >/dev/null; then
  [[ -f $role && $(cat "$role") == control && -f /etc/rancher/k3s/config.yaml ]] || refuse 'Existing unowned cluster.'
fi
[[ ! -e /etc/rancher/k3s/config.yaml.d ]] || refuse 'Unsupported K3s config.yaml.d.'
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/config.yaml" <<'YAML'
flannel-backend: none
disable-network-policy: true
cluster-cidr: 10.52.0.0/16
service-cidr: 10.53.0.0/16
write-kubeconfig-mode: "0600"
disable:
  - traefik
  - servicelb
  - metrics-server
YAML
if [[ -f /etc/rancher/k3s/config.yaml ]]; then
  cmp -s "$tmp/config.yaml" /etc/rancher/k3s/config.yaml || refuse 'Existing config differs (including Flannel). Automatic CNI migration is refused.'
fi
# Refuse stale Flannel/foreign CNI before writing configuration or starting K3s.
python3 - "$repo" <<'PY'
import sys
sys.path.insert(0, sys.argv[1] + '/deployment/cilium')
from preflight import check_cni
check_cni()
PY
if command -v k3s >/dev/null; then
  [[ $(k3s --version | awk 'NR==1 {print $3}') == v1.34.11+k3s1 ]] || refuse 'Existing K3s version differs.'
fi
bash "$repo/deployment/bootstrap/preflight.sh"
install -d -m 0755 /etc/railshot /etc/rancher/k3s
install -m 0600 "$tmp/config.yaml" /etc/rancher/k3s/config.yaml
printf 'control\n' > "$role"
chmod 0644 "$role"
if ! command -v k3s >/dev/null; then
  curl -fsSL --proto '=https' --tlsv1.2 --max-time 180 https://get.k3s.io -o "$tmp/install.sh"
  echo "e5cc3b3d9dfc1662c2d9be6da5abc9a4cd317d6abc3a5ffc02e3dd3248207fee  $tmp/install.sh" | sha256sum -c -
  INSTALL_K3S_VERSION=v1.34.11+k3s1 INSTALL_K3S_EXEC=server sh "$tmp/install.sh"
fi
systemctl enable --now k3s
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
for attempt in {1..60}; do
  k3s kubectl --request-timeout=5s get --raw=/readyz >/dev/null 2>&1 && break
  sleep 3
done
k3s kubectl --request-timeout=10s get --raw=/readyz >/dev/null
# NodeReady/CoreDNS require a CNI; the API can become ready without one.
bash "$repo/deployment/cilium/install.sh" control
k3s kubectl create namespace argocd --dry-run=client -o yaml | k3s kubectl apply -f -
curl -fsSL --proto '=https' --tlsv1.2 --max-time 180 \
  https://raw.githubusercontent.com/argoproj/argo-cd/v3.5.3/manifests/install.yaml -o "$tmp/argocd.yaml"
echo "7efe2d6bbc03f63623640f1e4198f16c84009d510fb810ef71e56df1b7614ba9  $tmp/argocd.yaml" | sha256sum -c -
k3s kubectl apply --server-side -n argocd -f "$tmp/argocd.yaml"
for resource in deployment/argocd-repo-server deployment/argocd-server statefulset/argocd-application-controller; do
  k3s kubectl -n argocd rollout status "$resource" --timeout=600s
done
k3s kubectl get nodes -o wide
k3s kubectl -n argocd get pods -o wide
