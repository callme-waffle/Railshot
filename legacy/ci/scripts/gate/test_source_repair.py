"""Actual native unit tests plus mocked container transport; no model/cloud calls."""
import base64
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import gate
import quality
import repair
from test_pipeline import imported_workspace
import loop
from runner.run_agent import apply_files, source_change_allowed, writable_rules, record_plan, proposal_rejection

TEST = """import test from 'node:test';
import assert from 'node:assert/strict';
import { calculate } from '../src/calculator.mjs';
test('calculator preserves arithmetic and rejects division by zero', () => {
  assert.strictEqual(calculate(2, 3, '+'), 5);
  assert.strictEqual(calculate(8, 2, '/'), 4);
  assert.throws(() => calculate(1, 0, '/'), /zero/);
});
"""
SOURCE = """export function calculate(a, b, operator) {
  if (operator === '+') return a + b;
  if (operator === '/' && b !== 0) return a / b;
  throw new Error('division by zero');
}
"""


class SourceRepairTest(unittest.TestCase):
    def test_native_behavior_tests_match_writer_contract(self):
        variants = [TEST.replace("import assert from 'node:assert/strict';", "import { strictEqual, throws } from 'node:assert/strict';")
                    .replace("assert.strictEqual", "strictEqual").replace("assert.throws", "throws"),
                    TEST.replace("import { calculate } from '../src/calculator.mjs';",
                                 "const { calculate } = await import(new URL('../src/calculator.mjs', import.meta.url));")]
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp); (ws / "src").mkdir(); (ws / "tests").mkdir()
            app, test = ws / "src/calculator.mjs", ws / "tests/calculator.test.mjs"
            for content in variants:
                source_change_allowed("tests/calculator.test.mjs", content, None)
                test.write_text(content)
                for source, passed in ((SOURCE, True), (SOURCE.replace("a + b", "a - b"), False)):
                    app.write_text(source)
                    result = subprocess.run(["node", "--test", str(test)], capture_output=True, timeout=10)
                    self.assertEqual(passed, result.returncode == 0)
        vite = TEST.replace("import { calculate } from '../src/calculator.mjs';",
                            "const { calculate } = await server.ssrLoadModule('/src/calculator.ts');")
        source_change_allowed("tests/calculator.test.mjs", vite, None)
        for invalid in (vite.replace("/src/calculator.ts", "https://example.com/fake.ts"),
                        "import {strictEqual} from 'node:assert/strict'; import '../src/calculator.mjs'; strictEqual(true,true);"):
            with self.assertRaises(ValueError):
                source_change_allowed("tests/calculator.test.mjs", invalid, None)
        before = json.dumps({"scripts": {}})
        for script in ("node --test", "node --test tests/calculator.test.mjs", "vitest --environment node", "vitest run --environment jsdom", "jest"):
            source_change_allowed("package.json", json.dumps({"scripts": {"test": script}}), before)
        with self.assertRaises(ValueError) as rejected:
            source_change_allowed("package.json", json.dumps({"scripts": {"test": "node --experimental-strip-types --test"}}), before)
        detail = proposal_rejection(rejected.exception)
        self.assertEqual("TEST_SCRIPT_UNSUPPORTED", detail["reason"])
        self.assertIn("node --test", detail["guidance"])
        self.assertIn("ssrLoadModule", detail["guidance"])

    def test_zero_collected_is_repairable_but_missing_report_and_custom_runner_are_not(self):
        collected = quality.quality_failure("No tests found", 204, repair_scope="source")
        self.assertTrue(collected["source_repair_eligible"])
        for code in (0, 200, 205):
            self.assertTrue(quality.quality_failure("No tests found", code, repair_scope="source").get("blocked"))
        for script, repairable in ((None, True), ('echo "Error: no test specified" && exit 1', True), ('react-scripts test', False)):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                package = {"scripts": {"test": script} if script else {}}
                (ws / "package.json").write_text(json.dumps(package))
                plan = quality.discovery_blocked(ValueError("UNSUPPORTED_TEST_REPORTER"), ".")
                with patch.object(quality, "discover", return_value=[plan]):
                    result = quality.run_quality(ws, ws / "run", repair_scope="source")
                self.assertEqual(repairable, result.get("source_repair_eligible", False))
        self.assertTrue(quality.quality_failure("Cannot find package jsdom", 204, repair_scope="source")["source_repair_eligible"])
        self.assertTrue(quality.quality_failure("Cannot find package jsdom; API_KEY required", 204, repair_scope="source").get("blocked"))

    def test_host_plans_all_gates_before_writes_for_every_failure(self):
        for layer, failure_class in (("L0", "F5"), ("L1", "F5"),
                                     ("L2", "F3"), ("L4", "F6"), ("L3", "F7")):
            with self.subTest(layer=layer), tempfile.TemporaryDirectory() as tmp:
                verdict = {"ok": False, "layers": [{"layer": layer, "ok": False}],
                           "failure": {"class": failure_class, "layer": layer, "signature": layer, "source_repair_eligible": layer == "Q"}}
                self.assertIsNone(loop.decide(verdict, None, set(), "source"))
                self.assertIsNone(loop.decide(verdict, None, {layer}, "source"))
                from runner.test_repair_evidence import case_fixture, bind_proposal
                ws = Path(tmp) / "work"; ws.mkdir()
                source = ws / "app.py"; source.write_text("original = True\n")
                raw = case_fixture(ws, Path(tmp))
                output = {"status": "proposed", "root_cause": layer + " evidence",
                          "files_changed": [{"path": "app.py", "why": "fix the observed cause"}],
                          "files": [{"path": "app.py", "content": "original = False\n"}]}
                with self.assertRaises(ValueError):
                    record_plan(Path(tmp), "fixer", output, ("L2",))
                output = bind_proposal(Path(tmp), output)
                record_plan(Path(tmp), "fixer", output, workspace=ws, expected=raw)
                receipt = json.loads((Path(tmp) / "fixer-plan.json").read_text())
                self.assertFalse(receipt["execution_verified"])
                self.assertEqual("original = True\n", source.read_text())
                self.assertEqual(list(gate.ORDER), [step["gate"] for step in receipt["gate_plan"]])
                prompt = loop.task_text("fixer", 1, 3, Path(tmp), None, "source")
                self.assertIn("reruns all gates from L0", prompt)
        for reason in ("missing secret", "network unavailable", "SDK outcome unknown"):
            verdict = {"ok": False, "layers": [{"layer": "Q", "blocked": reason}]}
            self.assertIn("blocked", loop.decide(verdict, None, set(), "source"))

    def test_no_tests_to_real_tests_then_source_regression_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = {"type": "module", "scripts": {"lint": "eslint ."}}
            ws = imported_workspace(tmp, {"package.json": json.dumps(package), "package-lock.json": "{}",
                                          "src/calculator.mjs": SOURCE})
            run = Path(tmp) / "quality"
            legacy = quality.run_quality(ws, run)
            self.assertEqual("BLOCKED", legacy["status"])
            initial = quality.run_quality(ws, run, repair_scope="source")
            self.assertEqual("FAIL", initial["status"])
            self.assertTrue(initial["source_repair_eligible"])
            self.assertIn("NO_TESTS", initial["errors"][0])
            verdict = {"ok": False, "failure": {"class": "QUALITY", "layer": "Q", "signature": "missing",
                        "source_repair_eligible": True}, "layers": [{"layer": "Q"}]}
            self.assertIn("stop:", loop.decide(verdict, None, set(), "source"))
            allow, deny = writable_rules("contract/paths.yaml", "source")
            package["scripts"]["test"] = "node --test"
            proposal = [{"path": "package.json", "content": json.dumps(package)}, {"path": "tests/calculator.test.mjs", "content": TEST}]
            apply_files(ws, proposal, allow, deny, repair_scope="source")
            plan = quality.discover(ws)[0]
            self.assertNotIn("blocked", plan)
            self.assertIn("node --test --test-reporter=junit", "\n".join(plan["commands"]))
            native_run = subprocess.run
            def execute(command, **kwargs):
                if command[:2] == ["docker", "rm"]:
                    return SimpleNamespace(returncode=0)
                # Transport is offline; exercise the generated test against real source.
                native = native_run(["node", "--test", "--test-reporter=junit", "tests/calculator.test.mjs"],
                                    cwd=ws, capture_output=True, text=True, timeout=10)
                kwargs["stdout"].write(("RAILSHOT_JUNIT_BEGIN\n" + native.stdout + "\nRAILSHOT_JUNIT_END\n").encode())
                return SimpleNamespace(returncode=204 if native.returncode else 0)
            with patch.object(gate, "require_ci_network"), patch.object(quality.subprocess, "run", side_effect=execute):
                checked = gate.run_gate(ws, Path(tmp) / "gate", ["L0", "Q"], repair_scope="source", quality_network="trusted")
                self.assertTrue(checked["checks_ok"], checked)
                self.assertFalse(checked["release_eligible"])  # Partial gates never authorize deployment.
                self.assertEqual(1, checked["layers"][1]["projects"][0]["tests"])
                (ws / "src/calculator.mjs").write_text(SOURCE.replace("a + b", "a - b"))
                broken = gate.run_gate(ws, Path(tmp) / "broken", ["L0", "Q"], repair_scope="source", quality_network="trusted")
                self.assertEqual("unit", broken["layers"][1]["check"])
                self.assertEqual("FAIL", broken["layers"][1]["outcome"])
                self.assertTrue(broken["layers"][1]["advisory"])
                self.assertIsNone(broken["failure"])
                self.assertEqual("INCOMPLETE", broken["status"])
            with self.assertRaisesRegex(ValueError, "immutable"):
                apply_files(ws, [{"path": "tests/calculator.test.mjs", "content": TEST.replace(", 5)", ", -1)")}], allow, deny, repair_scope="source")

    def test_writer_and_gate_reject_weakened_tests_escape_secrets_and_manifest_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = {"scripts": {"test": "node --test"}, "dependencies": {"react": "19.0.0"}}
            ws = imported_workspace(tmp, {"package.json": json.dumps(package), "tests/calculator.test.mjs": TEST,
                                          "src/calculator.mjs": SOURCE})
            allow, deny = writable_rules("contract/paths.yaml", "source")
            for path, content in (("../escape.js", SOURCE), ("/tmp/escape.js", SOURCE), (".env.local", "TOKEN=x"),
                                  ("tests/calculator.test.mjs", TEST.replace(", 5)", ", 0)")),
                                  ("tests/empty.test.mjs", "import test from 'node:test'; test('empty',()=>{});"),
                                  ("tests/fake.test.mjs", "import test from 'node:test'; import assert from 'node:assert/strict'; import '../src/calculator.mjs'; test('fake',()=>assert.strictEqual(true,true));"),
                                  ("tests/skip.test.mjs", TEST.replace("test(", "test.skip(")),
                                  ("package-lock.json", "{}"), ("eslint.config.js", "export default [];")):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    apply_files(ws, [{"path": path, "content": content}], allow, deny, repair_scope="source")
            for replacement in ({"scripts": {"test": "echo ok"}}, {**package, "dependencies": {"react": "20.0.0"}},
                                {**package, "jest": {"testPathIgnorePatterns": ["."]}}):
                with self.assertRaises(ValueError):
                    source_change_allowed("package.json", json.dumps(replacement), json.dumps(package))
            (ws / "tests/calculator.test.mjs").write_text(TEST.replace(", 5)", ", 0)"))
            policies = gate.yaml.safe_load((gate.PLATFORM / "contract/paths.yaml").read_text())
            self.assertTrue(any("immutable" in e for e in gate.l0(ws, policies, repair_scope="source")[0]))

    def test_native_lock_receipt_only_accepts_exact_generated_bytes_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = imported_workspace(tmp, {"package.json": '{"name":"calculator","version":"1.0.0"}'})
            original_run = repair.run_bounded
            commands = []
            def native(command, **kwargs):
                if command[:2] == ["docker", "rm"]:
                    return SimpleNamespace(returncode=0)
                if command[:2] != ["docker", "run"]:
                    return original_run(command, **kwargs)
                commands.append(command)
                with tempfile.TemporaryDirectory() as native_tmp:
                    Path(native_tmp, "package.json").write_bytes((ws / "package.json").read_bytes())
                    # A real no-dependency npm resolution; no registry call is needed.
                    original_run(["npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"],
                                 cwd=native_tmp, timeout=30, check=True)
                    data = Path(native_tmp, "package-lock.json").read_bytes()
                return SimpleNamespace(returncode=0, stdout="RAILSHOT_NATIVE_LOCK=" + base64.b64encode(data).decode() + "\n")
            with patch.object(gate, "require_ci_network"), patch.object(repair, "run_bounded", side_effect=native):
                receipts = repair.prepare_locks(ws, Path(tmp) / "prepare", network="trusted")
                package = json.loads((ws / "package.json").read_text())
                package["scripts"] = {"test": "node --test"}
                (ws / "package.json").write_text(json.dumps(package))
                receipts = repair.prepare_locks(ws, Path(tmp) / "prepare", network="trusted")
            self.assertEqual(1, len(commands))  # Adding only a script rebinds unchanged native lock evidence.
            command = commands[0]
            self.assertIn("65532:65532", command)
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertNotIn("--privileged", command)
            self.assertTrue(repair.verified_lock(ws, "package-lock.json", receipts))
            policies = gate.yaml.safe_load((gate.PLATFORM / "contract/paths.yaml").read_text())
            self.assertEqual([], gate.l0(ws, policies, repair_scope="source", native_locks=receipts)[0])
            self.assertTrue(gate.l0(ws, policies, repair_scope="source")[0])
            (ws / "package-lock.json").write_text("{}")
            self.assertFalse(repair.verified_lock(ws, "package-lock.json", receipts))


if __name__ == "__main__":
    unittest.main()
