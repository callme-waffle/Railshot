"""Run OpenStack CLI without putting authentication secrets in argv or logs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


class ProviderError(RuntimeError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


class OpenStackCLI:
    def __init__(self, auth, runner=subprocess.run, timeout=60):
        self.auth = dict(auth)
        self.runner = runner
        self.timeout = timeout

    def run(self, args, *, json_output=True):
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError('CLI arguments must be a list of strings')
        env = {k: v for k, v in os.environ.items() if not k.startswith('OS_')}
        with tempfile.TemporaryDirectory(prefix='railshot-openstack-') as directory:
            path = Path(directory) / 'clouds.yaml'
            auth = dict(self.auth)
            options = {k: auth.pop(k) for k in ('region_name', 'interface', 'cacert') if k in auth}
            kind = 'v3applicationcredential' if 'application_credential_id' in auth else 'v3password'
            path.write_text(json.dumps({'clouds': {'railshot': {'auth_type': kind, 'auth': auth, **options}}}))
            path.chmod(0o600)
            env.update(OS_CLIENT_CONFIG_FILE=str(path), OS_CLOUD='railshot')
            command = ['openstack', '--os-cloud', 'railshot', *args]
            if json_output:
                command += ['--format', 'json']
            try:
                result = self.runner(command, env=env, cwd=directory, shell=False,
                                     capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise ProviderError('timeout', 'OpenStack operation timed out; reconcile remote state before retrying') from None
            except OSError:
                raise ProviderError('cli_unavailable') from None
            if result.returncode:
                error = (result.stderr or '').lower()
                code = ('forbidden' if '403' in error or 'forbidden' in error else
                        'unauthorized' if '401' in error else
                        'not_found' if '404' in error or 'no ' in error and ' found' in error else
                        'connection_failed' if any(t in error for t in ('connection', 'ssl', 'timed out')) else 'command_failed')
                # Raw stdout/stderr may contain credentials or HTTP headers.
                raise ProviderError(code)
            if not json_output:
                return None
            try:
                return json.loads(result.stdout)
            except (ValueError, TypeError):
                raise ProviderError('invalid_json') from None
