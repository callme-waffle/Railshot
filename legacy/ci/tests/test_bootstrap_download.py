"""Isolated launcher downloads through fake curl, then runs real Python help.
These tests do not perform public network requests or demonstrate deployment hosting.
"""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "https://distribution.example.invalid/releases/test"


@pytest.fixture
def launcher(tmp_path):
    isolated = tmp_path / "entry"
    isolated.mkdir()
    shutil.copyfile(ROOT / "deployment/bootstrap/install.sh", isolated / "install.sh")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stages = tmp_path / "stages"
    stages.mkdir()
    log = tmp_path / "downloads.jsonl"
    executions = tmp_path / "executions.jsonl"
    fake_curl = binaries / "curl"
    fake_curl.write_text("#!" + sys.executable + "\n" + '''
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
output = pathlib.Path(args[args.index('--output') + 1])
url = args[-1]
record = {'url': url, 'output': str(output), 'token': os.environ.get('JASMIN_ENROLLMENT_TOKEN'), 'railshot_token': os.environ.get('RAILSHOT_ENROLLMENT_TOKEN'), 'internal_token': os.environ.get('ENROLLMENT_TOKEN'), 'argv': args}
with open(os.environ['DOWNLOAD_LOG'], 'a') as stream:
    stream.write(json.dumps(record) + '\\n')
prefix = os.environ['TEST_BASE_URL'].rstrip('/') + '/'
if not url.startswith(prefix):
    raise SystemExit(91)
relative = url[len(prefix):]
if relative == os.environ.get('FAIL_RELATIVE'):
    raise SystemExit(22)
source = pathlib.Path(os.environ['TEST_SOURCE_ROOT']) / relative
if not source.is_file():
    raise SystemExit(22)
shutil.copyfile(source, output)
print(os.environ.get('FAKE_HTTP_CODE', '200'), end='')
''')
    fake_curl.chmod(0o755)
    # Preserve the test interpreter's environment rather than finding a system Python.
    python_wrapper = binaries / "python3"
    python_wrapper.write_text("#!" + sys.executable + "\n" + '''
import json, os, sys
with open(os.environ['EXECUTION_LOG'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
    python_wrapper.chmod(0o755)
    cleanup_log = tmp_path / "cleanup.jsonl"
    rm_wrapper = binaries / "rm"
    rm_wrapper.write_text("#!" + sys.executable + "\n" + "import json, os, sys\nwith open(os.environ['CLEANUP_LOG'], 'a') as stream:\n    stream.write(json.dumps({'token': os.environ.get('JASMIN_ENROLLMENT_TOKEN'), 'railshot_token': os.environ.get('RAILSHOT_ENROLLMENT_TOKEN'), 'internal_token': os.environ.get('ENROLLMENT_TOKEN')}) + '\\n')\nos.execv('/bin/rm', ['/bin/rm', *sys.argv[1:]])\n")
    rm_wrapper.chmod(0o755)
    env = dict(os.environ)
    env.update(PATH=str(binaries) + os.pathsep + env['PATH'], TMPDIR=str(stages),
               TEST_SOURCE_ROOT=str(ROOT), TEST_BASE_URL=BASE_URL, DOWNLOAD_LOG=str(log),
               EXECUTION_LOG=str(executions), CLEANUP_LOG=str(cleanup_log), JASMIN_ENROLLMENT_TOKEN="retired-secret", RAILSHOT_ENROLLMENT_TOKEN="retired-railshot-secret",
               ENROLLMENT_TOKEN="exported-parent-secret")
    env.pop('JASMIN_DOWNLOAD_BASE_URL', None)
    def run(*args, overrides=None):
        child_env = dict(env)
        child_env.update(overrides or {})
        result = subprocess.run(['bash', str(isolated / 'install.sh'), *args], env=child_env,
                                text=True, capture_output=True, timeout=30, cwd=tmp_path)
        records = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        commands = [json.loads(line) for line in executions.read_text().splitlines()] if executions.exists() else []
        cleanup_records = [json.loads(line) for line in cleanup_log.read_text().splitlines()] if cleanup_log.exists() else []
        assert all(item['token'] is None and item['railshot_token'] is None for item in cleanup_records)
        if records:
            assert cleanup_records, 'download staging must be cleaned after execution'
        return result, records, commands, stages
    return run


def assert_cleaned(records, stages):
    assert not list(stages.iterdir())
    assert all(not Path(record['output']).exists() for record in records)


def test_downloaded_complete_payload_runs_real_help(launcher):
    result, records, commands, stages = launcher('--download-base-url', BASE_URL, '--help')
    assert result.returncode == 0, result.stderr
    assert 'usage:' in result.stdout
    assert 'verify-vm' in result.stdout
    downloaded = {record['url'][len(BASE_URL) + 1:] for record in records}
    for relative in ('deployment/bootstrap/client_setup/main.py', 'deployment/bootstrap/install_payload.py',
                     'deployment/bootstrap/requirements.lock', 'infrastructure/providers/openstack/identity.py',
                     'apps/agent/runner.py'):
        assert relative in downloaded
    # Independently traverse local imports, including preflight -> state -> report.
    # Other tools that happen to live in these subtrees are not bootstrap dependencies.
    pending = [ROOT / name for name in (
        'deployment/bootstrap/client_setup/main.py',
        'deployment/bootstrap/client_setup/preflight.py',
        'deployment/bootstrap/install_payload.py',
        'apps/agent/runner.py', 'apps/agent/sender.py', 'apps/agent/install_forced_command.py')]
    expected = {'deployment/bootstrap/requirements.lock', 'deployment/bootstrap/uninstall.sh'}
    def local_module(parts, directory):
        base = directory.joinpath(*parts)
        return [candidate for candidate in (base.with_suffix('.py'), base / '__init__.py') if candidate.is_file()]
    while pending:
        path = pending.pop()
        relative = path.relative_to(ROOT).as_posix()
        if relative in expected:
            continue
        expected.add(relative)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name.split('.') for alias in node.names]
                directories = [ROOT, ROOT / 'deployment/bootstrap', path.parent]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or '').split('.') if node.module else []]
                if node.level:
                    directory = path.parent
                    for _ in range(node.level - 1):
                        directory = directory.parent
                    directories = [directory]
                else:
                    directories = [ROOT, ROOT / 'deployment/bootstrap', path.parent]
                if not node.module:
                    names = [alias.name.split('.') for alias in node.names]
            else:
                continue
            for directory in directories:
                for parts in names:
                    pending.extend(local_module(parts, directory))
    assert expected <= downloaded, 'missing runtime import closure: ' + str(expected - downloaded)
    assert all(record['token'] is None and record['railshot_token'] is None for record in records)
    assert all('--proto' in record['argv'] and '=https' in record['argv'] for record in records)
    assert_cleaned(records, stages)


def test_environment_base_url_supported(launcher):
    result, records, _, stages = launcher('--help', overrides={'JASMIN_DOWNLOAD_BASE_URL': BASE_URL})
    assert result.returncode == 0, result.stderr
    assert records
    assert_cleaned(records, stages)


@pytest.mark.parametrize('url', ['http://distribution.invalid/root', 'https://user:password@distribution.invalid/root',
    'https://distribution.invalid/root?token=secret', 'https://distribution.invalid/root#fragment',
    'https://distribution.invalid/a b', 'file:///tmp/payload', 'https://distribution.invalid/\nother'])
def test_untrusted_url_rejected_before_download(launcher, url):
    result, records, commands, stages = launcher('--download-base-url', url, '--help')
    assert result.returncode != 0
    assert not records
    assert not any('client_setup.main' in command or 'client_setup.preflight' in command for command in commands)
    assert_cleaned(records, stages)


@pytest.mark.parametrize('missing', ['deployment/bootstrap/client_setup/main.py',
                                     'deployment/bootstrap/client_setup/preflight.py',
                                     'deployment/bootstrap/client_setup/state.py',
                                     'deployment/bootstrap/client_setup/report.py',
                                     'infrastructure/providers/openstack/identity.py',
                                     'apps/agent/runner.py'])
def test_missing_required_file_never_executes_partial_payload(launcher, missing):
    result, records, commands, stages = launcher('--download-base-url', BASE_URL, '--help', overrides={'FAIL_RELATIVE': missing})
    assert result.returncode != 0
    assert records
    assert not any('client_setup.main' in command or 'client_setup.preflight' in command for command in commands)
    assert not any(any('install_payload.py' in item for item in command) for command in commands)
    assert 'usage:' not in result.stdout
    assert_cleaned(records, stages)


def test_redirect_response_never_executes_payload(launcher):
    result, records, commands, stages = launcher('--download-base-url', BASE_URL, '--help', overrides={'FAKE_HTTP_CODE': '302'})
    assert result.returncode != 0
    assert not any('client_setup.main' in command or 'client_setup.preflight' in command for command in commands)
    assert_cleaned(records, stages)


def test_standalone_launcher_without_url_fails_explicitly(launcher):
    result, records, commands, stages = launcher('--help')
    assert result.returncode != 0
    assert not records
    assert '--download-base-url' in result.stderr or 'JASMIN_DOWNLOAD_BASE_URL' in result.stderr
    assert not any('client_setup.main' in command or 'client_setup.preflight' in command for command in commands)
    assert_cleaned(records, stages)


def test_complete_local_distribution_keeps_existing_help_path(tmp_path):
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    unexpected = tmp_path / 'unexpected-curl'
    curl = binaries / 'curl'
    curl.write_text('#!/bin/sh\ntouch "' + str(unexpected) + '"\nexit 90\n')
    curl.chmod(0o755)
    env = dict(os.environ)
    env.pop('JASMIN_DOWNLOAD_BASE_URL', None)
    env['PATH'] = str(binaries) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + env['PATH']
    result = subprocess.run(['bash', str(ROOT / 'deployment/bootstrap/install.sh'), '--help'],
                            env=env, capture_output=True, text=True, timeout=15, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'verify-vm' in result.stdout
    assert not unexpected.exists()


def test_missing_url_argument_rejected_without_execution(launcher):
    result, records, commands, stages = launcher('--download-base-url')
    assert result.returncode != 0
    assert not records and not commands
    assert_cleaned(records, stages)
