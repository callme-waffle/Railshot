#!/usr/bin/env bash
# 각 단계가 독립 실행돼도 동일한 설정과 실패 로그를 사용한다.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export ROOT_DIR
export PATH="/usr/local/bin:$PATH"
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
export K3S_VERSION=${K3S_VERSION:-v1.34.11+k3s1}
export CILIUM_VERSION=${CILIUM_VERSION:-1.20.2}
export CILIUM_CLI_VERSION=${CILIUM_CLI_VERSION:-v0.20.1}
export WAIT_TIMEOUT=${WAIT_TIMEOUT:-300s}
export WORKLOAD_NAMESPACE=${WORKLOAD_NAMESPACE:-railshot-demo}
STEP=${STEP:-$(basename -- "$0" .sh)}
TEMP_DIR=''
log() { printf '[%s] [%s] %s\n' "$(date -u +%FT%TZ)" "$STEP" "$*" >&2; }
die() { log "ERROR: $*" >&2; exit 1; }
kubectl() { /usr/local/bin/k3s kubectl --request-timeout=30s "$@"; }
valid_ipv4() {
  local part
  local -a parts
  [[ $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  IFS=. read -r -a parts <<< "$1"
  for part in "${parts[@]}"; do
    [[ ${#part} -le 3 ]] && (( 10#$part <= 255 )) || return 1
  done
}
download() { curl --fail --show-error --location --retry 3 --connect-timeout 10 --max-time 180 "$1" -o "$2"; }
diagnostics() {
  # Secret과 환경변수는 수집하지 않는다. API 장애 때도 명령별 대기를 제한한다.
  [[ $(uname -s) == Linux && -x /usr/local/bin/k3s && -s $KUBECONFIG ]] || return 0
  log '실패 원인 진단: Pod 상태·Service·최근 이벤트' >&2
  /usr/local/bin/k3s kubectl --request-timeout=5s get pods -A -o wide >&2 || true
  /usr/local/bin/k3s kubectl --request-timeout=5s -n "$WORKLOAD_NAMESPACE" get service,endpointslices -o wide >&2 || true
  /usr/local/bin/k3s kubectl --request-timeout=5s -n "$WORKLOAD_NAMESPACE" get pods \
    -o 'jsonpath={range .items[*]}{.metadata.name}{"\n"}{range .status.containerStatuses[*]}{.name}{": "}{.state.waiting.reason}{" "}{.state.waiting.message}{"\n"}{end}{end}' >&2 || true
  /usr/local/bin/k3s kubectl --request-timeout=5s get events -A --sort-by=.lastTimestamp 2>&1 | tail -n 25 >&2 || true
}
on_exit() {
  local code=$?
  [[ -z $TEMP_DIR ]] || rm -rf -- "$TEMP_DIR"
  if (( code != 0 )); then
    log "FAILED: 단계=$STEP, exit=$code. JSON error와 로그의 원인을 확인한 뒤 runtime 명령을 재실행하세요." >&2
    diagnostics
    log '진단: journalctl -u k3s -n 100; k3s kubectl get pods -A -o wide; k3s kubectl get events -A --sort-by=.lastTimestamp' >&2
  fi
}
trap 'log "ERROR: 단계=$STEP, line=$LINENO, exit=$?" >&2' ERR
trap on_exit EXIT
