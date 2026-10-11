"""Supervisor evidence only; no Docker, model or cloud calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gate
from progress import Progress


class ProgressTests(unittest.TestCase):
    def test_start_is_durable_before_check_and_receipt_matches_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, run = Path(tmp) / 'work', Path(tmp) / 'gate'
            ws.mkdir()
            def check(*args, **kwargs):
                rows = [json.loads(line) for line in (run / 'progress.jsonl').read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['outcome'], 'RUNNING')
                self.assertEqual(rows[0]['phase'], 'L0')
                return [], []
            with patch.object(gate, 'l0', side_effect=check):
                verdict = gate.run_gate(ws, run, ['L0'])
            rows = [json.loads(line) for line in (run / 'progress.jsonl').read_text().splitlines()]
            self.assertEqual(rows[-1], verdict['layers'][0]['event'])
            self.assertEqual(rows[-1]['attributes']['completed_steps'], 1)
            self.assertGreaterEqual(rows[-1]['attributes']['duration_s'], 0)
            self.assertEqual(verdict['status'], 'INCOMPLETE')
            self.assertEqual((run / 'progress.jsonl').stat().st_mode & 0o777, 0o600)

    def test_existing_journal_blocks_reexecution(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, run = Path(tmp) / 'work', Path(tmp) / 'gate'
            ws.mkdir(); run.mkdir(); (run / 'progress.jsonl').write_text('prior evidence\n')
            with patch.object(gate, 'l0') as check:
                verdict = gate.run_gate(ws, run, ['L0'])
            check.assert_not_called()
            self.assertEqual(verdict['status'], 'UNKNOWN')
            self.assertEqual((run / 'progress.jsonl').read_text(), 'prior evidence\n')

    def test_journal_write_failure_cannot_be_release_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / 'work'; ws.mkdir()
            with patch.object(Progress, 'append', side_effect=Progress.failure(OSError('synthetic'))), \
                    patch.object(gate, 'l0') as check:
                verdict = gate.run_gate(ws, Path(tmp) / 'gate', ['L0'])
            check.assert_not_called()
            self.assertEqual(verdict['status'], 'UNKNOWN')
            self.assertEqual(verdict['error']['code'], 'OBSERVATION_WRITE_FAILED')


if __name__ == '__main__':
    unittest.main()
