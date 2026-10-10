#!/bin/bash
# Run on the dedicated worker, after infrastructure/ansible/ci.yml succeeds.
set -euo pipefail
test "$(id -u)" = 0 || { echo 'Run as root on the dedicated CI VM.' >&2; exit 2; }
. /etc/os-release
test "$ID:$VERSION_ID" = ubuntu:24.04 || { echo 'Ubuntu 24.04 is required.' >&2; exit 2; }
marker=dedicated-ci-ubuntu-24.04
case "${1:-}" in
  --k3s-build-worker)
    test "$#" = 1
    test -d /var/lib/rancher/k3s/agent
    test ! -L /var/lib/rancher/k3s/agent
    test ! -e /var/lib/rancher/k3s/server
    test ! -L /var/lib/rancher/k3s/server
    if systemctl is-active --quiet k3s; then echo 'Refusing a control-plane node.' >&2; exit 2; fi
    systemctl is-active --quiet k3s-agent
    test ! -L /etc/rancher/k3s/config.yaml
    test ! -e /etc/rancher/k3s/config.yaml.d
    test ! -L /etc/rancher/k3s/config.yaml.d
    test "$(stat -c '%u:%a' /etc/rancher/k3s/config.yaml)" = '0:600'
    # Operator-owned agent config must select the isolated role at registration.
    python3 - <<'PY'
from pathlib import Path
import yaml
c = yaml.safe_load(Path('/etc/rancher/k3s/config.yaml').read_text())
assert 'railshot.io/node-role=build' in c.get('node-label', [])
assert 'railshot.io/dedicated=build:NoSchedule' in c.get('node-taint', [])
assert not c.get('docker'), 'K3s uses containerd; the CI Docker daemon is separate'
PY
    marker=ops-k3s-build-worker-ubuntu-24.04 ;;
  '')
    for path in /etc/rancher/k3s /var/lib/rancher/k3s /etc/kubernetes /var/lib/kubelet; do
      if [ -e "$path" ] || [ -L "$path" ]; then
        echo "Refusing an unapproved Kubernetes host: $path" >&2; exit 2
      fi
    done ;;
  *) echo 'Supported argument: --k3s-build-worker; omit for standalone Compose.' >&2; exit 2 ;;
esac
test -f /etc/railshot/ci-executor.yaml
/usr/local/sbin/railshot-ci-network --check
for path in /var/lib/railshot-runner /var/lib/railshot-runner/work /var/lib/railshot-runner/runs /var/lib/railshot-runner/codex; do
  test ! -L "$path" || { echo "Refusing symlink: $path" >&2; exit 2; }
  install -d -o root -g root -m 0700 "$path"
done
install -d -o root -g root -m 0700 /var/lib/railshot-runner/work/_temp
# These empty host directories let container preflight see any later K3s install.
install -d -o root -g root -m 0755 /etc/rancher /var/lib/rancher
# Install only on the dedicated host accepted above. Missing profiles fail Pod creation.
runner_source=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v apparmor_parser >/dev/null
for profile in /etc/apparmor.d/railshot-codex-bwrap /etc/railshot/runner-seccomp.json; do
  test ! -L "$profile" || { echo "Refusing symlink: $profile" >&2; exit 2; }
done
install -o root -g root -m 0644 "$runner_source/railshot-codex-bwrap.apparmor" /etc/apparmor.d/railshot-codex-bwrap
apparmor_parser -r /etc/apparmor.d/railshot-codex-bwrap
install -o root -g root -m 0644 "$runner_source/railshot-codex-bwrap.json" /etc/railshot/runner-seccomp.json
if [ "$marker" = ops-k3s-build-worker-ubuntu-24.04 ]; then
  test ! -L /var/lib/kubelet/seccomp
  install -d -o root -g root -m 0755 /var/lib/kubelet/seccomp
  test ! -L /var/lib/kubelet/seccomp/railshot-codex-bwrap.json
  install -o root -g root -m 0644 "$runner_source/railshot-codex-bwrap.json" /var/lib/kubelet/seccomp/railshot-codex-bwrap.json
fi
printf '%s\n' "$marker" > /etc/railshot/ci-runner-host
chown root:root /etc/railshot/ci-runner-host
chmod 0644 /etc/railshot/ci-runner-host
echo 'Build host prepared. Supply a fresh registration token before creating the runner.'
