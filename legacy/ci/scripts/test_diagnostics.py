import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from diagnostics import Diagnostics, bounded, fingerprint, locations, redact
from source_snapshot import source_digest
from source_snapshot import capture


class DiagnosticsTest(unittest.TestCase):
    def test_redaction_bounds_and_causal_fingerprint(self):
        token = 'apikey_' + 'a' * 32
        value, omitted = bounded('Authorization: Bearer ' + token + '\n' + '가' * 100000, 16000)
        self.assertNotIn(token, value)
        self.assertLessEqual(len(value.encode()), 16000)
        self.assertGreater(omitted, 0)
        self.assertEqual(redact('TYPESAFE_API_KEY=' + token), 'TYPESAFE_[REDACTED]')
        self.assertNotEqual(fingerprint('L2', 'F2', 'web: build failed\nerror TS2322 at src/a.ts:3'),
                            fingerprint('L2', 'F2', 'web: build failed\nerror TS2307 at src/a.ts:3'))

    def test_reference_cannot_escape_or_invent_lines(self):
        files = {'src/app.ts': {'sha256': 'a' * 64, 'lines': 3}}
        refs = locations('src/app.ts:2:4\n/etc/a.ts:1\n../src/app.ts:2\nsrc/app.ts:4\nvirtual.ts:1', files)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['line'], 2)

    def test_snapshot_binding_and_disabled_gates(self):
        with tempfile.TemporaryDirectory() as root:
            ws, run = Path(root) / 'source', Path(root) / 'run'; ws.mkdir(); run.mkdir()
            (ws / 'app.ts').write_text('let x: number = "no";\n')
            env = {'SOURCE_COMMIT': 'a' * 40, 'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '123',
                   'GITHUB_RUN_ATTEMPT': '1', 'APP': 'demo-app', 'TENANT': 'demo', 'TARGET_ID': 'aws-demo'}
            with patch.dict(os.environ, env):
                diagnostic = Diagnostics(ws, run, 'run123', 'run123:0', ['L0', 'L1', 'L2', 'L3'], 'packaging', source_digest(ws))
                diagnostic.capture()
            diagnostic.layer = 'L2'
            diagnostic.process(['docker', 'buildx', 'build'], subprocess.CompletedProcess([], 1, '', 'error TS2322 app.ts:1:5'), time.monotonic())
            diagnostic.finish({'failure': {'layer': 'L2', 'class': 'F2', 'excerpt': 'error TS2322 app.ts:1:5'},
                               'layers': [{'layer': 'L0', 'outcome': 'PASS'}, {'layer': 'L1', 'outcome': 'PASS'}, {'layer': 'L2', 'outcome': 'FAIL'}],
                               'source_sha256': source_digest(ws), 'release_eligible': False, 'status': 'FAIL'})
            case = json.loads((run / 'diagnostics/case.json').read_text())
            self.assertEqual(case['binding']['run_id'], 123)
            self.assertEqual(case['failure']['locations'][0]['path'], 'app.ts')
            checks = {c['check_id']: c for c in case['checks']}
            self.assertEqual(checks['L3']['outcome'], 'NOT_RUN')
            self.assertFalse(checks['Q']['required']); self.assertFalse(checks['L4']['required'])
            self.assertFalse(case['classification']['grants_write_authority'])
            self.assertEqual(case['source']['tested_sha256'], case['source']['after_sha256'])

    def test_key_and_links_prevent_source_export(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            file = ws / 'app.js'; file.write_text('apikey_' + 'x' * 32)
            with self.assertRaises(ValueError): capture(ws)
            file.unlink(); file.symlink_to('/etc/passwd')
            with self.assertRaises(ValueError): capture(ws)

    def test_changed_source_marks_evidence_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            ws, run = Path(root) / 'ws', Path(root) / 'run'; ws.mkdir(); run.mkdir()
            (ws / 'a.js').write_text('x')
            diagnostic = Diagnostics(ws, run, 'run', 'run:0', ['L0'], 'packaging', 'a' * 64)
            diagnostic.capture()
            self.assertIsNone(diagnostic.snapshot)
            self.assertIn('source_snapshot_unavailable', diagnostic.missing)
