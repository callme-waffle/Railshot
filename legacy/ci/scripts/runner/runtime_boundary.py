"""Small shared local boundaries; no credential contents or provider calls."""
import os
from pathlib import Path
import stat


def private_directory(path):
    """Require the current OS owner and restrict the run root before any output."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError('run directory owner mismatch')
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path.resolve()


def effective_auth_route(provider='codex', env=None):
    """Resolve the same non-secret route for invocation and resume binding."""
    env = os.environ if env is None else env
    if provider == 'codex':
        mode = env.get('RAILSHOT_AUTH_MODE', 'subscription')
        home = (env.get('RAILSHOT_CODEX_HOME') or env.get('CODEX_HOME')) if mode == 'subscription' else None
        # Keep relative paths invalid; resolving one here would silently grant it validity.
        if home and Path(home).is_absolute():
            home = str(Path(home).resolve())
        return {'provider': provider, 'mode': mode, 'credential_home': home}
    return {'provider': provider, 'endpoint': env.get('ANTHROPIC_BASE_URL')}
