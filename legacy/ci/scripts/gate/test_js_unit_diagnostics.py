"""Failed Jest/Vitest reports reach the fixer without replacing the test exit code."""
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import quality


class JSUnitDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "app.test.js").write_text("// Existing fixture; runner is controlled below.\n")
        (self.root / "package-lock.json").write_text('{"lockfileVersion":3}\n')
        self.bin = self.root / "bin"
        self.bin.mkdir()
        runner = self.bin / "npm"
        runner.write_text('#!/bin/sh\nif [ "$WRITE_REPORT" = 1 ]; then printf "%s\\n" "$REPORT_JSON" > "$REPORT_PATH"; fi\nexit "$TEST_EXIT"\n')
        runner.chmod(0o755)
        self.env = {"PATH": str(self.bin) + ":/usr/bin:/bin", "WRITE_REPORT": "1", "TEST_EXIT": "1",
                    "REPORT_PATH": str(self.root / "unit.json"),
                    "REPORT_JSON": json.dumps({"success": False, "testResults": [{"message": "AssertionError: expected 2 to equal 1"}]})}

    def command(self, framework):
        (self.root / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint .", "test": framework},
                                                          "devDependencies": {framework: "1.0.0"}}))
        return quality.npm_plan(self.root)["commands"][-2].replace("/tmp/unit.json", shlex.quote(self.env["REPORT_PATH"]))

    def execute(self, command, *, wrapped=False):
        if wrapped:
            command = quality.quality_script([(quality.command_stage(command), command),
                                              ("report", "printf REPORT_STAGE_MUST_NOT_RUN")])
        return subprocess.run(["sh", "-e", "-c", command], env=self.env, cwd=self.root,
                              capture_output=True, text=True, timeout=5)

    def test_both_framework_reports_reach_unit_failure_diagnostics(self):
        for framework in ("jest", "vitest"):
            with self.subTest(framework=framework):
                command = self.command(framework)
                self.assertEqual("unit", quality.command_stage(command))
                result = self.execute(command, wrapped=True)
                self.assertEqual(204, result.returncode)
                self.assertIn("AssertionError: expected 2 to equal 1", result.stdout)
                self.assertNotIn("REPORT_STAGE_MUST_NOT_RUN", result.stdout)
                failure = quality.quality_failure(result.stdout, result.returncode)
                self.assertTrue(failure["source_repair_eligible"])
                self.assertIn("AssertionError", failure["errors"][0])

    def test_original_failure_code_survives_missing_report_and_read_failure(self):
        for framework in ("jest", "vitest"):
            for write_report in ("0", "1"):
                with self.subTest(framework=framework, report=write_report):
                    self.env.update(TEST_EXIT="2", WRITE_REPORT=write_report)
                    Path(self.env["REPORT_PATH"]).unlink(missing_ok=True)
                    fake_cat = self.bin / "cat"
                    fake_cat.write_text("#!/bin/sh\nexit 73\n")
                    fake_cat.chmod(0o755)
                    result = self.execute(self.command(framework))
                    self.assertEqual(2, result.returncode)

    def test_success_does_not_print_failure_report(self):
        self.env["TEST_EXIT"] = "0"
        for framework in ("jest", "vitest"):
            result = self.execute(self.command(framework))
            self.assertEqual(0, result.returncode)
            self.assertEqual("", result.stdout)


if __name__ == "__main__":
    unittest.main()
