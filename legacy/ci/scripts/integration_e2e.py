#!/usr/bin/env python3
"""One real runtime smoke on a disposable GitHub-hosted VM; never a cloud deploy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
OWNER = Path('/run/railshot-integration-e2e.json')
CLUSTER = tuple(Path(p) for p in ('/usr/local/bin/k3s', '/etc/rancher/k3s/config.yaml',
                                '/var/lib/rancher/k3s', '/etc/systemd/system/k3s.service'))
cancelled = False


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def save(path, value):
    # Evidence contains only public fixture inputs/results, and must be uploadable by runner.
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
    try:
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def guard(output):
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or
            os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted' or
            sys.platform != 'linux' or os.geteuid() != 0 or platform.machine() != 'x86_64'):
        raise ValueError('requires a root process on a disposable GitHub-hosted Linux amd64 runner')
    identity = {name: os.environ.get(name, '') for name in
                ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'GITHUB_SHA')}
    if (not all(re.fullmatch(r'[1-9][0-9]{0,19}', identity[k]) for k in ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')) or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identity['GITHUB_JOB']) or
            not re.fullmatch(r'[a-f0-9]{40}', identity['GITHUB_SHA'])):
        raise ValueError('explicit GitHub run/attempt/job/source identity required')
    if Path(os.environ.get('GITHUB_WORKSPACE', '')).resolve() != ROOT.resolve():
        raise ValueError('script must belong to this run checkout')
    temporary = Path(os.environ.get('RUNNER_TEMP', ''))
    if (not temporary.is_absolute() or not temporary.is_dir() or not output.is_absolute() or
            output != output.resolve() or temporary.resolve() not in output.parents):
        raise ValueError('output must be a real directory below RUNNER_TEMP')
    return identity


def process_tree(pid):
    """Snapshot only this runtime's descendants, including its separate installer sessions."""
    rows = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            rows[int(path.parent.name)] = (int(fields[1]), fields[19], fields[0])
        except (OSError, ValueError, IndexError):
            continue
    selected = {pid}
    while True:
        found = {child for child, (parent, _, _) in rows.items() if parent in selected}
        if found <= selected:
            break
        selected |= found
    return {child: rows[child][1] for child in selected if child in rows}


def alive(pid, started):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] == started and fields[0] != 'Z'
    except (OSError, ValueError, IndexError):
        return False


def stop_runtime(process, output):
    # Runtime installers create new sessions, so killing only the outer process group is insufficient.
    owned = process_tree(process.pid)
    save(output / 'active-processes.json', owned)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, started in owned.items():
            try:
                if alive(pid, started):
                    os.kill(pid, sig)
            except (OSError, ValueError, IndexError):
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if sig == signal.SIGTERM:
            time.sleep(0.2)
    process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while any(alive(pid, started) for pid, started in owned.items()):
        if time.monotonic() >= deadline:
            raise RuntimeError('owned installer still active; cleanup must wait')
        time.sleep(0.1)


def invoke(action, output, request, *, deadline=None):
    phase_deadline = time.monotonic() + (300 if action == 'cleanup' else 900)
    if deadline is not None:
        phase_deadline = min(phase_deadline, deadline)
    if time.monotonic() >= phase_deadline:
        raise RuntimeError('runtime deadline exceeded before phase start')
    argv = ['/bin/bash', str(ROOT / 'deployment/scripts' / (action + '.sh')),
            '--input', str(output / 'request.json')]
    if action == 'cleanup':
        argv += ['--all', '--disposable-node']
    # Do not give the sample runtime GitHub/cloud/model credentials or inherited kubeconfig.
    env = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
           'HOME': '/root', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    with (output / (action + '.json')).open('wb') as result, (output / (action + '.log')).open('wb') as log:
        process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=result, stderr=log, start_new_session=True)
        save(output / 'active-processes.json', process_tree(process.pid))
        try:
            while process.poll() is None:
                if (cancelled and action != 'cleanup') or time.monotonic() >= phase_deadline:
                    raise RuntimeError('runtime interrupted or phase deadline exceeded')
                time.sleep(0.2)
        except BaseException:
            stop_runtime(process, output)
            raise
    value = json.loads((output / (action + '.json')).read_text())
    if (process.returncode != 0 or value.get('status') != ('cleaned' if action == 'cleanup' else 'ready') or
            value.get('environment_id') != request['environment_id'] or value.get('provider') != request['provider']):
        raise RuntimeError(action + ' did not return a successful bound runtime result')
    return value


