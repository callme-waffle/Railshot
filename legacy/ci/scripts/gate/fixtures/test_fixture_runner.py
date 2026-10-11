"""Local orchestration regression checks; these do not claim container E2E."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
import e2e

spec = importlib.util.spec_from_file_location("fixture_generator", ROOT / "generate.py")
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


class FixtureRunnerTests(unittest.TestCase):
    def test_private_source_variant_preserves_bytes_and_requires_private_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'npm-js'
            shutil.copytree(ROOT / 'npm-js', target)
            before = {p.relative_to(target): p.read_bytes() for p in target.rglob('*') if p.is_file()}
            self.assertEqual('PASS', e2e.variant(target, 'npm-js', 'private-source'))
            self.assertEqual(0o700, target.stat().st_mode & 0o777)
            for name, content in before.items():
                self.assertEqual(content, (target / name).read_bytes())
                self.assertEqual(0o600, (target / name).stat().st_mode & 0o777)

    def test_new_public_fixture_inputs_remain_readable_under_private_umask(self):
        for stack, filename in (("yarn-js", ".yarnrc.yml"), ("requirements-fastapi", "requirements.txt")):
            with self.subTest(stack=stack), tempfile.TemporaryDirectory() as directory, \
                    patch.object(generator.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)):
                previous = os.umask(0o077)
                try:
                    generator.generate(stack, Path(directory), "fixture-egress", 30)
                finally:
                    os.umask(previous)
                self.assertEqual((Path(directory) / stack / filename).stat().st_mode & 0o777, 0o644)

    def test_native_generation_failure_never_becomes_generated(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0 if command[1] == "rm" else 7)

        with tempfile.TemporaryDirectory() as directory, patch.object(generator.subprocess, "run", run):
            receipt = generator.generate("npm-js", Path(directory), "fixture-egress", 30)
            self.assertEqual("FAILED", receipt["status"])
            self.assertEqual(7, receipt["exit_code"])
            self.assertNotIn("generated_sha256", receipt)
            self.assertTrue(receipt["cleanup_ok"])
            self.assertTrue(any("/tmp:rw,exec," in arg for arg in calls[0]))
            self.assertIn('test "$(npm --version)" = 12.2.0', calls[0][-1])
            self.assertEqual(receipt, json.loads((Path(directory) / "npm-js-generation.json").read_text()))

    def test_unit_variant_changes_app_but_preserves_test_assertions(self):
        for stack in e2e.STACKS:
            with self.subTest(stack=stack), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / stack
                shutil.copytree(ROOT / stack, target)
                before = {p.relative_to(target): p.read_bytes() for p in target.rglob("*") if p.is_file()}
                self.assertEqual("FAIL", e2e.variant(target, stack, "unit"))
                changed = [name for name, content in before.items() if (target / name).read_bytes() != content]
                self.assertEqual([Path(e2e.SOURCE[stack])], changed)
                self.assertIn('return "broken"', (target / e2e.SOURCE[stack]).read_text())

    def test_unrelated_block_does_not_satisfy_negative_case(self):
        q = {"status": "BLOCKED", "blocked": "QUALITY_EXECUTION_BLOCKED"}
        self.assertFalse(e2e.matches_quality("npm-js", "missing-tool", "BLOCKED", q, "Docker daemon unavailable"))
        self.assertTrue(e2e.matches_quality("npm-js", "missing-tool", "BLOCKED", q, "railshot_checker_not_installed: not found"))
        q = {"status": "BLOCKED", "blocked": "QUALITY_DEPENDENCY_OR_ENV_BLOCKED", "projects": [{"exit_code": 204}]}
        self.assertFalse(e2e.matches_quality("fastapi", "missing-env", "BLOCKED", q, "dependency download failed"))
        self.assertTrue(e2e.matches_quality("fastapi", "missing-env", "BLOCKED", q, "RuntimeError: API_KEY required"))

    def test_js_missing_env_is_startup_failure_not_opaque_http_500(self):
        for stack in ("npm-js", "npm-ts", "nextjs"):
            with self.subTest(stack=stack), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / stack
                shutil.copytree(ROOT / stack, target)
                self.assertEqual("BLOCKED", e2e.variant(target, stack, "missing-env"))
                text = (target / e2e.SOURCE[stack]).read_text()
                self.assertTrue(text.startswith('if (!process.env.API_KEY) throw new Error("API_KEY required");\n'))

    def test_missing_generation_evidence_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            fixtures, output = Path(directory) / "fixtures", Path(directory) / "output"
            fixtures.mkdir()
            output.mkdir()
            result = e2e.execute(fixtures, output, "npm-js", "good", "quality", "fixture-egress", 30)
            self.assertEqual("BLOCKED", result["assertion"])
            self.assertFalse(result["release_eligible"])
            self.assertEqual("NOT_RUN", result["quality_status"])


if __name__ == "__main__":
    unittest.main()
