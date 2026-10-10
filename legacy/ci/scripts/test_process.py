"""Local process cleanup probes; no network, cloud or model calls."""
import errno
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch
import os

from process import OutputLimitError, ProcessCleanupError, run_bounded, stop_group


class ProcessCleanupTest(unittest.TestCase):
    def test_stdin_and_output_are_drained_concurrently_with_explicit_environment(self):
        chunks, ticks = [], []
        result = run_bounded([sys.executable, '-c',
            'import os,sys;sys.stdout.write("x"*100000);sys.stdout.flush();v=sys.stdin.buffer.read();print(len(v));print(os.getenv("TEST_BOUND"))'],
            input=b'y' * 1000000, env={'TEST_BOUND': 'safe'},
            on_output=lambda stream, data: chunks.append((stream, data)), on_tick=lambda: ticks.append(True))
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.endswith('1000000\nsafe\n'))
        self.assertEqual(b''.join(data for stream, data in chunks if stream == 'stdout').decode(), result.stdout)
        self.assertTrue(ticks)

    def test_callback_failure_still_cleans_real_process_group(self):
        children = []
        native = subprocess.Popen
        def launch(*args, **kwargs):
            child = native(*args, **kwargs); children.append(child); return child
        def reject(stream, data): raise ValueError('safe callback rejection')
        with patch('process.subprocess.Popen', side_effect=launch), self.assertRaises(ValueError):
            run_bounded([sys.executable, '-c', 'import time;print("started",flush=True);time.sleep(30)'], on_output=reject)
        self.assertIsNotNone(children[0].poll())
        with self.assertRaises(ProcessLookupError): os.killpg(children[0].pid, 0)

    def test_macos_reaped_zombie_and_absent_group_preserve_capture_error(self):
        proc = Mock(pid=12345)
        proc.poll.side_effect = [None, -signal.SIGTERM]
        with patch("process.os.killpg", side_effect=[None, PermissionError(errno.EPERM, "synthetic"),
                                                       ProcessLookupError(errno.ESRCH, "absent")]) as kill, \
             patch("process.time.sleep"):
            stop_group(proc)
        self.assertEqual([signal.SIGTERM, signal.SIGKILL, 0], [call.args[1] for call in kill.call_args_list])
        proc.wait.assert_called_once_with(timeout=5)

    def test_permission_failure_for_live_child_is_not_ignored(self):
        proc = Mock(pid=12345)
        proc.poll.return_value = None
        with patch("process.os.killpg", side_effect=PermissionError(errno.EPERM, "synthetic")), \
             self.assertRaises(PermissionError):
            stop_group(proc)
        proc.wait.assert_not_called()

    def test_exited_direct_child_does_not_prove_descendant_group_absent(self):
        for probe in (None, PermissionError(errno.EPERM, "inaccessible group")):
            proc = Mock(pid=12345)
            proc.poll.return_value = 0
            with self.subTest(probe=type(probe).__name__), \
                 patch("process.os.killpg", side_effect=[PermissionError(errno.EPERM, "synthetic"), probe]), \
                 self.assertRaises(PermissionError):
                stop_group(proc)
            proc.wait.assert_not_called()

    def test_real_short_child_capture_limit_is_not_cleanup_permission_error(self):
        for _ in range(5):
            with self.assertRaises(OutputLimitError):
                run_bounded([sys.executable, "-c", 'import os; os.write(1,b"x"*700); os.write(2,b"y"*700)'],
                            max_output_bytes=1024)
        result = run_bounded([sys.executable, "-c", 'print("ok")'], max_output_bytes=1024)
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual("ok\n", result.stdout)

    def test_cleanup_failure_does_not_enter_an_unbounded_popen_exit_wait(self):
        launched = []
        native_popen = subprocess.Popen

        def launch(*args, **kwargs):
            child = native_popen(*args, **kwargs)
            launched.append(child)
            return child

        started = time.monotonic()
        try:
            with patch("process.subprocess.Popen", side_effect=launch), \
                 patch("process.stop_group", side_effect=PermissionError(errno.EPERM, "synthetic")), \
                 self.assertRaises(ProcessCleanupError) as caught:
                run_bounded([sys.executable, "-c", "import time; time.sleep(3)"], timeout=0.1)
            self.assertLess(time.monotonic() - started, 2)
            self.assertIsInstance(caught.exception.__cause__, PermissionError)
            self.assertIsInstance(caught.exception.__cause__.__context__, subprocess.TimeoutExpired)
            self.assertNotIsInstance(caught.exception, PermissionError)
        finally:
            # Deliberately mocked cleanup above; the test owns and reaps its
            # real benign process independently even if an assertion fails.
            for child in launched:
                child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
