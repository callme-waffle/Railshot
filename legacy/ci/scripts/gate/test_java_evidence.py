"""GAP-009/013 regressions: native local shell/parser only; no Docker or downloads."""
import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

import e2e
import quality


def report(xml, path="./target/surefire-reports/TEST-App.xml"):
    encode = lambda value: base64.b64encode(value.encode()).decode()
    return "RAILSHOT_JAVA_REPORTS_BEGIN\nRAILSHOT_JAVA_REPORT=" + encode(path) + ":" + encode(xml) + "\nRAILSHOT_JAVA_REPORTS_END\n"


GOOD = '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="health" classname="AppTest"/></testsuite>'
MAVEN_DIAGNOSTICS = {
    "lint": "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-checkstyle-plugin:3.6.0:check (default) on project demo: Checkstyle violations",
    "type": "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.14.0:compile (default-compile) on project demo: incompatible types",
    "unit": "[ERROR] Failed to execute goal org.apache.maven.plugins:maven-surefire-plugin:3.5.3:test (default-test) on project demo: There are test failures",
}


class JavaEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "app"
        shutil.copytree(Path(__file__).parent / "fixtures/maven-spring", self.root)
        self.properties = self.root / ".mvn/wrapper/maven-wrapper.properties"
        self.properties.parent.mkdir(parents=True)
        # Synthetic metadata for offline planner checks, never a claimed native lock.
        self.properties.write_text("distributionUrl=https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9/apache-maven-3.9.9-bin.zip\ndistributionSha256Sum=" + "a" * 64 + "\n")
        (self.root / "mvnw").write_text("touch WRAPPER_WAS_EXECUTED\nprintf 'forged'\n")

    def test_uploaded_wrapper_is_never_the_checker_executable(self):
        plan = quality.java_plan(self.root)
        text = "\n".join(plan["commands"])
        self.assertNotIn("sh ./mvnw", text)
        self.assertIn("/usr/share/maven/bin/mvn -B", text)
        self.assertIn("sha256sum -c -", text)
        self.assertFalse(plan["toolchain"]["uploaded_wrapper_executed"])
        self.assertEqual("prepare", quality.command_stage(plan["commands"][2]))
        self.assertEqual("unit", next(quality.command_stage(c) for c in plan["commands"] if "-DfailIfNoTests" in c))

    def test_version_origin_and_duplicate_checksum_cannot_silently_fall_back(self):
        original = self.properties.read_text()
        for changed in (original.replace("3.9.9", "3.9.8"), original.replace("repo.maven.apache.org", "unapproved.invalid"),
                        original + "distributionSha256Sum=" + "b" * 64 + "\n"):
            self.properties.write_text(changed)
            with self.assertRaises(ValueError):
                quality.java_plan(self.root)

    def test_stdout_xml_empty_fake_and_stale_reports_cannot_count(self):
        for text in (GOOD, '<testsuite tests="9"></testsuite>',
                     GOOD + "\nRAILSHOT_JAVA_REPORTS_BEGIN\n\nRAILSHOT_JAVA_REPORTS_END\n",
                     report('<testsuite tests="1" failures="0"></testsuite>')):
            self.assertEqual(0, quality.test_count(text, "maven"))

    def test_canonical_real_testcases_are_counted_once(self):
        self.assertEqual(1, quality.test_count(report(GOOD), "maven"))
        self.assertEqual(1, quality.test_count(report(GOOD, "./api/build/test-results/test/TEST-App.xml"), "gradle"))
        self.assertEqual(0, quality.test_count(report(GOOD), "gradle"))

    def test_report_counts_paths_and_leaf_failures_must_agree(self):
        for xml in (GOOD.replace('tests="1"', 'tests="2"'), GOOD.replace('name="health"', 'name=""'),
                    GOOD.replace('classname="AppTest"', ''), GOOD.replace('/></testsuite>', '><failure/></testcase></testsuite>'),
                    GOOD.replace('skipped="0"', 'skipped="1"'), '<!DOCTYPE foo>' + GOOD, '<broken>'):
            self.assertEqual(0, quality.test_count(report(xml), "maven"), xml)
        for path in ("../../target/surefire-reports/TEST-App.xml", "/target/surefire-reports/TEST-App.xml", "logs/TEST-App.xml"):
            self.assertEqual(0, quality.test_count(report(GOOD, path), "maven"), path)
        self.assertEqual(0, quality.test_count(report(GOOD) * 2, "maven"))

    def shell_collection(self, new_xml=None, stdout_xml=""):
        plan = quality.java_plan(self.root)
        start = next(i for i, c in enumerate(plan["commands"]) if "-delete" in c)
        commands = plan["commands"][start:]
        marker = Path(self.temp.name) / "start"
        target = self.root / "target/surefire-reports/TEST-App.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(GOOD)
        os.utime(target, (1, 1))
        fake_check = "printf %s " + shlex.quote(stdout_xml)
        if new_xml is not None:
            script = "from pathlib import Path; import os; p=Path('target/surefire-reports/TEST-App.xml'); p.write_text(" + repr(new_xml) + "); s=Path(" + repr(str(marker)) + ").stat().st_mtime; os.utime(p,(s+1,s+1))"
            fake_check += "; " + shlex.quote(sys.executable) + " -c " + shlex.quote(script)
        checked = []
        for command in commands:
            if command.startswith("/usr/share/maven/bin/mvn -B"):
                checked.append(("unit", fake_check))
            else:
                checked.append((quality.command_stage(command), command.replace("/tmp/railshot-java-start", str(marker))))
        result = subprocess.run(["sh", "-c", quality.quality_script(checked)], cwd=self.root, capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.root / "WRAPPER_WAS_EXECUTED").exists())
        return result, target

    def test_real_collector_removes_stale_files_and_ignores_stdout_spoof(self):
        result, target = self.shell_collection(stdout_xml=GOOD)
        self.assertFalse(target.exists())
        self.assertEqual(0, quality.test_count(result.stdout, "maven"))

    def test_real_collector_accepts_a_fresh_canonical_file(self):
        result, target = self.shell_collection(new_xml=GOOD)
        self.assertTrue(target.exists())
        self.assertEqual(1, quality.test_count(result.stdout, "maven"), result.stdout)

    def test_e2e_requires_the_intended_failed_java_checker(self):
        for expected in ("lint", "type", "unit"):
            for actual, diagnostic in MAVEN_DIAGNOSTICS.items():
                q = {"status": "FAIL", "check": actual, "source_repair_eligible": True}
                self.assertEqual(expected == actual, e2e.matches_quality("maven-spring", expected, "FAIL", q, diagnostic))
            q = {"status": "FAIL", "check": expected, "source_repair_eligible": True}
            self.assertFalse(e2e.matches_quality("maven-spring", expected, "FAIL", q, "unrelated unit assertion failed"))

    def test_java_classifier_exposes_actual_checker_and_blocks_ambiguous_failure(self):
        for expected, text in MAVEN_DIAGNOSTICS.items():
            failure = quality.quality_failure(text, 204, stack="maven")
            self.assertEqual(expected, failure["check"])
            self.assertTrue(failure["source_repair_eligible"])
        self.assertEqual("BLOCKED", quality.quality_failure("unrelated unit error", 204, stack="maven")["status"])
        self.assertEqual("BLOCKED", quality.quality_failure("\n".join(MAVEN_DIAGNOSTICS.values()), 204, stack="maven")["status"])
        for task, kind in (("checkstyleMain", "lint"), ("compileJava", "type"), ("test", "unit")):
            self.assertEqual(kind, quality.java_failure_kind("> Task :app:" + task + " FAILED\n"))


if __name__ == "__main__":
    unittest.main()
