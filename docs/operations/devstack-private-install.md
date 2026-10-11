# Designated DevStack VM installation

This helper applies to the designated `railshot-onprem-e2e` Ubuntu 24.04 host,
fixed IP `172.28.102.160`, and the official DevStack `stable/2025.2` checkout at
`92e864aa017f61e911a8ce3976e828fa5b750ded`. It is a test installation tool under
`ci/scripts`, separate from the Railshot customer installer. It creates no VM,
floating IP, security group, or external cloud resource.

The original DevStack scripts include passwords in shell traces, several CLI
arguments, INI-editing `sed` arguments, generated CLI files, and the final
installation summary. Its shell also creates a broad sudoers rule. Running that
unmodified script would violate this verification session's constraints.

`ci/scripts/devstack-private-install.py` patches twelve files with exact SHA-256
preimage checks and validates the resulting shell/Python syntax before writing
them. It records hashes of the applied patch and its runtime helper. The `check`
and `run` actions refuse a different Git revision, changed patched files, other
tracked changes, custom `localrc`/`local.sh` hooks, additional configuration, an
unreviewed service list, or a credential configuration without private ownership
and 0600 permissions. The patch preserves the existing sudo authorization and
creates no new sudoers or secure-path rule.

The four generated installation passwords remain 48 hexadecimal characters in
the locally prepared private `local.conf`. Do not use the VM's SSH password in
that file. The helper accepts no password argument and does not read the SSH
credential file. Keystone bootstrap receives its password through the supported
`OS_BOOTSTRAP_PASSWORD` environment variable; service-user creation sends the
password from standard input directly to the OpenStack SDK. The database client
uses a private `.my.cnf`, and its password-setting SQL travels through standard
input. INI editing also sends its program through standard input instead of
`sed -e` process arguments. [Keystone bootstrap reference](https://docs.openstack.org/keystone/latest/admin/bootstrap.html)

RabbitMQ receives JSON containing a salted SHA-256 password hash through
`rabbitmqctl import_definitions` standard input, without a path argument or a
temporary file. Ubuntu's root CLI wrapper changes to the `rabbitmq` user, which
cannot read a 0600 file in the installing user's 0700 temporary directory. The
stdin form preserves private permissions and uses the CLI's supported no-path
input mode. Nova cell0 mapping receives a temporary 0600 config in `/dev/shm`;
its database password does not appear in `nova-manage` arguments, and the
temporary file is removed when that command finishes. Generated `.localrc.auto`,
`.stackenv`, `clouds.yaml`, `.my.cnf`, and secret-bearing service configuration
files are restricted before writing the secrets.
[RabbitMQ password hash format](https://www.rabbitmq.com/docs/passwords#computing-password-hashes),
[RabbitMQ definitions import](https://www.rabbitmq.com/docs/definitions)

The stdin behavior was checked against the official
[RabbitMQ 3.12.1 CLI implementation](https://github.com/rabbitmq/rabbitmq-server/blob/v3.12.1/deps/rabbitmq_cli/lib/rabbitmq/cli/ctl/commands/import_definitions_command.ex)
and the wrapper in Ubuntu's `rabbitmq-server_3.12.1-1ubuntu1.6_all.deb` package.

The required non-secret configuration matches the prepared host:

```ini
[[local|localrc]]
DEST=/opt/stack
HOST_IP=172.28.102.160
SERVICE_HOST=172.28.102.160
HOST_IPV6=
IP_VERSION=4
FIXED_RANGE=10.77.0.0/24
FLOATING_RANGE=172.24.4.0/24
PUBLIC_NETWORK_GATEWAY=172.24.4.1
Q_USE_PROVIDERNET_FOR_PUBLIC=False
LIBVIRT_TYPE=kvm
LOGFILE=
LOGDIR=/opt/stack/logs
VERBOSE=True
GLOBAL_VENV=True
DEVSTACK_PARALLEL=False
ENABLE_DEBUG_LOG_LEVEL=False
DATABASE_QUERY_LOGGING=False
MYSQL_GATHER_PERFORMANCE=False
MYSQL_SERVICE_NAME=mariadb
USE_JOURNAL=False
LOG_SUDO=
SYSLOG=False
DEBUG_LIBVIRT=False
DEBUG_LIBVIRT_COREDUMPS=False
GIT_DEPTH=1
DOWNLOAD_DEFAULT_IMAGES=False
IMAGE_URLS=
ENABLED_SERVICES=key,g-api,mysql,rabbit,n-api,n-api-meta,n-cond,n-cpu,n-sch,placement-api,placement-client,q-svc,ovn-controller,ovn-northd,ovs-vswitchd,ovsdb-server,q-ovn-agent,etcd3
```

The private configuration must additionally contain the four locally generated
`ADMIN_PASSWORD`, `DATABASE_PASSWORD`, `SERVICE_PASSWORD`, and `RABBIT_PASSWORD`
assignments. Populate them from the private credential file without printing
them. The listing above is documentation, not a complete executable local.conf.
The destination CIDRs describe the nested test cloud; they do not authorize
changing the outer cloud's VM, networking, floating IP, or security group.

Upload the helper without credentials and use these actions on the designated
host, with its actual checkout path:

```bash
python3 /opt/railshot-onprem-e2e/devstack-private-install.py patch \
  --source /opt/railshot-onprem-e2e/devstack
python3 /opt/railshot-onprem-e2e/devstack-private-install.py check \
  --source /opt/railshot-onprem-e2e/devstack
python3 /opt/railshot-onprem-e2e/devstack-private-install.py run \
  --source /opt/railshot-onprem-e2e/devstack \
  --log /opt/stack/logs/railshot-private-install-1.log
```

Run the last command as the existing `railshot` user. Root execution is rejected.
The output directory must be writable by that user. Each attempt requires a new
log path; the file is created with 0600 permissions and exclusive creation. The
runner captures raw stdout/stderr through a pipe, redacts the four exact
generated password values even when split between reads, and then writes the
sanitized bytes to that file and stdout. A systemd unit may capture this already
sanitized stdout. Do not redirect `stack.sh` to a file, start it directly, enable
shell tracing, or tee its output before the runner. Installation and failure
worlddump logs are disabled, and the required synchronous mode avoids async
worker log files. Existing sudo input/output logging causes execution to stop;
the helper does not change that logging policy.

## Nova rootwrap executable lookup

On the designated VM at 2026-10-09 16:47 UTC, the operator confirmed that
`/opt/stack/data/venv/bin/nova-rootwrap` exists, but root's `command -v
nova-rootwrap` failed and `/usr/local/bin/nova-rootwrap` did not exist. Nova's
`get_root_helper()` uses the bare command `sudo nova-rootwrap`; its compute
process passes that command to privilege separation. The original global-venv
setup links `privsep-helper` into `/usr/local/bin` but omits `nova-rootwrap`.
Keeping sudo's existing secure path therefore requires an executable lookup
repair before the first privileged Nova operation.

The operator may add this one link after confirming that the target is
executable and the destination is absent. These commands contain no credential
values and use the existing sudo authorization:

```bash
set -eu
sudo -n test -x /opt/stack/data/venv/bin/nova-rootwrap
if [ -e /usr/local/bin/nova-rootwrap ] || [ -L /usr/local/bin/nova-rootwrap ]; then
  printf '%s\n' 'Existing nova-rootwrap path requires review; stopping.' >&2
  exit 1
fi
sudo -n ln -sT /opt/stack/data/venv/bin/nova-rootwrap /usr/local/bin/nova-rootwrap
test "$(readlink /usr/local/bin/nova-rootwrap)" = /opt/stack/data/venv/bin/nova-rootwrap
sudo -n sh -c 'command -v nova-rootwrap'
```

GNU `ln -sT` without `--force` refuses an existing destination, including a
directory or dangling symlink, and preserves it if a path appears after the
absence check. Respect any existing path and stop for review; do not replace or
remove it. Successful verification must show root resolves
`/usr/local/bin/nova-rootwrap` and that it points to the designated venv
executable. This host adjustment changes no sudoers rule, helper, audited
DevStack source file, marker, or running installer. Record the actual link and
lookup result separately; the observation above records the missing path,
not completion of the repair.

## Upgrade the previously deployed runtime

The `upgrade` action accepts only the earlier audited runtime whose SHA-256 is
`9370684008f99b85f6ffc30f12dda7d6b0aabbe56b72a5fc9b64b9de0954fa90`.
Upload this new helper to the external helper path, then run as the existing
installing user:

```bash
python3 /opt/railshot-onprem-e2e/devstack-private-install.py upgrade \
  --source /opt/railshot-onprem-e2e/devstack
python3 /opt/railshot-onprem-e2e/devstack-private-install.py check \
  --source /opt/railshot-onprem-e2e/devstack
```

Before writing anything, upgrade checks the pinned Git revision, reconstructs
the twelve patched files from their pinned original Git contents, and compares
each reconstructed hash with the current file and marker. Unreviewed tracked
changes, a different legacy runtime hash, or a changed marker stop the upgrade.
It changes only `railshot-private-helper.py` and the marker; Git metadata,
DevStack scripts, and `local.conf` retain their content. Each file is published
using a flushed, atomic rename with its existing permissions, with the marker
published last. The pair is not one filesystem transaction: if marker
publication fails, `check`, `run`, and repeated `upgrade` refuse the inconsistent
state rather than bypassing a hash gate. A completed upgrade is idempotent.

This correction changes the later RabbitMQ helper subprocess only. When an
installation is still in its APT stage and has not reached RabbitMQ setup, the
runtime can be upgraded without restarting the installation. The running
runner's redaction behavior is unchanged. Wait for both actions above to pass
before allowing a new attempt; record any interrupted upgrade as a failed gate.

Run the helper's acceptance tests with the pinned official source available:

```bash
umask 077
RAILSHOT_DEVSTACK_AUDITED_SOURCE=/path/to/pinned/devstack \
  python3 -m pytest ci/tests/test_devstack_private_install.py -q
```

These tests exercise real subprocess argument capture for the INI editor,
private temporary files, RabbitMQ stdin without file access, the runner's
sanitized log, cross-read redaction, source transformations, strict runtime
upgrade, and refusal gates. Provider and RabbitMQ calls use
fakes. They establish the helper contracts; installation success, service
health, nested instance boot, live WireGuard, and Railshot `server list` require
the subsequent designated-host test. Record those actual outcomes separately.
