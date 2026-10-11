# On-premises client connection slice

The account owner obtains a 30-minute enrollment token and a credential-free installer URL. On the Ubuntu management host, the installer checks the API's connection readiness (`GET /api/v1/readiness?scope=personal&phase=connection`), verifies the pinned release digest, installs prerequisites, and prompts for the token without adding it to the URL or process arguments.

The client claims the token with its WireGuard and SSH public keys **before** asking for OpenStack administrator credentials. A successful claim returns a persistent client credential, target ID, generation, tunnel settings, and a PEM RSA public key with its SHA-256 SPKI fingerprint. The client pins this key in root-only configuration, installs WireGuard, and then prompts locally for the administrator identity. The administrator password is neither saved nor sent to Railshot. Identity creation keeps durable ownership and pending-intent records; an uncertain mutation is blocked for reconciliation on rerun.

The dedicated project service user's password and Application Credential are stored in separate `credentials.vault` files under `/etc/railshot-personal/identity-owner` and `/etc/railshot-personal`. Both use one transient 32-byte data key, independent 12-byte AES-GCM nonces, and authenticated context from canonical JSON with `generation`, `project_id`, and `target_id`. The data key is wrapped with the pinned RSA public key using OAEP SHA-256. No plaintext key is stored on the host. The Application Credential vault is copied with 0600 permissions to the restricted SSH account. Its metadata (`generation`, `project_id`, `key_id`, `wrapped_dek`) is posted with the client credential to `/api/v1/targets/{id}/vaults` to bind the project and let the backend unlock future jobs.

For each backend-approved job, the backend sends a new job ID, target ID, generation, allowed action, arguments, and base64 unlock key in the authenticated SSH request body. The forced command records the job ID before decrypting. It rejects wrong target/generation, malformed actions or arguments, and reused job IDs. The decrypted OpenStack configuration lives only in a private `/dev/shm` temporary directory during the CLI process; stdout and stderr are bounded and sanitized. Python cannot guarantee physical erasure of all in-memory copies, so the command process exits after responding.

Connection readiness requires a backend-initiated `server list` over the tunnel. The client heartbeat alone cannot assert OpenStack readiness because it cannot unlock the vault locally. The installer does not create the later deployment runtime VM.

Existing installations with `credentials.key` and `credentials.enc` continue to accept the earlier four-field SSH request. A `credentials.vault` installation rejects that request before loading credentials; its forced command requires target ID, generation, job ID, action, and a per-job unlock key. New enrollment never creates the legacy key files.

## Running a designated OpenStack connection test

The downloaded short installer reads the enrollment ID and token from `/dev/tty`.
Run it through an interactive terminal or an SSH session with a PTY. Do not put
the token or administrator password in command arguments, shell history, or a
captured terminal transcript. The installer must finish before its terminal is
closed; later heartbeats run through the installed systemd service.

After the token claim, the client asks for an OpenStack v3 authentication URL if
it cannot discover one, then the administrator ID, user domain (`Default`),
administrator project (`admin`), administrator project domain, Railshot project
domain, dedicated service user name, compute member role (`member`), and finally
the administrator password through a hidden terminal prompt. Endpoint discovery
reads the literal URL from known `clouds.yaml` or openrc paths; it never sources
an openrc file. `sudo` can remove `OS_AUTH_URL`, so use the prompt or a private
known configuration file when the endpoint is not discoverable after elevation.

For an explicitly designated development cloud with HTTP Keystone, the test
installer must use `--test-allow-http`. The API's installer configuration needs
`test_allow_http: true` to generate that option in its short script. This setting
is persisted in the local client binding so that reruns and the daemon retain
the same explicit choice. The default installer requires HTTPS.

A failed vault upload can be retried with the same enrollment ID and installed
payload: the saved client credential and encrypted vault are reused, the
one-time token is not claimed again, and no service identity is recreated.
Unknown OpenStack write outcomes require reconciliation before a rerun proceeds.

The customer crypto, upload retry, replay protection, and existing installer
regressions can be run without cloud or OS mutations:

```bash
umask 077
python3 -m pytest \
  apps/agent/tests/test_personal_connection.py \
  apps/agent/tests/test_personal.py \
  apps/agent/tests/test_openstack_control.py \
  ci/tests/test_personal_identity.py -q
```

The installer uses this private umask. The fixtures intentionally undergo the
same ownership and permission checks as production files; a permissive umask
may cause them to fail before exercising their intended behavior. The connection
tests use real RSA-OAEP and AES-GCM encryption and a private `/dev/shm` credential
file, with fake provider calls. A passing result does not prove a live WireGuard
handshake, SSH transport, or OpenStack authorization.

