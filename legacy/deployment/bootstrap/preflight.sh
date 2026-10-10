#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
source "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/common.sh"
STEP=preflight
[[ $(uname -s) == Linux ]] || die 'Linux 서버에서 실행해야 합니다.'
[[ $EUID == 0 ]] || die '대상 Linux 노드에서 runtime CLI를 sudo로 실행하세요.'
for command in curl tar sha256sum systemctl ip flock sysctl cmp awk grep; do
  command -v "$command" >/dev/null || die "필수 명령이 없습니다: $command"
done
[[ -d /run/systemd/system ]] || die 'systemd가 PID 1인 Linux 서버가 필요합니다.'
# shellcheck source=/dev/null
source /etc/os-release
case "$ID:$VERSION_ID" in
  ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;;
  *) die "PoC 지원 대상: Ubuntu 22.04/24.04, Debian 12/13 (현재 $ID $VERSION_ID)" ;;
esac
kernel=$(uname -r)
IFS=. read -r major minor _ <<< "$kernel"
(( major > 5 || (major == 5 && minor >= 10) )) || die 'Linux kernel 5.10 이상이 필요합니다.'
case $(uname -m) in x86_64|aarch64) ;; *) die 'amd64/arm64만 지원합니다.' ;; esac
[[ $(awk 'END {print NR}' /proc/swaps) -eq 1 ]] || die 'swap을 끄고 /etc/fstab에서도 비활성화하세요.'
[[ -z ${K3S_URL:-} && -z ${K3S_TOKEN:-} && -z ${K3S_CONFIG_FILE:-} ]] || die 'K3S_URL/K3S_TOKEN/K3S_CONFIG_FILE을 해제하세요. 단일 독립 서버 PoC입니다.'
[[ $WAIT_TIMEOUT =~ ^[1-9][0-9]*s$ ]] || die 'WAIT_TIMEOUT은 300s 같은 형식이어야 합니다.'
[[ $K3S_VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+\+k3s[0-9]+$ ]] || die 'K3S_VERSION 형식 오류'
[[ $CILIUM_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'CILIUM_VERSION 형식 오류'
[[ $CILIUM_CLI_VERSION =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'CILIUM_CLI_VERSION 형식 오류'
if [[ -n ${NODE_IP:-} ]]; then
  valid_ipv4 "$NODE_IP" || die 'NODE_IP은 서버 NIC에 할당된 IPv4여야 합니다.'
  ip -4 -o addr show | awk '{split($4,a,"/"); print a[1]}' | grep -Fxq "$NODE_IP" || die 'NODE_IP이 서버 NIC에 없습니다.'
fi
log 'Linux node prerequisites ready'
