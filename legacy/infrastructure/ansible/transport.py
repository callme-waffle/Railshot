"""Pinned SSH forwarding through native cloud CLIs; no user-supplied commands."""
from contextlib import contextmanager
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time


def transport_parts(reference):
    if not isinstance(reference, str):
        raise ValueError('transport_ref must name SSM or IAP')
    match = re.fullmatch(r'ssm:([a-z]{2}(?:-[a-z]+)+-\d):(i-[a-f0-9]{8,17})', reference)
    if match:
        return ('ssm', *match.groups())
    match = re.fullmatch(r'iap:([a-z][a-z0-9-]{4,61}[a-z0-9])/([a-z]+-[a-z]+[1-9][0-9]*-[a-z])/([a-z][a-z0-9-]{0,61}[a-z0-9])', reference)
    if match:
        return ('iap', *match.groups())
    raise ValueError('transport_ref must name a concrete SSM instance or IAP instance')


def tunnel_command(reference, port):
    kind, *parts = transport_parts(reference)
    if kind == 'ssm':
        region, instance = parts
        return ['aws', 'ssm', 'start-session', '--region', region, '--target', instance,
                '--document-name', 'AWS-StartPortForwardingSession', '--parameters',
                json.dumps({'portNumber': ['22'], 'localPortNumber': [str(port)]})]
    project, zone, instance = parts
    return ['gcloud', 'compute', 'start-iap-tunnel', instance, '22', '--project', project,
            '--zone', zone, '--local-host-port', f'127.0.0.1:{port}', '--quiet']


def owned_ssm_session(log):
    # Only the native plugin's announcement in this Popen's private output is authority.
    # pread does not move the file offset shared with the still-running child.
    output = os.pread(log.fileno(), 65537, 0)
    announcements = [line.removesuffix(b'\r') for line in output.split(b'\n') if line.startswith(b'Starting session with SessionId:')]
    if len(output) > 65536 or len(announcements) != 1:
        raise ValueError('SSM session ownership could not be verified')
    match = re.fullmatch(rb'Starting session with SessionId: ([A-Za-z0-9][A-Za-z0-9_+=,.@-]{0,95})', announcements[0])
    if match is None:
        raise ValueError('SSM session ownership could not be verified')
    return match[1].decode('ascii')


def terminate_ssm_session(executable, region, session_id, env):
    # Never enumerate sessions or infer ownership from a username, instance or start time.
    result = subprocess.run([executable, 'ssm', 'terminate-session', '--region', region,
        '--session-id', session_id, '--output', 'json', '--cli-connect-timeout', '3', '--cli-read-timeout', '3'],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        env={**env, 'AWS_MAX_ATTEMPTS': '1'})
    if result.returncode != 0 or json.loads(result.stdout).get('SessionId') != session_id:
        raise ValueError('SSM session cleanup could not be verified')


@contextmanager
def forwarded_port(reference, deadline):
    if reference is None:
        yield None
        return
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    command = tunnel_command(reference, port)
    ssm_region = transport_parts(reference)[1] if command[:3] == ['aws', 'ssm', 'start-session'] else None
    executable = shutil.which(command[0])
    if executable is None or (command[0] == 'aws' and shutil.which('session-manager-plugin') is None):
        raise ValueError('Native cloud tunnel executable is unavailable')
    command[0] = executable
    # Cloud CLI credentials stay in this controller child, never in Ansible or the guest.
    env = dict(os.environ, AWS_PAGER='', CLOUDSDK_CORE_DISABLE_PROMPTS='1')
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=env, start_new_session=True)
        session_id, failure = None, None
        try:
            ready_by = min(deadline, time.monotonic() + 60)
            while True:
                if process.poll() is not None:
                    raise ValueError('Cloud tunnel exited before SSH forwarding was ready')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= ready_by:
                        raise ValueError('Cloud SSH forwarding readiness timed out') from None
                    time.sleep(0.2)
            if ssm_region:
                session_id = owned_ssm_session(log)
            yield port
        except BaseException as error:
            normal_exit = isinstance(error, SystemExit) and (error.code is None or isinstance(error.code, int) and error.code == 0)
            failure = None if normal_exit else error
            raise
        finally:
            cleanup_failures = []
            try:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass
                finally:
                    # A finished CLI parent can still leave a forwarding child in its group.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    finally:
                        process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_failures.append('Local cloud tunnel process cleanup could not be verified ('
                                        + type(error).__name__ + '); operator reconciliation required')
            finally:
                if ssm_region:
                    try:
                        observed = owned_ssm_session(log)
                        if session_id is not None and observed != session_id:
                            raise ValueError('SSM session ownership changed')
                        terminate_ssm_session(executable, ssm_region, observed, env)
                    except Exception:
                        cleanup_failures.append('SSM session cleanup could not be verified; operator reconciliation required')
            if cleanup_failures:
                if failure is not None:
                    for message in cleanup_failures:
                        failure.add_note(message)
                else:
                    raise ValueError('; '.join(cleanup_failures)) from None
