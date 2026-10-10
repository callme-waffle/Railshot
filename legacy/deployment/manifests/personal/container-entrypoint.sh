#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ $(id -u) == 0 ]] || { echo 'Gateway initialization requires container root.' >&2; exit 1; }
python3 /opt/railshot/deployment/manifests/personal/container-init.py
python3 /opt/railshot/deployment/scripts/personal_wireguard.py \
  --config /etc/railshot-personal-gateway/config.json --restore
export HOME=/home/railshot
# No no-new-privileges flag: the reviewed setuid sudo wrapper must be able to
# grant NET_ADMIN to its fixed helper. API effective/ambient capabilities are zero.
setpriv --reuid=railshot --regid=railshot --init-groups --inh-caps=-all --ambient-caps=-all \
  python3 -m http.server 4184 --bind 0.0.0.0 --directory /opt/railshot/public &
exec setpriv --reuid=railshot --regid=railshot --init-groups --inh-caps=-all --ambient-caps=-all \
  node /opt/railshot/apps/api/src/server.js
