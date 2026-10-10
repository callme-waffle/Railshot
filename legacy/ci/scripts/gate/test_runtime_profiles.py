"""Offline plan checks; actual stack execution is recorded by e2e.py separately."""
import json
from pathlib import Path
import tempfile
import unittest

import quality


class JavascriptProfilesTest(unittest.TestCase):
    def test_managers_share_checks_and_preserve_native_lock_policy(self):
        for name, version, lock, policy in (
            ('npm', '10.9.2', 'package-lock.json', 'npm ci --engine-strict'),
            ('pnpm', '10.17.1', 'pnpm-lock.yaml', '--frozen-lockfile'),
            ('yarn', '4.10.3', 'yarn.lock', '--immutable'),
            ('yarn', '1.22.22', 'yarn.lock', '--frozen-lockfile')):
            with self.subTest(name=name, version=version), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                pkg = {'packageManager': name + '@' + version, 'scripts': {'lint': 'eslint .', 'test': 'vitest'},
                       'devDependencies': {'eslint': '10.11.0', 'vitest': '5.0.0'}}
                (root / 'package.json').write_text(json.dumps(pkg))
                (root / lock).write_text('{}')
                (root / 'app.test.js').write_text("test('sum',()=>expect(1+1).toBe(2))")
                plan = quality.npm_plan(root)
                self.assertEqual(name, plan['package_manager'])
                self.assertTrue(any(policy in c for c in plan['commands']))
                stages = [quality.command_stage(c) for c in plan['commands']]
                self.assertIn('lint', stages)
                self.assertIn('unit', stages)
                self.assertEqual(pkg, json.loads((root / 'package.json').read_text()))

    def test_missing_linter_gets_run_scoped_tools_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pkg = {'scripts': {'test': 'vitest'}, 'devDependencies': {'vitest': '5.0.0'}}
            (root / 'package.json').write_text(json.dumps(pkg))
            (root / 'package-lock.json').write_text('{}')
            (root / 'app.test.js').write_text("test('sum',()=>expect(1+1).toBe(2))")
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            plan = quality.npm_plan(root)
            self.assertIn('eslint', plan['generated_tools'])
            self.assertTrue(any('--prefix /tmp/quality-tools' in c for c in plan['commands']))
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_conflicting_manager_inputs_are_not_silently_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'package-lock.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'CONFLICT'):
                quality.javascript_manager(root, {'packageManager':'pnpm@10.17.1'})
            (root / 'yarn.lock').write_text('')
            with self.assertRaisesRegex(ValueError, 'AMBIGUOUS'):
                quality.javascript_manager(root, {})


if __name__ == '__main__':
    unittest.main()
