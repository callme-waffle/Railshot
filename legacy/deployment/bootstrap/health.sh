#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/common.sh"
[[ -x /usr/local/bin/k3s && -s $KUBECONFIG ]] || die 'K3s가 설치되지 않았습니다.'
grep -Fxq '# Managed by Railshot deployment runtime. Dedicated single-node server only.' /etc/rancher/k3s/config.yaml || die 'Railshot runtime이 관리하는 K3s가 아닙니다.'
actual=$(/usr/local/bin/k3s --version | awk 'NR==1 {print $3}')
[[ $actual == "$K3S_VERSION" ]] || die "K3s version mismatch: $actual != $K3S_VERSION"
kubectl get --raw=/readyz
