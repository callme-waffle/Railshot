"""검토한 소스를 원자적으로 배치하며 기존 설치를 덮어쓰지 않습니다."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


def _trusted(path, *, directory=False):
    try:
        metadata = path.lstat()
    except OSError:
        raise RuntimeError('설치 소유 파일을 확인하지 못했습니다.') from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(metadata.st_mode) or metadata.st_uid not in (0, os.geteuid())
            or metadata.st_mode & 0o022 or (not directory and metadata.st_nlink != 1)):
        raise RuntimeError('설치 파일 또는 디렉터리의 소유권·접근 권한이 안전하지 않습니다.')


def _trusted_ancestors(path):
    for parent in path.parents:
        if not parent.exists():
            continue
        metadata = parent.lstat()
        if os.geteuid() != 0 and metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX:
            continue
        _trusted(parent, directory=True)


def install_payload(source, target):
    source, target = Path(source).resolve(), Path(target)
    _trusted_ancestors(target)
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise RuntimeError('설치 경로 심볼릭 링크는 허용하지 않습니다.')
    files = {}
    for subtree in ('deployment/bootstrap', 'infrastructure/providers/openstack', 'apps/agent'):
        for path in (source / subtree).rglob('*'):
            relative = path.relative_to(source)
            if any(part in ('.venv', '__pycache__', '.pytest_cache', 'tests') for part in relative.parts):
                continue
            if path.is_symlink():
                raise RuntimeError('배포본의 심볼릭 링크는 허용하지 않습니다.')
            if path.is_file():
                files[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files or 'deployment/bootstrap/client_setup/main.py' not in files:
        raise RuntimeError('설치 배포본이 불완전합니다.')
    marker = '.jasmin-install.json'
    if target.exists():
        if target.stat().st_uid != os.geteuid() or target.stat().st_mode & 0o022:
            raise RuntimeError('기존 설치 경로 권한이 안전하지 않습니다.')
        record = target / marker
        _trusted(record)
        if record.is_symlink() or not record.is_file() or json.loads(record.read_text()) != files:
            raise RuntimeError('기존 설치 또는 다른 버전을 덮어쓰지 않습니다.')
        for relative, digest in files.items():
            path = target / relative
            _trusted(path)
            _trusted_ancestors(path)
            if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError('기존 설치의 무결성 검증에 실패했습니다.')
        return target
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if target.parent.stat().st_uid != os.geteuid() or target.parent.stat().st_mode & 0o022:
        raise RuntimeError('설치 상위 경로 권한이 안전하지 않습니다.')
    staging = Path(tempfile.mkdtemp(prefix='.railshot-install-', dir=target.parent))
    try:
        for relative in files:
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            shutil.copyfile(source / relative, destination)
            destination.chmod(0o644)
        (staging / marker).write_text(json.dumps(files, sort_keys=True))
        staging.chmod(0o755)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('영구 설치는 root 권한이 필요합니다.')
    install_payload(Path(__file__).resolve().parents[2], Path('/opt/jasmin/bootstrap'))
