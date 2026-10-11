# Isolated personal WireGuard gateway

`deployment/manifests/personal/gateway.k8s.yaml` runs one gateway Pod, one
private TCP Service, and one public UDP LoadBalancer Service. The gateway is a
separate Pod from `railshot-api`; only this container has `NET_ADMIN` and the
WireGuard private key. Its peer ledger and interface configuration use distinct
durable PVCs declared in the gateway manifest.

Before applying the manifest, build the `personal-gateway` target in
`apps/api/Dockerfile`, replace `BUILD_REQUIRED/personal-gateway` with the pinned
image reference, and provision these operator-owned resources in
`railshot-system`:

- `railshot-personal-gateway-key` Secret: key `private-key`, a 32-byte WireGuard
  private key in base64 text. Keep the same key through Pod restarts.
- `railshot-personal-gateway-ipc` Secret: key `ipc-token`, a random Bearer token
  shared only with the API Pod. The API must read a copy from a private file.
- `railshot-personal-settings` ConfigMap: `gateway_endpoint` must be the stable
  public `HOST:51820` that routes UDP to `railshot-personal-wireguard`. Reserve
  the LoadBalancer address before issuing installer links. The address advertised
  to customers must match the provisioned Service.
- `railshot-personal-vault` Secret: key `private.pem`, an RSA private key of at
  least 3072 bits. Mount it only in the API Pod; its init container copies it to
  private API state before the API process starts.
- `railshot-personal-gateway` and `railshot-personal-wireguard` PVCs from the
  gateway manifest. Retain them during a rollout or restart.

The gateway listens for internal RPC on its Pod IP only. The private Service is
`railshot-personal-gateway.railshot-system.svc.cluster.local:4184`. A
NetworkPolicy admits TCP 4184 only from Pods labeled `app: railshot-api` and
admits public UDP 51820 for WireGuard. Cluster networking must enforce the
NetworkPolicy. The API Pod must remove its old gateway sidecar and Unix-socket
volume, and use a private config like this:

```json
{
  "gateway": {
    "service_host": "railshot-personal-gateway.railshot-system.svc.cluster.local",
    "service_port": 4184,
    "token_file": "/var/lib/railshot/config/personal-gateway-token"
  },
  "vault": {
    "private_key_file": "/var/lib/railshot/config/personal-vault-private.pem"
  }
}
```

Mount the `railshot-personal-vault` RSA private-key Secret **only** in the API Pod. The
gateway does not need or receive OpenStack credentials or the vault key.

The authenticated internal protocol is:

| Operation | Request | Response |
| --- | --- | --- |
| Health | `GET /healthz` | `200 {"status":"ready"}` after native peer and forwarding verification |
| Peer | `POST /requests` with `{action,target_id,generation,public_key}` | Existing tunnel configuration and recent handshake state |
| SSH stream | `CONNECT /streams/<target_id>/<generation>` | `200 Connection Established`, then raw bytes to the registered peer IP, TCP port 2222 |

All calls use `Authorization: Bearer <ipc-token>`. The CONNECT route accepts
only a current, active ledger peer and checks the native WireGuard allowed IP.
It cannot select a destination IP or port. The gateway never interprets SSH or
OpenStack commands; the API performs SSH host-key and command authentication
through the stream. Up to 64 concurrent gateway requests or streams are allowed.
Each stream lives for at most five minutes and checks peer revocation at least
every five seconds. A timeout or unreachable peer does not imply a successful
connection.

The gateway entrypoint restores the persisted peer ledger before serving RPC.
For acceptance, enroll a **real** management host, verify the SSH operation
through the private Service, then restart the gateway and client host and
repeat the operation. The local stream tests verify routing and authorization
logic, but cannot prove an external WireGuard path or OpenStack readiness.
