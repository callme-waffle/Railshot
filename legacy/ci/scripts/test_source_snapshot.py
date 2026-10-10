import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import source_snapshot as snapshot
from bundle import source_digest


class SourceSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work, self.bundle = self.root / 'work', self.root / 'bundle'
        (self.work / '.railshot').mkdir(parents=True)
        self.bundle.mkdir()
        self.spec = b'apiVersion: railshot/v0\napp: demo-app\nservices:\n  - name: web\n    build: {dockerfile: Dockerfile}\n    port: 3000\n    route: /\n'
        (self.work / '.railshot/railshot.yaml').write_bytes(self.spec)
        (self.work / 'Dockerfile').write_text('FROM node:22\nUSER 10001\n')
        (self.work / 'src').mkdir()
        (self.work / 'src/generated.js').write_text('console.log("AI final source")\n')
        (self.work / 'src/generated.js').chmod(0o755)
        (self.work / '빈 폴더').mkdir()
        (self.work / '한글-😀.txt').write_text('Unicode source bytes')
        (self.work / '.git').mkdir()
        (self.work / '.git/config').write_text('excluded administrative metadata')
        self.env = {'SOURCE_COMMIT': 'a' * 40, 'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '789',
                    'GITHUB_RUN_ATTEMPT': '1', 'APP': 'demo-app', 'TENANT': 'demo', 'TARGET_ID': 'aws-demo'}
        self.bind_gate()

    def bind_gate(self):
        self.source = source_digest(self.work)
        verdict = {'ok': True, 'release_eligible': True, 'status': 'PASS', 'source_sha256': self.source,
                   'layers': [{'layer': layer, 'ok': True} for layer in ['L0', 'L1', 'Q', 'L2', 'L4', 'L3']],
                   'images': {'web': 'local/web:gate'}, 'image_ids': {'web': 'sha256:' + 'b' * 64}}
        files = {'railshot.yaml': self.spec, 'verdict.json': json.dumps(verdict).encode()}
        for name, content in files.items():
            (self.bundle / name).write_bytes(content)
        (self.bundle / 'manifest.json').write_text(json.dumps({'version': 1, 'trust': 'trusted-ci-artifact-not-a-signature',
            'source_sha256': self.source, 'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}))

    def export(self, name='snapshot.json'):
        output = self.root / 'source' / name
        snapshot.export(self.work, self.bundle, output, self.env)
        return output

    def test_exact_final_source_permissions_empty_directories_and_unicode_survive(self):
        output = self.export()
        value = json.loads(output.read_bytes())
        self.assertEqual(snapshot.entries_digest(value['entries']), self.source)
        self.assertEqual(value['run_id'], 789)
        entries = {row['path']: row for row in value['entries']}
        self.assertNotIn('.git', entries)
        self.assertEqual(entries['빈 폴더']['type'], 'd')
        self.assertEqual(entries['src/generated.js']['mode'], 0o755)
        self.assertIn(b'AI final source', base64.b64decode(entries['src/generated.js']['content']))
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        with self.assertRaises(ValueError):
            self.export()

    def test_source_or_mode_change_after_gate_cannot_be_exported(self):
        for change in ['bytes', 'mode']:
            with self.subTest(change=change):
                if change == 'bytes': (self.work / 'Dockerfile').write_text('modified')
                else: (self.work / 'Dockerfile').chmod(0o755)
                with self.assertRaises(ValueError): self.export()
                self.bind_gate()

    def test_secret_paths_tokens_links_and_limits_fail_before_writing(self):
        for name, content in [('.env', b'password=private'), ('private.pem', b'private'),
                              ('.npmrc', b'private'), ('auth.txt', b'-----BEGIN PRIVATE KEY-----'),
                              ('auth.txt', b'ghp_' + b'x' * 24), ('auth.txt', b'AKIA' + b'X' * 16)]:
            with self.subTest(name=name, content=content[:8]):
                file = self.work / name; file.write_bytes(content)
                try:
                    self.bind_gate()
                    with self.assertRaises(ValueError): self.export()
                finally: file.unlink()
        (self.work / 'link').symlink_to(self.work / 'Dockerfile')
        with self.assertRaises(ValueError): self.export()
        (self.work / 'link').unlink()
        os.link(self.work / 'Dockerfile', self.work / 'hardlink')
        self.bind_gate()
        with self.assertRaises(ValueError): self.export()
        (self.work / 'hardlink').unlink(); self.bind_gate()
        with patch.object(snapshot, 'MAX_FILES', 1), self.assertRaises(ValueError): self.export()
        with patch.object(snapshot, 'MAX_BYTES', 1), self.assertRaises(ValueError): self.export()
        self.assertFalse((self.root / 'source/snapshot.json').exists())

    def test_release_only_retry_rebinds_the_same_source_and_rejects_foreign_receipts(self):
        original = self.export()
        current = self.root / 'current/snapshot.json'
        env = {**self.env, 'GITHUB_RUN_ATTEMPT': '2'}
        snapshot.bind(original, self.bundle, current, env)
        value = json.loads(current.read_bytes())
        self.assertEqual(value['producer_attempt'], 2)
        self.assertEqual(value['source_sha256'], self.source)
        self.assertEqual(value['entries'], json.loads(original.read_bytes())['entries'])
        for field, changed in [('APP', 'foreign-app'), ('TARGET_ID', 'foreign'), ('GITHUB_RUN_ID', '790'), ('TENANT', 'other')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                snapshot.bind(original, self.bundle, self.root / 'other/snapshot.json', {**env, field: changed})
        with self.assertRaises(ValueError):
            snapshot.bind(current, self.bundle, self.root / 'older/snapshot.json', self.env)
        value['entries'][-1]['mode'] = 0
        current.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            snapshot.bind(current, self.bundle, self.root / 'tampered/snapshot.json', env)

    def test_workflow_binds_sources_to_exact_artifact_ids_and_retains_release_reruns(self):
        import yaml
        jobs = yaml.safe_load((Path(__file__).parents[1] / 'workflows/railshot-deploy.yml').read_text())['jobs']
        loop, release = jobs['loop'], jobs['release']
        self.assertEqual(loop['outputs']['source_id'], '${{ steps.source.outputs.artifact-id }}')
        source = next(step for step in loop['steps'] if step.get('id') == 'source')
        self.assertEqual(source['with']['name'], 'source-${{ github.run_attempt }}')
        self.assertEqual(source['with']['path'], 'source-snapshot/snapshot.json')
        exporter = next(step for step in loop['steps'] if step.get('name') == 'Export the exact gate-tested images')
        self.assertIn('source_snapshot.py export "$RUN_DIR/work" release-bundle', exporter['run'])
        downloads = [step['with'] for step in release['steps'] if step.get('uses', '').startswith('actions/download-artifact')]
        self.assertIn({'artifact-ids': '${{ needs.loop.outputs.source_id }}', 'path': 'source-snapshot', 'merge-multiple': True}, downloads)
        republished = next(step for step in release['steps'] if step.get('with', {}).get('name') == 'source-${{ github.run_attempt }}')
        self.assertEqual(republished['if'], 'needs.loop.outputs.source_attempt != github.run_attempt')
        self.assertLess(release['steps'].index(republished), next(i for i, step in enumerate(release['steps']) if step.get('id') == 'published'))


if __name__ == '__main__':
    unittest.main()
