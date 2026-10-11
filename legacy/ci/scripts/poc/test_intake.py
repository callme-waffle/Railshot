"""Real local intake integration; no source execution or database connections."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class IntakeDatabaseTests(unittest.TestCase):
    def test_scanner_is_in_real_intake_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload = root / 'upload'; upload.mkdir()
            source = b'import sqlite3\ndb = sqlite3.connect("data.sqlite")\n'
            (upload / 'app.py').write_bytes(source)
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('intake.py')),
                                     str(upload), str(root / 'work'), str(root / 'run')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads((root / 'run/ir.json').read_text())['database']
            self.assertEqual(evidence['facts']['engine'], 'sqlite')
            self.assertEqual(evidence['evidence_files'][0]['sha256'], hashlib.sha256(source).hexdigest())
            self.assertFalse(evidence['runtime_verified'])
            self.assertFalse(evidence['deployment_approved'])
            self.assertEqual(evidence['review']['support'], 'PERSISTENT_SQLITE_RENDERER_NOT_IMPLEMENTED')
            self.assertEqual((upload / 'app.py').read_bytes(), source)


if __name__ == '__main__':
    unittest.main()
