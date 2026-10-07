#!/bin/sh
set -eu
umask 077

: "${PERSONAL_GATEWAY_ENDPOINT:?set the reviewed public HOST:51820 endpoint}"
config=/var/lib/railshot-personal-gateway/config.json
if [ -z "${PERSONAL_GATEWAY_RPC_BIND:-}" ]; then
  socket_dir=/run/railshot-personal-gateway
  mkdir -p "$socket_dir"
  chmod 0750 "$socket_dir"
fi

python3 /opt/railshot/deployment/scripts/personal_gateway_service.py \
  --initialize --config "$config" --endpoint "$PERSONAL_GATEWAY_ENDPOINT"
python3 /opt/railshot/deployment/scripts/personal_wireguard.py \
  --config "$config" --restore >/dev/null
exec python3 /opt/railshot/deployment/scripts/personal_gateway_service.py \
  --serve --config "$config"
