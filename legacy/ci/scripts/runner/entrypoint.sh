#!/bin/bash
set -euo pipefail
umask 077
if [ "${1:-}" = --check-image ]; then
  exec python3 /opt/railshot/ci/scripts/runner/container_preflight.py --check-image
fi
if [ "$#" -ne 0 ]; then
  echo 'Supported argument: --check-image; omit to start the ephemeral runner.' >&2
  exit 2
fi
: "${RAILSHOT_RUNNER_URL:?Set the private apps repository URL}"
: "${RAILSHOT_RUNNER_NAME:?Set a unique runner name for this invocation}"
: "${RAILSHOT_RUNNER_LABELS:?Set the labels selected by RAILSHOT_CI_RUNNER_LABELS}"
# Keep the same host/container absolute path for Docker bind mounts, with a
# private checkout/temp directory per ephemeral runner. Never lock the whole host.
[[ "$RAILSHOT_RUNNER_NAME" =~ ^[a-z][a-z0-9-]{0,62}$ ]] || exit 2
work="/var/lib/railshot-runner/work/$RAILSHOT_RUNNER_NAME"
[[ ! -L "$work" ]] || exit 2
mkdir -p "$work"
exec 9>"$work/.runner.lock"
flock -n 9 || { echo 'This runner workspace is already active.' >&2; exit 2; }
export TMPDIR="$work/_temp"
python3 /opt/railshot/ci/scripts/runner/container_preflight.py
# This creates local Buildx connection metadata only. The existing Ansible
# bootstrap owns the BuildKit container, resource bounds and network policy.
builder=$(jq -er '.builder.name' /etc/railshot/ci-executor.yaml)
container=$(jq -er '.builder.container' /etc/railshot/ci-executor.yaml)
docker buildx create --name "$builder" --driver remote "docker-container://$container" >/dev/null
python3 /opt/railshot/ci/scripts/runner/container_preflight.py --check-builder
mkdir -p "$TMPDIR"
token=$(cat /run/secrets/github-runner-registration-token)
if [ -z "$token" ]; then echo 'Empty runner registration token file.' >&2; exit 2; fi
./config.sh --unattended --ephemeral --disableupdate \
  --url "$RAILSHOT_RUNNER_URL" --token "$token" \
  --name "$RAILSHOT_RUNNER_NAME" --labels "$RAILSHOT_RUNNER_LABELS" \
  --work "$work"
unset token
# Keep durable run evidence outside checkout; discard only this runner's
# disposable workspace after the runner has exited, including failed jobs.
./run.sh &
runner_pid=$!
trap 'kill -TERM "$runner_pid" 2>/dev/null || true' TERM INT
set +e
wait "$runner_pid"
result=$?
# A signal interrupts wait before the child exits. Do not remove its workspace
# while it is still finishing cancellation and post-job steps.
if kill -0 "$runner_pid" 2>/dev/null; then wait "$runner_pid"; fi
trap - TERM INT
rm -rf -- "$work"
exit "$result"
