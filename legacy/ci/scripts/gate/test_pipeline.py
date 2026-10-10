"""Offline contract tests: no Docker daemon, agent, dependency install or cloud calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "loop"))
import gate
import quality
import loop


def workspace(tmp):
    path = Path(tmp) / "work"
    path.mkdir()
    return path


def imported_workspace(tmp, files):
    root = Path(tmp)
    upload, work, run = root / "upload", root / "work", root / "run"
    upload.mkdir()
    for name, content in files.items():
        path = upload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run([sys.executable, str(gate.PLATFORM / "poc/intake.py"), str(upload), str(work), str(run)],
                   check=True, capture_output=True, text=True)
    return work


class PipelineTest(unittest.TestCase):
    def test_trusted_app_identity_fails_at_l1_then_all_gates_accept_corrected_spec(self):
        spec = {"apiVersion": "railshot/v0", "app": "calculator", "services": [
            {"name": "web", "build": {"dockerfile": "Dockerfile"}, "port": 8080, "health": "/", "route": "/"}]}
        files = {".railshot/railshot.yaml": gate.yaml.safe_dump(spec), ".dockerignore": ".git\n.env*\n",
                 "Dockerfile": 'FROM node:22.23.3-bookworm-slim\nUSER 65532\nEXPOSE 8080\nCMD ["node", "app.js"]\n'}
        image_id = "sha256:" + "c" * 64
        with tempfile.TemporaryDirectory() as tmp:
            ws = imported_workspace(tmp, files)
            with patch.object(gate, "run_quality") as quality_check:
                failed = gate.run_gate(ws, Path(tmp) / "failed", list(gate.ORDER), app_id="fixture-npm-js")
                quality_check.assert_not_called()
            self.assertEqual([r["layer"] for r in failed["layers"]], ["L0", "L1"])
            self.assertEqual((failed["status"], failed["failure"]["class"]), ("FAIL", "F5"))
            self.assertIn("trusted app identity is fixture-npm-js", failed["failure"]["excerpt"])
            self.assertIsNone(loop.decide(failed, None, set()))
            self.assertEqual("calculator", gate.yaml.safe_load((ws / ".railshot/railshot.yaml").read_text())["app"])
            spec["app"] = "fixture-npm-js"
            (ws / ".railshot/railshot.yaml").write_text(gate.yaml.safe_dump(spec))
            original_shell = gate.sh
            def shell(cmd, **kwargs):
                return SimpleNamespace(stdout=image_id) if cmd[:3] == ["docker", "image", "inspect"] else original_shell(cmd, **kwargs)
            with patch.object(gate, "run_quality", return_value={"ok": True}), patch.object(gate, "require_ci_network"), \
                    patch.object(gate, "docker_ok", return_value=True), \
                    patch.object(gate, "l2", return_value=([], {"web": "test:identity"})), \
                    patch.object(gate, "sh", side_effect=shell), \
                    patch.object(gate, "l4", return_value=[]), patch.object(gate, "l3", return_value=[]):
                passed = gate.run_gate(ws, Path(tmp) / "passed", list(gate.ORDER), app_id="fixture-npm-js")
            self.assertTrue(passed["release_eligible"])
            self.assertEqual(passed["app_id"], "fixture-npm-js")
            self.assertEqual([r["layer"] for r in passed["layers"]], list(gate.ORDER))
            invalid = gate.run_gate(ws, Path(tmp) / "invalid", list(gate.ORDER), app_id="../other")
            self.assertEqual(invalid["status"], "BLOCKED")
            self.assertEqual(invalid["layers"][0]["blocked"], "INVALID_APP_ID")

    def test_l0_deletions_share_runner_scope_and_protect_existing_tests(self):
        paths = gate.yaml.safe_load((gate.PLATFORM / 'contract/paths.yaml').read_text())
        for path, scope, allowed in [('old.Dockerfile', 'packaging', True),
                                     ('src/old.java', 'source', True),
                                     ('src/old.java', 'packaging', False),
                                     ('tests/test_app.py', 'source', False),
                                     ('package.json', 'source', False),
                                     ('schema/model.py', 'source', False)]:
            with self.subTest(path=path, scope=scope), tempfile.TemporaryDirectory() as tmp:
                ws = imported_workspace(tmp, {path: '{}'})
                (ws / path).unlink()
                errors, changed = gate.l0(ws, paths, repair_scope=scope)
                self.assertEqual(not errors, allowed, errors)
                self.assertIn(path, changed)
        with tempfile.TemporaryDirectory() as tmp:
            ws = imported_workspace(tmp, {'old.Dockerfile': 'x' * 20001})
            (ws / 'old.Dockerfile').unlink()
            errors, _ = gate.l0(ws, paths)
            self.assertTrue(any('patch too large' in error for error in errors), errors)

    def test_imported_ignored_source_is_tracked_and_policy_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = imported_workspace(tmp, {".gitignore": "app.py\n", "app.py": "print('before')\n"})
            tracked = gate.sh(["git", "ls-files", "-z", "--", "app.py"], cwd=ws, check=True).stdout
            self.assertEqual(tracked, "app.py\0")
            (ws / "app.py").write_text("print('after')  # noqa\n")
            paths = gate.yaml.safe_load((gate.PLATFORM / "contract/paths.yaml").read_text())
            errors, changed = gate.l0(ws, paths, repair_scope="source")
            self.assertIn("app.py", changed)
            self.assertTrue(any("forbidden pattern" in error for error in errors), errors)

    def test_new_ignored_source_is_policy_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = imported_workspace(tmp, {".gitignore": "ignored*.py\n", "app.py": "print('before')\n"})
            (ws / "ignored_new.py").write_text("print('new')  # noqa\n")
            self.assertIn(("A", "ignored_new.py"), gate.changed_files(ws))
            paths = gate.yaml.safe_load((gate.PLATFORM / "contract/paths.yaml").read_text())
            errors, _ = gate.l0(ws, paths, repair_scope="source")
            self.assertTrue(any("forbidden pattern" in error for error in errors), errors)

    def test_attributes_cannot_hide_policy_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = imported_workspace(tmp, {".gitattributes": "*.py -diff\n", "app.py": "print('before')\n"})
            (ws / "app.py").write_text("print('after')  # noqa\n")
            paths = gate.yaml.safe_load((gate.PLATFORM / "contract/paths.yaml").read_text())
            errors, _ = gate.l0(ws, paths, repair_scope="source")
            self.assertTrue(any("forbidden pattern" in error for error in errors), errors)

    def test_nul_paths_and_renames_are_not_misparsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            unusual = "odd\r\nname.py"
            ws = imported_workspace(tmp, {unusual: "print('before')\n", "app.py": "print('rename')\n"})
            (ws / unusual).write_text("print('after')  # noqa\n")
            gate.sh(["git", "mv", "--", "app.py", "renamed.py"], cwd=ws, check=True)
            changes = gate.changed_files(ws)
            self.assertIn(("M", unusual), changes)
            self.assertIn(("D", "app.py"), changes)
            self.assertIn(("A", "renamed.py"), changes)
            lines = gate.added_lines(ws, [item for item in changes if item[0] != "D"])
            self.assertEqual(lines.count("print('rename')"), 1)
            paths = gate.yaml.safe_load((gate.PLATFORM / "contract/paths.yaml").read_text())
            errors, _ = gate.l0(ws, paths, repair_scope="source")
            self.assertTrue(any("forbidden pattern" in error for error in errors), errors)
            self.assertFalse(any("app.py" in error for error in errors), errors)
            self.assertTrue(any("forbidden pattern" in error for error in errors), errors)

    def test_default_gate_runs_the_built_image_id_without_optional_scan(self):
        spec = {"services": [{"name": "web"}]}
        image_id = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "l0", return_value=([], [])), \
                patch.object(gate, "l1", return_value=([], spec)), patch.object(gate, "run_quality", return_value={"ok": True}), \
                patch.object(gate, "require_ci_network"), \
                patch.object(gate, "docker_ok", return_value=True), patch.object(gate, "l2", return_value=([], {"web": "test:one"})), \
                patch.object(gate, "sh", return_value=SimpleNamespace(stdout=image_id)), patch.object(gate, "l4", return_value=[]) as scan, \
                patch.object(gate, "l3", return_value=[]) as runtime:
            verdict = gate.run_gate(workspace(tmp), Path(tmp) / "run", list(gate.ORDER))
        self.assertTrue(verdict["release_eligible"])
        self.assertEqual(verdict["image_ids"], {"web": image_id})
        scan.assert_not_called()
        self.assertEqual(runtime.call_args.args[1], {"web": image_id})
        self.assertEqual([r["layer"] for r in verdict["layers"]], list(gate.ORDER))

    def test_build_reuse_requires_same_operation_source_spec_network_and_image(self):
        spec = {"services": [{"name": "web"}]}
        image_id = "sha256:" + "a" * 64
        for changed in (None, 'source', 'spec', 'network', 'operation', 'image', 'missing_image'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp, \
                    patch.dict(os.environ, {'RAILSHOT_RUN_ID': 'same-operation'}), \
                    patch.object(gate, 'l0', return_value=([], [])), \
                    patch.object(gate, 'l1', return_value=([], spec)), patch.object(gate, 'require_ci_network'), \
                    patch.object(gate, 'docker_ok', return_value=True), \
                    patch.object(gate, 'l2', return_value=([], {'web': 'test:one'})) as build, \
                    patch.object(gate, 'sh', return_value=SimpleNamespace(stdout=image_id, returncode=0)) as shell, \
                    patch.object(gate, 'l3', return_value=[]) as runtime:
                ws = workspace(tmp)
                first = Path(tmp) / 'first'
                self.assertTrue(gate.run_gate(ws, first, list(gate.ORDER))['ok'])
                receipt = first / 'build.json'
                if changed == 'source': (ws / 'app.py').write_text('changed')
                if changed == 'spec':
                    record = json.loads(receipt.read_text()); record['binding']['spec'] = {}
                    receipt.write_text(json.dumps(record))
                if changed == 'operation': os.environ['RAILSHOT_RUN_ID'] = 'another-operation'
                if changed in ('image', 'missing_image'):
                    mismatch = [SimpleNamespace(stdout='sha256:' + 'b' * 64,
                                                returncode=int(changed == 'missing_image'))]
                    shell.side_effect = lambda *args, **kwargs: (
                        mismatch.pop() if mismatch and args[0][:3] == ['docker', 'image', 'inspect']
                        else SimpleNamespace(stdout=image_id, returncode=0))
                target = Path(tmp) / 'second'
                result = gate.run_gate(ws, target, list(gate.ORDER), build_receipt=receipt,
                                       quality_network='changed' if changed == 'network' else None)
                self.assertTrue(result['ok'], result)
                self.assertEqual(build.call_count, 1 if changed is None else 2)
                self.assertEqual(runtime.call_count, 2)  # Runtime is never a cached PASS.
                self.assertEqual(json.loads((target / 'L2.json').read_text())['reused'], changed is None)
                self.assertEqual(json.loads((target / 'L3.json').read_text())['stage'], 'image.runtime')

    def test_source_mutation_blocks_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = workspace(tmp)
            def mutate(*args, **kwargs):
                (ws / "changed.txt").write_text("changed during gate")
                return [], []
            with patch.object(gate, "l0", side_effect=mutate):
                verdict = gate.run_gate(ws, Path(tmp) / "run", ["L0"])
        self.assertFalse(verdict["ok"])
        self.assertIn("SOURCE_MUTATED", verdict["layers"][-1]["blocked"])

    def test_subscription_cost_remains_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = {"result": "blocked", "attempts": [{"agent_invoked": True, "agent_meta": {}}]}
            loop.finish(Path(tmp), ev, time.time())
        self.assertIsNone(ev["cost_usd"])
        self.assertEqual(ev["cost_status"], "unknown")

    def test_build_paths_cannot_escape_or_be_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            for context, dockerfile in (("../secret", "Dockerfile"), (".", "/tmp/Dockerfile"), (".", "../Dockerfile")):
                errors = gate.check_dockerfile(Path(tmp), {"name": "web", "build": {"context": context, "dockerfile": dockerfile}}, [])
                self.assertIn("relative without traversal", errors[0])

    def test_postgres_roles_keep_runtime_out_of_ddl(self):
        sql = gate.postgres_role_sql()
        self.assertIn("ALTER DATABASE app OWNER TO app_owner", sql)
        self.assertIn("REVOKE ALL ON SCHEMA public FROM PUBLIC", sql)
        self.assertIn("GRANT USAGE ON SCHEMA public TO app_rw", sql)
        self.assertNotIn("CREATE ON SCHEMA public TO app_rw", sql)
        self.assertNotIn("SUPERUSER", sql)

    def test_invalid_layer_lists(self):
        for layers in ([], ["NOPE"], ["L0", "L0"], ["L3", "L2"], ["L4"], ["L0", "L1", "L2", "L3", "L4"]):
            self.assertIsNotNone(gate.validate_layers(layers), layers)
        self.assertIsNone(gate.validate_layers(list(gate.ORDER)))

    def test_partial_success_is_not_release(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "l0", return_value=([], [])):
            verdict = gate.run_gate(workspace(tmp), Path(tmp) / "run", ["L0"])
        self.assertTrue(verdict["checks_ok"])
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["status"], "INCOMPLETE")
        self.assertIn("partial", loop.decide(verdict, None, set()))

    def test_timeout_is_structured_unknown(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "l0", side_effect=subprocess.TimeoutExpired("git", 1)):
            verdict = gate.run_gate(workspace(tmp), Path(tmp) / "run", ["L0"])
        self.assertEqual(verdict["status"], "UNKNOWN")
        self.assertEqual(verdict["error"]["causes"][0]["type"], "subprocess.TimeoutExpired")
        self.assertEqual(verdict["error"]["retry_policy"], "after_reconcile")
        self.assertFalse(verdict["release_eligible"])

    def test_quality_advisories_continue_but_runtime_and_boundary_failures_stop(self):
        missing = quality.blocked("NO_TESTS")
        failed = quality.quality_failure("assert actual == expected", 204)
        cleanup = quality.blocked("QUALITY_CLEANUP_FAILED", error=gate.OperationError(
            "GATE_EXECUTION_FAILED", component="gate", phase="Q.cleanup", outcome="UNKNOWN"))
        for result, build_errors, runtime_errors, expected in (
                (missing, [], [], "PASS"), (failed, [], [], "PASS"),
                (missing, ["COPY failed"], [], "FAIL"), (missing, [], ["health returned 500"], "FAIL"),
                (cleanup, [], [], "UNKNOWN")):
            with self.subTest(expected=expected, result=result), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(gate, "l0", return_value=([], [])), \
                    patch.object(gate, "l1", return_value=([], {"services": []})), \
                    patch.object(gate, "require_ci_network"), patch.object(gate, "docker_ok", return_value=True), \
                    patch.object(gate, "run_quality", return_value=result), \
                    patch.object(gate, "l2", return_value=(build_errors, {})) as build, \
                    patch.object(gate, "l4", return_value=[]), patch.object(gate, "l3", return_value=runtime_errors) as runtime:
                verdict = gate.run_gate(workspace(tmp), Path(tmp) / "run", list(gate.FULL_GATE_ORDER))
            self.assertEqual(verdict["status"], expected)
            self.assertEqual(verdict["release_eligible"], expected == "PASS")
            q = verdict["layers"][2]
            self.assertFalse(q["ok"])
            self.assertEqual(q["advisory"], expected != "UNKNOWN")
            self.assertEqual(build.call_count, int(expected != "UNKNOWN"))
            self.assertEqual(runtime.call_count, int(not build_errors and expected != "UNKNOWN"))
            if build_errors:
                self.assertEqual(verdict["failure"]["layer"], "L2")
                self.assertIsNone(loop.decide(verdict, None, set(), "source"))
            if expected == "PASS":
                self.assertEqual(loop.decide(verdict, None, set()), "passed")

    def test_missing_manifest_or_lock_never_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            self.assertIn("UNSUPPORTED", quality.discover(ws)[0]["blocked"])
            (ws / "package.json").write_text('{"scripts":{"test":"jest"}}')
            self.assertIn("MISSING_LOCK", quality.discover(ws)[0]["blocked"])
            (ws / "package-lock.json").write_text('{}')
            self.assertIn("NO_TESTS", quality.discover(ws)[0]["blocked"])

    def test_npm_plan_preserves_lock_and_real_test_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            package = {"scripts": {"lint": "eslint .", "test": "vitest"}, "devDependencies": {"eslint": "9.0.0", "vitest": "3.0.0"}}
            (ws / "package.json").write_text(json.dumps(package))
            (ws / "package-lock.json").write_text('{}')
            self.assertIn("NO_TESTS", quality.discover(ws)[0]["blocked"])
            (ws / "app.test.js").write_text("test('real', () => expect(1).toBe(1))")
            plan = quality.discover(ws)[0]
            self.assertTrue(any("npm ci --engine-strict" in c for c in plan["commands"]))
            self.assertFalse(any("passWithNoTests" in c for c in plan["commands"]))
            result = quality.run_quality(ws, ws / "run")
            self.assertIn("DEPENDENCY_NETWORK_UNCONFIGURED", result["blocked"])

    def test_container_has_no_host_credentials_or_write_mount(self):
        command = quality.docker_command(Path("/tmp/work"), {"path": ".", "image": "node:22.23.3-bookworm-slim", "commands": ["npm ci"]}, "test", "reviewed-ci")
        text = " ".join(command)
        for option in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--cpus=2", "--memory=2g", "--pids-limit=256", "readonly"):
            self.assertIn(option, text)
        self.assertNotIn("docker.sock", text)
        self.assertNotIn("--privileged", text)
        self.assertNotIn("AWS_", text)

    def test_zero_or_skipped_tests_not_counted(self):
        self.assertEqual(quality.test_count("", "javascript"), 0)
        self.assertEqual(quality.test_count("RAILSHOT_TESTS=0", "python"), 0)
        self.assertEqual(quality.test_count('<testsuite tests="3" skipped="1"></testsuite>', "maven"), 0)
        self.assertEqual(quality.test_count('<testsuite tests="3" failures="0"></testsuite>', "gradle"), 0)

    def test_quality_and_repeated_signature_stop(self):
        verdict = {"ok": False, "failure": {"class": "QUALITY", "signature": "q"}}
        self.assertIn("reviewed application", loop.decide(verdict, None, set()))
        verdict["failure"]["class"] = "F2"
        self.assertIsNone(loop.decide(verdict, None, {"q"}))

    def test_quality_never_triggers_source_repair(self):
        failure = {"layer": "Q", "class": "QUALITY", "signature": "lint-1", "source_repair_eligible": True}
        verdict = {"ok": False, "failure": failure, "layers": [{"layer": "Q", "ok": False}]}
        self.assertIn("reviewed application", loop.decide(verdict, None, set()))
        self.assertIn("reviewed application", loop.decide(verdict, None, set(), "source"))
        self.assertIn("reviewed application", loop.decide(verdict, None, {"lint-1"}, "source"))
        verdict["layers"][0]["blocked"] = "MISSING_LOCK"
        self.assertIn("blocked", loop.decide(verdict, None, set(), "source"))

    def test_prepare_and_environment_failures_are_not_source_repairable(self):
        for text, code in (("RAILSHOT_STAGE=prepare\nlock mismatch", 201), ("RAILSHOT_STAGE=unit\nNo tests found", 204),
                           ("RAILSHOT_STAGE=lint\nETIMEDOUT registry", 202), ("RAILSHOT_STAGE=unit\nAPI_KEY required", 204)):
            result = quality.quality_failure(text, code)
            self.assertTrue(result.get("blocked"), result)
            self.assertFalse(result.get("source_repair_eligible", False))
        result = quality.quality_failure("RAILSHOT_STAGE=type\nsrc/app.ts: error TS2322: string not assignable to number", 203)
        self.assertTrue(result["source_repair_eligible"])
        self.assertEqual(result["check"], "type")

    def test_source_scope_preserves_tests_configs_and_data_paths(self):
        allowed, denied = gate.writable_rules("contract/paths.yaml", scope="source")
        self.assertTrue(gate.path_ok("src/service.ts", allowed, denied))
        self.assertTrue(gate.path_ok("app.py", allowed, denied))
        for path in ("eslint.config.js", "migrations/0001.py",
                     "schemas/customer.ts", "generated/api.ts", ".github/workflows/check.yml"):
            self.assertFalse(gate.path_ok(path, allowed, denied), path)
        # These paths have additional content/original-byte checks at writer and L0.
        for path in ("tests/test_app.py", "src/app.test.ts", "package.json"):
            self.assertTrue(gate.path_ok(path, allowed, denied), path)
        default_allow, default_deny = gate.writable_rules("contract/paths.yaml")
        self.assertFalse(gate.path_ok("src/service.ts", default_allow, default_deny))

    def test_baseline_pass_does_not_call_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            def intake_fixture(*args, **kwargs):
                (run / "ir.json").write_text('{}')
                return 0, {"ok": True}, ""
            def gate_fixture(*args, **kwargs):
                verdict = {"ok": True, "release_eligible": True, "status": "PASS"}
                (run / "gate-0").mkdir(exist_ok=True)
                (run / "gate-0/verdict.json").write_text(json.dumps(verdict))
                return verdict
            with patch.object(sys, "argv", ["loop", str(Path(tmp) / "upload"), str(run)]), \
                    patch.object(loop, "run_json", side_effect=intake_fixture), \
                    patch.object(loop, "gate", side_effect=gate_fixture), \
                    patch.object(loop, "agent") as agent:
                self.assertEqual(loop.main(), 0)
                agent.assert_not_called()
            evidence = json.loads((run / "evidence.json").read_text())
            self.assertEqual(evidence["llm_calls"], 0)


class GateObservationTest(unittest.TestCase):
    def test_final_partial_event_preserves_parent_identity_without_claiming_pass(self):
        run_id = "5b095c21-82bd-4c61-8eac-6e50eddd7370"
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "l0", return_value=([], [])), \
                patch.dict(gate.os.environ, {"RAILSHOT_RUN_ID": run_id, "RAILSHOT_ATTEMPT_ID": run_id + ":2"}):
            verdict = gate.run_gate(workspace(tmp), Path(tmp) / "run", ["L0"])
        self.assertEqual(verdict["event"]["outcome"], "INCOMPLETE")
        self.assertIsNone(verdict["event"]["error"])
        self.assertEqual(verdict["layers"][0]["event"]["outcome"], "PASS")
        self.assertEqual(verdict["event"]["attempt_id"], run_id + ":2")
        self.assertEqual(verdict["layers"][0]["event"]["run_id"], run_id)
        self.assertNotEqual(verdict["event"]["event_id"], verdict["layers"][0]["event"]["event_id"])

    def test_exception_chain_does_not_disclose_message_arguments_or_absolute_paths(self):
        secret = "arbitrary-private-canary-8d2be"
        def crash(*args, **kwargs):
            try:
                raise PermissionError(13, secret, "/private/credentials/" + secret)
            except PermissionError as exc:
                raise RuntimeError("command --token " + secret) from exc
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "l0", side_effect=crash):
            verdict = gate.run_gate(workspace(tmp), Path(tmp) / "run", ["L0"])
            stored = (Path(tmp) / "run/verdict.json").read_text()
        self.assertNotIn(secret, stored)
        self.assertNotIn("/private/credentials", stored)
        self.assertNotIn("detail", verdict["layers"][0])
        self.assertEqual(verdict["status"], "UNKNOWN")
        causes = verdict["error"]["causes"]
        self.assertEqual([c["type"] for c in causes], ["builtins.RuntimeError", "builtins.PermissionError"])
        self.assertEqual(causes[1]["errno"], 13)
        self.assertTrue(causes[0]["frames"])
        self.assertTrue(all("/" not in frame["file"] for cause in causes for frame in cause.get("frames", [])))

    def test_configuration_and_check_failures_have_different_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = workspace(tmp)
            invalid = gate.run_gate(ws, Path(tmp) / "invalid", [])
            with patch.object(gate, "l0", return_value=(["deleted file: tests/test_app.py"], [])):
                failed = gate.run_gate(ws, Path(tmp) / "failed", ["L0"])
        self.assertEqual(invalid["error"]["code"], "GATE_CONFIG_INVALID")
        self.assertEqual(invalid["error"]["retry_policy"], "after_configuration")
        self.assertEqual(failed["status"], "FAIL")
        self.assertEqual(failed["error"]["code"], "GATE_CHECK_FAILED")
        self.assertEqual(failed["error"]["retry_policy"], "never")
        self.assertEqual(failed["failure"]["class"], "F5")
        self.assertEqual(failed["failure"]["classification_source"], "heuristic")

    def test_spec_validation_cause_does_not_serialize_uploaded_value(self):
        secret = "schema-private-canary-c17e"
        with tempfile.TemporaryDirectory() as tmp:
            ws = workspace(tmp)
            (ws / ".railshot").mkdir()
            (ws / ".railshot/railshot.yaml").write_text("apiVersion: " + secret)
            verdict = gate.run_gate(ws, Path(tmp) / "run", ["L1"])
        self.assertEqual(verdict["status"], "FAIL")
        self.assertEqual(verdict["failure"]["class"], "F5")
        self.assertNotIn(secret, json.dumps(verdict))
        self.assertTrue(any("ValidationError" in c["type"] for c in verdict["error"]["causes"]))

    def test_evidence_write_failure_cannot_return_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "l0", return_value=([], [])):
            ws = workspace(tmp)
            with patch.object(Path, "write_text", side_effect=OSError(28, "private-disk-canary")):
                verdict = gate.run_gate(ws, Path(tmp) / "run", ["L0"])
        self.assertEqual(verdict["status"], "UNKNOWN")
        self.assertEqual(verdict["error"]["code"], "OBSERVATION_WRITE_FAILED")
        self.assertFalse(verdict["checks_ok"])
        self.assertNotIn("private-disk-canary", json.dumps(verdict))

    def test_quality_timeout_and_cleanup_failure_preserve_both_causes(self):
        plan = {"path": ".", "stack": "javascript"}
        timeout = subprocess.TimeoutExpired(["docker", "private-token-canary"], 1, output="private-output-canary")
        with tempfile.TemporaryDirectory() as tmp, patch.object(quality, "discover", return_value=[plan]), \
                patch.object(quality, "docker_command", return_value=["docker", "run"]), \
                patch.object(quality.subprocess, "run", side_effect=[timeout, SimpleNamespace(returncode=1)]):
            result = quality.run_quality(Path(tmp), Path(tmp) / "run", network="reviewed")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["error"]["phase"], "Q.cleanup")
        self.assertEqual(result["error"]["side_effect"], "unknown")
        self.assertEqual(result["error"]["retry_policy"], "after_reconcile")
        self.assertEqual([c["type"] for c in result["error"]["causes"]],
                         ["subprocess.CalledProcessError", "subprocess.TimeoutExpired"])
        self.assertNotIn("private-token-canary", json.dumps(result))
        self.assertNotIn("private-output-canary", json.dumps(result))

    def test_quality_missing_executable_is_configuration_block_without_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(quality, "discover", return_value=[{"path": "."}]), \
                patch.object(quality, "docker_command", return_value=["docker"]), \
                patch.object(quality.subprocess, "run", side_effect=FileNotFoundError(2, "private-path-canary")) as launch:
            result = quality.run_quality(Path(tmp), Path(tmp) / "run", network="reviewed")
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["error"]["side_effect"], "none")
        self.assertEqual(result["error"]["retry_policy"], "after_configuration")
        self.assertNotIn("private-path-canary", json.dumps(result))

    def test_quality_actual_exit_failure_is_not_an_execution_exception(self):
        def command(*args, **kwargs):
            if "stdout" in kwargs:
                kwargs["stdout"].write(b"unused variable\n")
                return SimpleNamespace(returncode=202)
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp, patch.object(quality, "discover", return_value=[{"path": ".", "stack": "javascript"}]), \
                patch.object(quality, "docker_command", return_value=["docker"]), patch.object(quality.subprocess, "run", side_effect=command):
            result = quality.run_quality(Path(tmp), Path(tmp) / "run", network="reviewed")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["error"]["code"], "GATE_CHECK_FAILED")
        self.assertEqual(result["error"]["phase"], "Q.lint")
        self.assertEqual(result["error"]["causes"][0]["returncode"], 202)
        self.assertTrue(result["source_repair_eligible"])

    def test_quality_discovery_parser_errors_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "package.json").write_text('{"private-canary-829c": broken}')
            result = quality.discover(ws)
        self.assertTrue(result[0]["blocked"])
        self.assertNotIn("private-canary-829c", json.dumps(result))
        self.assertEqual(result[0]["error"]["code"], "GATE_CONFIG_INVALID")

    def test_quality_timeout_envelope_survives_gate_aggregation(self):
        cause = subprocess.TimeoutExpired(["docker", "private-argument-canary"], 3)
        error = gate.OperationError("GATE_EXECUTION_FAILED", component="gate", phase="Q.command", outcome="UNKNOWN",
                                    retry_policy="after_reconcile", side_effect="unknown", cause=cause)
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "require_ci_network"), \
                patch.object(gate, "run_quality", return_value=quality.blocked("QUALITY_EXECUTION_BLOCKED", error=error)):
            verdict = gate.run_gate(workspace(tmp), Path(tmp) / "run", ["Q"])
        self.assertEqual(verdict["status"], "UNKNOWN")
        self.assertEqual(verdict["error"], verdict["layers"][0]["error"])
        self.assertEqual(verdict["event"]["error"], verdict["error"])
        self.assertNotIn("private-argument-canary", json.dumps(verdict))
        self.assertIsNone(verdict["failure"])


class GateNetworkTest(unittest.TestCase):
    def test_executor_profile_is_shared_with_installer_and_native_verifier(self):
        builder, digest = gate.ci_profile()
        self.assertEqual(len(digest), 64)
        infra = gate.PLATFORM.parents[1] / "infrastructure/ansible"
        tasks = gate.yaml.safe_load((infra / "ci.yml").read_text())[0]["tasks"]
        copy = next(t["ansible.builtin.copy"] for t in tasks
                    if t.get("ansible.builtin.copy", {}).get("dest") == "/etc/railshot/ci-executor.yaml")
        self.assertEqual((infra / copy["src"]).resolve(), gate.CI_PROFILE_PATH)
        self.assertEqual(copy["owner"], "root")
        build = next(t["ansible.builtin.shell"] for t in tasks if "ansible.builtin.shell" in t)
        for key in ("image", "memory_bytes", "cpu_quota", "cpu_period"):
            self.assertIn(".builder." + key, build)
        self.assertNotIn(builder["image"], build)
        subprocess.run(["bash", "-n"], input=build, text=True, check=True, capture_output=True)
        native = (infra / "test-ci-network.sh").read_text()
        self.assertIn('builder=$(jq -er', native)
        self.assertIn('--builder "$builder"', native)

    def test_gate_requires_the_installed_profile_byte_digest(self):
        _, digest = gate.ci_profile()
        with patch.object(Path, "is_file", return_value=True), patch.object(gate, "sh") as shell:
            gate.require_ci_network(gate.CI_NETWORK)
        self.assertEqual(shell.call_args.args[0][-2:], ["--check", digest])

    def test_unregistered_executor_profile_is_a_configuration_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.yaml"
            profile = json.loads(gate.CI_PROFILE_PATH.read_text())
            profile["schema_version"] = 2
            path.write_text(json.dumps(profile))
            with patch.object(gate, "CI_PROFILE_PATH", path):
                with self.assertRaises(gate.OperationError) as raised:
                    gate.ci_profile()
        self.assertEqual(raised.exception.code, "GATE_CONFIG_INVALID")
        self.assertEqual(raised.exception.phase, "executor-profile")

    def test_unverified_network_also_blocks_quality_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "run_quality") as quality_run:
            verdict = gate.run_gate(workspace(tmp), Path(tmp) / "run", ["Q"])
        self.assertEqual(verdict["status"], "BLOCKED")
        self.assertEqual(verdict["error"]["code"], "GATE_ENVIRONMENT_UNAVAILABLE")
        self.assertEqual(verdict["error"]["phase"], "network")
        quality_run.assert_not_called()

    def test_unverified_worker_blocks_before_any_build(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "l1", return_value=([], {"services": []})), \
                patch.object(gate, "docker_ok", return_value=True), patch.object(gate, "sh") as shell:
            verdict = gate.run_gate(workspace(tmp), Path(tmp) / "run", ["L1", "L2"])
        self.assertEqual(verdict["status"], "BLOCKED")
        self.assertEqual(verdict["error"]["code"], "GATE_ENVIRONMENT_UNAVAILABLE")
        self.assertEqual(verdict["error"]["phase"], "network")
        self.assertIsNone(verdict["failure"])
        shell.assert_not_called()

    def test_l2_uses_filtered_container_builder_not_default_driver(self):
        spec = {"app": "demo", "services": [{"name": "web", "build": {"dockerfile": "Dockerfile"}}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, "require_ci_network") as network, \
                patch.object(gate, "require_ci_builder") as builder, \
                patch.object(gate, "sh", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as shell:
            (Path(tmp) / "Dockerfile").write_text("FROM scratch\n")
            errors, images = gate.l2(Path(tmp), spec, "a" * 16, network=gate.CI_NETWORK)
        self.assertFalse(errors)
        self.assertIn("web", images)
        network.assert_called_once_with(gate.CI_NETWORK)
        builder.assert_called_once_with()
        command = shell.call_args.args[0]
        self.assertEqual(command[command.index("--builder") + 1], "railshot-ci")
        self.assertEqual(command[command.index("--network") + 1], "default")
        self.assertNotIn("--allow", command)

    def test_builder_drift_and_other_driver_are_blocked(self):
        valid = {"NetworkSettings": {"Networks": {gate.CI_NETWORK: {}}},
                 "HostConfig": {"NetworkMode": gate.CI_NETWORK, "Memory": 4 * 1024 ** 3,
                                "MemorySwap": 4 * 1024 ** 3, "CpuQuota": 200000, "CpuPeriod": 100000},
                 "State": {"Running": True}, "Config": {"Image": "moby/buildkit:v0.33.0", "Cmd": ["--oci-worker-net=bridge"],
                                                        "Entrypoint": ["buildkitd"], "Labels": {"railshot.component": "ci-builder"}}}
        metadata = {"Name": "railshot-ci", "Driver": "remote", "Nodes": [{"Endpoint": "docker-container://railshot-buildkit"}]}
        for field, bad in (("NetworkSettings", {"Networks": {"bridge": {}}}),
                           ("HostConfig", {**valid["HostConfig"], "NetworkMode": "host"}),
                           ("HostConfig", {**valid["HostConfig"], "Memory": 0}),
                           ("Config", {**valid["Config"], "Cmd": ["--allow-insecure-entitlement=network.host"]})):
            with self.subTest(field=field, bad=bad), patch.object(gate, "sh", side_effect=[
                    SimpleNamespace(stdout=json.dumps(metadata)),
                    SimpleNamespace(stdout=json.dumps([{**valid, field: bad}]))]):
                with self.assertRaises(gate.OperationError) as raised:
                    gate.require_ci_builder()
                self.assertEqual(raised.exception.code, "GATE_ENVIRONMENT_UNAVAILABLE")
        with patch.object(gate, "sh", return_value=SimpleNamespace(stdout='{"Driver":"docker","Nodes":[{}]}')):
            with self.assertRaises(gate.OperationError) as raised:
                gate.require_ci_builder()
            self.assertEqual(raised.exception.phase, "builder")
        with patch.object(gate, "sh", side_effect=[SimpleNamespace(stdout=json.dumps(metadata) + '\n' + json.dumps(metadata)),
                                                   SimpleNamespace(stdout=json.dumps([valid]))]):
            gate.require_ci_builder()

    def test_runtime_internal_network_keeps_run_identity_and_cleanup(self):
        run_id = "a" * 16
        def command(cmd, **kwargs):
            if cmd[:3] == ["docker", "network", "inspect"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps([{
                    "Driver": "bridge", "Internal": True, "EnableIPv6": False,
                    "Options": {"com.docker.network.bridge.name": "rsrun-aaaaaaaa"}}]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(gate, "require_ci_network"), patch.object(gate, "sh", side_effect=command) as shell:
            self.assertEqual(gate.l3({"services": []}, {}, run_id, network=gate.CI_NETWORK), [])
        commands = [call.args[0] for call in shell.call_args_list]
        self.assertIn("--internal", commands[0])
        self.assertIn("com.docker.network.bridge.name=rsrun-aaaaaaaa", commands[0])
        self.assertEqual(commands[-1], ["docker", "network", "rm", "railshot-gate-" + run_id])

    def test_external_runtime_profile_fails_closed_without_creating_network(self):
        with patch.object(gate, "require_ci_network"), patch.object(gate, "sh") as shell:
            with self.assertRaises(gate.OperationError) as raised:
                gate.l3({"services": [], "egress": ["api.example.com"]}, {}, "a" * 16, network=gate.CI_NETWORK)
            self.assertEqual(raised.exception.phase, "runtime-egress")
        shell.assert_not_called()

    def test_runtime_health_uses_its_internal_address_without_publishing_ports(self):
        net = "railshot-gate-" + "a" * 16
        def command(cmd, **kwargs):
            if cmd[:3] == ["docker", "network", "inspect"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps([{
                    "Driver": "bridge", "Internal": True, "EnableIPv6": False,
                    "Options": {"com.docker.network.bridge.name": "rsrun-aaaaaaaa"}}]))
            if cmd[:3] == ["docker", "inspect", "--format"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({net: {"IPAddress": "172.18.0.2"}}))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        spec = {"services": [{"name": "web", "port": 8080, "health": "/health",
                              "storage": {"mountPath": "/var/opt/memos", "sizeGi": 1}}]}
        with patch.object(gate, "require_ci_network"), patch.object(gate, "sh", side_effect=command) as shell, \
             patch.object(gate, "http_status", return_value=200) as http:
            self.assertEqual([], gate.l3(spec, {"web": "sha256:" + "b" * 64}, "a" * 16, network=gate.CI_NETWORK))
        http.assert_called_once_with("http://172.18.0.2:8080/health")
        self.assertFalse(any("-p" in call.args[0] for call in shell.call_args_list))
        launch = next(call.args[0] for call in shell.call_args_list if call.args[0][:2] == ['docker', 'run'])
        self.assertIn('/var/opt/memos:rw,uid=65532,gid=65532,mode=0700,size=1g', launch)
        self.assertNotIn('--read-only', launch)

    def test_runtime_early_exit_keeps_logs_and_cleans_up(self):
        net = "railshot-gate-" + "a" * 16
        def command(cmd, **kwargs):
            if cmd[:3] == ["docker", "network", "inspect"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps([{
                    "Driver": "bridge", "Internal": True, "EnableIPv6": False,
                    "Options": {"com.docker.network.bridge.name": "rsrun-aaaaaaaa"}}]))
            if cmd[:3] == ["docker", "inspect", "--format"]:
                return SimpleNamespace(returncode=0, stdout=json.dumps({net: {"IPAddress": ""}}))
            if cmd[:2] == ["docker", "logs"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="startup permission denied")
            if cmd[:3] == ["docker", "inspect", "-f"]:
                return SimpleNamespace(returncode=0, stdout='{"Running":false,"ExitCode":1}')
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        spec = {"services": [{"name": "web", "port": 8080}]}
        with patch.object(gate, "require_ci_network"), patch.object(gate, "sh", side_effect=command) as shell, \
             patch.object(gate, "http_status") as http:
            errors = gate.l3(spec, {"web": "image"}, "a" * 16, network=gate.CI_NETWORK)
        self.assertEqual(len(errors), 1)
        self.assertIn("startup permission denied", errors[0])
        self.assertIn('"ExitCode":1', errors[0])
        http.assert_not_called()
        commands = [call.args[0] for call in shell.call_args_list]
        self.assertIn(["docker", "rm", "-f", net + "-web"], commands)
        self.assertEqual(commands[-1], ["docker", "network", "rm", net])

    def test_runtime_empty_address_reports_exited_app_or_unknown_network(self):
        net = "railshot-gate-" + "a" * 16
        spec = {"services": [{"name": "web", "port": 8080}]}
        for status in ("exited", "running"):
            def command(cmd, **kwargs):
                if cmd[:3] == ["docker", "network", "inspect"]:
                    return SimpleNamespace(returncode=0, stdout=json.dumps([{
                        "Driver": "bridge", "Internal": True, "EnableIPv6": False,
                        "Options": {"com.docker.network.bridge.name": "rsrun-aaaaaaaa"}}]))
                if cmd[:3] == ["docker", "inspect", "--format"]:
                    data = {"Status": status, "ExitCode": 1} if cmd[3] == "{{json .State}}" else {net: {"IPAddress": ""}}
                    return SimpleNamespace(returncode=0, stdout=json.dumps(data))
                if cmd[:2] == ["docker", "logs"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="unable to load evlib plugin evlib_uv")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            with self.subTest(status=status), patch.object(gate, "require_ci_network"), \
                 patch.object(gate, "sh", side_effect=command) as shell, patch.object(gate, "http_status") as http:
                if status == "exited":
                    errors = gate.l3(spec, {"web": "sha256:" + "b" * 64}, "a" * 16, network=gate.CI_NETWORK)
                    self.assertIn("container exited before health check (exit code 1)", errors[0])
                    self.assertIn("unable to load evlib plugin evlib_uv", errors[0])
                else:
                    with self.assertRaises(gate.OperationError) as raised:
                        gate.l3(spec, {"web": "sha256:" + "b" * 64}, "a" * 16, network=gate.CI_NETWORK)
                    self.assertEqual(raised.exception.phase, "runtime-network")
                    self.assertEqual(raised.exception.outcome, "UNKNOWN")
                http.assert_not_called()
                commands = [call.args[0] for call in shell.call_args_list]
                self.assertIn(["docker", "rm", "-f", net + "-web"], commands)
                self.assertEqual(commands[-1], ["docker", "network", "rm", net])

    def test_worker_installer_and_native_verifier_shell_parse(self):
        infra = gate.PLATFORM.parents[1] / "infrastructure/ansible"
        tasks = gate.yaml.safe_load((infra / "ci.yml").read_text())[0]["tasks"]
        installer = next(t["ansible.builtin.copy"]["content"] for t in tasks
                         if t.get("ansible.builtin.copy", {}).get("dest") == gate.CI_NETWORK_HELPER)
        self.assertIn("railshot-runtime-host-block", installer)
        self.assertIn('reply_states = {"set": ["established", "related"]}', installer)
        self.assertIn("network-verified.sha256", installer)
        subprocess.run(["bash", "-n"], input=installer, text=True, check=True, capture_output=True)
        subprocess.run(["bash", "-n", str(infra / "test-ci-network.sh")], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
