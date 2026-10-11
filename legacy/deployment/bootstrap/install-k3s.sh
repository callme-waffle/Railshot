#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/common.sh"
TEMP_DIR=$(mktemp -d)
config=/etc/rancher/k3s/config.yaml
cat > "$TEMP_DIR/config.yaml" <<'YAML'
# Managed by Railshot deployment runtime. Dedicated single-node server only.
flannel-backend: none
disable-network-policy: true
cluster-cidr: 10.42.0.0/16
service-cidr: 10.43.0.0/16
write-kubeconfig-mode: "0600"
disable:
  - traefik
  - servicelb
  - metrics-server
  - local-storage
YAML
if [[ -n ${NODE_IP:-} ]]; then
  printf 'node-ip: "%s"\n' "$NODE_IP" >> "$TEMP_DIR/config.yaml"
fi
# Native health registration owns this one optional extension. Preserve only its
# exact reviewed config on repeat installs; arbitrary API arguments still fail cmp.
if [[ -f $config && -f /etc/rancher/k3s/runtime-healthz-authentication.json ]]; then
  python3 - "$ROOT_DIR/bootstrap/runtime-healthz.py" "$TEMP_DIR/config.yaml" "$config" <<'PY'
import json, pathlib, runpy, sys
health = runpy.run_path(sys.argv[1])
expected = pathlib.Path(sys.argv[2])
live = pathlib.Path(sys.argv[3]).read_bytes()
auth = pathlib.Path(health['AUTH_PATH'])
if (not auth.is_symlink() and json.loads(auth.read_text()) == health['AUTH']
        and live == health['desired_config'](expected.read_bytes())):
    expected.write_bytes(live)
PY
fi
if [[ -e $config ]]; then
  cmp -s "$TEMP_DIR/config.yaml" "$config" || die '기존 K3s 설정과 다릅니다. 기존 클러스터/CNI를 자동으로 변경하지 않습니다. 전용 노드를 사용하세요.'
else
  if [[ -e /var/lib/rancher/k3s || -e /etc/systemd/system/k3s.service ]] || command -v k3s >/dev/null; then
    die '관리하지 않는 기존 K3s가 있습니다. 새 전용 노드를 사용하세요.'
  fi
fi
[[ ! -d /etc/rancher/k3s/config.yaml.d ]] || die 'K3s config.yaml.d가 있습니다. 설정 충돌을 피하도록 전용 노드를 사용하세요.'
install -d -m 755 /etc/rancher/k3s
install -m 600 "$TEMP_DIR/config.yaml" "$config"
if [[ -n ${RAILSHOT_BUNDLE:-} ]]; then
  python3 "$ROOT_DIR/airgap/scripts/bundle.py" stage-k3s --bundle "$RAILSHOT_BUNDLE" \
    --manifest-sha256 "$RAILSHOT_BUNDLE_SHA256"
elif [[ ${RAILSHOT_OFFLINE:-false} == true ]]; then
  die 'offline 설치에는 검증된 bundle이 필요합니다.'
fi
sysctl -w net.ipv4.ip_forward=1
printf 'net.ipv4.ip_forward = 1\n' > /etc/sysctl.d/90-railshot-deployment.conf
if [[ -x /usr/local/bin/k3s ]]; then
  actual=$(/usr/local/bin/k3s --version | awk 'NR==1 {print $3}')
  [[ $actual == "$K3S_VERSION" ]] || die "기존 K3s=$actual, 요청=$K3S_VERSION. 자동 업그레이드는 하지 않습니다."
fi
if [[ ! -x /usr/local/bin/k3s || ! -e /etc/systemd/system/k3s.service ]]; then
  log "공식 installer로 K3s $K3S_VERSION 설치 (kube-proxy 유지)"
  if [[ -n ${RAILSHOT_BUNDLE:-} ]]; then
    installer=$(python3 -c 'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); print(p/json.loads((p/"bundle-manifest.json").read_text())["files"]["installer"]["path"])' "$RAILSHOT_BUNDLE")
    INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_SKIP_SELINUX_RPM=true INSTALL_K3S_EXEC=server sh "$installer"
  else
    # Immutable release tag: latest detection never selects the runtime version.
    download "https://raw.githubusercontent.com/k3s-io/k3s/$K3S_VERSION/install.sh" "$TEMP_DIR/k3s-install.sh"
    INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_EXEC=server sh "$TEMP_DIR/k3s-install.sh"
  fi
else
  log '동일 설정/버전의 K3s 재사용'
fi
systemctl enable --now k3s
log 'Kubernetes API 준비 대기. Node Ready는 Cilium 설치 후 확인합니다.'
deadline=$((SECONDS + ${WAIT_TIMEOUT%s}))
until [[ -s $KUBECONFIG ]] && kubectl get --raw=/readyz >/dev/null 2>&1; do
  (( SECONDS < deadline )) || die 'Kubernetes API 준비 시간 초과'
  sleep 3
done
kubectl get nodes -o wide
