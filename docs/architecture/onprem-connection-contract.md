# On-premises connection implementation contract

This document defines the first working slice for a personal OpenStack management host. It is an implementation contract, not evidence of a live connection.

## Identity and ownership

- A verified email/password login creates a durable, opaque `account_id`. New personal targets belong to that account. The existing anonymous owner cookie remains a legacy identity and must not silently claim an account's targets.
- Account sessions use their own server-side session and cookie, distinct from anonymous dashboard and MCP sessions. State-changing account and target requests require origin/CSRF protection. Public account registration, verification, login, reset, and logout have bounded rate limits and safe non-enumerating errors.
- Passwords are stored with a salted slow password hash. Email verification is required before a target can be created. Recovery tokens are single-use, hashed at rest, and expire.

## Enrollment

- `POST /api/v1/targets/{id}/enrollments` requires the authenticated owner account. It returns a random 256-bit one-time enrollment token once, `enrollment_id`, `expires_at`, `script_url`, and a pinned script digest. The token expires exactly 30 minutes after issuance. Neither URL nor logs contain the token.
- `GET /api/v1/installers/personal` returns a short, credential-free Bash bootstrap script. It fetches a pinned immutable client release and checks its digest before execution. The user enters the enrollment token at a terminal prompt; no token is embedded in a URL, process argument, or shell history. The existing `/api/v1/installers/openstack` ZIP installer remains a separate contract.
- A claim atomically checks token hash, expiry, revocation, target, account, and generation; consumes the token; binds client WireGuard and SSH public keys; and registers one `/32` peer. The client receives a separate persistent client credential. A failed or uncertain peer mutation remains `attention` for reconciliation and cannot replay the enrollment token.
- Claim happens before lengthy OpenStack identity or runtime preparation. Connection readiness is independent of CI/runtime/deployment readiness. `connection_status=ready` requires a server-initiated tunnel SSH operation and successful OpenStack `server list`; `deployable` remains false until the later runtime checks.

## Gateway

- The WireGuard gateway is a separate Kubernetes Pod with one stable public UDP endpoint, one replica, durable peer/key state, and NET_ADMIN only in the gateway container. The API Pod does not share its network namespace or WireGuard interface.
- Internal authenticated gateway RPCs support peer add/verify/remove and a byte stream limited by a registered `target_id` to the approved management port (initially 2222). The gateway does not parse OpenStack commands, hold OpenStack credentials, or accept arbitrary destination IPs/ports. The API performs end-to-end SSH authentication, host-key checking, and forced-command protocol validation through the gateway stream.
- Initial deployment is a single gateway replica. Peer state reconciliation and a stable endpoint are required before any scaling scheme.

## Customer identity and vault

- The customer installer prompts locally for an OpenStack administrator identity, creates a dedicated project-scoped service user and restricted application credential through the existing OpenStack identity implementation, verifies it, and never sends or persists the administrator password to Railshot.
- The customer client encrypts the application credential with a fresh symmetric data key using authenticated encryption, with target ID, generation, and project ID bound as authenticated context. It wraps that data key with the pinned server public key and stores only ciphertext, wrapped key, metadata, and local ownership evidence. The server private key stays in the backend secret store and never reaches the client or gateway.
- For each approved `job_id`, the backend unwraps the data key and includes the per-job unlock key in the end-to-end authenticated SSH request. The forced command checks target, generation, job ID, allowed action, and replay state before decryption; uses credentials for one command; and removes plaintext temporary files afterward. Credentials and unlock keys never appear in argv, responses, or logs. Python memory erasure cannot be guaranteed; use a short-lived process and a private tmpfs for CLI files.
- Keep current durable intent/ownership and unknown-result semantics for OpenStack mutations. The first end-to-end command is read-only `server list`.

## Acceptance

1. Two login accounts cannot list, enroll, or command each other's target.
2. Enrollment expiry, replay, revocation, wrong generation, and failed peer mutation are handled without falsely reporting connection success.
3. A real management host establishes WireGuard with the separate gateway Pod; after restarts, the peer and approved internal route recover.
4. Backend SSH over the gateway stream executes customer-local `server list` with server-assisted unlock and returns only bounded, sanitized results.
5. `GET /api/v1/targets?scope=owned&provider=openstack` shows `connection_status=ready` only after the real command succeeds, while `deployable=false` until runtime preparation.
