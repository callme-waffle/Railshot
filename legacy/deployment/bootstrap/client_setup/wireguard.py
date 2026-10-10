"""Retired WireGuard path: remove only an existing installation-owned config."""
import os
from pathlib import Path
import stat
import subprocess

MARKER = '# Managed by jasmin bootstrap v1\n'


def _run(args, runner=subprocess.run, *, input=None):
    try:
        result = runner(args, input=input, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('WireGuard 명령 실행에 실패했습니다.') from None
    if result.returncode:
        raise RuntimeError('WireGuard 명령 실행에 실패했습니다.')
    return result.stdout.strip()



def _read_private(path):
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1 or metadata.st_mode & 0o077):
        raise RuntimeError('비밀 설정은 실행 계정 소유의 단일 일반 파일이며 소유자만 접근해야 합니다.')
    return path.read_text()



def _interface(config_path):
    path = Path(config_path)
    if path.name != 'jasmin0.conf':
        raise ValueError('WireGuard 설정 파일 이름은 jasmin0.conf여야 합니다.')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeError('WireGuard 설정 심볼릭 링크를 허용하지 않습니다.')
    return 'jasmin0'



def uninstall_wireguard(config_path, *, runner=subprocess.run):
    path = Path(config_path)
    _interface(path)
    if runner is subprocess.run and path != Path('/etc/wireguard/jasmin0.conf'):
        raise ValueError('실제 설치 설정 경로는 /etc/wireguard/jasmin0.conf로 고정됩니다.')
    if not path.exists():
        return {'removed': False}
    if not _read_private(path).startswith(MARKER):
        raise RuntimeError('이 설치가 소유하지 않은 WireGuard 설정입니다.')
    _run(['systemctl', 'disable', '--now', 'wg-quick@jasmin0'], runner)
    path.unlink()
    return {'removed': True}
