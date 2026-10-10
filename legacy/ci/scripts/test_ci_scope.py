"""Run with: python3 -m unittest discover -s ci/scripts -p test_ci_scope.py -v."""
import copy
import json
import os
from pathlib import Path
import subprocess
import shlex
import tempfile
import unittest
from unittest.mock import patch

import ci_scope


class ScopeTests(unittest.TestCase):
    def test_release_scope_covers_non_image_policy_and_excludes_only_documentation(self):
        for path in ('deployment/scripts/platform_workers.py', 'deployment/scripts/runtime-update.py',
                     'deployment/airgap/versions.json', 'deployment/cilium/preflight.py',
                     'infrastructure/terraform/gcp-edge/main.tf', 'infrastructure/terraform/openstack-edge/main.tf',
                     'infrastructure/ansible/runtime.yml', 'gitops/credentials.py',
                     'observability/register.py', 'ci/workflows/railshot-deploy.yml',
                     'docs/api/ansible.openapi.json',
                     'new-scope/policy.json', '../README.md'):
            with self.subTest(path=path):
                self.assertTrue(ci_scope.release_required([path]))
        for paths in ([], ['apps/api/test/product.test.js'], ['deployment/scripts/tests/test_runtime_update.py'], ['README.md'], ['docs/operations/release.md', 'apps/api/README.md']):
            self.assertFalse(ci_scope.release_required(paths))
        self.assertTrue(ci_scope.release_required(None))  # Unknown diff fails toward validation.

    def test_normal_runs_build_only_images_and_manual_runs_keep_full_checks(self):
        cases = [(['apps/api/src/server.js'], ['api']), (['apps/dashboard/app.js'], ['dashboard']),
                 (['apps/agent/src/remote-mcp.js'], ['mcp']),
                 (['ci/scripts/ci_scope.py'], ['dashboard', 'api', 'mcp', 'personal-gateway', 'ci-runner']),
                 (['ci/workflows/railshot-deploy.yml'], ['dashboard', 'api', 'ci-runner']),
                 (['deployment/scripts/platform_workers.py'], []),
                 (['docs/operations/release.md'], []), (['apps/api/test/product.test.js'], []),
                 (['ci/scripts/loop/test_native_packaging.py'], []), ([], []),
                 (None, ['dashboard', 'api', 'mcp', 'personal-gateway', 'ci-runner'])]
        for event in ('push', 'pull_request', 'workflow_dispatch'):
            for paths, automatic_components in cases:
                with self.subTest(event=event, paths=paths), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / 'event').write_text('{}')
                    env = {'AUTO_RELEASE': str(event == 'push').lower(), 'GITHUB_EVENT_NAME': event,
                           'GITHUB_EVENT_PATH': str(root / 'event'), 'GITHUB_OUTPUT': str(root / 'output'),
                           'GITHUB_STEP_SUMMARY': str(root / 'summary')}
                    with patch.dict(os.environ, env), patch('sys.argv', ['ci_scope.py', 'select']), \
                            patch.object(ci_scope, 'changed_paths', return_value=paths), \
                            patch.object(ci_scope, 'previous_release_complete', return_value=True):
                        ci_scope.main()
                    values = dict(line.split('=', 1) for line in (root / 'output').read_text().splitlines())
                    components = json.loads(values['container_components'])
                    if event == 'push':
                        self.assertEqual(components, automatic_components)
                        self.assertEqual(values['release'], str(bool(components)).lower())
                    expected = (set(ci_scope.JOBS) if paths is None else ci_scope.select(paths)) \
                        if event == 'workflow_dispatch' else ({'containers'} if components else set())
                    self.assertEqual(set(json.loads(values['selected'])), expected)
                    checks = {job: {'result': 'success' if job in expected else 'skipped'}
                              for job in (ci_scope.JOBS if event == 'workflow_dispatch' else ['containers'])}
                    checks['changes'] = {'result': 'success', 'outputs': values}
                    ci_scope.validate_gate(checks)
                    if components:
                        checks['containers']['result'] = 'failure'
                        with self.assertRaises(ValueError):
                            ci_scope.validate_gate(checks)

    def test_unfinished_predecessor_catches_up_all_deployed_images(self):
        for paths in (['apps/api/src/server.js'], ['docs/operations/release.md'], []):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); (root / 'event').write_text('{}')
                env = {'AUTO_RELEASE': 'true', 'GITHUB_EVENT_NAME': 'push',
                       'GITHUB_EVENT_PATH': str(root / 'event'), 'GITHUB_OUTPUT': str(root / 'output'),
                       'GITHUB_STEP_SUMMARY': str(root / 'summary')}
                with patch.dict(os.environ, env), patch('sys.argv', ['ci_scope.py', 'select']), \
                        patch.object(ci_scope, 'changed_paths', return_value=paths), \
                        patch.object(ci_scope, 'previous_release_complete', return_value=False):
                    ci_scope.main()
                values = dict(line.split('=', 1) for line in (root / 'output').read_text().splitlines())
                self.assertEqual(json.loads(values['container_components']), list(ci_scope.COMPONENTS))
                self.assertEqual(values['release'], 'true')
                self.assertEqual(json.loads(values['selected']), ['containers'])

    def test_previous_green_but_skipped_deploy_is_not_a_completed_release(self):
        runs = {'workflow_runs': [{'id': 12, 'run_attempt': 1, 'head_branch': 'integration/test', 'conclusion': 'success'}]}
        for conclusion, expected in [('success', True), ('skipped', False), ('failure', False)]:
            jobs = {'total_count': 1, 'jobs': [{'steps': [{
                'name': 'Verify the exact Argo revision, running digests and public edge', 'conclusion': conclusion}]}]}
            with patch.dict(os.environ, {'GITHUB_REF_NAME': 'integration/test'}), \
                    patch.object(subprocess, 'check_output', side_effect=[json.dumps(runs), json.dumps(jobs)]):
                self.assertEqual(ci_scope.previous_release_complete('a' * 40), expected)
        jobs = {'total_count': 2, 'jobs': [
            {'name': 'Build, smoke and publish images / publish (ci-runner)', 'conclusion': 'success'},
            {'steps': [{'name': 'Verify the exact Argo revision, running digests and public edge', 'conclusion': 'success'},
                       {'name': 'Promote the tested CI controller runner and workflow source', 'conclusion': 'skipped'}]}]}
        with patch.dict(os.environ, {'GITHUB_REF_NAME': 'integration/test'}), \
                patch.object(subprocess, 'check_output', side_effect=[json.dumps(runs), json.dumps(jobs)]):
            self.assertFalse(ci_scope.previous_release_complete('a' * 40))
        with patch.object(subprocess, 'check_output', side_effect=subprocess.TimeoutExpired('gh', 20)):
            self.assertFalse(ci_scope.previous_release_complete('a' * 40))

    def test_directory_dependencies_and_document_fixtures(self):
        cases = {
            'docs/architecture/README.md': set(),
            'README.md': set(),
            'AGENT.md': set(),
            'apps/api/src/server.js': {'api-browser', 'containers'},
            'apps/api/src/metrics.js': {'api-browser', 'observability', 'containers'},
            'apps/dashboard/app.js': {'api-browser', 'containers'},
            'ci/browser/smoke.test.mjs': {'api-browser'},
            'package.json': {'api-browser', 'containers'},
            'package-lock.json': {'api-browser', 'containers'},
            '.dockerignore': {'containers'},
            'infrastructure/providers/openstack/pyproject.toml': {'contracts', 'openstack'},
            'apps/agent/sender.py': {'openstack', 'api-browser', 'containers'},
            'deployment/bootstrap/client_setup/main.py': {'openstack', 'api-browser', 'containers'},
            'deployment/bootstrap/templates/wg-client.conf.tmpl': {'openstack'},
            'deployment/bootstrap/install.sh': {'openstack', 'api-browser', 'containers'},
            'deployment/bootstrap/uninstall.sh': {'openstack', 'api-browser', 'containers'},
            'deployment/bootstrap/install_payload.py': {'openstack', 'api-browser', 'containers'},
            'deployment/bootstrap/claim_token.py': {'openstack', 'api-browser', 'containers'},
            'deployment/bootstrap/requirements.lock': {'openstack', 'api-browser', 'containers'},
            'infrastructure/providers/terraform_tools/costs.py': {'contracts', 'terraform', 'api-browser', 'containers'},
            'infrastructure/terraform/aws-edge/main.tf': {'contracts', 'terraform', 'api-browser', 'containers'},
            'infrastructure/terraform/openstack-edge/main.tf': {'contracts', 'terraform'},
            'infrastructure/ansible/runtime.yml': {'contracts', 'database-ansible', 'api-browser', 'containers'},
            'infrastructure/ansible/ci.yml': {'contracts', 'database-ansible', 'terraform', 'containers'},
            'deployment/manifests/build-runner.yaml': {'contracts', 'containers'},
            'deployment/manifests/build-controller.yaml': {'contracts', 'containers'},
            'deployment/bootstrap/install-k3s.sh': {'contracts', 'runtime-smoke', 'api-browser', 'containers'},
            'gitops/argo/render.py': {'contracts'},
            'observability/compose.yaml': {'observability', 'api-browser', 'containers'},
            'docs/api/ansible.openapi.json': {'contracts'},
            'docs/api/product.openapi.json': {'contracts', 'api-browser'},
            'examples/ansible/runtime-single-node.json': {'contracts'},
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(ci_scope.select([path]), expected)

    def test_shared_and_unknown_changes_validate_everything(self):
        for path in ('contracts/ansible-job.schema.json', '.github/workflows/railshot-ci.yml',
                     '.github/workflows/platform-containers.yml', 'ci/scripts/publication.py',
                     'new-owner/service.py', 'docs/new-executable.json', '../bad'):
            with self.subTest(path=path):
                self.assertEqual(ci_scope.select([path]), set(ci_scope.JOBS))

    def test_scope_union(self):
        self.assertEqual(ci_scope.select(['docs/api/README.md', 'apps/dashboard/styles.css',
                                         'infrastructure/terraform/gcp/main.tf']),
                         {'api-browser', 'contracts', 'terraform', 'containers'})

    def test_image_build_context_dependencies(self):
        cases = {
            'apps/dashboard/styles.css': {'dashboard'},
            'apps/dashboard/package.json': {'dashboard', 'api', 'mcp'},
            'apps/api/src/server.js': {'api'},
            'apps/api/Dockerfile': {'api', 'mcp', 'personal-gateway'},
            'apps/agent/src/mcp.js': {'mcp'},
            'apps/agent/test/agent.test.js': {'mcp'},
            'apps/agent/package.json': {'mcp'},
            'apps/agent/sender.py': {'dashboard', 'api'},
            'apps/api/package.json': {'dashboard', 'api', 'mcp'},
            'package-lock.json': {'dashboard', 'api', 'mcp'},
            '.dockerignore': set(ci_scope.COMPONENTS),
            'ci/scripts/publication.py': {'api', 'ci-runner'},
            'ci/scripts/ci_scope.py': set(ci_scope.COMPONENTS),
            'ci/scripts/runner/entrypoint.sh': {'ci-runner'},
            'ci/scripts/runner/replenish.py': {'api', 'ci-runner'},
            'ci/runner-compose.yml': {'ci-runner'},
            'deployment/manifests/platform.yaml': {'dashboard', 'api', 'mcp', 'personal-gateway'},
            'deployment/manifests/build-runner.yaml': {'ci-runner'},
            'deployment/manifests/build-controller.yaml': set(ci_scope.COMPONENTS),
            'infrastructure/ansible/ci.yml': {'ci-runner'},
            'deployment/scripts/render-platform.py': set(ci_scope.COMPONENTS),
            'deployment/bootstrap/install-k3s.sh': {'dashboard', 'api'},
            'deployment/bootstrap/claim_token.py': {'api'},
            'docs/architecture/README.md': set(),
            'docs/api/product.openapi.json': set(),
            'gitops/bridge.py': {'api'},
            'gitops/credentials.py': {'api'},
            'infrastructure/terraform/gcp/main.tf': {'api'},
            'deployment/cilium/preflight.py': {'dashboard', 'api'},
            '.github/workflows/platform-containers.yml': set(ci_scope.COMPONENTS),
            'ci/scripts/container-smoke.py': set(ci_scope.COMPONENTS),
            'unknown/source.py': set(ci_scope.COMPONENTS),
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(ci_scope.container_components([path]), expected)
                self.assertEqual('containers' in ci_scope.select([path]), bool(expected))

    def test_api_native_copy_sources_select_api_image_and_http_checks(self):
        # Audit the actual Dockerfile: a new COPY source must not silently bypass API validation.
        root = Path(__file__).resolve().parents[2]
        for line in (root / 'apps/api/Dockerfile').read_text().splitlines():
            if not line.startswith('COPY ') or '--from=' in line:
                continue
            sources = [value for value in shlex.split(line)[1:-1] if not value.startswith('--')]
            for source in sources:
                path = root / source
                files = [file for file in path.rglob('*') if file.is_file()] if path.is_dir() else [path]
                for file in files:
                    relative = file.relative_to(root).as_posix()
                    # Bundled static files support standalone API use; production serves the separate FE image.
                    if relative.startswith('apps/dashboard/') and relative != 'apps/dashboard/package.json':
                        continue
                    if ci_scope.documentation(relative):
                        continue
                    if relative.startswith('apps/agent/src/') or relative == 'apps/agent/package.json':
                        with self.subTest(path=relative):
                            self.assertIn('mcp', ci_scope.container_components([relative]))
                        continue
                    if relative in ci_scope.PERSONAL_GATEWAY_FILES and not ci_scope.api_native_dependency(relative):
                        with self.subTest(path=relative):
                            self.assertIn('personal-gateway', ci_scope.container_components([relative]))
                        continue
                    with self.subTest(path=relative):
                        self.assertIn('api', ci_scope.container_components([relative]))
                        self.assertIn('api-browser', ci_scope.select([relative]))

    def test_gate_requires_success_for_selected_and_skip_only_for_unselected(self):
        checks = {job: {'result': 'skipped'} for job in ci_scope.JOBS}
        checks['changes'] = {'result': 'success', 'outputs': {'selected': '[]'}}
        ci_scope.validate_gate(checks)  # A real docs-only diff remains mergeable.
        checks['changes']['outputs']['selected'] = json.dumps(['api-browser'])
        with self.assertRaises(ValueError):
            ci_scope.validate_gate(checks)  # Unexpected skipped dependency cannot turn green.
        checks['api-browser']['result'] = 'success'
        ci_scope.validate_gate(checks)
        for state in ('failure', 'cancelled', 'skipped'):
            bad = copy.deepcopy(checks)
            bad['changes']['result'] = state
            with self.assertRaises(ValueError):
                ci_scope.validate_gate(bad)
            bad = copy.deepcopy(checks)
            bad['api-browser']['result'] = state
            with self.assertRaises(ValueError):
                ci_scope.validate_gate(bad)
        for selected in ('{}', '["unknown"]', '["api-browser", "api-browser"]', '[1]', ''):
            bad = copy.deepcopy(checks)
            bad['changes']['outputs']['selected'] = selected
            with self.assertRaises(ValueError):
                ci_scope.validate_gate(bad)
        for mutation in ('omit', 'extra', 'unexpected_success'):
            bad = copy.deepcopy(checks)
            if mutation == 'omit':
                del bad['terraform']
            elif mutation == 'extra':
                bad['unchecked'] = {'result': 'success'}
            else:
                bad['terraform']['result'] = 'success'
            with self.assertRaises(ValueError):
                ci_scope.validate_gate(bad)


class GitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'CI scope test')
        self.git('config', 'user.email', 'ci-scope@example.invalid')
        self.write('apps/api/old.js', 'before\n')
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, stderr=subprocess.PIPE).decode().strip()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self):
        self.git('add', '--all')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def test_push_rename_deletion_and_literal_newline_filenames(self):
        (self.root / 'docs').mkdir(exist_ok=True)
        self.git('mv', 'apps/api/old.js', 'docs/old.md')
        self.write('apps/dashboard/line\n$(printf dangerous).js', 'literal\n')
        head = self.commit()
        paths = ci_scope.changed_paths('push', {'before': self.base, 'after': head}, self.root)
        self.assertEqual(set(paths), {'apps/api/old.js', 'docs/old.md',
                                     'apps/dashboard/line\n$(printf dangerous).js'})
        self.assertEqual(ci_scope.select(paths), {'api-browser', 'containers'})
        self.git('rm', '--', 'apps/dashboard/line\n$(printf dangerous).js')
        deleted = self.commit()
        self.assertEqual(ci_scope.select(ci_scope.changed_paths(
            'push', {'before': head, 'after': deleted}, self.root)), {'api-browser', 'containers'})

    def test_pull_request_uses_merge_base_not_unrelated_base_branch_changes(self):
        self.git('checkout', '-qb', 'feature')
        self.write('docs/new.md', 'documentation\n')
        head = self.commit()
        self.git('checkout', '-q', 'main')
        self.write('deployment/new.sh', 'new base work\n')
        base = self.commit()
        event = {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}
        paths = ci_scope.changed_paths('pull_request', event, self.root)
        self.assertEqual(paths, ['docs/new.md'])
        self.assertEqual(ci_scope.select(paths), set())

    def test_manual_new_branch_missing_base_and_invalid_boundaries(self):
        self.assertIsNone(ci_scope.changed_paths('workflow_dispatch', {}, self.root))
        self.assertIsNone(ci_scope.changed_paths('other', {}, self.root))
        for base in ('0' * 40, 'f' * 40):
            self.assertIsNone(ci_scope.changed_paths('push', {'before': base, 'after': self.base}, self.root))
        with self.assertRaises(ValueError):
            ci_scope.changed_paths('push', {'before': '--output=/tmp/bad', 'after': self.base}, self.root)
        self.assertEqual(ci_scope.changed_paths('push', {'before': self.base, 'after': self.base}, self.root), [])


if __name__ == '__main__':
    unittest.main()
