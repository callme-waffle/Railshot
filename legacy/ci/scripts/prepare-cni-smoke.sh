#!/usr/bin/env bash
# GitHub's preinstalled, unused Podman config shares K3s's external-CNI directory.
# Preserve just that known fixture for this disposable job; never relax product guards.
set -Eeuo pipefail
fail() { echo "CNI smoke prerequisite: $*" >&2; exit 1; }
[[ $# == 1 && ($1 == prepare || $1 == restore) ]] || fail 'expected prepare or restore'
[[ ${GITHUB_ACTIONS:-} == true && ${RUNNER_ENVIRONMENT:-} == github-hosted && $(uname -s) == Linux && $(id -u) == 0 ]] || fail 'requires a root process on a GitHub-hosted Linux runner'
[[ ${RUNNER_TEMP:-} == /* && -d $RUNNER_TEMP && ! -L $RUNNER_TEMP ]] || fail 'RUNNER_TEMP must be a real absolute directory'
config=/etc/cni/net.d/87-podman-bridge.conflist
saved="$RUNNER_TEMP/railshot-cni-smoke-podman"
backup="$saved/87-podman-bridge.conflist"
if [[ $1 == restore && ! -e $backup && ! -L $backup ]]; then exit 0; fi
for path in /usr/local/bin/k3s /etc/rancher/k3s /var/lib/rancher/k3s /etc/systemd/system/k3s.service; do
  [[ ! -e $path && ! -L $path ]] || fail "K3s remains at $path; preserving any Podman backup at $backup"
done
if [[ $1 == prepare ]]; then
  [[ ! -e $saved && ! -L $saved ]] || fail "backup already exists: $saved"
  [[ -e $config || -L $config ]] || exit 0
  [[ -f $config && ! -L $config ]] || fail 'known Podman config must be a regular file'
  containers=$(podman ps --all --quiet)
  [[ -z $containers ]] || fail 'Podman containers exist; refusing to move their network config'
  interfaces=$(ip -j link show)
  python3 - "$config" "$interfaces" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
plugins = config.get('plugins', [])
assert config.get('name') == 'podman' and plugins, 'Unexpected Podman network configuration'
assert plugins[0].get('type') == 'bridge' and plugins[0].get('bridge') == 'cni-podman0', 'Unexpected Podman bridge'
assert all(p.get('type') in ('bridge', 'portmap', 'firewall', 'tuning') for p in plugins), 'Unexpected Podman plugin'
assert not any(link['ifname'] == 'cni-podman0' for link in json.loads(sys.argv[2])), 'Podman bridge exists'
PY
  install -d -m 0700 "$saved"
  mv -- "$config" "$backup"
  echo "Preserved unused hosted-runner Podman CNI config at $backup"
else
  [[ -d $saved && ! -L $saved && -f $backup && ! -L $backup ]] || fail 'backup is not a regular owned file'
  [[ ! -e $config && ! -L $config ]] || fail "refusing to overwrite $config; backup remains at $backup"
  install -d -m 0755 /etc/cni/net.d
  mv -- "$backup" "$config"
  rmdir -- "$saved"
  echo 'Restored hosted-runner Podman CNI config'
fi
