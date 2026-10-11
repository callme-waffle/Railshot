"""Private atomic state; no secrets are allowed in persistent progress records."""
from __future__ import annotations
import json
import os
from pathlib import Path
import stat
import tempfile
from .report import sanitize


def private_directory(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for parent in reversed((path, *path.parents)):
        if parent.is_symlink():
            raise ValueError("Symbolic link in private path")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("Private directory ownership mismatch")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("Private directory must have mode 0700")
    return path


def read_private(path: Path) -> bytes:
    private_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1:
            raise ValueError("Unsafe private file ownership or permissions")
        return stream.read()


def atomic_private_write(path: Path, content: bytes) -> None:
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        read_private(path)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(read_private(path)) if path.exists() or path.is_symlink() else {"version": 1, "stages": {}, "resources": []}
        if self.data.get("version") != 1:
            raise ValueError("Unsupported installation state version")

    def save(self):
        atomic_private_write(self.path, json.dumps(sanitize(self.data), indent=2).encode())

    def start(self, stage: str):
        self.data["current_stage"] = stage
        self.data["stages"][stage] = {"status": "running"}
        self.save()

    def fail_current(self, error_type: str):
        stage = self.data.get("current_stage", "configuration")
        self.data["stages"][stage] = {"status": "failed", "error_type": error_type}
        self.save()
        return stage

    def complete(self, stage: str, result=None):
        self.data["stages"][stage] = {"status": "complete", "result": sanitize(result)}
        self.save()

    def record_resource(self, resource: dict):
        self.data["resources"].append(sanitize(resource))
        self.save()
