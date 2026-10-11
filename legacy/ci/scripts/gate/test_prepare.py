"""Discovery only: no cloud, package installation, Docker or model calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import prepare
import quality


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name).resolve()

    def write(self, path, text=""):
        target = self.ws / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def test_maven_children_use_parent_planner_once(self):
        self.write("pom.xml", "<project><modules><module>api</module></modules></project>")
        self.write("api/pom.xml", "<project/>")
        with patch.object(quality, "java_plan", return_value={"stack": "maven", "commands": []}) as planner:
            plans = quality.discover(self.ws)
        self.assertEqual(["."], [p["path"] for p in plans])
        planner.assert_called_once_with(self.ws)
        self.assertEqual([self.ws], prepare.select_build_roots(self.ws, "api"))

    def test_gradle_declared_nested_members_group_and_dynamic_graph_blocks(self):
        self.write("build.gradle.kts")
        self.write("settings.gradle.kts", 'include("api", ":libs:core")')
        self.write("api/build.gradle.kts")
        self.write("libs/core/build.gradle.kts")
        self.assertEqual([self.ws], prepare.select_build_roots(self.ws))
        self.write("settings.gradle.kts", "include(computedName)")
        result = prepare.prepare_plan(self.ws)
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("dynamic Gradle", result["blockers"][0])

    def test_js_workspace_groups_but_does_not_claim_executable_support(self):
        self.write("package.json", json.dumps({"workspaces": ["packages/*"], "packageManager": "npm@10.9.2"}))
        self.write("packages/api/package.json", "{}")
        self.write("packages/web/package.json", "{}")
        result = prepare.prepare_plan(self.ws)
        self.assertEqual("NEEDS_PREPARATION", result["status"])
        self.assertEqual(["packages/api", "packages/web"], result["projects"][0]["members"])
        self.assertIn("workspaces", result["blockers"][0])
        self.assertFalse(result["projects"][0]["commands_verified"])

    def test_pnpm_workspace_patterns_preserve_exclusions(self):
        self.write("package.json", '{"packageManager":"pnpm@10.5.0"}')
        self.write("pnpm-workspace.yaml", 'packages: ["packages/*", "!packages/example"]')
        self.write("packages/api/package.json", "{}")
        self.write("packages/example/package.json", "{}")
        roots = prepare.select_build_roots(self.ws)
        self.assertEqual([self.ws, self.ws / "packages/example"], roots)

    def test_metadata_is_excluded_but_explicit_choice_runs_real_planner(self):
        self.write("requirements.txt", "flask==3.1.0")
        self.write("docs/sample/package.json", "not valid JSON")
        self.assertEqual([self.ws], prepare.select_build_roots(self.ws))
        self.write("examples/api/requirements.txt", "fastapi==0.115.0")
        with patch.object(quality, "python_plan", wraps=quality.python_plan) as planner:
            result = prepare.prepare_plan(self.ws, selected_root="examples/api")
        self.assertEqual(["examples/api"], [p["root"] for p in result["projects"]])
        planner.assert_called_once_with(self.ws / "examples/api")
        self.assertNotIn("SELECTED_ROOT_NOT_PLANNED", str(result["blockers"]))

    def test_selection_rejects_escape_absolute_missing_and_symlink(self):
        self.write("api/package.json", "{}")
        (self.ws / "linked").symlink_to(self.ws / "api", target_is_directory=True)
        for selection in ("../api", str(self.ws / "api"), "absent", "linked"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                prepare.select_build_roots(self.ws, selection)

    def test_prepared_commands_and_evidence_are_read_only(self):
        path = self.write("package.json", '{"packageManager":"npm@10.9.2","scripts":{"build":"tsc"}}')
        self.write("package-lock.json", "{}")
        before = path.read_bytes()
        plans = [{"path": ".", "stack": "typescript", "image": "node:22.23.3-bookworm-slim",
                  "commands": ["npm ci", "npm run lint", "npm run typecheck", "npm run test"]}]
        with patch.object(quality.subprocess, "run", side_effect=AssertionError("must not execute")):
            result = prepare.prepare_plan(self.ws, quality_plans=plans)
        project = result["projects"][0]
        self.assertEqual("READY", result["status"])
        self.assertEqual(["npm run build"], project["commands"]["build"])
        self.assertEqual(["npm run typecheck"], project["commands"]["type"])
        self.assertEqual("npm@10.9.2", project["package_manager"]["pin"])
        evidence = {item["path"]: item["sha256"] for item in project["source_evidence"]}
        self.assertEqual(hashlib.sha256(before).hexdigest(), evidence["package.json"])
        self.assertEqual(before, path.read_bytes())

    def test_independent_apps_remain_distinct(self):
        self.write("api/requirements.txt", "fastapi==0.115.0")
        self.write("web/package.json", "{}")
        result = prepare.prepare_plan(self.ws)
        self.assertTrue(result["selection_required"])
        self.assertEqual("NEEDS_SELECTION", result["status"])
        self.assertIn("BUILD_ROOT_SELECTION_REQUIRED", result["blockers"][0])
        self.assertEqual(["api", "web"], [p["root"] for p in result["projects"]])

    def test_quality_requires_choice_before_any_container_launch(self):
        self.write("api/requirements.txt", "fastapi==0.115.0")
        self.write("web/package.json", "{}")
        with patch.object(quality.subprocess, "run", side_effect=AssertionError("must not launch")):
            result = quality.run_quality(self.ws, self.ws / "evidence", network="railshot-quality")
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("BUILD_ROOT_SELECTION_REQUIRED", result["blocked"])

    def test_quality_executes_only_explicit_selected_root_and_preserves_source(self):
        source = self.write("api/requirements.txt", "fastapi==0.115.0")
        self.write("web/package.json", "{}")
        before = source.read_bytes()
        calls = []

        def transport(command, **kwargs):
            calls.append(command)
            if command[:2] == ["docker", "run"]:
                kwargs["stdout"].write(b"RAILSHOT_TESTS=1\n")
            return subprocess.CompletedProcess(command, 0)

        plan = {"stack": "python", "image": "python:3.12.14-slim-bookworm", "commands": ["pytest"]}
        with patch.object(quality, "python_plan", return_value=plan) as planner, \
             patch.object(quality, "npm_plan", side_effect=AssertionError("unselected checker")), \
             patch.object(quality.subprocess, "run", side_effect=transport):
            result = quality.run_quality(self.ws, self.ws / "evidence", network="railshot-quality", selected_root="api")
        self.assertTrue(result["ok"])
        self.assertEqual(["api"], [p["path"] for p in result["projects"]])
        planner.assert_called_once_with(self.ws / "api")
        self.assertEqual(1, sum(command[:2] == ["docker", "run"] for command in calls))
        self.assertIn("cd /work/api", calls[0][-1])
        self.assertEqual(before, source.read_bytes())

    def test_invalid_workspace_yaml_and_symlink_evidence_are_structured_blocks(self):
        self.write("package.json", "{}")
        self.write("pnpm-workspace.yaml", "packages: [")
        self.assertEqual("BLOCKED", prepare.prepare_plan(self.ws)["status"])
        (self.ws / "pnpm-workspace.yaml").unlink()
        (self.ws / "package-lock.json").symlink_to(self.ws / "package.json")
        result = prepare.prepare_plan(self.ws)
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("symlink", result["blockers"][0])


if __name__ == "__main__":
    unittest.main()
