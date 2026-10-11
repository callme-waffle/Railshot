"""No cloud calls: argv, secret stream and pinned DevStack patch verification."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('devstack_private_install', ROOT / 'ci/scripts/devstack-private-install.py')
install = importlib.util.module_from_spec(spec); spec.loader.exec_module(install)
SECRET = b'0123456789abcdef' * 3


@pytest.mark.parametrize('cut', range(1, 48))
def test_stream_redaction_never_emits_secret_split_between_reads(cut):
    redactor = install.Redactor((SECRET,))
    first = redactor.feed(b'prefix ' + SECRET[:cut])
    second = redactor.feed(SECRET[cut:] + b' suffix')
    assert first + second + redactor.feed(b'', final=True) == b'prefix [REDACTED] suffix'


def test_redaction_handles_long_stream_with_multiple_passwords():
    another = b'a' * 48
    raw = b'header\n' + SECRET + b'/' + another + b'\n' + b'x' * 65536 + SECRET
    redactor = install.Redactor((SECRET, another))
    result = b''.join(redactor.feed(raw[index:index + 4096]) for index in range(0, len(raw), 4096))
    result += redactor.feed(b'', final=True)
    assert result == raw.replace(SECRET, b'[REDACTED]').replace(another, b'[REDACTED]')


def test_rabbit_password_hash_uses_stdin_without_cross_user_file_access(monkeypatch):
    calls = []
    monkeypatch.setattr(install.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(SECRET)))
    def execute(argv, **options):
        calls.append(argv)
        assert argv == ['sudo', '-n', 'rabbitmqctl', 'import_definitions']
        raw = options['input']
        assert options['check'] is True
        assert SECRET not in raw
        user = json.loads(raw)['users'][0]
        decoded = base64.b64decode(user['password_hash'])
        assert decoded[4:] == hashlib.sha256(decoded[:4] + SECRET).digest()
        assert user['hashing_algorithm'] == 'rabbit_password_hashing_sha256'
        assert SECRET.decode() not in json.dumps(argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(install.subprocess, 'run', execute)
    monkeypatch.setattr(install.tempfile, 'TemporaryDirectory',
                        lambda **kw: pytest.fail('RabbitMQ must not need a private file'))
    assert install.helper('rabbit-user', ['railshot']) == 0
    assert len(calls) == 1


def test_map_cell0_uses_tmpfs_config_without_password_argv(monkeypatch):
    connection = b'mysql+pymysql://root:' + SECRET + b'@127.0.0.1/nova_cell0?charset=utf8\n'
    monkeypatch.setattr(install.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(connection)))
    paths = []
    def execute(argv, **options):
        assert SECRET.decode() not in json.dumps(argv)
        assert '--database_connection' not in argv
        path = Path(argv[4]); paths.append(path)
        assert str(path).startswith('/dev/shm/railshot-cell0-')
        assert path.stat().st_mode & 0o077 == 0
        assert path.read_text().endswith('/nova?charset=utf8\n')
        assert SECRET.decode() in path.read_text()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(install.subprocess, 'run', execute)
    assert install.helper('map-cell0', ['/opt/stack/data/venv/bin/nova-manage', '/etc/nova/nova.conf']) == 0
    assert not paths[0].exists()


def test_create_user_delivers_password_directly_to_sdk_without_subprocess(monkeypatch, capsys):
    user = SimpleNamespace(id='service-user-id')
    identity = SimpleNamespace(find_domain=lambda *a, **kw: SimpleNamespace(id='default'),
                               find_user=lambda *a, **kw: None)
    calls = []
    identity.create_user = lambda **options: calls.append(options) or user
    class Connection:
        def __enter__(self): return SimpleNamespace(identity=identity)
        def __exit__(self, *args): pass
    monkeypatch.setitem(sys.modules, 'openstack', SimpleNamespace(connect=lambda **kw: Connection()))
    monkeypatch.setattr(install.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(SECRET)))
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **kw: pytest.fail('No password CLI process'))
    assert install.helper('user', ['nova', 'Default', '']) == 0
    assert calls[0]['password'] == SECRET.decode()
    assert capsys.readouterr().out == 'service-user-id\n'


@pytest.fixture
def audited_source():
    path = Path(os.environ.get('RAILSHOT_DEVSTACK_AUDITED_SOURCE', '/tmp/railshot-devstack-password-review'))
    if not (path / '.git').is_dir():
        pytest.skip('Set RAILSHOT_DEVSTACK_AUDITED_SOURCE to the pinned official source checkout')
    install.source_revision(path)
    return path


@pytest.fixture
def patched_files(audited_source):
    original = {}
    for relative, expected in install.PREIMAGES.items():
        raw = subprocess.run(['git', '-c', f'safe.directory={audited_source}', 'show', 'HEAD:' + relative],
                             cwd=audited_source, capture_output=True, check=True).stdout
        assert install.digest(raw) == expected
        original[relative] = raw.decode()
    return install.transforms(original)


@pytest.fixture
def legacy_deployment(audited_source, patched_files, tmp_path, monkeypatch):
    source = tmp_path / 'devstack'
    subprocess.run(['git', '-c', f'safe.directory={audited_source}', 'clone',
                    '--shared', '--quiet', str(audited_source), str(source)], check=True)
    old_runtime = b'# synthetic legacy runtime; no credentials\n'
    # Exercise the upgrade algorithm with a distinct known predecessor. The
    # immutable production predecessor is asserted separately below.
    monkeypatch.setattr(install, 'UPGRADE_FROM', install.digest(old_runtime))
    contents = {relative: content.encode() for relative, content in patched_files.items()}
    contents[install.RUNTIME] = old_runtime
    for relative, content in contents.items():
        (source / relative).write_bytes(content)
    (source / install.RUNTIME).chmod(0o600)
    marker = {'version': 1, 'revision': install.REVISION,
              'files': {relative: install.digest(content) for relative, content in contents.items()}}
    (source / install.MARKER).write_text(json.dumps(marker))
    (source / install.MARKER).chmod(0o600)
    (source / 'local.conf').write_bytes(b'private configuration must not be rewritten\n')
    (source / 'local.conf').chmod(0o600)
    return source


def test_upgrade_predecessor_is_the_audited_installed_helper():
    assert install.UPGRADE_FROM == '9370684008f99b85f6ffc30f12dda7d6b0aabbe56b72a5fc9b64b9de0954fa90'


def test_upgrade_replaces_only_runtime_then_marker_and_is_idempotent(legacy_deployment, monkeypatch):
    source = legacy_deployment
    unchanged = {path: (source / path).read_bytes()
                 for path in (*install.PREIMAGES, 'local.conf', '.git/HEAD', '.git/config')}
    original_replace = install.atomic_replace
    calls = []
    def publish(path, content):
        calls.append(path.name)
        if path.name == install.MARKER:
            assert (source / install.RUNTIME).read_bytes() == Path(install.__file__).read_bytes()
        original_replace(path, content)
    monkeypatch.setattr(install, 'atomic_replace', publish)
    install.upgrade(source)
    assert calls == [install.RUNTIME, install.MARKER]
    install.verify_patch(source)
    install.upgrade(source)
    assert len(calls) == 2
    for relative, content in unchanged.items():
        assert (source / relative).read_bytes() == content
    for relative in (install.RUNTIME, install.MARKER, 'local.conf'):
        assert (source / relative).stat().st_mode & 0o777 == 0o600
    assert not list(source.glob('.railshot-private-helper.py-*'))
    assert not list(source.glob('..railshot-private-install.json-*'))


@pytest.mark.parametrize('drift', ('runtime', 'patch', 'marker', 'tracked'))
def test_upgrade_refuses_drift_before_any_replacement(legacy_deployment, monkeypatch, drift):
    source = legacy_deployment
    marker_path = source / install.MARKER
    marker = json.loads(marker_path.read_text())
    if drift == 'runtime':
        (source / install.RUNTIME).write_bytes(b'unknown helper revision\n')
        marker['files'][install.RUNTIME] = install.digest((source / install.RUNTIME).read_bytes())
        marker_path.write_text(json.dumps(marker))
    elif drift == 'patch':
        (source / 'stack.sh').write_text('# source drift\n')
        # Matching marker cannot bless an unreviewed source modification.
        marker['files']['stack.sh'] = install.digest((source / 'stack.sh').read_bytes())
        marker_path.write_text(json.dumps(marker))
    elif drift == 'marker':
        marker['files']['stack.sh'] = '0' * 64
        marker_path.write_text(json.dumps(marker))
    else:
        with (source / 'README.rst').open('a') as stream:
            stream.write('\nUnreviewed tracked change\n')
    monkeypatch.setattr(install, 'atomic_replace', lambda *a: pytest.fail('Do not publish on failed gate'))
    with pytest.raises(ValueError, match='DEVSTACK_'):
        install.upgrade(source)


def test_marker_publication_failure_keeps_check_gate_closed(legacy_deployment, monkeypatch):
    source = legacy_deployment
    original_replace = install.atomic_replace
    def publish(path, content):
        if path.name == install.MARKER:
            raise OSError('synthetic marker publication failure')
        original_replace(path, content)
    monkeypatch.setattr(install, 'atomic_replace', publish)
    with pytest.raises(OSError):
        install.upgrade(source)
    with pytest.raises(ValueError, match='DEVSTACK_HELPER_VERSION_CHANGED'):
        install.verify_patch(source)
    with pytest.raises(ValueError, match='DEVSTACK_PATCH_CHANGED'):
        install.upgrade(source)


def test_patch_disables_traces_internal_logs_password_argv_and_new_sudoers(patched_files):
    assert 'set -o xtrace' not in patched_files['stack.sh']
    assert 'set -o xtrace' not in patched_files['inc/async']
    assert 'worlddump.py' not in patched_files['stack.sh']
    for relative in ('stack.sh', 'inc/rootwrap', 'lib/neutron'):
        assert 'NOPASSWD:' not in patched_files[relative]
    assert '--bootstrap-password' not in patched_files['lib/keystone']
    assert '--os-password' not in patched_files['functions-common']
    assert '-p$DATABASE_PASSWORD' not in patched_files['lib/databases/mysql']
    assert 'mysqladmin -u root password' not in patched_files['lib/databases/mysql']
    assert 'rabbitmqctl change_password' not in patched_files['lib/rpc_backend']
    assert 'rabbitmqctl add_user' not in patched_files['lib/rpc_backend']
    assert 'map_cell0 --database_connection' not in patched_files['lib/nova']
    for relative, content in patched_files.items():
        if not relative.endswith('.py'):
            result = subprocess.run(['bash', '-n'], input=content, text=True, capture_output=True)
            assert result.returncode == 0


def test_iniset_preserves_values_while_sed_never_receives_password_in_argv(patched_files, tmp_path):
    scripts = tmp_path / 'bin'; scripts.mkdir()
    wrapper = scripts / 'sed'
    wrapper.write_text('''#!/usr/bin/python3
import json, os, sys
with open(os.environ['RAILSHOT_TEST_ARGV'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
os.execv('/usr/bin/sed', ['/usr/bin/sed', *sys.argv[1:]])
'''); wrapper.chmod(0o755)
    ini = tmp_path / 'ini.sh'; ini.write_text(patched_files['inc/ini-config'])
    target = tmp_path / 'config'; arg_log = tmp_path / 'argv'
    environment = dict(os.environ, PATH=str(scripts) + ':' + os.environ['PATH'],
                       RAILSHOT_TEST_ARGV=str(arg_log), RAILSHOT_TEST_SECRET=SECRET.decode(),
                       RAILSHOT_TEST_CONFIG=str(target), RAILSHOT_TEST_INI=str(ini))
    body = '''set +x
source "$RAILSHOT_TEST_INI"
iniset "$RAILSHOT_TEST_CONFIG" auth password "$RAILSHOT_TEST_SECRET"
iniset "$RAILSHOT_TEST_CONFIG" auth password "prefix$RAILSHOT_TEST_SECRET"
iniadd_literal "$RAILSHOT_TEST_CONFIG" auth another "$RAILSHOT_TEST_SECRET"
iniset_multiline "$RAILSHOT_TEST_CONFIG" auth multiple "$RAILSHOT_TEST_SECRET" two
'''
    result = subprocess.run(['bash'], input=body, text=True, env=environment, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert SECRET.decode() not in arg_log.read_text()
    assert 'password = prefix' + SECRET.decode() in target.read_text()
    assert 'another = ' + SECRET.decode() in target.read_text()
    assert target.stat().st_mode & 0o077 == 0


def test_source_anchor_change_fails_before_mutation(patched_files):
    changed = dict(patched_files)
    with pytest.raises(ValueError, match='DEVSTACK_SOURCE_ANCHOR_CHANGED'):
        install.transforms(changed)


def test_config_gate_requires_audited_settings_and_private_literals(tmp_path):
    config = tmp_path / 'local.conf'
    settings = {**install.REQUIRED, **install.HOST_CONFIG,
                'ENABLED_SERVICES': ','.join(sorted(install.SERVICES))}
    settings.update({name: SECRET.decode() for name in install.PASSWORDS})
    config.write_text('[[local|localrc]]\n' + '\n'.join(name + '=' + value for name, value in settings.items()))
    config.chmod(0o600)
    assert len(install.config_gate(tmp_path)) == 4
    config.chmod(0o644)
    with pytest.raises(ValueError, match='DEVSTACK_PRIVATE_FILE_REQUIRED'):
        install.config_gate(tmp_path)
    config.chmod(0o600)
    with config.open('a') as stream:
        stream.write('\nenable_plugin other https://example.test/repo\n')
    with pytest.raises(ValueError, match='DEVSTACK_CONFIG_MUST_BE_LITERAL'):
        install.config_gate(tmp_path)


def test_runner_preserves_exit_code_and_writes_only_sanitized_private_log(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source'; source.mkdir()
    (source / 'stack.sh').write_text("printf '%s\\n' 'prefix " + SECRET.decode() + " suffix'\nexit 7\n")
    monkeypatch.setattr(install, 'verify_patch', lambda source: None)
    monkeypatch.setattr(install, 'config_gate', lambda source: (SECRET,))
    monkeypatch.setattr(install.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout='Defaults !log_input, !log_output'))
    log = tmp_path / 'sanitized.log'
    assert install.run_install(source, log) == 7
    assert log.read_bytes() == b'prefix [REDACTED] suffix\n'
    assert log.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == 'prefix [REDACTED] suffix\n'
    with pytest.raises(FileExistsError):
        install.run_install(source, log)


def test_runner_blocks_existing_sudo_input_logging_before_start(tmp_path, monkeypatch):
    monkeypatch.setattr(install, 'verify_patch', lambda source: None)
    monkeypatch.setattr(install, 'config_gate', lambda source: (SECRET,))
    monkeypatch.setattr(install.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout='Defaults env_reset, log_input, log_output'))
    monkeypatch.setattr(install.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Do not start installation'))
    log = tmp_path / 'sanitized.log'
    with pytest.raises(ValueError, match='DEVSTACK_SUDO_IO_LOGGING_ENABLED'):
        install.run_install(tmp_path, log)
    assert not log.exists()
