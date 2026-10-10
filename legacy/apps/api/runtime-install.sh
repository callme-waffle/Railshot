#!/usr/bin/env bash
# Invoke only in the Dockerfile's api stage as root; the mcp stage stays Node-only.
set -Eeuo pipefail
[[ ${RAILSHOT_CONTAINER_BUILD:-} == 1 && $(id -u) == 0 && $(uname -s) == Linux && $(uname -m) == x86_64 ]] || {
  printf '%s\n' 'API image build on Linux amd64 as root is required.' >&2; exit 1;
}
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl unzip git openssh-client openssl \
  python3 python3-venv groff-base less
python3 -m venv /opt/railshot-python
python3 "$ROOT/apps/api/runtime-smoke.py" --requirements > /tmp/railshot-runtime-requirements.txt
/opt/railshot-python/bin/python -m pip install --no-cache-dir --disable-pip-version-check \
  -r /tmp/railshot-runtime-requirements.txt
# Invoke the interpreter inside its venv; an external symlink loses pyvenv.cfg.
export PATH="/opt/railshot-python/bin:$PATH"
TASK_TMP="$(mktemp -d)"
trap 'rm -rf -- "$TASK_TMP"; rm -f /tmp/railshot-runtime-requirements.txt' EXIT
python3 "$ROOT/apps/api/runtime-smoke.py" --downloads > "$TASK_TMP/downloads.tsv"
while IFS=$'\t' read -r filename url checksum; do
  curl --fail --show-error --location --proto '=https' --proto-redir '=https' \
    --retry 3 --connect-timeout 10 --max-time 300 "$url" -o "$TASK_TMP/$filename"
  printf '%s  %s\n' "$checksum" "$TASK_TMP/$filename" | sha256sum --check --strict
done < "$TASK_TMP/downloads.tsv"
# No downloaded program is executed until every artifact has passed its pinned checksum.
unzip -q "$TASK_TMP/terraform.zip" -d "$TASK_TMP/terraform"
install -m 0755 "$TASK_TMP/terraform/terraform" /usr/local/bin/terraform
install -m 0755 "$TASK_TMP/kubectl" /usr/local/bin/kubectl
unzip -q "$TASK_TMP/aws.zip" -d "$TASK_TMP"
"$TASK_TMP/aws/install" --install-dir /opt/aws-cli --bin-dir /usr/local/bin
dpkg -i "$TASK_TMP/ssm.deb"
tar -xzf "$TASK_TMP/gcloud.tar.gz" -C /opt
ln -s /opt/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud
rm -rf /var/lib/apt/lists/*
python3 "$ROOT/apps/api/runtime-smoke.py"
