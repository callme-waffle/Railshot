#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/common.sh"
# Callers hold /run/railshot-deployment.lock. No argument is the customer profile.
[[ $# -le 1 ]] || die 'usage: install.sh [customer|control]'
profile=${1:-customer}
pod_cidr=$(python3 "$ROOT_DIR/cilium/preflight.py" "$profile")
TEMP_DIR=$(mktemp -d)
case $(uname -m) in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) die '지원하지 않는 CPU' ;; esac
# 전역 cilium 바이너리를 덮어쓰지 않고 PoC 전용 경로에 checksum 검증 후 설치.
CILIUM=/usr/local/lib/railshot-deployment/cilium
if [[ ! -x $CILIUM ]] || [[ $("$CILIUM" version --client | awk '/cilium-cli:/ {print $2}') != "$CILIUM_CLI_VERSION" ]]; then
  [[ ${RAILSHOT_OFFLINE:-false} != true ]] || die 'offline Cilium CLI가 preload되지 않았습니다.'
  archive="cilium-linux-$arch.tar.gz"
  url="https://github.com/cilium/cilium-cli/releases/download/$CILIUM_CLI_VERSION/$archive"
  download "$url" "$TEMP_DIR/$archive"
  download "$url.sha256sum" "$TEMP_DIR/$archive.sha256sum"
  (cd "$TEMP_DIR" && sha256sum --check "$archive.sha256sum")
  tar -xzf "$TEMP_DIR/$archive" -C "$TEMP_DIR" cilium
  install -d -m 755 /usr/local/lib/railshot-deployment
  install -m 755 "$TEMP_DIR/cilium" "$CILIUM"
fi
options=(--version "$CILIUM_VERSION" --namespace kube-system
  --set kubeProxyReplacement=false
  --set ipam.mode=cluster-pool
  --set "ipam.operator.clusterPoolIPv4PodCIDRList=$pod_cidr"
  --set operator.replicas=1
  --set routingMode=tunnel --set tunnelProtocol=vxlan
  --set hubble.enabled=false)
if [[ -n ${RAILSHOT_BUNDLE:-} ]]; then
  # Verify again before extracting executable installation templates.
  python3 "$ROOT_DIR/airgap/scripts/bundle.py" verify --bundle "$RAILSHOT_BUNDLE" \
    --manifest-sha256 "$RAILSHOT_BUNDLE_SHA256" >/dev/null
  python3 - "$ROOT_DIR" "$RAILSHOT_BUNDLE" "$TEMP_DIR" "${RAILSHOT_OFFLINE:-false}" <<'PY'
import json, sys, tarfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from airgap.scripts.bundle import cilium_values, safe_archive
root, destination = Path(sys.argv[2]), Path(sys.argv[3])
manifest = json.loads((root/'bundle-manifest.json').read_text())
with tarfile.open(root/manifest['files']['cilium_chart']['path']) as archive:
    safe_archive(archive)
    archive.extractall(destination)
(destination/'values.json').write_text(json.dumps(cilium_values(None, sys.argv[4] == 'true')))
PY
  options+=(--chart-directory "$TEMP_DIR/cilium" --values "$TEMP_DIR/values.json")
else
  [[ ${RAILSHOT_OFFLINE:-false} != true ]] || die 'offline 설치에는 로컬 Cilium chart가 필요합니다.'
  # Pinned image digests from the tested profile, even when online.
  python3 - "$ROOT_DIR/airgap/versions.json" "$TEMP_DIR/values.json" <<'PY'
import json, sys
from pathlib import Path
images = json.loads(Path(sys.argv[1]).read_text())['cilium_images']
keys = {'agent': 'image', 'operator': 'operator.image', 'envoy': 'envoy.image'}
Path(sys.argv[2]).write_text(json.dumps({keys[k]+'.override': value for k, value in images.items()}))
PY
  # --set flags are needed for dotted keys; values JSON uses nested Helm objects below.
  while IFS= read -r setting; do options+=(--set-string "$setting"); done < <(python3 - "$TEMP_DIR/values.json" <<'PY'
import json, sys
for key, value in json.load(open(sys.argv[1])).items(): print(key+'='+value)
PY
  )
fi
# CLI 설치는 Helm release를 만든다. API 조회 실패를 '미설치'로 오인하지 않는다.
release=$(kubectl -n kube-system get secrets -l owner=helm,name=cilium -o name)
if [[ -n $release ]]; then
  log "Cilium Helm release 재조정: $CILIUM_VERSION"
  "$CILIUM" upgrade "${options[@]}"
else
  log "공식 Cilium CLI로 설치: $CILIUM_VERSION"
  "$CILIUM" install "${options[@]}"
fi
"$CILIUM" status --wait --wait-duration "$WAIT_TIMEOUT"
kubectl wait --for=condition=Ready nodes --all --timeout="$WAIT_TIMEOUT"
# Recheck the same profile after rollout; a newly joined arbitrary node must
# not pass merely because every node became Ready.
python3 "$ROOT_DIR/cilium/preflight.py" "$profile" >/dev/null
kubectl -n kube-system rollout status deployment/coredns --timeout="$WAIT_TIMEOUT"
kubectl get nodes -o wide
kubectl -n kube-system get pods -l k8s-app=cilium -o wide
