"""Admission regressions retained when the deployment renderer was removed."""
import copy
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import jsonschema
import yaml

import gate


class SemanticsTest(unittest.TestCase):
    def setUp(self):
        self.spec = {"apiVersion": "railshot/v0", "app": "memo", "services": [
            {"name": "api", "build": {"dockerfile": "Dockerfile"}, "port": 8000, "route": "/"}]}

    def test_build_root_and_docker_healthcheck_allowance_keeps_final_root_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            dockerfile = workspace / "Dockerfile"
            text = ('FROM node:22-slim AS build\nUSER root\nRUN mkdir /output\n'
                    'FROM node:22-slim\nUSER 65532\n'
                    'HEALTHCHECK CMD curl -f http://localhost:8000/ || exit 1\n'
                    'CMD ["node", "app.js"]\n')
            dockerfile.write_text(text)
            with patch.object(gate, "base_allowed", return_value=True):
                self.assertEqual([], gate.check_dockerfile(workspace, self.spec['services'][0], {}))
                dockerfile.write_text(text.replace('USER 65532', 'USER root'))
                self.assertTrue(any('final USER' in error for error in
                                    gate.check_dockerfile(workspace, self.spec['services'][0], {})))

    def test_unsupported_autoscaling_and_invalid_static_inputs_are_rejected(self):
        mutations = [lambda s: s.update(autoscaling={"minReplicas": 1, "maxReplicas": 3, "cpu": 70}),
                     lambda s: s.update(replicas=0), lambda s: s.update(replicas=4),
                     lambda s: s.update(size="XL"), lambda s: s.update(strategy="canary"),
                     lambda s: s.update(migrate={"command": ["python", "migrate.py"]})]
        original = copy.deepcopy(self.spec)
        for mutation in mutations:
            self.spec = copy.deepcopy(original)
            mutation(self.spec["services"][0])
            with self.subTest(spec=self.spec), self.assertRaises(jsonschema.ValidationError):
                gate.validate_semantics(self.spec)

    def test_removed_controller_flags_fail_before_execution(self):
        for entrypoint in (gate.PLATFORM / "gate/gate.py", gate.PLATFORM / "loop/loop.py"):
            for flags in (["--enable-keda"], ["--autoscaling-profile", "bounded-v1"]):
                with self.subTest(entrypoint=entrypoint, flags=flags):
                    result = subprocess.run([sys.executable, str(entrypoint), "--self-test", *flags],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("unrecognized arguments", result.stderr)

    def test_persistent_service_admission_preserves_single_writer_and_safe_mounts(self):
        self.spec['services'][0]['storage'] = {'mountPath': '/var/opt/memos', 'sizeGi': 1}
        gate.validate_semantics(self.spec)
        for path in ('/', '/tmp', '/etc', '/var/lib/../etc', '/data//db', '/data/./db'):
            bad = copy.deepcopy(self.spec)
            bad['services'][0]['storage']['mountPath'] = path
            with self.subTest(path=path), self.assertRaises(jsonschema.ValidationError):
                gate.validate_semantics(bad)
        for mutation in ('replicas', 'migration', 'services'):
            bad = copy.deepcopy(self.spec)
            if mutation == 'replicas': bad['services'][0]['replicas'] = 2
            if mutation == 'migration': bad['services'][0]['migrate'] = {'command': ['migrate']}
            if mutation == 'services': bad['services'].append({**bad['services'][0], 'name': 'other'})
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, jsonschema.ValidationError)):
                gate.validate_semantics(bad)

    def test_duplicate_services_and_reserved_bindings_still_fail_l1(self):
        original = copy.deepcopy(self.spec)
        mutations = [lambda s: s["services"].append(copy.deepcopy(s["services"][0]))]
        mutations += [lambda s, key=key: s["services"][0].update(env={key: "override"})
                      for key in ("PORT", "DATABASE_URL", "MIGRATION_DATABASE_URL")]
        mutations += [lambda s, key=key: s["services"][0].update(secrets=[key])
                      for key in ("PORT", "MIGRATION_DATABASE_URL")]
        for mutation in mutations:
            self.spec = copy.deepcopy(original)
            mutation(self.spec)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / ".railshot").mkdir()
                (root / ".railshot/railshot.yaml").write_text(yaml.safe_dump(self.spec))
                (root / ".dockerignore").write_text(".git\n.env*\n")
                with patch.object(gate, "check_dockerfile", return_value=[]), self.assertRaises(gate.OperationError) as caught:
                    gate.l1(root)
                self.assertEqual(caught.exception.code, "GATE_CHECK_FAILED")
                self.assertEqual(caught.exception.phase, "L1")

    def test_static_l1_works_without_deployment_renderer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".railshot").mkdir()
            (root / ".railshot/railshot.yaml").write_text(yaml.safe_dump(self.spec))
            (root / ".dockerignore").write_text(".git\n.env*\n")
            before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            with patch.object(gate, "check_dockerfile", return_value=[]):
                errors, spec = gate.l1(root)
            self.assertEqual(errors, [])
            self.assertEqual(spec, self.spec)
            self.assertEqual(before, sorted(str(p.relative_to(root)) for p in root.rglob("*")))

    def test_legacy_spec_remains_valid_and_duplicate_spec_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.jasmin').mkdir()
            legacy = {**self.spec, 'apiVersion': 'jasmin/v0'}
            (root / '.jasmin/jasmin.yaml').write_text(yaml.safe_dump(legacy))
            (root / '.dockerignore').write_text('.git\n.env*\n')
            with patch.object(gate, 'check_dockerfile', return_value=[]):
                self.assertEqual(gate.l1(root), ([], legacy))
                (root / '.railshot').mkdir()
                (root / '.railshot/railshot.yaml').write_text(yaml.safe_dump(self.spec))
                errors, spec = gate.l1(root)
            self.assertIsNone(spec)
            self.assertIn('exactly one', errors[0])


if __name__ == "__main__":
    unittest.main()
