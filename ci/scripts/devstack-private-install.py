#!/usr/bin/env python3
"""Pinned DevStack test installer: keep generated passwords out of argv/logs.

This helper patches only the audited 2025.2 checkout. It does not install cloud
software until `run` is explicitly invoked on the designated disposable host.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse


REVISION = '92e864aa017f61e911a8ce3976e828fa5b750ded'
MARKER = '.railshot-private-install.json'
RUNTIME = 'railshot-private-helper.py'
UPGRADE_FROM = '9370684008f99b85f6ffc30f12dda7d6b0aabbe56b72a5fc9b64b9de0954fa90'
PREIMAGES = {
    'stack.sh': 'ad487ea76f95d88eba0cfd76018b2917123a273d5a763e2e605d4562558e44bd',
    'functions-common': '7ed0895474ade7096fd8f6e61dc33e7769ced29dc9e19e9ce9e5e94b6dc5fc65',
    'inc/ini-config': '7c003728fa035792e01c2d9a9ba796ff92e009661ee55bbcc4bbef349caeff41',
    'inc/meta-config': '48bc9da206815f66591161fdfdf3190991e6b407d9a7d3b25712ec47f0023274',
    'inc/async': '989556aab2c1f89ec677f00ea0de50d42a0b3e71503fad6833ab4138ea04f472',
    'inc/rootwrap': 'fa4725673ffe310dd055f3896a5c0206b318b54f55d26d356061343447cd1de8',
    'lib/keystone': '60654f83de8aa426199cf68eee0647043a523b82406b4fa0597fd2e1e32f45da',
    'lib/databases/mysql': '9c94b66cd7203c63b6a71fec5a469089cba6c403ca1d08754c8c1ad8210a70b1',
    'lib/rpc_backend': '9b7cc01b3a4fbb27a976018e3569d07c4b67f121bece3f507f4d92703adcbf9e',
    'lib/nova': '1fe5f56cf5799ce71aad7029a4e6fc10af706be3917d952b2767835c3b7a3181',
    'lib/neutron': '11f08ba68e44ab184e88ac8b1cddc75f15ad1d57cf0a1ba07517397d886740a3',
    'tools/update_clouds_yaml.py': '9803ed071a02c08585f4e01c0fb4265b2235b3195b1f7df416286ee5dd05dc2f',
}
SERVICES = set('key,g-api,mysql,rabbit,n-api,n-api-meta,n-cond,n-cpu,n-sch,placement-api,placement-client,q-svc,ovn-controller,ovn-northd,ovs-vswitchd,ovsdb-server,q-ovn-agent,etcd3'.split(','))
PASSWORDS = ('ADMIN_PASSWORD', 'DATABASE_PASSWORD', 'SERVICE_PASSWORD', 'RABBIT_PASSWORD')
REQUIRED = {'DEVSTACK_PARALLEL': 'False', 'ENABLE_DEBUG_LOG_LEVEL': 'False',
            'DATABASE_QUERY_LOGGING': 'False', 'MYSQL_GATHER_PERFORMANCE': 'False',
            'GLOBAL_VENV': 'True', 'USE_JOURNAL': 'False', 'LOGFILE': '',
            'LOG_SUDO': '', 'SYSLOG': 'False', 'DEBUG_LIBVIRT': 'False',
            'DEBUG_LIBVIRT_COREDUMPS': 'False', 'MYSQL_SERVICE_NAME': 'mariadb'}
HOST_CONFIG = {'DEST': '/opt/stack', 'HOST_IP': '172.28.102.160',
               'SERVICE_HOST': '172.28.102.160', 'HOST_IPV6': '', 'IP_VERSION': '4',
               'FIXED_RANGE': '10.77.0.0/24', 'FLOATING_RANGE': '172.24.4.0/24',
               'PUBLIC_NETWORK_GATEWAY': '172.24.4.1',
               'Q_USE_PROVIDERNET_FOR_PUBLIC': 'False', 'LIBVIRT_TYPE': 'kvm',
               'LOGDIR': '/opt/stack/logs', 'VERBOSE': 'True', 'GIT_DEPTH': '1',
               'DOWNLOAD_DEFAULT_IMAGES': 'False', 'IMAGE_URLS': ''}


def require(value, code='DEVSTACK_PRIVATE_GATE_FAILED'):
    if not value:
        raise ValueError(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def replace(text, old, new, count=1):
    require(text.count(old) == count, 'DEVSTACK_SOURCE_ANCHOR_CHANGED')
    return text.replace(old, new)


def transforms(files):
    result = dict(files)
    text = result['stack.sh']
    text = replace(text, 'set -o xtrace\n', 'set +o xtrace\n')
    start = text.index('# UEC images ')
    end = text.index('# Configure Distro Repositories', start)
    text = text[:start] + '# Reuse already-authorized sudo; do not write sudoers.\nsudo -n true || exit 1\n\n' + text[end:]
    text = replace(text, 'if [[ -z $LOGDIR ]]; then\n            ${PYTHON} $TOP_DIR/tools/worlddump.py\n        else\n            ${PYTHON} $TOP_DIR/tools/worlddump.py -d $LOGDIR\n        fi',
                   'echo "Automatic worlddump disabled: private installation"')
    text = replace(text, 'USERRC_PARAMS="-PA --target-dir $TOP_DIR/accrc --os-password $ADMIN_PASSWORD"',
                   'USERRC_PARAMS="-PA --target-dir $TOP_DIR/accrc"')
    text = replace(text, '$TOP_DIR/tools/create_userrc.sh $USERRC_PARAMS',
                   ': # clouds.yaml supplies CLI authentication; do not create plaintext accrc files')
    text = text.replace('-p$DATABASE_PASSWORD ', '')
    text = replace(text, 'echo "The password: $ADMIN_PASSWORD"',
                   'echo "Administrator password is retained in private local configuration."')
    result['stack.sh'] = text

    text = result['functions-common']
    text = replace(text, '    $PYTHON $TOP_DIR/tools/update_clouds_yaml.py \\\n',
                   '    OS_PASSWORD="$ADMIN_PASSWORD" $PYTHON $TOP_DIR/tools/update_clouds_yaml.py \\\n', count=10)
    text = replace(text, '        --os-password $ADMIN_PASSWORD \\\n', '', count=10)
    start = text.index('function get_or_create_user {')
    end = text.index('\n}\n', start) + 3
    text = text[:start] + '''function get_or_create_user {
    printf '%s' "$2" | $PYTHON "$TOP_DIR/railshot-private-helper.py" user "$1" "$3" "${4:-}"
}
''' + text[end:]
    text = replace(text, '    echo "# $time_stamp $tag" >$TOP_DIR/.stackenv',
                   '    (umask 077; : >$TOP_DIR/.stackenv)\n    chmod 600 $TOP_DIR/.stackenv\n    echo "# $time_stamp $tag" >$TOP_DIR/.stackenv')
    text = replace(text, '    sudo chown -R $STACK_USER /etc/openstack',
                   '    sudo chown -R $STACK_USER /etc/openstack\n    (umask 077; touch "$CLOUDS_YAML")\n    chmod 600 "$CLOUDS_YAML"')
    result['functions-common'] = text

    text = result['tools/update_clouds_yaml.py']
    text = replace(text, "parser.add_argument('--os-password', required=True)",
                   "parser.add_argument('--os-password', default=os.environ.get('OS_PASSWORD'))")
    text = replace(text, '    args = parser.parse_args()\n',
                   "    args = parser.parse_args()\n    if not args.os_password:\n        parser.error('OS_PASSWORD is required')\n")
    # touch creates an empty private clouds.yaml before the first invocation.
    text = replace(text, 'self._clouds = yaml.safe_load(clouds_file)',
                   'self._clouds = yaml.safe_load(clouds_file) or {}')
    result['tools/update_clouds_yaml.py'] = text

    text = result['inc/meta-config']
    text = replace(text, '                    echo "# Generated file, do not edit" >$localautofile',
                   '                    (umask 077; : >"$localautofile")\n                    chmod 600 "$localautofile"\n                    echo "# Generated file, do not edit" >$localautofile')
    result['inc/meta-config'] = text
    # Never turn tracing on in async_inner, even if a future operator enables
    # parallelism. The execution gate still requires synchronous installation.
    result['inc/async'] = replace(result['inc/async'], '    set -o xtrace\n', '    set +o xtrace\n')

    text = result['inc/ini-config']
    text = replace(text, '''$sudo sed -i -e "/^\\[$section\\]/ a\\\\
$option = $value
" "$file"''', '''printf '%s\\n' "/^\\[$section\\]/ a\\\\
$option = $value
" | $sudo sed -i -f /dev/stdin "$file"''', count=2)
    old = "$sudo sed -i -e '/^\\['${section}'\\]/,/^\\[.*\\]/ s'${sep}'^\\('" + '"${option}"' + "'[ \\t]*=[ \\t]*\\).*$'${sep}'\\1'" + '"${value}"${sep} "$file"'
    new = "printf '%s\\n' '/^\\['${section}'\\]/,/^\\[.*\\]/ s'${sep}'^\\('" + '"${option}"' + "'[ \\t]*=[ \\t]*\\).*$'${sep}'\\1'" + '"${value}"${sep} | $sudo sed -i -f /dev/stdin "$file"'
    text = replace(text, old, new)
    text = replace(text, '''$sudo sed -i -e "/^\\[$section\\]/ a\\\\
$option = $v
" "$file"''', '''printf '%s\\n' "/^\\[$section\\]/ a\\\\
$option = $v
" | $sudo sed -i -f /dev/stdin "$file"''')
    # Restrict files containing the audited password/URL settings before writes.
    start = text.index('function iniset {')
    point = text.index('    # Escape the ampersand', start)
    text = text[:point] + '''    case "$option" in
        *password*|connection|transport_url)
            if [[ -e "$file" ]]; then $sudo chmod 600 "$file"
            else $sudo install -m 600 /dev/null "$file"; fi ;;
    esac
''' + text[point:]
    point = text.index('    $xtrace\n}', start)
    text = text[:point] + '''    case "$option" in
        *password*|connection|transport_url) $sudo chmod 600 "$file" ;;
    esac
''' + text[point:]
    result['inc/ini-config'] = text

    text = result['lib/keystone']
    text = replace(text, '    $KEYSTONE_BIN_DIR/keystone-manage bootstrap \\\n',
                   '    OS_BOOTSTRAP_PASSWORD="$ADMIN_PASSWORD" $KEYSTONE_BIN_DIR/keystone-manage bootstrap \\\n')
    text = replace(text, '        --bootstrap-password "$ADMIN_PASSWORD" \\\n', '')
    text = replace(text, '        openstack endpoint create --region "$REGION_NAME" \\\n',
                   '        OS_PASSWORD="$ADMIN_PASSWORD" openstack endpoint create --region "$REGION_NAME" \\\n')
    text = replace(text, '            --os-password "$ADMIN_PASSWORD" \\\n', '')
    result['lib/keystone'] = text

    text = result['lib/databases/mysql']
    text = text.replace('-p$DATABASE_PASSWORD ', '')
    text = replace(text, '        sudo mysqladmin -u root password $DATABASE_PASSWORD || true',
                   '        die $LINENO "Private installer requires Ubuntu 24.04 MariaDB"')
    text = replace(text, '''        sudo mysql -e "ALTER USER $DATABASE_USER@localhost IDENTIFIED VIA mysql_native_password USING PASSWORD('$DATABASE_PASSWORD');"''',
                   '''        printf '%s\\n' "ALTER USER $DATABASE_USER@localhost IDENTIFIED VIA mysql_native_password USING PASSWORD('$DATABASE_PASSWORD');" | sudo mysql''')
    text = replace(text, '''        sudo mysql $cmd_args -e "CREATE USER IF NOT EXISTS '$DATABASE_USER'@'%' identified by '$DATABASE_PASSWORD';"''',
                   '''        die $LINENO "Private installer does not support legacy MySQL configuration"''')
    text = replace(text, '        cat <<EOF >$HOME/.my.cnf',
                   '        (umask 077; : >"$HOME/.my.cnf")\n        chmod 600 "$HOME/.my.cnf"\n        cat <<EOF >$HOME/.my.cnf')
    result['lib/databases/mysql'] = text

    text = result['lib/rpc_backend']
    text = replace(text, '''            # change the rabbit password since the default is "guest"
            sudo rabbitmqctl change_password \\
                $RABBIT_USERID $RABBIT_PASSWORD || rc=$?
            if [ $rc -ne 0 ]; then
                continue;
            fi''', '            # rabbit_setuser imported the new salted password hash already.')
    text = replace(text, '''        sudo rabbitmqctl change_password "$user" "$pass" ||''',
                   '''        printf '%s' "$pass" | $PYTHON "$TOP_DIR/railshot-private-helper.py" rabbit-user "$user" ||''')
    text = replace(text, '''        sudo rabbitmqctl add_user "$user" "$pass" ||''',
                   '''        printf '%s' "$pass" | $PYTHON "$TOP_DIR/railshot-private-helper.py" rabbit-user "$user" ||''')
    result['lib/rpc_backend'] = text

    text = result['lib/nova']
    text = replace(text, '        $NOVA_BIN_DIR/nova-manage cell_v2 map_cell0 --database_connection `database_connection_url nova_cell0`',
                   '        database_connection_url nova_cell0 | $PYTHON "$TOP_DIR/railshot-private-helper.py" map-cell0 "$NOVA_BIN_DIR/nova-manage" "$NOVA_CONF"')
    result['lib/nova'] = text

    text = result['inc/rootwrap']
    start = text.index('    # Set up the rootwrap sudoers\n')
    end = text.index('\n}\n', start)
    text = text[:start] + '    # Existing sudo authorization covers the absolute rootwrap path.\n    sudo -n true\n' + text[end:]
    start = text.index('function add_sudo_secure_path {')
    end = text.index('\n}\n', start) + 3
    text = text[:start] + 'function add_sudo_secure_path {\n    : # Existing sudo secure_path is preserved.\n}\n' + text[end:]
    result['inc/rootwrap'] = text

    text = result['lib/neutron']
    start = text.index('    # Set up the rootwrap sudoers for neutron\n')
    end = text.index('    # Update the root_helper\n', start)
    text = text[:start] + '    # Existing sudo authorization covers the absolute neutron rootwrap path.\n    sudo -n true\n\n' + text[end:]
    result['lib/neutron'] = text
    return result


def private(path, *, owner=None):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == (os.geteuid() if owner is None else owner)
            and not info.st_mode & 0o077 and info.st_nlink == 1, 'DEVSTACK_PRIVATE_FILE_REQUIRED')
    return path.read_bytes()


def source_revision(source):
    result = subprocess.run(['git', '-c', f'safe.directory={source}', 'rev-parse', 'HEAD'],
                            cwd=source, capture_output=True, text=True, check=True)
    require(result.stdout.strip() == REVISION, 'DEVSTACK_REVISION_CHANGED')


def verify_patch(source):
    source_revision(source)
    marker = json.loads((source / MARKER).read_text())
    require(marker.get('version') == 1 and marker.get('revision') == REVISION)
    require(set(marker['files']) == set(PREIMAGES) | {RUNTIME})
    require(marker['files'][RUNTIME] == digest(Path(__file__).read_bytes()), 'DEVSTACK_HELPER_VERSION_CHANGED')
    for relative, expected in marker['files'].items():
        path = source / relative
        require(path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == expected,
                'DEVSTACK_PATCH_CHANGED')
    changed = subprocess.run(['git', '-c', f'safe.directory={source}', 'diff', '--name-only', 'HEAD'],
                             cwd=source, capture_output=True, text=True, check=True).stdout.splitlines()
    require(set(changed) <= set(PREIMAGES), 'DEVSTACK_UNAUDITED_SOURCE_CHANGE')
    return marker


def patch(source):
    source_revision(source)
    if (source / MARKER).exists():
        verify_patch(source)
        return
    files = {}
    for relative, expected in PREIMAGES.items():
        raw = (source / relative).read_bytes()
        require(digest(raw) == expected, 'DEVSTACK_SOURCE_PREIMAGE_CHANGED')
        files[relative] = raw.decode()
    transformed = transforms(files)
    # Validate shell syntax before replacing any file. No generated credentials
    # are read by this operation, and failed anchors leave the source unchanged.
    for relative, content in transformed.items():
        if relative.endswith('.py'):
            compile(content, relative, 'exec')
        else:
            result = subprocess.run(['bash', '-n'], input=content, text=True, capture_output=True)
            require(result.returncode == 0, 'DEVSTACK_PATCH_SYNTAX_INVALID')
    transformed[RUNTIME] = Path(__file__).read_text()
    for relative, content in transformed.items():
        (source / relative).write_text(content)
    marker = {'version': 1, 'revision': REVISION,
              'files': {relative: digest(content.encode()) for relative, content in transformed.items()}}
    (source / MARKER).write_text(json.dumps(marker, sort_keys=True, indent=2) + '\n')


def atomic_replace(path, content):
    """Publish a complete file; a failed marker publication leaves gates closed."""
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def upgrade(source):
    """Upgrade the one audited runtime, without changing DevStack or settings."""
    source_revision(source)
    marker_path = source / MARKER
    runtime_path = source / RUNTIME
    for path in (marker_path, runtime_path):
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                'DEVSTACK_UPGRADE_FILE_INVALID')
    marker = json.loads(marker_path.read_text())
    require(marker.get('version') == 1 and marker.get('revision') == REVISION)
    require(set(marker['files']) == set(PREIMAGES) | {RUNTIME})
    originals = {}
    for relative, expected in PREIMAGES.items():
        original = subprocess.run(['git', '-c', f'safe.directory={source}',
                                   'show', 'HEAD:' + relative], cwd=source,
                                  capture_output=True, check=True).stdout
        require(digest(original) == expected, 'DEVSTACK_SOURCE_PREIMAGE_CHANGED')
        originals[relative] = original.decode()
    # Recompute all twelve expected patches independently of the legacy marker.
    for relative, content in transforms(originals).items():
        expected = digest(content.encode())
        path = source / relative
        require(path.is_file() and not path.is_symlink()
                and digest(path.read_bytes()) == expected
                and marker['files'][relative] == expected, 'DEVSTACK_PATCH_CHANGED')
    changed = subprocess.run(['git', '-c', f'safe.directory={source}',
                              'diff', '--name-only', 'HEAD'], cwd=source,
                             capture_output=True, text=True, check=True).stdout.splitlines()
    require(set(changed) <= set(PREIMAGES), 'DEVSTACK_UNAUDITED_SOURCE_CHANGE')
    current = digest(runtime_path.read_bytes())
    replacement = Path(__file__).read_bytes()
    require(marker['files'][RUNTIME] == current, 'DEVSTACK_PATCH_CHANGED')
    if current == digest(replacement):
        verify_patch(source)
        return
    require(current == UPGRADE_FROM, 'DEVSTACK_UPGRADE_VERSION_NOT_AUDITED')
    compile(replacement, RUNTIME, 'exec')
    atomic_replace(runtime_path, replacement)
    marker['files'][RUNTIME] = digest(replacement)
    # The runtime is complete before the marker allows subsequent check/run.
    atomic_replace(marker_path, (json.dumps(marker, sort_keys=True, indent=2) + '\n').encode())
    verify_patch(source)


def config_gate(source):
    require(not (source / 'localrc').exists() and not (source / 'local.sh').exists(),
            'DEVSTACK_CUSTOM_HOOK_NOT_AUDITED')
    settings = {}
    for line in private(source / 'local.conf').decode().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#') or stripped == '[[local|localrc]]':
            continue
        match = re.fullmatch(r'([A-Z][A-Z0-9_]*)=(.*)', stripped)
        require(match, 'DEVSTACK_CONFIG_MUST_BE_LITERAL')
        name, value = match.groups()
        if value.startswith(("'", '"')):
            require(len(value) >= 2 and value[-1] == value[0])
            value = value[1:-1]
        require(not any(char in value for char in ('$', '`', ';', '\\', '\n', '"', "'")),
                'DEVSTACK_CONFIG_MUST_BE_LITERAL')
        require(name not in settings, 'DEVSTACK_DUPLICATE_CONFIG')
        settings[name] = value
    for name, value in REQUIRED.items():
        require(settings.get(name) == value, 'DEVSTACK_PRIVATE_CONFIG_REQUIRED_' + name)
    for name, value in HOST_CONFIG.items():
        require(settings.get(name) == value, 'DEVSTACK_HOST_CONFIG_REQUIRED_' + name)
    require(set(settings) == set(REQUIRED) | set(HOST_CONFIG) | set(PASSWORDS) | {'ENABLED_SERVICES'},
            'DEVSTACK_EXTRA_CONFIG_NOT_AUDITED')
    require(set(settings.get('ENABLED_SERVICES', '').split(',')) == SERVICES,
            'DEVSTACK_SERVICES_NOT_AUDITED')
    # Fixed hex values do not acquire quote/URL transformations in diagnostics.
    for name in PASSWORDS:
        require(re.fullmatch(r'[a-f0-9]{48}', settings.get(name, '')), 'DEVSTACK_HEX_PASSWORD_REQUIRED')
    for name in settings:
        if re.search(r'PASSWORD|TOKEN|SECRET', name):
            require(name in PASSWORDS, 'DEVSTACK_EXTRA_SECRET_NOT_AUDITED')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    require(release.get('ID', '').strip('"') == 'ubuntu' and release.get('VERSION_ID', '').strip('"') == '24.04',
            'DEVSTACK_OS_NOT_AUDITED')
    return tuple(settings[name].encode() for name in PASSWORDS)


class Redactor:
    """Exact generated-value redaction, including values split across reads."""
    def __init__(self, secrets):
        self.secrets = tuple(sorted(set(secrets), key=len, reverse=True))
        self.keep = max(map(len, self.secrets), default=1) - 1
        self.pending = b''

    def feed(self, data, *, final=False):
        self.pending += data
        for value in self.secrets:
            self.pending = self.pending.replace(value, b'[REDACTED]')
        if final:
            result, self.pending = self.pending, b''
            return result
        if len(self.pending) <= self.keep:
            return b''
        count = len(self.pending) - self.keep
        result, self.pending = self.pending[:count], self.pending[count:]
        return result


def run_install(source, log):
    require(os.geteuid() != 0, 'DEVSTACK_NONROOT_REQUIRED')
    verify_patch(source)
    secrets = config_gate(source)
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(('OS_', 'BASH', 'SHELLOPTS'))}
    environment.update(PYTHONUNBUFFERED='1')
    subprocess.run(['sudo', '-n', 'true'], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=environment)
    sudo = subprocess.run(['sudo', '-n', '-l'], check=True, capture_output=True,
                          text=True, env=environment)
    require(not re.search(r'(^|[\s,])(log_input|log_output)(?=[\s,]|$)|\bLOG_(INPUT|OUTPUT):',
                          sudo.stdout, flags=re.I), 'DEVSTACK_SUDO_IO_LOGGING_ENABLED')
    # An output path can contain only the sanitized stream. Never redirect the
    # shell's raw output or tee it before this process has performed redaction.
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    redactor = Redactor(secrets)
    with os.fdopen(descriptor, 'wb') as output:
        process = subprocess.Popen(['bash', str(source / 'stack.sh')], cwd=source,
                                   env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        while True:
            raw = os.read(process.stdout.fileno(), 16384)
            if not raw:
                break
            sanitized = redactor.feed(raw)
            output.write(sanitized); output.flush()
            sys.stdout.buffer.write(sanitized); sys.stdout.buffer.flush()
        sanitized = redactor.feed(b'', final=True)
        output.write(sanitized)
        sys.stdout.buffer.write(sanitized); sys.stdout.buffer.flush()
        return process.wait()


def helper(command, args):
    secret = sys.stdin.buffer.read(4097)
    require(0 < len(secret) <= 4096)
    if command == 'user':
        require(len(args) == 3)
        import openstack
        with openstack.connect(cloud='devstack-system-admin') as connection:
            domain = connection.identity.find_domain(args[1], ignore_missing=False)
            user = connection.identity.find_user(args[0], domain_id=domain.id)
            if user is None:
                user = connection.identity.create_user(name=args[0], domain_id=domain.id,
                    password=secret.decode(), email=args[2] or None, is_enabled=True)
            print(user.id)
        return 0
    if command == 'rabbit-user':
        require(len(args) == 1 and re.fullmatch(r'[A-Za-z0-9_-]+', args[0]))
        salt = os.urandom(4)
        password_hash = base64.b64encode(salt + hashlib.sha256(salt + secret).digest()).decode()
        document = {'users': [{'name': args[0], 'password_hash': password_hash,
                              'hashing_algorithm': 'rabbit_password_hashing_sha256', 'tags': []}]}
        # Ubuntu's root CLI wrapper drops to rabbitmq before importing. The
        # no-path form reads JSON from stdin and needs no cross-user file access.
        subprocess.run(['sudo', '-n', 'rabbitmqctl', 'import_definitions'],
                       input=json.dumps(document).encode(), check=True)
        return 0
    if command == 'map-cell0':
        require(len(args) == 2)
        url = urllib.parse.urlsplit(secret.decode().strip())
        require(url.path == '/nova_cell0')
        # Nova appends _cell0 to CONF.database.connection when no explicit CLI
        # URL is supplied. The extra private config changes only this invocation.
        base = urllib.parse.urlunsplit(url._replace(path='/nova'))
        with tempfile.TemporaryDirectory(prefix='railshot-cell0-', dir='/dev/shm') as folder:
            path = Path(folder) / 'cell0.conf'
            path.write_text('[database]\nconnection = ' + base + '\n'); path.chmod(0o600)
            return subprocess.run([args[0], '--config-file', args[1], '--config-file', str(path),
                                   'cell_v2', 'map_cell0']).returncode
    raise ValueError('DEVSTACK_HELPER_ACTION_INVALID')


def main():
    if len(sys.argv) >= 2 and sys.argv[1] in ('user', 'rabbit-user', 'map-cell0'):
        try:
            return helper(sys.argv[1], sys.argv[2:])
        except Exception:
            print('DEVSTACK_PRIVATE_HELPER_FAILED', file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('patch', 'upgrade', 'check', 'run'))
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--log', type=Path)
    args = parser.parse_args()
    try:
        source = args.source.resolve()
        if args.action == 'patch':
            patch(source)
        elif args.action == 'upgrade':
            upgrade(source)
        elif args.action == 'check':
            verify_patch(source); config_gate(source)
        else:
            require(args.log is not None, 'DEVSTACK_SANITIZED_LOG_REQUIRED')
            return run_install(source, args.log)
        print(json.dumps({'status': 'passed', 'action': args.action, 'revision': REVISION}))
        return 0
    except Exception as exc:
        # Never emit source contents or exception text from credential readers.
        print(str(exc) if isinstance(exc, ValueError) and str(exc).startswith('DEVSTACK_')
              else 'DEVSTACK_PRIVATE_GATE_FAILED', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
