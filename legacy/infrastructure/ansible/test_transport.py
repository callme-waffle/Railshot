"""Native local process groups only; no cloud CLI or guest connections."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import transport


class TunnelCleanupTests(unittest.TestCase):
    @contextmanager
    def native_cli(self, output, *, exited=False, reachable=True, cleanup=None):
        process = MagicMock(pid=12345)
        process.poll.return_value = 1 if exited else None
        def launch(*args, **kwargs):
            kwargs['stdout'].write(output)
            kwargs['stdout'].flush()
            return process
        with patch.object(transport.shutil, 'which', side_effect=lambda name: '/trusted/' + name), \
                patch.object(transport.subprocess, 'Popen', side_effect=launch), \
                patch.object(transport.socket, 'create_connection', side_effect=None if reachable else OSError('not ready')), \
                patch.object(transport.os, 'killpg') as stop, \
                patch.object(transport.subprocess, 'run', side_effect=cleanup) as terminate:
            yield process, stop, terminate

    def test_only_this_native_cli_session_is_terminated_on_every_exit_path(self):
        session_id = 'fixture_owned@example.test-0123456789abcdef0'
        output = ('\nStarting session with SessionId: ' + session_id + '\nPort 12345 opened\n').encode()
        for mode in ('success', 'body-error', 'early-exit', 'readiness-timeout'):
            with self.subTest(mode=mode):
                def cleanup(command, **kwargs):
                    self.assertEqual(stop.call_count, 2, 'the local group must already be stopped')
                    self.assertIsNotNone(process.wait.call_args)
                    return subprocess.CompletedProcess(command, 0, json.dumps({'SessionId': session_id}).encode())
                with self.native_cli(output, exited=mode == 'early-exit', reachable=mode != 'readiness-timeout',
                                     cleanup=cleanup) as (process, stop, terminate):
                    def use():
                        with transport.forwarded_port('ssm:ap-northeast-2:i-0123456789abcdef0', time.monotonic()) as port:
                            self.assertGreater(port, 0)
                            if mode == 'body-error':
                                raise RuntimeError('caller failed')
                    if mode == 'success':
                        use()
                    else:
                        expected = 'caller failed' if mode == 'body-error' else 'exited before' if mode == 'early-exit' else 'timed out'
                        with self.assertRaisesRegex(RuntimeError if mode == 'body-error' else ValueError, expected):
                            use()
                    terminate.assert_called_once()
                    command = terminate.call_args.args[0]
                    self.assertEqual(command, ['/trusted/aws', 'ssm', 'terminate-session', '--region', 'ap-northeast-2',
                        '--session-id', session_id, '--output', 'json', '--cli-connect-timeout', '3', '--cli-read-timeout', '3'])
                    self.assertEqual(terminate.call_args.kwargs['timeout'], 10)
                    self.assertEqual(terminate.call_args.kwargs['env']['AWS_MAX_ATTEMPTS'], '1')

    def test_missing_ambiguous_or_malformed_ownership_never_terminates_any_session(self):
        own = b'Starting session with SessionId: own-0123456789abcdef0\n'
        for output in (b'', b'other log: ' + own, own + own,
                own + b'Starting session with SessionId: other-0123456789abcdef0\n',
                b'Starting session with SessionId: own;echo-token\n',
                b'Starting session with SessionId: own\rmalformed\n',
                b'Starting session with SessionId: ' + b'a' * 97 + b'\n', own + b'x' * 65536):
            with self.subTest(output_size=len(output)), self.native_cli(output) as (_, stop, terminate):
                with self.assertRaisesRegex(ValueError, 'ownership could not be verified'):
                    with transport.forwarded_port('ssm:ap-northeast-2:i-0123456789abcdef0', time.monotonic() + 1):
                        self.fail('An unowned SSM tunnel must not admit SSH')
                terminate.assert_not_called()
                self.assertEqual(stop.call_count, 2)

    def test_cleanup_failure_is_bounded_and_generic_without_hiding_the_callers_error(self):
        output = b'Starting session with SessionId: own-0123456789abcdef0\n'
        failures = (subprocess.CompletedProcess([], 1, b'sensitive output', b'sensitive error'),
                    subprocess.CompletedProcess([], 0, b'{"SessionId":"foreign-session"}'),
                    subprocess.CompletedProcess([], 0, b'invalid sensitive JSON'),
                    subprocess.TimeoutExpired('sensitive command', 10))
        for result in failures:
            for body_error in (False, True):
                with self.subTest(result=type(result).__name__, body_error=body_error):
                    def cleanup(*args, **kwargs):
                        if isinstance(result, Exception):
                            raise result
                        return result
                    with self.native_cli(output, cleanup=cleanup) as (_, _, terminate):
                        with self.assertRaises(RuntimeError if body_error else ValueError) as raised:
                            with transport.forwarded_port('ssm:ap-northeast-2:i-0123456789abcdef0', time.monotonic() + 1):
                                if body_error:
                                    raise RuntimeError('original caller failure')
                        rendered = str(raised.exception) + repr(getattr(raised.exception, '__notes__', []))
                        self.assertIn('cleanup could not be verified', rendered)
                        self.assertNotIn('sensitive', rendered)
                        if body_error:
                            self.assertEqual(str(raised.exception), 'original caller failure')
                        terminate.assert_called_once()

    def test_local_cleanup_failures_preserve_failed_jobs_and_never_hide_behind_successful_exit(self):
        output = b'Starting session with SessionId: own-0123456789abcdef0\n'
        for mode in ('sigterm', 'sigkill', 'wait-timeout'):
            for original in (None, RuntimeError('original caller failure'), SystemExit(None), SystemExit(0), SystemExit(7)):
                for remote_failure in (False, True):
                    with self.subTest(mode=mode, original=repr(original), remote_failure=remote_failure):
                        cleanup = lambda *a, **kw: subprocess.CompletedProcess([], 1 if remote_failure else 0,
                            b'sensitive cleanup output' if remote_failure else b'{"SessionId":"own-0123456789abcdef0"}')
                        with self.native_cli(output, cleanup=cleanup) as (process, stop, terminate):
                            def signal_group(_pid, sig):
                                if sig == (signal.SIGTERM if mode == 'sigterm' else signal.SIGKILL) and mode != 'wait-timeout':
                                    raise PermissionError(1, 'sensitive local detail')
                            stop.side_effect = signal_group
                            if mode == 'wait-timeout':
                                process.wait.side_effect = subprocess.TimeoutExpired('sensitive command', 5)
                            preserve = isinstance(original, RuntimeError) or isinstance(original, SystemExit) and original.code == 7
                            with self.assertRaises(type(original) if preserve else ValueError) as raised:
                                with transport.forwarded_port('ssm:ap-northeast-2:i-0123456789abcdef0', time.monotonic() + 1):
                                    if original is not None:
                                        raise original
                            if preserve:
                                self.assertIs(raised.exception, original)
                            rendered = str(raised.exception) + repr(getattr(raised.exception, '__notes__', []))
                            self.assertIn('Local cloud tunnel process cleanup could not be verified', rendered)
                            self.assertIn('TimeoutExpired' if mode == 'wait-timeout' else 'PermissionError', rendered)
                            self.assertEqual('SSM session cleanup could not be verified' in rendered, remote_failure)
                            self.assertNotIn('sensitive', rendered)
                            self.assertEqual(stop.call_count, 2)
                            self.assertTrue(process.wait.call_count)
                            self.assertTrue(all(call.kwargs == {'timeout': 5} for call in process.wait.call_args_list))
                            terminate.assert_called_once()

    def test_iap_and_direct_connections_never_call_aws_termination(self):
        with self.native_cli(b'Starting session with SessionId: unrelated-session\n') as (_, _, terminate):
            with transport.forwarded_port('iap:railshot-demo/asia-northeast3-a/railshot-gcp', time.monotonic() + 1):
                pass
            with transport.forwarded_port(None, time.monotonic()) as port:
                self.assertIsNone(port)
            terminate.assert_not_called()

    def test_parent_exit_and_sigterm_resistance_never_leave_a_tunnel_child(self):
        child_code = '''import os,signal,socket,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sys.argv[3] != 'early-exit':
    listener=socket.socket(); listener.bind(('127.0.0.1',int(sys.argv[1]))); listener.listen()
Path(sys.argv[2]).write_text(str(os.getpid()))
time.sleep(60)
'''
        parent_code = '''import signal,subprocess,sys,time
from pathlib import Path
if sys.argv[4] == 'ignore-term': signal.signal(signal.SIGTERM,signal.SIG_IGN)
subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:]])
while not Path(sys.argv[3]).exists(): time.sleep(.01)
if sys.argv[4] != 'early-exit': time.sleep(60)
'''
        native_popen = subprocess.Popen
        for mode in ('early-exit', 'parent-term', 'ignore-term', 'body-error'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                pid_file = Path(directory) / 'child.pid'
                processes = []
                def launch(*args, **kwargs):
                    process = native_popen(*args, **kwargs)
                    processes.append(process)
                    return process
                def command(_reference, port):
                    return [sys.executable, '-c', parent_code, child_code, str(port), str(pid_file), mode]
                try:
                    with patch.object(transport, 'tunnel_command', side_effect=command), \
                            patch.object(transport.subprocess, 'Popen', side_effect=launch):
                        if mode == 'early-exit':
                            with self.assertRaisesRegex(ValueError, 'exited before'):
                                with transport.forwarded_port('local-fixture', time.monotonic() + 5):
                                    self.fail('Fixture does not open a listener')
                        elif mode == 'body-error':
                            with self.assertRaisesRegex(RuntimeError, 'caller failed'):
                                with transport.forwarded_port('local-fixture', time.monotonic() + 5):
                                    raise RuntimeError('caller failed')
                        else:
                            with transport.forwarded_port('local-fixture', time.monotonic() + 5):
                                self.assertTrue(pid_file.exists())
                    self.assertEqual(len(processes), 1)
                    self.assertIsNotNone(processes[0].poll())
                    child_pid = pid_file.read_text()
                    deadline = time.monotonic() + 2
                    while True:
                        state = subprocess.run(['ps', '-p', child_pid, '-o', 'stat='],
                                               capture_output=True, text=True, timeout=5).stdout.strip()
                        if not state or state.startswith('Z') or time.monotonic() >= deadline:
                            break
                        time.sleep(.02)
                    self.assertTrue(not state or state.startswith('Z'), 'Fixture child is still running: ' + state)
                finally:
                    # Even a regression must leave no fixture process behind.
                    for process in processes:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
