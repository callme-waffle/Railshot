#!/usr/bin/env python3
"""Exercise REAL generated fixtures through intake and the existing product gate.

This runner does not generate tests, invent lock files, call an LLM, or publish.
QUALITY_PASS and full_gate_status are deliberately different evidence fields.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from bundle import source_digest
from quality import java_failure_kind
from execution import quality_advisory

HERE = Path(__file__).resolve().parent
PLATFORM = HERE.parent
STACKS = ("npm-js", "npm-ts", "nextjs", "fastapi", "maven-spring", "gradle-spring")
ALIASES = ("pnpm-js", "yarn-js")
EXTRA_STACKS = ("requirements-fastapi", "node-test-js")
CASES = ("good", "private-source", "lint", "type", "unit", "missing-env", "missing-tool", "missing-lock", "no-tests", "zero-tests", "skip")
SOURCE = {"npm-js": "src/app.js", "npm-ts": "src/app.ts", "nextjs": "lib/message.ts", "fastapi": "app.py",
          "maven-spring": "src/main/java/dev/railshot/HealthController.java",
          "gradle-spring": "src/main/java/dev/railshot/HealthController.java"}
SOURCE.update({stack: "src/app.js" for stack in ALIASES})
SOURCE.update({"requirements-fastapi": "app.py", "node-test-js": "src/app.js"})


def variant(root, stack, case):
    """Mutate trusted fixtures BEFORE intake; zero/skip cases explicitly alter test fixtures."""
    if case == "good":
        return "PASS"
    if case == "private-source":
        # Reproduce the ephemeral runner's 077 checkout/intake without changing
        # application bytes. Only Q's separate snapshot may become readable.
        for path in (root, *root.rglob("*")):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ValueError("private source fixture requires regular files/directories")
            path.chmod(0o700 if path.is_dir() or path.stat().st_mode & 0o111 else 0o600)
        return "PASS"
    if case in {"zero-tests", "skip"}:
        if stack != "node-test-js":
            return None
        test = root / "src/app.test.js"
        test.write_text("// Intentionally no registered node:test cases.\n" if case == "zero-tests" else
                        test.read_text().replace('test("health', 'test.skip("health', 1))
        return "BLOCKED"
    if stack == "requirements-fastapi" or stack == "node-test-js" and case != "unit":
        return None  # Requirements preparation has only a full good fixture so far.
    if case == "type" and stack in {"npm-js", *ALIASES}:
        return None  # this JS fixture does not claim static type coverage
    if case == "missing-tool" and stack == "fastapi":
        return None  # requires a separately native-generated Python dependency lock
    path = root / SOURCE[stack]
    text = path.read_text()
    if case == "unit":
        text = text.replace('return "ready"', 'return "broken"')
    elif case == "type":
        text = text.replace('return "ready"', 'return 7')
    elif case == "lint":
        if stack == "fastapi":
            text = "import os\n" + text
        elif stack.endswith("spring"):
            text = text.replace("import java.util.Map;", "import java.util.Map;\nimport java.util.List;")
        else:
            text = "const unusedFixtureValue = 1;\n" + text
    elif case == "missing-env":
        if stack == "fastapi":
            text = "import os\n" + text
            text = text.replace('    return "ready"', '    if not os.environ.get("API_KEY"):\n        raise RuntimeError("API_KEY required")\n    return "ready"')
        elif stack.endswith("spring"):
            text = text.replace('        return "ready"', '        if (System.getenv("API_KEY") == null) {\n            throw new IllegalStateException("API_KEY required");\n        }\n        return "ready"')
        else:
            # Startup configuration errors must remain visible in the reporter;
            # an Express handler would turn this exception into an opaque HTTP 500.
            text = 'if (!process.env.API_KEY) throw new Error("API_KEY required");\n' + text
    elif case == "missing-tool":
        if stack.endswith("spring"):
            (root / ("mvnw" if stack.startswith("maven") else "gradlew")).unlink()
        else:
            manifest = root / "package.json"
            package = json.loads(manifest.read_text())
            package["scripts"]["lint"] = "railshot_checker_not_installed"
            manifest.write_text(json.dumps(package, indent=2) + "\n")
        return "BLOCKED"
    elif case == "missing-lock":
        lock = "uv.lock" if stack == "fastapi" else ".mvn/wrapper/maven-wrapper.properties" if stack == "maven-spring" else "gradle.lockfile" if stack == "gradle-spring" else "package-lock.json"
        lock = {"pnpm-js": "pnpm-lock.yaml", "yarn-js": "yarn.lock"}.get(stack, lock)
        if stack == "maven-spring":
            properties = root / lock
            properties.write_text(re.sub(r"(?m)^distributionSha256Sum=.*\n?", "", properties.read_text()))
        else:
            (root / lock).unlink()
        return "BLOCKED"
    elif case == "no-tests":
        for candidate in root.rglob("*"):
            if candidate.is_file() and re.search(r"(^test_.*\.py$|\.(test|spec)\.[jt]sx?$|.*Tests?\.java$)", candidate.name):
                candidate.unlink()
        return "BLOCKED"
    path.write_text(text)
    return "BLOCKED" if case == "missing-env" else "FAIL"


def matches_quality(stack, case, expected, q, diagnostic=""):
    """Do not count an unrelated infrastructure block as the intended negative case."""
    if not q or q.get("status") != expected:
        return False
    if expected == "FAIL":
        return (q.get("check") == case and q.get("source_repair_eligible") is True and
                (not stack.endswith("spring") or java_failure_kind(diagnostic) == case))
    reason = q.get("blocked", "")
    if case == "missing-env":
        return ("QUALITY_DEPENDENCY_OR_ENV_BLOCKED" in reason and
                any(p.get("exit_code") == 204 for p in q.get("projects", [])) and
                re.search(r"API_KEY.*required", diagnostic) is not None)
    if case == "missing-lock":
        return "MISSING_LOCK" in reason
    if case == "no-tests":
        return "NO_TESTS" in reason
    if case in {"zero-tests", "skip"}:
        return ("NO_TESTS_OR_REPORT" in reason and "RAILSHOT_JUNIT_BEGIN" in diagnostic and
                any(p.get("exit_code") == 0 for p in q.get("projects", [])) and
                (("<skipped" in diagnostic) == (case == "skip")))
    if case == "missing-tool":
        return ("MISSING_TOOLCHAIN" in reason if stack.endswith("spring") else
                ("QUALITY_EXECUTION_BLOCKED" in reason or "QUALITY_DEPENDENCY_OR_ENV_BLOCKED" in reason) and
                re.search(r"railshot_checker_not_installed.*(?:not found|ENOENT)|(?:not found|ENOENT).*railshot_checker_not_installed", diagnostic) is not None)
    return expected == "PASS"


def call(command, log, timeout):
    record = {"command": [str(c) for c in command], "started": int(time.time()), "log": str(log)}
    try:
        with log.open("w") as stream:
            result = subprocess.run(record["command"], stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        record["exit_code"] = result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        record.update(exit_code=None, error=type(exc).__name__)
    record["finished"] = int(time.time())
    return record


def generation_evidence(fixtures, stack):
    receipt_path = fixtures / f"{stack}-generation.json"
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("status") != "GENERATED" or not receipt.get("generated_sha256"):
        raise ValueError("native generation was not successful")
    for name, digest in receipt["generated_sha256"].items():
        path = fixtures / stack / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("generated dependency evidence changed: " + name)
    return receipt


def execute(fixtures, output, stack, case, mode, network, timeout):
    case_dir = output / (stack + "--" + case)
    case_dir.mkdir()  # Never overwrite earlier evidence.
    result = {"stack": stack, "case": case, "mode": mode, "quality_status": "NOT_RUN",
              "full_gate_status": "NOT_RUN", "release_eligible": False, "assertion": "BLOCKED",
               "commands": [], "started": int(time.time())}
    previous_umask = os.umask(0o077) if case == "private-source" else None
    try:
        receipt = generation_evidence(fixtures, stack)
        result["generation_evidence"] = receipt
        # Fresh, isolated source per case; uploaded workflows are never executed.
        upload = case_dir / "upload"
        shutil.copytree(fixtures / stack, upload)
        expected = variant(upload, stack, case)
        result["upload_source_sha256_before"] = source_digest(upload)
        result["expected_quality_status"] = expected
        if expected is None:
            result.update(assertion="NOT_APPLICABLE", note="No static JS type profile / no separately generated Python missing-tool lock")
            return result
        workspace, run = case_dir / "workspace", case_dir / "run"
        command = [sys.executable, str(PLATFORM / "poc/intake.py"), str(upload), str(workspace), str(run)]
        intake = call(command, case_dir / "intake.log", 120)
        result["commands"].append(intake)
        if intake["exit_code"] != 0:
            result["error"] = "INTAKE_BLOCKED"
            return result
        layers = "L0,L1,Q" if mode == "quality" else "L0,L1,Q,L2,L4,L3"
        command = [sys.executable, str(HERE / "gate.py"), str(workspace), str(run), "--layers", layers,
                   "--quality-network", network]
        observed = call(command, case_dir / "gate.log", timeout)
        result["commands"].append(observed)
        verdict_path = run / "verdict.json"
        if observed.get("error") or not verdict_path.is_file():
            result["error"] = "GATE_EXECUTION_BLOCKED"
            return result
        verdict = json.loads(verdict_path.read_text())
        result["verdict"] = verdict
        q = next((item for item in verdict.get("layers", []) if item.get("layer") == "Q"), None)
        result["quality_status"] = q.get("status", "BLOCKED") if q else "NOT_RUN"
        result["full_gate_status"] = verdict["status"] if mode == "full" else "NOT_RUN"
        result["release_eligible"] = verdict.get("release_eligible") is True
        result["quality_only_pass"] = mode == "quality" and result["quality_status"] == "PASS"
        plan_path = run / "quality-plan.json"
        result["quality_plan"] = json.loads(plan_path.read_text()) if plan_path.exists() else []
        # Generation versions are in the immutable generation log; record actual
        # quality image identity too, including when an image could not be pulled.
        result["quality_images"] = []
        for image in sorted({p["image"] for p in result["quality_plan"] if "image" in p}):
            image_log = case_dir / ("image-" + str(len(result["quality_images"])) + ".json")
            identity = call(["docker", "image", "inspect", "--format", "{{json .}}", image], image_log, 30)
            result["commands"].append(identity)
            if identity["exit_code"] == 0:
                detail = json.loads(image_log.read_text())
                result["quality_images"].append({"image": image, "image_id": detail["Id"], "repo_digests": detail.get("RepoDigests", [])})
            else:
                result["quality_images"].append({"image": image, "status": "UNAVAILABLE"})
        logs = sorted(run.glob("quality-*.log"))
        result["quality_log_evidence"] = [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in logs]
        diagnostic = "\n".join(p.read_text(errors="replace") for p in logs)
        matches = matches_quality(stack, case, expected, q, diagnostic)
        if mode == "full" and expected == "PASS":
            matches = matches and verdict.get("ok") is True and result["release_eligible"]
        elif mode == "quality" or not q or not quality_advisory(q):
            matches = matches and not result["release_eligible"]
        result["assertion"] = "MATCH" if matches else "MISMATCH"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        if result.get("upload_source_sha256_before"):
            result["upload_source_sha256_after"] = source_digest(case_dir / "upload")
            result["upload_unchanged"] = result["upload_source_sha256_before"] == result["upload_source_sha256_after"]
            if not result["upload_unchanged"]:
                result.update(assertion="MISMATCH", error="UPLOADED_SOURCE_CHANGED")
        result["finished"] = int(time.time())
        try:
            (case_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        finally:
            if previous_umask is not None:
                os.umask(previous_umask)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", required=True, type=Path, help="native generate.py output")
    parser.add_argument("--output", required=True, type=Path, help="new, empty evidence directory")
    parser.add_argument("--stacks", default=",".join(STACKS))
    parser.add_argument("--cases", default="good")
    parser.add_argument("--mode", choices=["quality", "full"], default="quality")
    parser.add_argument("--network", required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    stacks, cases = args.stacks.split(","), args.cases.split(",")
    if any(s not in (*STACKS, *ALIASES, *EXTRA_STACKS) for s in stacks) or any(c not in CASES for c in cases):
        parser.error("unknown stack or case")
    if len(set(stacks)) != len(stacks) or len(set(cases)) != len(cases):
        parser.error("duplicate stacks/cases are not allowed")
    if args.network in {"host", "bridge", "none"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.network):
        parser.error("supply the administrator-registered egress-filtered network")
    fixtures, output = args.fixtures.resolve(), args.output.resolve()
    if output.exists() or fixtures == output or fixtures in output.parents or output in fixtures.parents:
        parser.error("output must be a new directory outside the generated fixture tree")
    output.mkdir(parents=True)
    summary = {"mode": args.mode, "evidence_version": 1, "results": [],
               "note": "MATCH means expected test behavior; a negative fixture MATCH is not an application pass or release."}
    for stack in stacks:
        for case in cases:
            result = execute(fixtures, output, stack, case, args.mode, args.network, args.timeout)
            row = {k: result.get(k) for k in ("stack", "case", "assertion", "quality_status", "full_gate_status", "release_eligible")}
            summary["results"].append(row)
            (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(json.dumps(row), flush=True)
    return 0 if all(r["assertion"] in {"MATCH", "NOT_APPLICABLE"} for r in summary["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