def cleanup(output):
    identity = guard(output)
    receipt_path = output / 'ownership.json'
    if not receipt_path.exists():
        if OWNER.exists():
            raise ValueError('host owner exists without this request evidence; refusing cleanup')
        return {'cleanup': 'not_started'}
    receipt = json.loads(receipt_path.read_text())
    request_path = output / 'request.json'
    if receipt.get('binding') != {**identity, 'output': str(output),
                                  'request_sha256': hashlib.sha256(request_path.read_bytes()).hexdigest()}:
        raise ValueError('request/run ownership differs; refusing cleanup')
    if receipt.get('cleanup') == 'succeeded' and not OWNER.exists():
        return {'cleanup': 'already_succeeded'}
    if (OWNER.is_symlink() or not OWNER.is_file() or OWNER.stat().st_uid != 0 or
            OWNER.stat().st_mode & 0o077 or json.loads(OWNER.read_text()) != receipt['binding']):
        raise ValueError('host owner differs; refusing cleanup')
    request = json.loads(request_path.read_text())
    active = output / 'active-processes.json'
    if active.exists() and any(alive(int(pid), started) for pid, started in json.loads(active.read_text()).items()):
        raise RuntimeError('owned installer still active; refusing concurrent cleanup')
    receipt['cleanup'] = 'running'; save(receipt_path, receipt)
    try:
        invoke('cleanup', output, request)
        if any(path.exists() for path in CLUSTER):
            raise RuntimeError('runtime cleanup returned but cluster files remain')
        receipt['cleanup'] = 'succeeded'; save(receipt_path, receipt)
        OWNER.unlink()
    except BaseException:
        receipt['cleanup'] = 'failed'; save(receipt_path, receipt)
        raise
    return {'cleanup': 'succeeded'}


def run(output):
    identity = guard(output)
    if OWNER.exists() or any(path.exists() for path in CLUSTER) or Path('/etc/rancher/k3s').exists():
        raise ValueError('existing cluster/owner found; refusing disposable smoke')
    output.mkdir(mode=0o755, parents=True, exist_ok=False)
    request = json.loads((ROOT / 'deployment/scripts/tests/fixtures/aws.json').read_text())
    suffix = hashlib.sha256(encoded(identity)).hexdigest()[:16]
    request['environment_id'] = 'railshot-ci-' + suffix
    request['node'] = {'host': 'localhost'}
    request['workload']['namespace'] = 'railshot-ci-' + suffix
    request['runtime']['timeout_seconds'] = 600
    save(output / 'request.json', request)
    binding = {**identity, 'output': str(output), 'request_sha256': hashlib.sha256(encoded(request)).hexdigest()}
    receipt = {'binding': binding, 'smoke': 'running', 'cleanup': 'pending'}
    save(output / 'ownership.json', receipt)
    descriptor = os.open(OWNER, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(encoded(binding)); stream.flush(); os.fsync(stream.fileno())
    try:
        # Reserve five minutes for cleanup within the 30-minute hosted job.
        deadline = time.monotonic() + 1200
        with (output / 'swapoff.log').open('wb') as log:
            subprocess.run(['/sbin/swapoff', '-a'], check=True, timeout=30,
                           stdout=log, stderr=log,
                           env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
        invoke('deploy', output, request, deadline=deadline)
        invoke('verify', output, request, deadline=deadline)
        receipt['smoke'] = 'passed'
    except BaseException:
        receipt['smoke'] = 'failed'
        raise
    finally:
        save(output / 'ownership.json', receipt)
        cleanup(output)
    return {'smoke': receipt['smoke'], 'cleanup': 'succeeded', 'scope': 'github-hosted-linux-amd64-runtime'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'cleanup'))
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()

    def interrupted(_signal, _frame):
        global cancelled
        cancelled = True

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        result = run(args.output_dir) if args.action == 'run' else cleanup(args.output_dir)
        print(json.dumps(result))
        return 1 if cancelled else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # Native diagnostics are in the bounded run's log files, never dump process environment.
        print(json.dumps({'status': 'failed', 'error': str(exc), 'scope': 'github-hosted-linux-amd64-runtime'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
