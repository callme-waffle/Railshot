"""Durable writes inside a local administrator-owned directory, not tenant authorization."""
import os
from pathlib import Path
import stat
import uuid


def durable_write(path, data: bytes, mode=0o600):
    path = Path(path)
    if not isinstance(data, bytes) or path.name in ('', '.', '..'):
        raise ValueError('a named file and bytes are required')
    if type(mode) is not int or mode & ~0o777 or mode & 0o022:
        raise ValueError('group/world writable storage is not supported')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(path.parent, flags)
    temporary = '.' + path.name + '-' + uuid.uuid4().hex
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            raise PermissionError('storage directory must be owned and writable only by this administrator')
        try:
            existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing and (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.geteuid()):
            raise PermissionError('storage destination must be an owned regular file')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode, dir_fd=directory)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Directory fd binds both names despite a path rename by another process.
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
