"""Private bounded native commands for project-scoped GCP control."""
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[3]


class GcpError(ValueError):
    def __init__(self, code, *, unknown=False):
        self.code, self.status, self.outcome_unknown = code, 409, unknown
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def private_json(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path: raise GcpError('GCP_PRIVATE_FILE_REQUIRED')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or not 0 < info.st_size <= 4 * 1024 * 1024:
            raise GcpError('GCP_PRIVATE_FILE_REQUIRED')
        return json.load(stream)


def directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path: raise GcpError('GCP_PRIVATE_STATE_INVALID')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077: raise GcpError('GCP_PRIVATE_STATE_INVALID')
    return path


def save(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally: temporary.unlink(missing_ok=True)


def command(argv, *, env, cwd=None, timeout=1800, mutation=False):
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=error, start_new_session=True)
            try:
                rc = process.wait(timeout=timeout)
            finally:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                process.wait()
            if rc: raise GcpError('GCP_NATIVE_COMMAND_FAILED', unknown=mutation)
            output.seek(0); data = output.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024: raise GcpError('GCP_NATIVE_RESPONSE_TOO_LARGE', unknown=mutation)
            return data.decode()
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GcpError('GCP_NATIVE_EXECUTION_FAILED', unknown=mutation) from exc
