"""Python preparation contracts; offline tests never install packages or run Docker."""
import json
import hashlib
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import quality


class PythonProfilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "test_app.py").write_text("def test_existing(): assert True\n")

    def project(self, packages=()):
        (self.root / "pyproject.toml").write_text('[project]\nname="fixture"\nversion="0.1.0"\nrequires-python=">=3.12"\n')
        (self.root / "uv.lock").write_text("version = 1\n" + "".join(
            f'[[package]]\nname = "{name}"\nversion = "{version}"\n' for name, version in packages))
        return quality.python_plan(self.root)

    def code(self, plan, marker):
        command = next(c for c in plan["commands"] if marker in c)
        return shlex.split(command)[-1].replace("/tmp/railshot-python", str(self.root / "artifacts"))

    def run_code(self, code, packages):
        # Real generated Python code, with controlled installed package metadata.
        prelude = "import importlib.metadata as M\nfrom types import SimpleNamespace\n"
        prelude += f"installed = {packages!r}\n"
        prelude += "M.distributions = lambda: [SimpleNamespace(metadata={'Name': n}, version=v) for n, v in installed.items()]\n"
        prelude += "M.version = lambda name: installed[name]\n"
        return subprocess.run([sys.executable, "-c", prelude + code], capture_output=True, text=True, timeout=5)

    def test_uv_missing_tools_are_pinned_without_mutating_inputs(self):
        self.project((("fastapi", "0.115.0"),))
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        plan = quality.python_plan(self.root)
        self.assertEqual({"ruff": "0.16.9", "mypy": "2.3.1", "pytest": "9.1.1"}, plan["generated_tools"])
        self.assertIn("uv sync --locked --all-groups --no-progress", plan["commands"])
        self.assertTrue(any("UV_PYTHON=python" in c for c in plan["commands"]))
        self.assertTrue(all("--with-requirements" in c for c in plan["commands"] if quality.command_stage(c) in {"lint", "type", "unit"}))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_pytest_exit_five_repairs_only_a_valid_native_zero_test_report(self):
        plan = self.project()
        command = next(c for c in plan['commands'] if 'pytest --junitxml' in c)
        wrapper = 'sh -c "exit 5" || ' + command.split(' || ', 1)[1]
        wrapper = wrapper.replace('/tmp/unit.xml', str(self.root / 'unit.xml'))
        for xml, expected in (('<testsuites><testsuite tests="0" failures="0" errors="0" skipped="0"/></testsuites>', 204),
                              ('<broken>', 200), ('<testsuites/>', 200),
                              ('<testsuites><testsuite tests="0" errors="1"/></testsuites>', 200)):
            with self.subTest(xml=xml):
                (self.root / 'unit.xml').write_text(xml)
                result = subprocess.run(['sh', '-c', quality.quality_script([('unit', wrapper)])],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(expected, result.returncode, result.stderr)
                classified = quality.quality_failure(result.stdout, result.returncode, repair_scope='source')
                self.assertEqual(expected == 204, classified.get('source_repair_eligible', False))
        (self.root / 'unit.xml').unlink()
        missing = subprocess.run(['sh', '-c', quality.quality_script([('unit', wrapper)])], capture_output=True, text=True, timeout=5)
        self.assertEqual(200, missing.returncode)

    def test_existing_locked_checker_versions_are_not_replaced(self):
        plan = self.project((("ruff", "0.9.0"), ("mypy", "1.14.1"), ("pytest", "8.3.4")))
        self.assertEqual({}, plan["generated_tools"])
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        (artifacts / "app-requirements.txt").write_text("pytest==8.3.4\nruff==0.9.0\nmypy==1.14.1\n")
        result = self.run_code(self.code(plan, "RAILSHOT_PYTHON_PREPARED="), {"ruff": "0.9.0", "mypy": "1.14.1", "pytest": "8.3.4"})
        self.assertEqual(0, result.returncode, result.stderr)
        record = json.loads((artifacts / "provenance.json").read_text())
        self.assertEqual({}, record["generated_tools"])
        self.assertEqual("8.3.4", record["retained_tools"]["pytest"])
        self.assertNotIn("9.1.1", (artifacts / "overlay.in").read_text())

    def test_overlay_freezes_app_dependencies_and_only_adds_missing_tools(self):
        plan = self.project()
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        (artifacts / "app-requirements.txt").write_text("fastapi==0.115.0\npytest==8.3.4\n")
        result = self.run_code(self.code(plan, "RAILSHOT_PYTHON_PREPARED="), {"fastapi": "0.115.0", "pytest": "8.3.4"})
        self.assertEqual(0, result.returncode, result.stderr)
        overlay = (artifacts / "overlay.in").read_text()
        self.assertIn("fastapi==0.115.0", overlay)
        self.assertIn("pytest==8.3.4", overlay)
        self.assertIn("ruff==0.16.9", overlay)
        self.assertNotIn("pytest==9.1.1", overlay)
        verify = self.code(plan, "RAILSHOT_PYTHON_PROVENANCE=")
        packages = {"fastapi": "0.115.0", "pytest": "8.3.4", "ruff": "0.16.9", "mypy": "2.3.1"}
        self.assertEqual(0, self.run_code(verify, packages).returncode)
        packages["fastapi"] = "0.116.0"
        failure = self.run_code(verify, packages)
        self.assertNotEqual(0, failure.returncode)
        self.assertIn("APP_DEPENDENCY_CHANGED_BY_TOOL_OVERLAY", failure.stderr)

    def test_requirements_compiles_hashes_then_syncs_before_checks(self):
        (self.root / "requirements.txt").write_text("fastapi==0.115.0\n")
        original = (self.root / "requirements.txt").read_bytes()
        plan = quality.python_plan(self.root)
        commands = plan["commands"]
        compile_at = next(i for i, c in enumerate(commands) if "uv pip compile" in c)
        sync_at = next(i for i, c in enumerate(commands) if "uv pip sync" in c)
        checks_at = next(i for i, c in enumerate(commands) if quality.command_stage(c) == "lint")
        self.assertLess(compile_at, sync_at)
        self.assertLess(sync_at, checks_at)
        self.assertIn("--generate-hashes", commands[compile_at])
        self.assertIn("--require-hashes", commands[sync_at])
        self.assertIn("--active", commands[checks_at])
        self.assertEqual(original, (self.root / "requirements.txt").read_bytes())

    def test_artifact_record_is_allowlisted_hash_bound_and_size_limited(self):
        plan = self.project()
        artifacts = self.root / "artifacts"
        artifacts.mkdir()
        (artifacts / "app-requirements.txt").write_text("fastapi==0.115.0\n")
        (artifacts / "requirements.lock").write_text("fastapi==0.115.0 --hash=sha256:fixture\n")
        packages = {"fastapi": "0.115.0", "pytest": "8.3.4", "ruff": "0.9.0", "mypy": "1.14.1"}
        self.assertEqual(0, self.run_code(self.code(plan, "RAILSHOT_PYTHON_PREPARED="), packages).returncode)
        (artifacts / "not-exported.txt").write_text("not part of the artifact contract")
        verify = self.code(plan, "RAILSHOT_PYTHON_PROVENANCE=")
        result = self.run_code(verify, packages)
        self.assertEqual(0, result.returncode, result.stderr)
        blocks = [line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("RAILSHOT_PYTHON_ARTIFACTS=")]
        self.assertEqual(1, len(blocks))
        exported = json.loads(blocks[0])
        self.assertEqual({"requirements.lock", "app-requirements.txt", "overlay.in", "provenance.json", "resolved-environment.json"}, set(exported))
        provenance = json.loads(exported["provenance.json"])
        for name, digest in provenance["artifacts"].items():
            self.assertEqual(digest, hashlib.sha256(exported[name].encode()).hexdigest())
        (artifacts / "app-requirements.txt").write_text("x" * (1024 * 1024))
        failure = self.run_code(verify, packages)
        self.assertNotEqual(0, failure.returncode)
        self.assertIn("PYTHON_ARTIFACTS_TOO_LARGE", failure.stderr)
        self.assertNotIn("RAILSHOT_PYTHON_ARTIFACTS=", failure.stdout)

    def test_preparation_and_provenance_cannot_be_misclassified_as_checks(self):
        plan = self.project()
        self.assertEqual(["lint", "type", "unit", "report"], [quality.command_stage(c) for c in plan["commands"]][-4:])
        self.assertTrue(all(quality.command_stage(c) == "prepare" for c in plan["commands"][:-4]))

    def test_requirements_with_pyproject_checker_config_preserves_config(self):
        (self.root / "requirements.txt").write_text("fastapi==0.115.0\n")
        config = '[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n'
        (self.root / "pyproject.toml").write_text(config)
        plan = quality.python_plan(self.root)
        self.assertEqual("run-scoped-hashed-requirements", plan["lock"])
        self.assertEqual(config, (self.root / "pyproject.toml").read_text())
        self.assertFalse(any("--config" in c for c in plan["commands"]))

    def test_tests_lock_and_supported_exact_python_are_still_required(self):
        self.project()
        (self.root / "test_app.py").unlink()
        with self.assertRaisesRegex(ValueError, "NO_TESTS"):
            quality.python_plan(self.root)
        (self.root / "test_app.py").write_text("def test_existing(): pass\n")
        (self.root / "uv.lock").unlink()
        with self.assertRaisesRegex(ValueError, "MISSING_LOCK"):
            quality.python_plan(self.root)
        self.project()
        (self.root / ".python-version").write_text("3.14.0")
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED"):
            quality.python_plan(self.root)


if __name__ == "__main__":
    unittest.main()