## Checks before and after a live installer run

Use the nested test cloud's Keystone endpoint, not the outer cloud that owns
the test VM. Registration creates or reuses the shared `railshot` project in the
selected domain, creates a new dedicated user, assigns the existing `member`
role, and creates an Application Credential with `unrestricted: false`. It
checks the role before cloud writes. An existing dedicated user name causes
`existing_service_user`; the installer never adopts that user or resets its
password. This connection phase creates no VM, network, floating IP, or runtime
cluster and deletes no OpenStack resource.

Before starting, record these non-secret checks:

- `GET /api/v1/readiness?scope=personal&phase=connection` returns ready.
- The login account owns the target; enrollment expiry is still in the future
  and its generation matches the target. Do not print the enrollment token.
- The downloaded API short script's SHA-256 matches `script_sha256` from the
  enrollment response, and its pinned archive matches the release manifest.
  The static release manifest records paths and hashes, not test options.
- For this HTTP development cloud, the API process has
  `RAILSHOT_PERSONAL_TEST_ALLOW_HTTP=1` and the generated short script passes
  `--test-allow-http` through `install.sh` to the personal client.
- The selected Keystone URL is the expected local `/identity/v3` endpoint;
  administrator/domain/project names are correct and the dedicated service
  name is unused. Endpoint discovery may skip the URL prompt when exactly one
  valid endpoint exists. A driver must match actual prompts rather than send
  answers by position. Discovery prints only the selected public URL.
- The PTY is active, tracing and terminal capture are disabled, and the same
  pinned release is used for any retry. Check that existing Railshot paths are
  owned by the expected installation before proceeding.

After the installer, verify status and permissions without dumping private
configuration or decrypted credentials:

- `wg-quick@railshot0`, `railshot-personal-runtime`, and `railshot-personal` are
  active. `wg show railshot0 latest-handshakes` reports a recent handshake with
  the expected public peer; do not use `wg showconf` or dump the WireGuard file.
- SSH port 2222 listens only on the assigned tunnel address. The dedicated
  SSH account has public-key authentication, a forced OpenStack command, and
  no shell, PTY, forwarding, or root-login access through that listener.
- Client configuration, identity state, both encrypted vaults, and the copied
  SSH-account vault have their expected ownership and private permissions.
  `credentials.key` and `credentials.enc` are absent. Inspect file metadata,
  presence, and selected booleans instead of printing file contents.
- Identity ownership contains the dedicated user, member role assignment, and
  one Application Credential. Identity progress is `complete` after local
  project/token and compute-list verification. Existing resources keep their
  ownership; the shared project and service user are retained on removal.
- The backend has registered the same target/generation/project vault binding
  and executed `server list` through gateway SSH with server-assisted unlock.
  An empty server array is a valid result. The backend verifies the token's
  project before executing the command.
- The owning account's target list shows `connection_status: ready` and
  `deployable: false`; the overall status is normally `preparing` until the
  separate runtime phase. A client heartbeat by itself cannot assert ready.
  Verify a second account cannot read or command the target, and record the
  actual command result without token, password, wrapped key, or unlock key.

Retry according to the durable stage, rather than deleting state:

| Observed failure | Required action |
| --- | --- |
| Claim was never accepted; no client configuration exists | Check expiry/revocation and use a fresh enrollment only if the target remains unclaimed. |
| Claim response was lost after the server consumed the token, before local `client.json` persisted | Reconcile the server target and peer; replaying the token or issuing a replacement to that bound target is refused. Automatic recovery is not implemented. |
| Client configuration exists; tunnel or local service setup failed | Retry the same enrollment ID, API URL, explicit HTTP setting, and pinned release. The stored client credential is reused. |
| Identity intent is pending, or some identity writes succeeded before vault persistence | Reconcile the recorded resource IDs and cloud outcome. The installer blocks instead of repeating unknown writes or adopting an existing user. |
| Encrypted identity is persisted but vault upload failed | Retry with the same configuration and vault. Upload is idempotent for identical metadata; no identity is recreated. |
| Backend connection verification failed or timed out | Preserve state and inspect handshake, listener, forced-command, vault binding, and compute authorization. The daemon retries heartbeats; readiness requires the real backend command to succeed. |

These are code-based expectations. Record observed live outcomes separately and
do not infer tunnel or cloud success from a passing local test.

## Current removal limit

New installations also protect the service user's revocation password with the server-assisted key. The existing local removal helper cannot decrypt that password by itself and therefore stops with `identity_revocation_unlock_required`, leaving the credential and local state in place. A backend-authorized revocation job must be added before unattended removal of these new installations is enabled. This fail-closed behavior avoids storing a second locally usable OpenStack credential.
