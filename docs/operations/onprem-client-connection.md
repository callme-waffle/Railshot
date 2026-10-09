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

## Current removal limit

New installations also protect the service user's revocation password with the server-assisted key. The existing local removal helper cannot decrypt that password by itself and therefore stops with `identity_revocation_unlock_required`, leaving the credential and local state in place. A backend-authorized revocation job must be added before unattended removal of these new installations is enabled. This fail-closed behavior avoids storing a second locally usable OpenStack credential.
