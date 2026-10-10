"""Cross-process limits for heavy stages on the existing, trusted CI host."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time

from observability import OperationError

PROFILE = Path('/etc/railshot/ci-executor.yaml')
LOCKS = Path('/var/lib/railshot-runner/work/.capacity')


@contextmanager
def slots(directory, kind, limit, timeout=1200):
    if kind not in ('gate', 'agent') or type(limit) is not int or not 1 <= limit <= 16:
        raise ValueError('invalid host capacity')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('private host capacity directory required')
    started, last_report, fd = time.monotonic(), -30, None
    try:
        while fd is None:
            for index in range(limit):
                candidate = os.open(directory / f'{kind}-{index}.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                try:
                    info = os.fstat(candidate)
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                        raise ValueError('private host capacity lock required')
                    try:
                        fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    fd = candidate
                    break
                finally:
                    if fd != candidate:
                        os.close(candidate)
            waited = time.monotonic() - started
            if fd is not None:
                break
            if waited >= timeout:
                raise OperationError('STEP_TIMEOUT', component='runner', phase='host-capacity', retry_policy='after_configuration')
            if waited - last_report >= 30:
                print(json.dumps({'event': 'ci.capacity.waiting', 'kind': kind, 'limit': limit, 'waited_seconds': int(waited)}), file=sys.stderr, flush=True)
                last_report = waited
            time.sleep(min(.1, max(0, timeout - waited)))
        yield
    finally:
        if fd is not None:
            os.close(fd)


@contextmanager
def host_capacity(kind):
    # Local/unit invocations do not run on a managed runner. Uploaded source
    # cannot change this inherited runner identity or select a policy file.
    if not os.environ.get('RAILSHOT_RUNNER_NAME'):
        yield
        return
    try:
        info = PROFILE.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('operator-owned executor profile required')
        policy = json.loads(PROFILE.read_text()).get('concurrency', {'gate': 1, 'agent': 1})
        limit = policy[kind]
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError('invalid host capacity')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OperationError('GATE_CONFIG_INVALID', component='runner', phase='host-capacity', retry_policy='after_configuration', cause=exc) from exc
    with slots(LOCKS, kind, limit):
        yield
