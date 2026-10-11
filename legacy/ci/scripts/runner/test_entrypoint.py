"""Execute the real runner exit block against an isolated disposable workspace."""
from pathlib import Path
import os
import signal
import subprocess
import tempfile
import time
import unittest


class RunnerExitTest(unittest.TestCase):
    def test_success_failure_and_cancel_remove_only_own_checkout(self):
        block = Path(__file__).with_name('entrypoint.sh').read_text().split('# Keep durable run evidence outside checkout;', 1)[1]
        block = '# Keep durable run evidence outside checkout;' + block
        for mode in ('success', 'failure', 'cancel'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                own, other, evidence = root / 'own', root / 'other', root / 'evidence'
                for path in (own, other, evidence):
                    path.mkdir()
                    (path / 'keep').write_text('content')
                child = root / 'run.sh'
                child.write_text('#!/bin/bash\n' + (
                    'trap \'test -d "$work" || exit 99; exit 143\' TERM\ntouch ready\nwhile :; do sleep .1; done\n'
                    if mode == 'cancel' else 'exit ' + ('7' if mode == 'failure' else '0') + '\n'))
                child.chmod(0o700)
                process = subprocess.Popen(['bash', '-c', block], cwd=root, env={**os.environ, 'work': str(own)})
                try:
                    if mode == 'cancel':
                        deadline = time.monotonic() + 5
                        while not (root / 'ready').exists() and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue((root / 'ready').exists())
                        process.send_signal(signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=5), {'success': 0, 'failure': 7, 'cancel': 143}[mode])
                    self.assertFalse(own.exists())
                    self.assertEqual((other / 'keep').read_text(), 'content')
                    self.assertEqual((evidence / 'keep').read_text(), 'content')
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait()


if __name__ == '__main__':
    unittest.main()
