#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/common.sh"
CILIUM=/usr/local/lib/railshot-deployment/cilium
[[ -x $CILIUM ]] || die 'Railshot Cilium CLI가 없습니다.'
"$CILIUM" status --wait --wait-duration "$WAIT_TIMEOUT"
kubectl wait --for=condition=Ready nodes --all --timeout="$WAIT_TIMEOUT"
nodes=$(kubectl get nodes -o name)
[[ $(printf '%s\n' "$nodes" | wc -l) -eq 1 ]] || die '현재 runtime은 단일 K3s 서버만 지원합니다.'
kubectl -n kube-system rollout status deployment/coredns --timeout="$WAIT_TIMEOUT"
