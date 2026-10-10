#!/bin/sh
set -eu
umask 077
token=$(cat "${RAILSHOT_API_TOKEN_FILE:-/run/secrets/api/token}")
case "$token" in ''|*[!a-zA-Z0-9._~-]*) echo 'A private URL-safe API token is required' >&2; exit 1 ;; esac
test "${#token}" -ge 32
upstream=${RAILSHOT_API_UPSTREAM:-railshot-api:4173}
case "$upstream" in ''|*[!a-zA-Z0-9.:-]*) echo 'Invalid API upstream' >&2; exit 1 ;; esac
mcp_upstream=${RAILSHOT_MCP_UPSTREAM:-railshot-mcp:4185}
case "$mcp_upstream" in ''|*[!a-zA-Z0-9.:-]*) echo 'Invalid MCP upstream' >&2; exit 1 ;; esac
# This file is outside the web root and readable only by the Nginx process user.
cat > /tmp/railshot-proxy.conf <<EOF
# Personal clients authenticate with an enrollment or client token. These exact
# routes must preserve that bearer value instead of replacing it with the
# dashboard's operator token.
location ~ ^/api/v1/(enrollments/[A-Za-z0-9._-]+/claims|targets/[A-Za-z0-9._-]+/(heartbeats|receipts|runtimes))$ {
    proxy_pass http://$upstream;
    proxy_http_version 1.1;
    proxy_set_header Host \$http_host;
    proxy_set_header Authorization \$http_authorization;
    proxy_set_header Connection "";
    proxy_request_buffering off;
    proxy_connect_timeout 2s;
    proxy_read_timeout 610s;
    proxy_hide_header X-Powered-By;
}
location /api/ {
    proxy_pass http://$upstream;
    proxy_http_version 1.1;
    proxy_set_header Host \$http_host;
    proxy_set_header Authorization "Bearer $token";
    proxy_set_header Connection "";
    proxy_request_buffering off;
    proxy_connect_timeout 2s;
    proxy_read_timeout 610s;
    proxy_hide_header X-Powered-By;
}
location = /onpremise/install.sh {
    access_log off;
    proxy_pass http://$upstream;
    proxy_http_version 1.1;
    proxy_set_header Host \$http_host;
    proxy_set_header Authorization "";
    proxy_set_header Connection "";
    proxy_hide_header X-Powered-By;
}
location = /mcp {
    proxy_pass http://$mcp_upstream;
    proxy_http_version 1.1;
    proxy_set_header Host \$http_host;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 610s;
}
location /mcp/ {
    proxy_pass http://$mcp_upstream;
    proxy_http_version 1.1;
    proxy_set_header Host \$http_host;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 610s;
}
location /.well-known/oauth- {
    proxy_pass http://$mcp_upstream;
    proxy_http_version 1.1;
    proxy_set_header Host \$http_host;
    proxy_set_header Connection "";
}
EOF
unset token
exec nginx -g 'daemon off;'
