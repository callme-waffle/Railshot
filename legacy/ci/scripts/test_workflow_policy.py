"""Regression checks for CI publication and the CD handoff; no cloud access."""
from pathlib import Path
import json
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import yaml

HERE = Path(__file__).resolve().parent


class WorkflowPolicyTest(unittest.TestCase):
    def run_loop_policy(self, root, attempts, *, provider='codex', credentials=False,
                        loop_exit=0, entry=None, packaging=None):
        """Run the actual workflow shell; pip and the expensive gate are local stand-ins."""
        step = next(s for s in yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())
                    ['jobs']['loop']['steps'] if s.get('id') == 'loop')
        self.assertEqual(step['env']['RAILSHOT_MAX_REPAIR_ATTEMPTS'], '${{ vars.RAILSHOT_MAX_REPAIR_ATTEMPTS }}')
        root = Path(root)
        app = root / 'apps/demo/example'
        app.mkdir(parents=True, exist_ok=True)
        (app / 'app.py').write_text('print("fixture")\n')
        script = root / '.railshot/ci/scripts/loop/loop.py'
        script.parent.mkdir(parents=True, exist_ok=True)
        (script.parent / 'checks_progress.py').touch()
        script.write_text(entry or textwrap.dedent('''\
            import json, os, sys
            from pathlib import Path
            Path('invocation.json').write_text(json.dumps({'args': sys.argv[1:],
                'model_env': [key for key in os.environ if key in (
                    'CODEX_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY',
                    'CODEX_HOME', 'RAILSHOT_CODEX_HOME', 'RAILSHOT_AUTH_MODE')]}))
            sys.exit(int(os.environ['LOOP_EXIT']))
            '''))
        env = {**os.environ, 'TENANT': 'demo', 'APP': 'example', 'PROVIDER': provider,
               'QUALITY_NETWORK': 'railshot-quality', 'REPAIR_SCOPE': 'packaging',
               'RUN_DIR': str(root / 'run'), 'GITHUB_OUTPUT': str(root / 'output'),
               'PIP_LOG': str(root / 'pip.log'), 'TEST_PYTHON': sys.executable,
               'TRUSTED_LOOP_DIRECTORY': str(HERE / 'loop'), 'LOOP_EXIT': str(loop_exit),
               'RAILSHOT_PROGRESS_TOKEN': ''}
        for key in ('CODEX_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CODEX_HOME',
                    'RAILSHOT_CODEX_HOME', 'RAILSHOT_AUTH_MODE', 'ANTHROPIC_BASE_URL',
                    'RAILSHOT_MAX_REPAIR_ATTEMPTS', 'RAILSHOT_MAX_PACKAGING_ATTEMPTS'):
            env.pop(key, None)
        if attempts is not None:
            env['RAILSHOT_MAX_REPAIR_ATTEMPTS'] = attempts
        if packaging is not None:
            env['RAILSHOT_MAX_PACKAGING_ATTEMPTS'] = packaging
        if credentials:
            env.update(RAILSHOT_AUTH_MODE='api-key', CODEX_API_KEY='synthetic-codex',
                       ANTHROPIC_API_KEY='synthetic-claude', OPENAI_API_KEY='synthetic-openai',
                       CODEX_HOME=str(root / 'absent-auth'), RAILSHOT_CODEX_HOME=str(root / 'absent-auth'))
        stub = ('pip() { printf "%s\\n" "$*" >> "$PIP_LOG"; }\n'
                'python() { "$TEST_PYTHON" "$@"; }\n')
        return subprocess.run(['bash', '-c', stub + step['run']], cwd=root, env=env,
                              capture_output=True, text=True, timeout=30)

    def test_zero_repair_runs_without_model_auth_or_sdk_and_does_not_promote_failure(self):
        for provider in ('codex', 'claude'):
            for credentials in (False, True):
                for loop_exit in (0, 1):
                    with self.subTest(provider=provider, credentials=credentials, loop_exit=loop_exit), tempfile.TemporaryDirectory() as tmp:
                        result = self.run_loop_policy(tmp, '0', provider=provider,
                                                      credentials=credentials, loop_exit=loop_exit)
                        self.assertEqual(result.returncode, loop_exit, result.stderr)
                        invocation = json.loads(Path(tmp, 'invocation.json').read_text())
                        args = invocation['args']
                        self.assertEqual(args[args.index('--max-attempts') + 1], '0')
                        self.assertEqual(invocation['model_env'], [])
                        self.assertEqual(Path(tmp, 'pip.log').read_text(), 'install -q pyyaml jsonschema\n')
                        self.assertEqual(Path(tmp, 'output').read_text(), f'passed={str(loop_exit == 0).lower()}\n')

    def test_default_allows_two_adapters_and_eight_fixers_with_configured_provider(self):
        for attempts in (None, ''):
            with self.subTest(attempts=attempts), tempfile.TemporaryDirectory() as tmp:
                result = self.run_loop_policy(tmp, attempts, credentials=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                invocation = json.loads(Path(tmp, 'invocation.json').read_text())
                self.assertEqual(invocation['args'][invocation['args'].index('--max-attempts') + 1], '8')
                self.assertEqual(invocation['args'][invocation['args'].index('--max-packaging-attempts') + 1], '2')
                self.assertIn('CODEX_API_KEY', invocation['model_env'])

    def test_nonzero_repair_requires_explicit_budget_and_provider_auth(self):
        for attempts in ('1', '2'):
            for provider, sdk in (('codex', 'openai-codex==0.159.3'), ('claude', 'claude-agent-sdk==0.2.158')):
                for credentials in (False, True):
                    with self.subTest(attempts=attempts, provider=provider, credentials=credentials), tempfile.TemporaryDirectory() as tmp:
                        result = self.run_loop_policy(tmp, attempts, provider=provider, credentials=credentials)
                        if not credentials:
                            self.assertNotEqual(result.returncode, 0)
                            self.assertFalse(Path(tmp, 'invocation.json').exists())
                            self.assertFalse(Path(tmp, 'pip.log').exists())
                            continue
                        self.assertEqual(result.returncode, 0, result.stderr)
                        args = json.loads(Path(tmp, 'invocation.json').read_text())['args']
                        self.assertEqual(args[args.index('--max-attempts') + 1], attempts)
                        self.assertEqual(Path(tmp, 'pip.log').read_text().splitlines(),
                                         [f'install -q {sdk}', 'install -q pyyaml jsonschema'])

    def test_packaging_accepts_eight_and_rejects_larger_or_noncanonical_values(self):
        for packaging in ('0', '1', '2', '3', '8', '9', '-1', '02', '2.0', ' 2', '2\n'):
            with self.subTest(packaging=packaging), tempfile.TemporaryDirectory() as tmp:
                result = self.run_loop_policy(tmp, '2', packaging=packaging, credentials=True)
                if packaging in ('0', '1', '2', '3', '8'):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    args = json.loads(Path(tmp, 'invocation.json').read_text())['args']
                    self.assertEqual(args[args.index('--max-packaging-attempts') + 1], packaging)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(Path(tmp, 'pip.log').exists())
                    self.assertFalse(Path(tmp, 'invocation.json').exists())

    def test_repair_attempts_reject_noncanonical_values_before_install_or_loop(self):
        for attempts in ('-1', '9', '16', '00', '03', '1.0', 'true', ' 0', '0 ', '0\n', '1; true', '$(true)'):
            with self.subTest(attempts=attempts), tempfile.TemporaryDirectory() as tmp:
                result = self.run_loop_policy(tmp, attempts, credentials=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('RAILSHOT_MAX_REPAIR_ATTEMPTS must be', result.stdout)
                self.assertFalse(Path(tmp, 'pip.log').exists())
                self.assertFalse(Path(tmp, 'invocation.json').exists())

    def test_zero_repair_uses_real_loop_checkpoint_and_rejects_changed_retry_budget(self):
        # Real intake, loop CLI, binding and SQLite checkpoints; only Docker gates are mocked.
        entry = textwrap.dedent('''\
            import json, os, sys
            from pathlib import Path
            from unittest.mock import patch
            sys.path.insert(0, os.environ['TRUSTED_LOOP_DIRECTORY'])
            import loop
            def gate(workspace, run, attempt, *args, **kwargs):
                with Path('gate-calls').open('a') as output:
                    output.write(str(attempt) + '\\n')
                target = run / f'gate-{attempt}'
                target.mkdir()
                verdict = {'ok': True, 'release_eligible': True, 'status': 'PASS'}
                (target / 'verdict.json').write_text(json.dumps(verdict))
                return verdict
            with patch.object(loop, 'gate', side_effect=gate), patch.object(loop, 'agent', side_effect=AssertionError('SDK must not run')):
                sys.exit(loop.main())
            ''')
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(2):
                result = self.run_loop_policy(tmp, '0', entry=entry)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(Path(tmp, 'gate-calls').read_text(), '0\n')
            evidence = json.loads(Path(tmp, 'run/evidence.json').read_text())
            self.assertTrue(evidence['passed'])
            self.assertEqual(evidence['max_attempts'], 0)
            self.assertEqual(evidence['agent_attempts'], 0)
            self.assertEqual(evidence['sdk_invocations'], 0)
            changed = self.run_loop_policy(tmp, '1', credentials=True, entry=entry)
            self.assertNotEqual(changed.returncode, 0)
            self.assertIn('STATE_BINDING_MISMATCH', changed.stdout)
            self.assertEqual(Path(tmp, 'gate-calls').read_text(), '0\n')

    def test_progress_token_is_only_forwarded_to_trusted_loop_and_checks_permission_is_job_scoped(self):
        jobs = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']
        self.assertEqual(jobs['loop']['permissions'], {'contents': 'read', 'checks': 'write'})
        self.assertNotIn('checks', jobs['release']['permissions'])
        steps = [step for step in jobs['loop']['steps'] if 'RAILSHOT_PROGRESS_TOKEN' in step.get('env', {})]
        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step['env']['RAILSHOT_PROGRESS_TOKEN'], '${{ github.token }}')
        for attempts in ('0', '2'):
            with self.subTest(attempts=attempts), tempfile.TemporaryDirectory() as directory:
                env = {**os.environ, 'RAILSHOT_PROGRESS_TOKEN': 'sentinel-progress-token', 'RAILSHOT_AUTH_MODE': 'api-key',
                       'RAILSHOT_MAX_REPAIR_ATTEMPTS': attempts,
                       'PROVIDER': 'codex', 'CODEX_API_KEY': 'synthetic', 'REPAIR_SCOPE': 'source', 'QUALITY_NETWORK': '',
                       'APP': 'calculator', 'TENANT': 'demo', 'RUN_DIR': directory + '/run', 'GITHUB_OUTPUT': directory + '/output'}
                # Execute the real shell flow. Setup/install subprocesses must not receive the publisher token.
                stub = '''python() {
                  if [ "$1" = ".railshot/ci/scripts/loop/loop.py" ]; then
                    test "$RAILSHOT_PROGRESS_TOKEN" = sentinel-progress-token
                  else
                    test -z "${RAILSHOT_PROGRESS_TOKEN+x}"
                  fi
                }
                pip() { test -z "${RAILSHOT_PROGRESS_TOKEN+x}"; }
                '''
                # A stale platform pin must stop before handing the token to an older loop.
                result = subprocess.run(['bash', '-c', stub + step['run']], cwd=directory, env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(Path(env['GITHUB_OUTPUT']).exists())
                capability = Path(directory, '.railshot/ci/scripts/loop/checks_progress.py')
                capability.parent.mkdir(parents=True); capability.touch()
                result = subprocess.run(['bash', '-c', stub + step['run']], cwd=directory, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(env['GITHUB_OUTPUT']).read_text(), 'passed=true\n')
                self.assertNotIn('sentinel-progress-token', result.stdout + result.stderr)

    def test_repository_scope_outputs_and_required_gate_cover_every_job(self):
        import ci_scope
        workflow = yaml.safe_load((HERE.parents[1] / '.github/workflows/railshot-ci.yml').read_text())
        jobs = workflow['jobs']
        self.assertEqual(set(jobs), {'changes', 'containers', 'gate', 'release', 'full-checks'})
        self.assertEqual(set(jobs['gate']['needs']), {'changes', 'containers'})
        self.assertEqual(jobs['gate']['if'], "always() && github.event_name != 'workflow_dispatch'")
        self.assertEqual(jobs['full-checks']['if'], "github.event_name == 'workflow_dispatch'")
        self.assertEqual(jobs['changes']['if'], "github.event_name != 'workflow_dispatch'")
        self.assertEqual(set(jobs['changes']['outputs']),
                         {'containers', 'selected', 'container_components', 'release'})
        full = yaml.safe_load((HERE.parents[1] / '.github/workflows/platform-checks.yml').read_text())
        self.assertEqual(set(full.get('on', full.get(True))), {'workflow_call', 'workflow_dispatch'})
        self.assertEqual(set(full['jobs']), set(ci_scope.JOBS))
        self.assertIs(full['jobs']['containers']['with']['publish'], False)
        events = workflow.get('on', workflow.get(True))  # PyYAML's YAML 1.1 "on" key.
        for event in ('pull_request', 'push'):
            self.assertFalse({'paths', 'paths-ignore'} & set(events[event] or {}))
        for job in ('containers',):
            self.assertEqual(jobs[job]['needs'], 'changes')
            output = f"['{job}']" if '-' in job else f'.{job}'
            self.assertEqual(jobs[job]['if'], f"needs.changes.outputs{output} == 'true'")
        self.assertEqual(jobs['containers']['uses'], './.github/workflows/platform-containers.yml')
        self.assertEqual(jobs['containers']['permissions'], {'contents': 'read', 'packages': 'write'})
        trusted = "github.event_name == 'push' && github.ref == vars.RAILSHOT_PLATFORM_VERIFY_REF && vars.RAILSHOT_AUTO_RELEASE == 'true'"
        selection = next(step for step in jobs['changes']['steps'] if step.get('id') == 'select')
        self.assertEqual(selection['env']['AUTO_RELEASE'], '${{ ' + trusted + ' }}')
        self.assertEqual(jobs['containers']['with']['components'], '${{ needs.changes.outputs.container_components }}')
        self.assertEqual(jobs['containers']['with']['publish'], "${{ needs.changes.outputs.release == 'true' && " + trusted + ' }}')
        release = jobs['release']
        self.assertEqual(release['needs'], ['changes', 'gate'])
        self.assertEqual(release['if'], "${{ always() && !cancelled() && needs.gate.result == 'success' && needs.changes.outputs.release == 'true' && " + trusted + ' }}')
        self.assertEqual(release['uses'], './.github/workflows/platform-publish.yml')
        self.assertEqual(release['with'], {'components': '${{ needs.changes.outputs.container_components }}',
            'publish': True, 'deploy': True, 'multicloud': False, 'skip_build': True,
            'ci_run_id': "${{ format('{0}', github.run_id) }}"})
        self.assertEqual(release['permissions'], {'contents': 'write', 'actions': 'read', 'packages': 'write', 'id-token': 'write'})

    def test_pull_credential_delivery_is_write_only_scoped_and_cleans_private_file(self):
        workflow = yaml.safe_load((HERE.parent / 'workflows/railshot-pull-credential.yml').read_text())
        job = workflow['jobs']['deliver']; steps = job['steps']
        self.assertEqual(job['environment'], 'railshot-release')
        self.assertEqual(job['permissions'], {'id-token': 'write'})
        self.assertEqual(steps[1]['uses'], 'aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd')
        policy = json.loads(steps[1]['with']['inline-session-policy'])
        self.assertEqual(policy['Statement'], [{'Effect': 'Allow', 'Action': 'ssm:PutParameter',
            'Resource': 'arn:aws:ssm:ap-northeast-2:721622471953:parameter/railshot/registry/ghcr/pull_token'}])
        self.assertEqual(steps[-1]['env'], {'GHCR_PULL_TOKEN': '${{ secrets.GHCR_PULL_TOKEN }}'})
        self.assertFalse(any('upload-artifact' in step.get('uses', '') for step in steps))
        script = steps[-1]['run'].split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        for outcome in ('success', 'failure', 'timeout'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                paths = []; output = io.StringIO(); token = 'synthetic-secret-never-log'
                def run(args, **kwargs):
                    self.assertEqual(args[:5], ['aws', 'ssm', 'put-parameter', '--region', 'ap-northeast-2'])
                    path = Path(args[args.index('--cli-input-json') + 1].removeprefix('file://')); paths.append(path)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    document = json.loads(path.read_text())
                    self.assertEqual(document['Name'], '/railshot/registry/ghcr/pull_token')
                    self.assertEqual(document['Type'], 'SecureString'); self.assertEqual(document['Value'], token)
                    self.assertTrue(document['Overwrite']); self.assertNotIn(token, json.dumps(args))
                    self.assertNotIn('GHCR_PULL_TOKEN', kwargs['env'])
                    self.assertEqual(kwargs['stdout'], subprocess.DEVNULL); self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
                    if outcome == 'timeout': raise subprocess.TimeoutExpired(args, 60)
                    return subprocess.CompletedProcess(args, 1 if outcome == 'failure' else 0)
                with patch.dict(os.environ, {'RUNNER_TEMP': tmp, 'GHCR_PULL_TOKEN': token}), \
                        patch('subprocess.run', side_effect=run), patch('sys.stdout', output):
                    if outcome == 'success': exec(compile(script, '<delivery>', 'exec'), {})
                    else:
                        with self.assertRaises((SystemExit, subprocess.TimeoutExpired)) as error:
                            exec(compile(script, '<delivery>', 'exec'), {})
                        self.assertNotIn(token, str(error.exception))
                self.assertEqual(len(paths), 1); self.assertFalse(paths[0].exists())
                self.assertNotIn(token, output.getvalue())

    def test_registry_target_and_credentials_belong_only_to_trusted_release(self):
        jobs = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']
        release = jobs['release']
        self.assertEqual(release['environment'], 'railshot-release')
        self.assertEqual(release['env']['REGISTRY_PREFIX'], '${{ vars.REGISTRY_PREFIX }}')
        self.assertEqual(release['env']['REGISTRY_VISIBILITY'], "${{ vars.REGISTRY_VISIBILITY || 'private' }}")
        self.assertEqual(release['permissions'], {'contents': 'read', 'actions': 'read', 'packages': 'write'})
        self.assertNotIn('REGISTRY_', json.dumps(jobs['loop']))
        login = next(s for s in release['steps'] if s.get('name') == 'Log in to GHCR')
        self.assertEqual(login['with'], {'registry': 'ghcr.io', 'username': '${{ github.actor }}', 'password': '${{ secrets.GITHUB_TOKEN }}', 'logout': True})
        self.assertEqual([s['name'] for s in release['steps'] if 'secrets.GITHUB_TOKEN' in json.dumps(s)],
                         ['Log in to GHCR', "Recover the same workflow run's publication history"])
        scripts = '\n'.join(s.get('run', '') for s in release['steps'])
        self.assertIn('"$REGISTRY_PREFIX/${TENANT}-${APP}"', scripts)
        self.assertTrue(login['uses'].startswith('docker/login-action@'))
        self.assertNotIn('REGISTRY_PASSWORD', json.dumps(release))
        self.assertNotIn('REGISTRY_USERNAME', json.dumps(release))
        self.assertNotIn('GITOPS_TOKEN', json.dumps(release))

    def test_registry_binding_rejects_ambiguous_or_source_controlled_targets(self):
        release = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']['release']
        script = next(s['run'] for s in release['steps'] if s.get('name') == 'Validate trusted platform bindings')
        env = {**os.environ, 'PLATFORM_REF': 'a' * 40, 'REGISTRY_VISIBILITY': 'public'}
        for prefix in ('ghcr.io/owner', 'ghcr.io/owner/project/nested'):
            with self.subTest(prefix=prefix):
                result = subprocess.run(['bash', '-c', script], env={**env, 'REGISTRY_PREFIX': prefix}, capture_output=True)
                self.assertEqual(result.returncode, 0)
        for prefix in ('', 'owner/project', 'https://ghcr.io/owner', 'ghcr.io/owner/',
                       'user:password@ghcr.io/owner', '--help', 'ghcr.io/owner; false',
                       'ghcr.io/owner\nother/project', 'ghcr.io/../project', 'harbor.example.test/project', 'ghcr.io:443/owner'):
            with self.subTest(prefix=prefix):
                result = subprocess.run(['bash', '-c', script], env={**env, 'REGISTRY_PREFIX': prefix}, capture_output=True)
                self.assertNotEqual(result.returncode, 0)

    def test_private_requires_credentials_and_target_reference_before_push(self):
        release = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']['release']
        guard = release['steps'][0]
        self.assertEqual(guard['name'], 'Validate trusted platform bindings')
        env = {**os.environ, 'PLATFORM_REF': 'a' * 40, 'REGISTRY_PREFIX': 'ghcr.io/owner', 'REGISTRY_READY': 'true', 'IMAGE_PULL_SECRET_READY': 'true'}
        for visibility in ('', 'private', 'true', 'PUBLIC'):
            with self.subTest(visibility=visibility):
                result = subprocess.run(['bash', '-c', guard['run']], env={**env, 'REGISTRY_VISIBILITY': visibility}, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('BLOCKED:', result.stderr)
        env.pop('REGISTRY_VISIBILITY', None)
        self.assertNotEqual(subprocess.run(['bash', '-c', guard['run']], env=env, capture_output=True).returncode, 0)
        configured = {**env, 'REGISTRY_VISIBILITY': 'private', 'GHCR_PULL_USERNAME': 'operator',
                      'GHCR_PULL_TOKEN': 'synthetic', 'PULL_SECRET_NAMESPACE': 'tenant-demo', 'PULL_SECRET_NAME': 'ghcr-pull'}
        self.assertEqual(subprocess.run(['bash', '-c', guard['run']], env=configured, capture_output=True).returncode, 0)
        for name in ('GHCR_PULL_USERNAME', 'GHCR_PULL_TOKEN', 'PULL_SECRET_NAMESPACE', 'PULL_SECRET_NAME'):
            self.assertNotEqual(subprocess.run(['bash', '-c', guard['run']], env={**configured, name: ''}, capture_output=True).returncode, 0)

    def test_standard_docker_actions_keep_private_ephemeral_credentials(self):
        release = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']['release']
        steps = release['steps']
        setup = next(s for s in steps if s.get('name') == 'Match the CI containerd image store')
        self.assertTrue(setup['uses'].startswith('docker/setup-docker-action@'))
        self.assertTrue(json.loads(setup['with']['daemon-config'])['features']['containerd-snapshotter'])
        login = next(s for s in steps if s.get('name') == 'Log in to GHCR')
        self.assertRegex(login['uses'], r'^docker/login-action@[0-9a-f]{40}$')
        self.assertIs(login['with']['logout'], True)
        self.assertIs(setup['with']['set-host'], True)
        prepare = next(s for s in steps if s.get('name') == 'Set registry credential path')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, 'RUNNER_TEMP': tmp, 'GITHUB_ENV': str(root / 'env'),
                   'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
            subprocess.run(['bash', '-c', prepare['run']], env=env, check=True)
            env['DOCKER_CONFIG'] = str(root / 'railshot-registry-123-2')
            self.assertEqual(Path(env['DOCKER_CONFIG']).stat().st_mode & 0o777, 0o700)

    def test_zero_disables_sdk_even_with_packaging_enabled(self):
        for packaging in ('0', '1', '2', '3', '8', '9', '-1'):
            with self.subTest(packaging=packaging), tempfile.TemporaryDirectory() as tmp:
                result = self.run_loop_policy(tmp, '0', packaging=packaging, credentials=True)
                if packaging not in ('0', '1', '2', '3', '8'):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(Path(tmp, 'pip.log').exists())
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                invocation = json.loads(Path(tmp, 'invocation.json').read_text())
                args = invocation['args']
                self.assertEqual(args[args.index('--max-packaging-attempts') + 1], packaging)
                self.assertEqual(invocation['model_env'], [])
                self.assertEqual(Path(tmp, 'pip.log').read_text(), 'install -q pyyaml jsonschema\n')

    def test_pull_credentials_reach_only_trusted_release_validation(self):
        jobs = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']
        self.assertNotIn('GHCR_PULL_', json.dumps(jobs['loop']))
        steps = jobs['release']['steps']
        readers = [step for step in steps if 'secrets.GHCR_PULL_TOKEN' in json.dumps(step)]
        self.assertEqual([step['name'] for step in readers],
                         ['Validate trusted platform bindings', 'Bind publication to the source and target'])
        writer = readers[-1]
        self.assertIn('publication.py release-bundle images.json published', writer['run'])
        self.assertLess(steps.index(writer), next(i for i, step in enumerate(steps) if step.get('id') == 'published'))

    def test_submitted_commit_and_operator_target_match_before_any_agent_work(self):
        jobs = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']
        guard = next(step for step in jobs['loop']['steps'] if step.get('name') == 'Validate inputs')
        release_guard = next(step for step in jobs['release']['steps'] if step.get('name') == 'Validate inputs')
        self.assertEqual(guard, release_guard)
        self.assertEqual(guard['env']['CONFIGURED_TARGETS'], '${{ vars.RAILSHOT_TARGET_IDS }}')
        env = {**os.environ, 'TENANT': 'demo', 'APP': 'my-app', 'SOURCE_COMMIT': 'a' * 40,
               'GITHUB_SHA': 'a' * 40, 'CHECKOUT_SHA': 'a' * 40, 'TARGET_ID': 'aws-demo',
               'CONFIGURED_TARGET': 'aws-demo', 'CONFIGURED_TARGETS': ''}
        stub = 'git() { test "$*" = "rev-parse HEAD" || return 9; printf "%s\\n" "$CHECKOUT_SHA"; }\n'
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, '.railshot').symlink_to(HERE.parents[1], target_is_directory=True)
            Path(tmp, 'publication.py').write_text('raise RuntimeError("untrusted checkout module imported")')
            self.assertEqual(subprocess.run(['bash', '-c', stub + guard['run']], cwd=tmp, env=env, capture_output=True).returncode, 0)
            for change in ({'GITHUB_SHA': 'b' * 40}, {'CHECKOUT_SHA': 'b' * 40}, {'SOURCE_COMMIT': 'main'},
                           {'TARGET_ID': 'onprem-demo'}, {'CONFIGURED_TARGET': ''}, {'APP': 'ab'}, {'TENANT': 'team-demo'}):
                with self.subTest(change=change):
                    self.assertNotEqual(subprocess.run(['bash', '-c', stub + guard['run']], cwd=tmp,
                                                       env={**env, **change}, capture_output=True).returncode, 0)

    def test_publication_handoff_contains_bound_evidence_without_deployment(self):
        text = (HERE.parent / 'workflows/railshot-deploy.yml').read_text()
        jobs = yaml.safe_load(text)['jobs']
        self.assertEqual(set(jobs), {'loop', 'release'})
        release = jobs['release']
        self.assertEqual(release['needs'], 'loop')
        self.assertEqual(release['outputs']['bundle_id'], '${{ needs.loop.outputs.bundle_id }}')
        self.assertEqual(release['outputs']['published_id'], '${{ steps.published.outputs.artifact-id }}')
        handoff = next(step for step in release['steps'] if step.get('id') == 'published')
        self.assertEqual(handoff['with']['name'], 'published-${{ github.run_attempt }}')
        self.assertEqual(handoff['with']['path'], 'published')
        receipt = next(step for step in release['steps'] if step.get('name') == 'Bind publication to the source and target')
        self.assertEqual(receipt['env']['BUNDLE_ARTIFACT_ID'], '${{ needs.loop.outputs.bundle_id }}')
        self.assertIn('publication.py release-bundle images.json published', receipt['run'])
        downloaded = next(step for step in release['steps'] if step.get('uses', '').startswith('actions/download-artifact'))
        self.assertEqual(downloaded['with']['artifact-ids'], '${{ needs.loop.outputs.bundle_id }}')
        scripts = '\n'.join(step.get('run', '') for step in release['steps'])
        self.assertIn('bundle.py publish release-bundle', scripts)
        self.assertNotIn('docker build', scripts)
        for removed in ('GITOPS_', 'RAILSHOT_DOMAIN', 'STORAGE_CLASS', 'render.py', 'render.json', 'kubectl', 'git push', 'KEDA', 'AUTOSCALING_PROFILE'):
            self.assertNotIn(removed, text)
        self.assertIn('exit 1', next(s['run'] for s in jobs['loop']['steps'] if s.get('id') == 'loop').split('passed=false')[1])

    def test_hosted_rerun_restores_same_run_journals_and_always_saves_only_receipt(self):
        steps = yaml.safe_load((HERE.parent / 'workflows/railshot-deploy.yml').read_text())['jobs']['release']['steps']
        recovery = next(s for s in steps if s.get('name') == "Recover the same workflow run's publication history")
        publish = next(s for s in steps if s.get('name') == 'Verify bundle and publish tested images')
        journal = next(s for s in steps if s.get('name') == 'Preserve publication journal without credentials')
        self.assertLess(steps.index(recovery), steps.index(publish))
        self.assertLess(steps.index(publish), steps.index(journal))
        self.assertIn('--github-history release-history.json --github-journals recovered-journals', publish['run'])
        self.assertEqual(publish['env']['BUNDLE_ARTIFACT_ID'], '${{ needs.loop.outputs.bundle_id }}')
        self.assertEqual(journal['if'], 'always()')
        self.assertEqual(journal['with']['path'], 'release-bundle-publish/publish.json')
        self.assertEqual(journal['with']['name'], 'publish-journal-${{ github.run_attempt }}')
        # Execute the actual history step with a native CLI stub: a missing artifact
        # is recoverable, while a failed history query stops before publishing.
        for api_exit, expected in [(0, 0), (1, 1)]:
            with tempfile.TemporaryDirectory() as tmp:
                env = {**os.environ, 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_RUN_ID': '123',
                       'GITHUB_REPOSITORY': 'owner/apps', 'API_EXIT': str(api_exit)}
                stub = 'gh() { if [[ "$1" = api ]]; then printf \'[{"jobs":[]}]\'; return "$API_EXIT"; else return 1; fi; }\n'
                result = subprocess.run(['bash', '-c', stub + recovery['run']], cwd=tmp, env=env, capture_output=True)
                self.assertEqual(result.returncode, expected)
                self.assertEqual(json.loads(Path(tmp, 'release-history.json').read_text()), [{'jobs': []}])


if __name__ == '__main__':
    unittest.main()
