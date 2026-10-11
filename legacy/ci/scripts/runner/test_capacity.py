"""Real process contention, without Docker/cloud/model calls."""
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capacity import slots


def contend(directory, active, peak, mutex, start):
    start.wait()
    with slots(Path(directory), 'gate', 2, timeout=10):
        with mutex:
            active.value += 1
            peak.value = max(peak.value, active.value)
        time.sleep(.08)
        with mutex:
            active.value -= 1


class CapacityTests(unittest.TestCase):
    def test_twelve_processes_obey_two_heavy_slots(self):
        ctx = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as directory:
            active, peak, mutex, start = ctx.Value('i', 0), ctx.Value('i', 0), ctx.Lock(), ctx.Event()
            children = [ctx.Process(target=contend, args=(directory, active, peak, mutex, start)) for _ in range(12)]
            for child in children:
                child.start()
            start.set()
            for child in children:
                child.join(12)
                if child.is_alive():
                    child.kill(); child.join()
                self.assertEqual(child.exitcode, 0)
            self.assertEqual(peak.value, 2)
            self.assertEqual(active.value, 0)

    def test_symlink_lock_and_invalid_limits_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'other'
            target.write_text('keep')
            (root / 'gate-0.lock').symlink_to(target)
            with self.assertRaises(OSError), slots(root, 'gate', 1):
                pass
            self.assertEqual(target.read_text(), 'keep')
            for limit in (0, 17, True):
                with self.assertRaises(ValueError), slots(root, 'gate', limit):
                    pass

    def test_exception_releases_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'failure'), slots(Path(directory), 'gate', 1):
                raise RuntimeError('failure')
            with slots(Path(directory), 'gate', 1, timeout=.1):
                pass


if __name__ == '__main__':
    unittest.main()
