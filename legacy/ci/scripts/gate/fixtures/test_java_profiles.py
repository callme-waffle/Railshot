"""JVM preparation boundary regressions, without downloading or running Java."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quality


class JavaPreparationTests(unittest.TestCase):
    def test_wrapper_bootstrap_failure_is_preparation_not_source_failure(self):
        for wrapper in ("mvnw", "gradlew"):
            with self.subTest(wrapper=wrapper), tempfile.TemporaryDirectory() as directory:
                # A failed executable is a boundary probe, not a generated wrapper.
                (Path(directory) / wrapper).write_text("echo 'distribution download failed' >&2\nexit 1\n")
                command = "sh ./" + wrapper + " --version"
                stage = quality.command_stage(command)
                self.assertEqual("prepare", stage)
                result = subprocess.run(["sh", "-c", quality.quality_script([(stage, command)])],
                                        cwd=directory, capture_output=True, text=True)
                self.assertEqual(201, result.returncode)
                decision = quality.quality_failure(result.stderr, result.returncode)
                self.assertEqual("BLOCKED", decision["status"])
                self.assertFalse(decision.get("source_repair_eligible", False))

    def test_dependency_integrity_failure_cannot_request_source_repair(self):
        for message in ("Failed to validate Maven distribution SHA-256", "Dependency verification failed for configuration 'classpath'"):
            with self.subTest(message=message):
                decision = quality.quality_failure(message, 204)
                self.assertEqual("BLOCKED", decision["status"])
                self.assertFalse(decision.get("source_repair_eligible", False))


if __name__ == "__main__":
    unittest.main()
