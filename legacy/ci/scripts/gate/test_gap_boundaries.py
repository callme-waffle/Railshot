"""Failure-boundary regressions from GAP-001/010/011/012/014; no cloud or model."""
import json
import hashlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch

import gate
from execution import APP_UID, docker_command, docker_security, pod_security
from process import OutputLimitError, run_bounded


class BoundaryTest(unittest.TestCase):
    def test_archive_config_identity_binds_classic_and_containerd_ids(self):
        config = b'{"architecture":"amd64","os":"linux"}'
        config_id = 'sha256:' + hashlib.sha256(config).hexdigest()
        manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': config_id}}).encode()
        manifest_id = 'sha256:' + hashlib.sha256(manifest).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            for classic in (True, False):
                archive = Path(tmp) / str(classic)
                entries = ({'config.json': config, 'manifest.json': b'[{"Config":"config.json"}]'} if classic else
                           {'blobs/sha256/' + config_id[7:]: config, 'blobs/sha256/' + manifest_id[7:]: manifest})
                with tarfile.open(archive, 'w') as tar:
                    for name, value in entries.items():
                        member = tarfile.TarInfo(name); member.size = len(value)
                        tar.addfile(member, io.BytesIO(value))
                self.assertEqual(gate.archive_config_id(archive, config_id if classic else manifest_id), config_id)
                with self.assertRaises(ValueError):
                    gate.archive_config_id(archive, 'sha256:' + 'a' * 64)
            # A matching manifest filename alone is insufficient when the config bytes differ.
            with tarfile.open(archive, 'w') as tar:
                for name, value in {'blobs/sha256/' + config_id[7:]: config + b' ', 'blobs/sha256/' + manifest_id[7:]: manifest}.items():
                    member = tarfile.TarInfo(name); member.size = len(value)
                    tar.addfile(member, io.BytesIO(value))
            with self.assertRaisesRegex(ValueError, 'content digest differs'):
                gate.archive_config_id(archive, manifest_id)

    def test_dockerignore_accepts_active_env_globs_not_comment_or_negation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.dockerignore'
            for pattern in ('.env', '.env*', '**/.env*'):
                path.write_text('.git\n' + pattern + '\n')
                self.assertEqual(gate.dockerignore_errors(path), [])
            for pattern in ('# .env*', '!.env*', '# .env'):
                path.write_text('.git\n' + pattern + '\n')
                self.assertEqual(gate.dockerignore_errors(path), ['.dockerignore must exclude .env (C6)'])

    def test_build_context_physically_excludes_git_and_env_despite_ignore_negations(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            for name in ('.git/config', '.env', 'app/.env.production', 'app/.git/config',
                         'app/main.py', 'app/Dockerfile', 'app/Dockerfile.dockerignore'):
                p = ws / name; p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('!**/.git\n!**/.env*\n' if p.name.endswith('dockerignore') else 'fixture\n')
            inspected = []
            def build(command, **kwargs):
                context = Path(command[-1])
                inspected.extend(p.relative_to(context).as_posix() for p in context.rglob('*'))
                self.assertTrue((context / 'main.py').exists())
                self.assertFalse(any(p.name == '.git' or p.name.startswith('.env') for p in context.rglob('*')))
                self.assertFalse((context.parent / '.git').exists())
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch.object(gate, 'require_ci_network'), patch.object(gate, 'require_ci_builder'), patch.object(gate, 'sh', side_effect=build):
                errors, images = gate.l2(ws, {'app': 'test', 'services': [{'name': 'web', 'build': {'context': 'app', 'dockerfile': 'Dockerfile'}}]}, 'abc', network='railshot-quality')
            self.assertEqual(errors, [])
            self.assertTrue(inspected)
            self.assertIn('web', images)

    def test_private_build_context_is_readable_by_nonroot_and_preserves_source_on_success_or_failure(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                previous = os.umask(0o077)
                try:
                    ws = root / 'source'; ws.mkdir()
                    for name in ('src/server.js', 'start.sh', 'Dockerfile', '.git/config', '.env.secret'):
                        path = ws / name; path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('fixture\n')
                    (ws / 'start.sh').chmod(0o700)
                    evidence = root / 'run'; evidence.mkdir()
                    (evidence / 'record.json').write_text('{}')
                    auth = root / 'auth.json'; auth.write_text('{}')
                    before = gate.source_digest(ws)
                    contexts = []
                    def build(command, **kwargs):
                        context = Path(command[-1]); contexts.append(context)
                        self.assertNotEqual(context, ws)
                        self.assertEqual(stat.S_IMODE(context.parent.stat().st_mode), 0o700)
                        for name, mode in (('.', 0o755), ('src', 0o755), ('src/server.js', 0o644),
                                           ('start.sh', 0o755), ('Dockerfile', 0o644)):
                            self.assertEqual(stat.S_IMODE((context / name).stat().st_mode), mode)
                        self.assertEqual((context / 'src/server.js').read_bytes(), (ws / 'src/server.js').read_bytes())
                        self.assertFalse((context / '.git').exists())
                        self.assertFalse((context / '.env.secret').exists())
                        self.assertEqual(command[:3], ['docker', 'buildx', 'build'])
                        self.assertEqual(command[command.index('--network') + 1], 'default')
                        self.assertEqual(command[command.index('--platform') + 1], 'linux/amd64')
                        self.assertIn('--load', command)
                        return subprocess.CompletedProcess(command, returncode, '', 'synthetic build failure' if returncode else '')
                    with patch.object(gate, 'require_ci_network'), patch.object(gate, 'require_ci_builder'), patch.object(gate, 'sh', side_effect=build):
                        errors, images = gate.l2(ws, {'app': 'test', 'services': [{'name': 'web', 'build': {'dockerfile': 'Dockerfile'}}]},
                                                '0123456789abcdef', network='railshot-quality')
                finally:
                    os.umask(previous)
                self.assertEqual(bool(errors), bool(returncode))
                self.assertEqual(bool(images), not bool(returncode))
                self.assertEqual(gate.source_digest(ws), before)
                for path, mode in ((ws, 0o700), (ws / 'src', 0o700), (ws / 'src/server.js', 0o600),
                                   (ws / '.git/config', 0o600), (ws / '.env.secret', 0o600), (ws / 'start.sh', 0o700),
                                   (auth, 0o600), (evidence, 0o700), (evidence / 'record.json', 0o600)):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                self.assertEqual(len(contexts), 1)
                self.assertFalse(contexts[0].parent.exists())

    def test_build_rejects_linked_or_special_source_before_docker(self):
        for kind in ('symlink', 'fifo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                (ws / 'Dockerfile').write_text('FROM scratch\n')
                if kind == 'symlink': (ws / 'input').symlink_to(ws / 'Dockerfile')
                else: os.mkfifo(ws / 'input')
                with patch.object(gate, 'require_ci_network'), patch.object(gate, 'require_ci_builder'), \
                        patch.object(gate, 'sh') as docker, self.assertRaises(ValueError):
                    gate.l2(ws, {'app': 'test', 'services': [{'name': 'web', 'build': {'dockerfile': 'Dockerfile'}}]},
                            '0123456789abcdef', network='railshot-quality')
                docker.assert_not_called()

    def test_external_copy_is_checked_but_previous_stage_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = {"name": "web", "build": {"dockerfile": "Dockerfile"}}
            for ref, rejected in (("bad.example/tool:v1", True), ("builder", False), ("0", False)):
                (root / 'Dockerfile').write_text(f'FROM node:22-slim AS builder\nFROM node:22-slim\nCOPY --from={ref} /app /app\nUSER 65532\nCMD ["node","app.js"]\n')
                errors = gate.check_dockerfile(root, service, ['node:22-slim'])
                self.assertEqual(bool(errors), rejected, errors)

    def test_docker_and_kubernetes_override_and_uid_contract(self):
        self.assertEqual(docker_command('image', ['python', 'app.py']), ['--entrypoint', 'python', 'image', 'app.py'])
        self.assertEqual(docker_command('image'), ['image'])
        self.assertIn(f'{APP_UID}:{APP_UID}', docker_security())
        self.assertEqual(pod_security()['runAsUser'], APP_UID)
        self.assertNotIn('--read-only', docker_security())

    def test_stdout_and_stderr_share_a_real_capture_limit(self):
        with self.assertRaises(OutputLimitError):
            run_bounded([sys.executable, '-c', 'import os; os.write(1,b"x"*700); os.write(2,b"y"*700)'], max_output_bytes=1024)
        result = run_bounded([sys.executable, '-c', 'print("ok")'], max_output_bytes=1024)
        self.assertEqual(result.stdout, 'ok\n')

    def test_scanner_start_failure_is_environment_error_not_findings(self):
        image = 'sha256:' + 'a' * 64
        commands = []
        def run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[1:3] == ['image', 'save']:
                Path(cmd[4]).write_bytes(b'archive')
            if cmd[1] == 'run':
                return subprocess.CompletedProcess(cmd, 125, '', 'permission denied')
            return subprocess.CompletedProcess(cmd, 0, '1024', '')
        with patch.object(gate, 'require_ci_network'), patch.object(gate, 'archive_config_id', return_value=image), \
                patch.object(gate.os, 'chown') as owner, patch.object(gate, 'sh', side_effect=run):
            with self.assertRaises(gate.OperationError) as caught:
                gate.l4({'web': image}, network='railshot-quality')
        self.assertEqual(caught.exception.code, 'GATE_ENVIRONMENT_UNAVAILABLE')
        self.assertNotIn('/var/run/docker.sock', repr(commands))
        scanner = next(c for c in commands if c[1] == 'run')
        self.assertIn('--read-only', scanner)
        self.assertIn('--memory=2g', scanner)
        self.assertIn('--memory-swap=2g', scanner)
        self.assertIn('TMPDIR=/cache', scanner)
        self.assertEqual(owner.call_args.args[1:], (APP_UID, APP_UID))
        self.assertFalse(owner.call_args.args[0].exists())
        self.assertIn(['docker', 'rm', '-f', scanner[scanner.index('--name') + 1]], commands)

    def test_scanner_timeout_removes_container_and_private_disk_cache(self):
        commands = []
        def run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[1:3] == ['image', 'save']:
                Path(cmd[4]).write_bytes(b'archive')
            if cmd[1] == 'run':
                raise subprocess.TimeoutExpired(cmd, 1800)
            return subprocess.CompletedProcess(cmd, 0, '1024', '')
        with patch.object(gate, 'require_ci_network'), patch.object(gate, 'archive_config_id', return_value='sha256:' + 'a' * 64), \
                patch.object(gate.os, 'chown') as owner, patch.object(gate, 'sh', side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired):
                gate.l4({'web': 'sha256:' + 'a' * 64}, network='railshot-quality')
        self.assertFalse(owner.call_args.args[0].exists())
        self.assertEqual(commands[-1][:3], ['docker', 'rm', '-f'])


if __name__ == '__main__':
    unittest.main()
