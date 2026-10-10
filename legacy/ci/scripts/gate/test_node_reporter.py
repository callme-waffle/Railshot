"""Node's native reporter contract, with real local Node runs when available."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import quality


class NodeReporterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sample.test.cjs").write_text("require('node:test')('works', () => {});\n")
        (self.root / "package-lock.json").write_text('{"lockfileVersion":3}\n')

    def plan(self, script="node --test", **scripts):
        (self.root / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint .", "test": script, **scripts}}))
        return quality.npm_plan(self.root)

    def framed(self, xml):
        return "RAILSHOT_JUNIT_BEGIN\n" + xml + "\nRAILSHOT_JUNIT_END\n"

    def test_flags_precede_verified_paths_and_globs_are_quoted(self):
        for script in ("node --test", "node --test sample.test.cjs", 'node --test "*.test.cjs"'):
            with self.subTest(script=script):
                plan = self.plan(script)
                command = plan["commands"][-2]
                self.assertEqual("junit", plan["report_format"])
                self.assertTrue(command.startswith("node --test --test-reporter=junit --test-reporter-destination=/tmp/unit.xml"))
                self.assertIn("RAILSHOT_JUNIT_BEGIN", plan["commands"][-1])
                if "*" in script:
                    self.assertIn("'*.test.cjs'", command)

    def test_custom_flags_commands_paths_and_hooks_are_not_rewritten(self):
        for script in ("node --test --test-only", "node --test; echo ok", "node --test && true",
                       "node --test $(echo sample.test.cjs)", "node --test ../sample.test.cjs",
                       "node --test /tmp/sample.test.cjs", "node --test missing.test.cjs", "node custom-runner.js"):
            with self.subTest(script=script), self.assertRaisesRegex(ValueError, "UNSUPPORTED_TEST_REPORTER"):
                self.plan(script)
        for hook in ("pretest", "posttest"):
            with self.subTest(hook=hook), self.assertRaisesRegex(ValueError, "UNSUPPORTED_TEST_REPORTER"):
                self.plan(**{hook: "node prepare.js"})

    def test_junit_counts_leaf_cases_without_nested_suite_double_counting(self):
        xml = '<testsuites><testsuite tests="2"><testcase name="one"/><testsuite tests="1"><testcase name="two"/></testsuite></testsuite></testsuites>'
        self.assertEqual(2, quality.test_count(self.framed(xml), "javascript", "junit"))
        self.assertEqual(0, quality.test_count(self.framed('<testsuites><testcase name="empty.test.cjs"/></testsuites>'), "javascript", "junit"))

    def test_junit_rejects_skip_todo_failure_invalid_and_spoofed_count(self):
        for element in ('<skipped type="todo"/>', '<skipped/>', '<failure/>', '<error/>'):
            xml = '<testsuites><testcase name="one"/>' + element + '</testsuites>'
            self.assertEqual(0, quality.test_count(self.framed(xml), "javascript", "junit"))
        for text in ("RAILSHOT_TESTS=999\n", self.framed("<broken>"), self.framed("<testsuites/>"),
                     self.framed('<testsuites><testcase name="one"/></testsuites>') * 2):
            self.assertEqual(0, quality.test_count(text, "javascript", "junit"))
            self.assertFalse(quality.zero_node_report(text))

    def test_existing_json_and_java_paths_remain_compatible(self):
        for name in ("jest", "vitest"):
            (self.root / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint .", "test": name}, "devDependencies": {name: "1.0.0"}}))
            plan = quality.npm_plan(self.root)
            self.assertEqual("json", plan["report_format"])
            self.assertIn("/tmp/unit.json", plan["commands"][-1])
        self.assertEqual(2, quality.test_count("RAILSHOT_TESTS=2\n", "javascript"))
        self.assertEqual(0, quality.test_count('<testsuite tests="2"></testsuite>', "maven"))

    @unittest.skipUnless(shutil.which("node"), "local Node runtime not installed")
    def test_native_pass_fail_skip_todo_empty_and_nested(self):
        cases = {
            "pass": ("test('works', () => assert.equal(1, 1));", 0, 1),
            "fail": ("test('fails', () => assert.equal(1, 2));", 204, 0),
            "skip": ("test.skip('later', () => {});", 0, 0),
            "todo": ("test.todo('later');", 0, 0),
            "empty": ("", 0, 0),
            "nested": ("describe('outer', () => { test('one', () => {}); describe('inner', () => test('two', () => {})); });", 0, 2),
        }
        for name, (body, exit_code, count) in cases.items():
            with self.subTest(name=name):
                (self.root / "sample.test.cjs").write_text("const {test, describe} = require('node:test'); const assert = require('node:assert/strict');\n" + body)
                plan = self.plan('node --test "*.test.cjs"')
                commands = [c.replace("/tmp/unit.xml", str(self.root / "unit.xml")) for c in plan["commands"][-2:]]
                self.assertEqual(["unit", "report"], [quality.command_stage(c) for c in commands])
                result = subprocess.run(["sh", "-c", quality.quality_script([(quality.command_stage(c), c) for c in commands])],
                                        cwd=self.root, capture_output=True, text=True, timeout=20)
                self.assertEqual(exit_code, result.returncode, result.stderr)
                self.assertEqual(count, quality.test_count(result.stdout, "javascript", "junit"))
                self.assertEqual(name == "empty", quality.zero_node_report(result.stdout))
                if name == "fail":
                    self.assertIn("ERR_ASSERTION", result.stdout)


if __name__ == "__main__":
    unittest.main()
