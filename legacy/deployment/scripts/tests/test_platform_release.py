"""Run the release workflow's shell against a local bare Git remote, never GHCR/K3s."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKLOAD = "gitops/applications/railshot-platform/workload.json"
REFERENCE = "refs/heads/deployment/platform"


class PlatformReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="railshot-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "source"
        self.repo.mkdir()
        self.remote = self.home / "remote.git"
        self.artifacts = self.home / "platform-published"
        self.artifacts.mkdir()
        for name in ("deployment/scripts/publish-platform.py", "deployment/scripts/render-platform.py",
                     "deployment/manifests/platform.yaml"):
            destination = self.repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        (self.repo / "README.md").write_text("reviewed source\n")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", ".")
        self.git("commit", "-m", "reviewed source")
        self.source_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("init", "--bare", str(self.remote))
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "main")
        self.workflow = yaml.safe_load((ROOT / ".github/workflows/platform-publish.yml").read_text())
        self.write_images("a")

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.repo, check=check, text=True, capture_output=True)

    def write_images(self, digest):
        for name in ("dashboard", "api"):
            (self.artifacts / f"{name}.json").write_text(json.dumps({
                name: f"ghcr.io/jasmin-softbank/railshot-{name}@sha256:{digest * 64}"}))

    def run_step(self, job, name, overrides=None):
        step = next(step for step in self.workflow["jobs"][job]["steps"] if step.get("name") == name)
        env = {**os.environ, "RUNNER_TEMP": str(self.home), "GITHUB_SHA": self.source_sha,
               "GITHUB_REF": "refs/heads/integration/test", "PUBLISH": "true", "DEPLOY": "true",
               "SKIP_BUILD": "false", "AUTO_RELEASE": "false", "MULTICLOUD": "false", "MULTICLOUD_ENABLED": "",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ID": "123", "CI_RUN_ID": "123",
               "GITHUB_REPOSITORY": "Jasmin-Softbank/Railshot", "GITHUB_REPOSITORY_ID": "1400202256",
               "GITHUB_OUTPUT": str(self.home / "github-output"), "VERIFY_REF": "refs/heads/integration/test",
               "VERIFY_ROLE": "arn:aws:iam::721622471953:role/railshot-platform-verifier",
               "VERIFY_VERSION": "1", "VERIFY_HASH": "e" * 64,
               "RELEASE_VERSION": "", "RELEASE_HASH": "",
               "COMPONENTS": '["dashboard","api"]', "PLATFORM_TARGET": "k3s-aws", "PLATFORM_PORT": "31080",
               "PROVIDER_TARGETS": "{}", "PERSONAL_ENABLED": "false",
               **(overrides or {})}
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=self.repo,
                              env=env, text=True, capture_output=True)

    def deploy(self, overrides=None):
        return self.run_step("deploy", "Commit the tested digest declaration to the platform branch", overrides)

    def remote_revision(self):
        result = self.git("ls-remote", "--exit-code", "origin", REFERENCE, check=False)
        return result.stdout.split()[0] if result.returncode == 0 else None

    def test_trust_checks_remain_in_the_deployment_job(self):
        name = 'Validate publication and deployment inputs'
        self.assertEqual(self.run_step('deploy', name).returncode, 0)
        for overrides in ({'PUBLISH': 'false'}, {'COMPONENTS': '["ci-runner"]'},
                          {'PLATFORM_TARGET': ''}, {'PLATFORM_PORT': '443'},
                          {'GITHUB_REF': 'refs/heads/feature/unreviewed'},
                          {'COMPONENTS': '["dashboard","api","api"]'},
                          {'VERIFY_REF': 'refs/heads/main'}, {'VERIFY_HASH': ''},
                          {'VERIFY_ROLE': ''}, {'GITHUB_REPOSITORY_ID': '1'},
                          {'PROVIDER_TARGETS': '{"openstack":"k3s-aws"}'}):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(self.run_step('deploy', name, overrides).returncode, 0)
        for component in ('dashboard', 'api', 'mcp'):
            self.assertEqual(self.run_step('deploy', name, {'COMPONENTS': json.dumps([component])}).returncode, 0)
        automatic = {'SKIP_BUILD': 'true', 'AUTO_RELEASE': 'true', 'GITHUB_EVENT_NAME': 'push'}
        self.assertEqual(self.run_step('deploy', name, automatic).returncode, 0)
        for changes in ({'GITHUB_EVENT_NAME': 'pull_request'}, {'AUTO_RELEASE': 'false'},
                        {'CI_RUN_ID': '124'}, {'MULTICLOUD': 'true', 'MULTICLOUD_ENABLED': 'true'}):
            self.assertNotEqual(self.run_step('deploy', name, {**automatic, **changes}).returncode, 0)
        maintenance = {'MULTICLOUD': 'true', 'MULTICLOUD_ENABLED': 'true',
                       'COMPONENTS': '["dashboard","api","ci-runner"]',
                       'RELEASE_VERSION': '2', 'RELEASE_HASH': 'f' * 64,
                       'PROVIDER_TARGETS': '{"aws":"k3s-aws","gcp":"k3s-gcp","openstack":"k3s-openstack"}'}
        self.assertEqual(self.run_step('deploy', name, maintenance).returncode, 0)
        self.assertNotEqual(self.run_step('deploy', name, {**maintenance, 'MULTICLOUD_ENABLED': 'false'}).returncode, 0)

    def test_normal_release_has_no_archive_transfer_or_multicloud_dependency(self):
        jobs = self.workflow['jobs']
        self.assertEqual(set(jobs), {'publish', 'deploy', 'ci-runtime', 'multicloud'})
        self.assertEqual(jobs['deploy']['needs'], 'publish')
        self.assertEqual(jobs['ci-runtime']['needs'], 'deploy')
        self.assertIn("contains(fromJSON(inputs.components), 'ci-runner')", jobs['ci-runtime']['if'])
        self.assertIn("github.event_name == 'workflow_dispatch'", jobs['multicloud']['if'])
        ci = yaml.safe_load((ROOT / '.github/workflows/railshot-ci.yml').read_text())
        self.assertIs(ci['jobs']['release']['with']['multicloud'], False)
        self.assertEqual(ci['concurrency'], {
            'group': 'railshot-ci-${{ github.event.pull_request.number || github.sha }}',
            'cancel-in-progress': False,
        })
        build = yaml.safe_load((ROOT / '.github/workflows/platform-containers.yml').read_text())
        steps = build['jobs']['publish']['steps']
        guard = next(step for step in steps if step.get('name') == 'Restrict publication to the trusted repository and ref')
        self.assertEqual(guard['if'], 'inputs.publish')
        for event, ref, repo, success in [('push', 'refs/heads/integration/test', 'Jasmin-Softbank/Railshot', True),
                                          ('pull_request', 'refs/heads/integration/test', 'Jasmin-Softbank/Railshot', False),
                                          ('push', 'refs/heads/feature/test', 'Jasmin-Softbank/Railshot', False),
                                          ('push', 'refs/heads/integration/test', 'fork/Railshot', False)]:
            result = subprocess.run(['bash', '-e', '-c', guard['run']], env={**os.environ,
                'GITHUB_EVENT_NAME': event, 'GITHUB_REF': ref, 'GITHUB_REPOSITORY': repo,
                'GITHUB_REPOSITORY_ID': '1400202256', 'VERIFY_REF': 'refs/heads/integration/test'}, capture_output=True)
            self.assertEqual(result.returncode == 0, success)
        text = json.dumps(steps)
        self.assertNotIn('docker save', text)
        self.assertNotIn('docker load', text)
        self.assertNotIn('tested-image-', text)
        publisher = next(i for i, step in enumerate(steps) if step.get('name') == 'Publish the built image')
        self.assertEqual(steps[publisher - 1]['name'], 'Run the built image without cloud or GitHub credentials')
        self.assertEqual(steps[publisher]['if'], 'inputs.publish')
        self.assertIn('docker push', steps[publisher]['run'])
        deploy = jobs['deploy']['steps']
        check = next(step for step in deploy if step.get('name') == 'Verify the exact Argo revision, running digests and public edge')
        self.assertIn('git switch --detach "$GITHUB_SHA"', check['run'])
        self.assertEqual(check['env']['REVISION'], '${{ steps.declaration.outputs.revision }}')
        self.assertEqual(self.workflow['concurrency'], {'group': 'platform-release', 'cancel-in-progress': False})

    def test_current_source_is_checked_before_each_remote_write(self):
        for job, mutation in (('deploy', 'Commit the tested digest declaration to the platform branch'),
                              ('ci-runtime', 'Promote the tested CI controller runner and workflow source'),
                              ('multicloud', 'Apply the same approved release and require three verified providers')):
            steps = self.workflow['jobs'][job]['steps']
            index = next(i for i, step in enumerate(steps) if step.get('name') == mutation)
            self.assertIn('release_admission.py', steps[index - 1]['run'])
            self.assertEqual(steps[index - 1]['id'], 'source')
            self.assertEqual(steps[index]['if'], "steps.source.outputs.admitted == 'true'")

    def test_release_keeps_enabled_provider_targets_across_image_updates(self):
        settings = {'PROVIDER_TARGETS': '{"openstack":"k3s-openstack"}'}
        self.assertEqual(self.run_step('deploy', 'Validate publication and deployment inputs', settings).returncode, 0)
        revisions = []
        for digest in ('a', 'b'):
            self.git('switch', '--detach', self.source_sha)
            self.write_images(digest)
            result = self.deploy(settings)
            self.assertEqual(result.returncode, 0, result.stderr)
            revisions.append(self.remote_revision())
            workload = json.loads(self.git('show', f'{revisions[-1]}:{WORKLOAD}').stdout)
            api = next(item for item in workload['items'] if item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-api')
            container = api['spec']['template']['spec']['containers'][0]
            self.assertTrue(container['image'].endswith(digest * 64))
            env = {item['name']: item.get('value') for item in container['env']}
            self.assertEqual(env['RAILSHOT_TARGET_ID'], 'k3s-aws')
            self.assertEqual(env['RAILSHOT_TARGET_PROVIDER'], 'aws')
            self.assertEqual(json.loads(env['RAILSHOT_PROVIDER_TARGETS']), {'openstack': 'k3s-openstack'})
            self.assertEqual(env['RAILSHOT_TARGET_IDS'], 'k3s-aws,k3s-openstack')
        self.assertEqual(self.git('rev-parse', f'{revisions[1]}^').stdout.strip(), revisions[0])
        self.assertEqual(self.git('diff', '--name-only', *revisions).stdout.strip(), WORKLOAD)

    def test_invalid_provider_map_blocks_release_before_branch_creation(self):
        for value in ('bad-json', 'null', '[]', '{"openstack":"k3s-aws"}'):
            with self.subTest(provider_targets=value):
                self.assertNotEqual(self.deploy({'PROVIDER_TARGETS': value}).returncode, 0)
                self.assertIsNone(self.remote_revision())

    def test_release_creates_reviewed_branch_and_preserves_its_existing_files(self):
        first = self.deploy()
        self.assertEqual(first.returncode, 0, first.stderr)
        first_revision = self.remote_revision()
        self.assertEqual(json.loads(first.stdout)["revision"], first_revision)
        outputs = (self.home / "github-output").read_text()
        self.assertIn(f"revision={first_revision}\n", outputs)
        self.assertIn(f"dashboard_digest={'a' * 64}\n", outputs)
        self.assertIn(f"api_digest={'a' * 64}\n", outputs)
        self.assertEqual(self.git("rev-parse", f"{first_revision}^").stdout.strip(), self.source_sha)
        self.assertEqual(self.git("diff", "--name-only", self.source_sha, first_revision).stdout.strip(), WORKLOAD)

        # A newer source tree must not replace independently maintained branch files.
        self.git("switch", "main")
        (self.repo / "README.md").write_text("new source, not a config branch update\n")
        self.git("commit", "-am", "new reviewed source")
        self.source_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("switch", "--detach", first_revision)
        (self.repo / "operator-note.txt").write_text("preserve this branch file\n")
        self.git("add", "operator-note.txt")
        self.git("commit", "-m", "operator branch change")
        operator_revision = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("push", "origin", f"HEAD:{REFERENCE}")
        self.git("switch", "--detach", self.source_sha)
        self.write_images("b")
        second = self.deploy()
        self.assertEqual(second.returncode, 0, second.stderr)
        second_revision = self.remote_revision()
        self.assertEqual(self.git("rev-parse", f"{second_revision}^").stdout.strip(), operator_revision)
        self.assertEqual(self.git("diff", "--name-only", operator_revision, second_revision).stdout.strip(), WORKLOAD)
        self.assertEqual(self.git("show", f"{second_revision}:operator-note.txt").stdout, "preserve this branch file\n")
        self.assertEqual(self.git("show", f"{second_revision}:README.md").stdout, "reviewed source\n")
        workload = json.loads(self.git("show", f"{second_revision}:{WORKLOAD}").stdout)
        deployments = [item for item in workload["items"] if item["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 2)
        for item in deployments:
            self.assertTrue(item["spec"]["template"]["spec"]["containers"][0]["image"].endswith("b" * 64))
        self.git("switch", "--detach", self.source_sha)
        unchanged = self.deploy()
        self.assertEqual(unchanged.returncode, 0, unchanged.stderr)
        self.assertEqual(json.loads(unchanged.stdout)["status"], "unchanged")
        self.assertEqual(self.remote_revision(), second_revision)

    def test_partial_release_preserves_unchanged_deployment_and_customer_tree(self):
        self.assertEqual(self.deploy().returncode, 0)
        first = self.remote_revision()
        previous = json.loads(self.git("show", f"{first}:{WORKLOAD}").stdout)
        api = next(x for x in previous['items'] if x['kind'] == 'Deployment' and x['metadata']['name'] == 'railshot-api')
        self.git("switch", "--detach", self.source_sha)
        (self.artifacts / 'api.json').unlink()
        (self.artifacts / 'dashboard.json').write_text(json.dumps({'dashboard': 'ghcr.io/jasmin-softbank/railshot-dashboard@sha256:' + 'b' * 64}))
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(self.git("show", f"{self.remote_revision()}:{WORKLOAD}").stdout)
        self.assertEqual(next(x for x in after['items'] if x['kind'] == 'Deployment' and x['metadata']['name'] == 'railshot-api'), api)
        self.assertEqual(self.git('diff', '--name-only', first, self.remote_revision()).stdout.strip(), WORKLOAD)
        # Symmetric API-only updates retain the complete dashboard Deployment.
        dashboard = next(x for x in after['items'] if x['kind'] == 'Deployment' and x['metadata']['name'] == 'railshot-dashboard')
        self.git("switch", "--detach", self.source_sha)
        (self.artifacts / 'dashboard.json').unlink()
        (self.artifacts / 'api.json').write_text(json.dumps({'api': 'ghcr.io/jasmin-softbank/railshot-api@sha256:' + 'c' * 64}))
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(self.git("show", f"{self.remote_revision()}:{WORKLOAD}").stdout)
        self.assertEqual(next(x for x in after['items'] if x['kind'] == 'Deployment' and x['metadata']['name'] == 'railshot-dashboard'), dashboard)

    def test_personal_mode_toggle_rerenders_api_even_when_only_dashboard_changes(self):
        self.assertEqual(self.deploy().returncode, 0)
        self.git('switch', '--detach', self.source_sha)
        (self.artifacts / 'personal-gateway.json').write_text(json.dumps({'personal-gateway':
            'ghcr.io/jasmin-softbank/railshot-personal-gateway@sha256:' + 'b' * 64}))
        enabled = self.deploy({'PERSONAL_ENABLED': 'true'})
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        declaration = json.loads(self.git('show', f'{self.remote_revision()}:{WORKLOAD}').stdout)
        api = next(item for item in declaration['items'] if item['kind'] == 'Deployment'
                   and item['metadata']['name'] == 'railshot-api')
        self.assertTrue(any(item['name'] == 'personal-gateway'
                            for item in api['spec']['template']['spec']['initContainers']))
        self.git('switch', '--detach', self.source_sha)
        (self.artifacts / 'api.json').unlink()
        (self.artifacts / 'personal-gateway.json').unlink()
        (self.artifacts / 'dashboard.json').write_text(json.dumps({'dashboard':
            'ghcr.io/jasmin-softbank/railshot-dashboard@sha256:' + 'c' * 64}))
        still_enabled = self.deploy({'PERSONAL_ENABLED': 'true'})
        self.assertEqual(still_enabled.returncode, 0, still_enabled.stderr)
        declaration = json.loads(self.git('show', f'{self.remote_revision()}:{WORKLOAD}').stdout)
        api = next(item for item in declaration['items'] if item['kind'] == 'Deployment'
                   and item['metadata']['name'] == 'railshot-api')
        self.assertTrue(any(item['name'] == 'personal-gateway'
                            for item in api['spec']['template']['spec']['initContainers']))
        self.git('switch', '--detach', self.source_sha)
        (self.artifacts / 'dashboard.json').write_text(json.dumps({'dashboard':
            'ghcr.io/jasmin-softbank/railshot-dashboard@sha256:' + 'd' * 64}))
        disabled = self.deploy({'PERSONAL_ENABLED': 'false'})
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        declaration = json.loads(self.git('show', f'{self.remote_revision()}:{WORKLOAD}').stdout)
        api = next(item for item in declaration['items'] if item['kind'] == 'Deployment'
                   and item['metadata']['name'] == 'railshot-api')
        self.assertFalse(any(item['name'] == 'personal-gateway'
                             for item in api['spec']['template']['spec']['initContainers']))
        self.assertFalse(any(item['metadata']['name'].startswith('railshot-personal-')
                             for item in declaration['items']))

    def test_initial_personal_enablement_requires_both_runtime_images(self):
        self.assertEqual(self.deploy().returncode, 0)
        self.git('switch', '--detach', self.source_sha)
        (self.artifacts / 'api.json').unlink()
        (self.artifacts / 'personal-gateway.json').write_text(json.dumps({'personal-gateway':
            'ghcr.io/jasmin-softbank/railshot-personal-gateway@sha256:' + 'b' * 64}))
        result = self.deploy({'PERSONAL_ENABLED': 'true'})
        self.assertNotEqual(result.returncode, 0)

    def test_dashboard_only_release_preserves_preparation_hook_without_changing_api(self):
        self.assertEqual(self.deploy().returncode, 0)
        self.git('switch', '--detach', self.source_sha)
        self.write_images('b')
        self.assertEqual(self.deploy().returncode, 0)
        previous = json.loads(self.git('show', f'{self.remote_revision()}:{WORKLOAD}').stdout)
        hook = next(item for item in previous['items'] if item['kind'] == 'Job')
        api = next(item for item in previous['items'] if item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-api')
        self.git('switch', '--detach', self.source_sha)
        (self.artifacts / 'api.json').unlink()
        (self.artifacts / 'dashboard.json').write_text(json.dumps({'dashboard': 'ghcr.io/jasmin-softbank/railshot-dashboard@sha256:' + 'c' * 64}))
        self.assertEqual(self.deploy().returncode, 0)
        after = json.loads(self.git('show', f'{self.remote_revision()}:{WORKLOAD}').stdout)
        self.assertEqual(next(item for item in after['items'] if item['kind'] == 'Job'), hook)
        self.assertEqual(next(item for item in after['items'] if item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-api'), api)

    def test_mcp_only_release_adds_remote_service_and_later_preserves_it(self):
        self.assertEqual(self.deploy().returncode, 0)
        self.git('switch', '--detach', self.source_sha)
        (self.artifacts / 'dashboard.json').unlink()
        (self.artifacts / 'api.json').unlink()
        (self.artifacts / 'mcp.json').write_text(json.dumps({'mcp': 'ghcr.io/jasmin-softbank/railshot-mcp@sha256:' + 'b' * 64}))
        first = self.deploy()
        self.assertEqual(first.returncode, 0, first.stderr)
        workload = json.loads(self.git('show', f'{self.remote_revision()}:{WORKLOAD}').stdout)
        mcp = next(item for item in workload['items'] if item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-mcp')
        self.assertTrue(mcp['spec']['template']['spec']['containers'][0]['image'].endswith('b' * 64))
        self.assertTrue(any(item['kind'] == 'Service' and item['metadata']['name'] == 'railshot-mcp' for item in workload['items']))
        self.git('switch', '--detach', self.source_sha)
        (self.artifacts / 'mcp.json').unlink()
        (self.artifacts / 'dashboard.json').write_text(json.dumps({'dashboard': 'ghcr.io/jasmin-softbank/railshot-dashboard@sha256:' + 'c' * 64}))
        second = self.deploy()
        self.assertEqual(second.returncode, 0, second.stderr)
        after = json.loads(self.git('show', f'{self.remote_revision()}:{WORKLOAD}').stdout)
        self.assertEqual(next(item for item in after['items'] if item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-mcp'), mcp)

    def test_untrusted_or_missing_digest_fails_before_branch_creation(self):
        image = self.artifacts / "api.json"
        image.write_text(json.dumps({"api": "ghcr.io/jasmin-softbank/railshot-api:latest"}))
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertIsNone(self.remote_revision())
        image.unlink()
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertIsNone(self.remote_revision())

    def test_initial_branch_is_created_even_when_source_already_contains_declaration(self):
        images = self.home / "images.json"
        images.write_text(json.dumps({name: json.loads((self.artifacts / f"{name}.json").read_text())[name]
                                     for name in ("dashboard", "api")}))
        rendered = subprocess.run(["python3", "deployment/scripts/render-platform.py", str(images),
                                   "--target-id", "k3s-aws", "--dashboard-node-port", "31080"],
                                  cwd=self.repo, check=True, text=True, capture_output=True)
        path = self.repo / WORKLOAD
        path.parent.mkdir(parents=True)
        path.write_text(rendered.stdout)
        self.git("add", WORKLOAD)
        self.git("commit", "-m", "reviewed declaration already present")
        self.source_sha = self.git("rev-parse", "HEAD").stdout.strip()
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_revision(), self.source_sha)
        self.assertEqual(json.loads(result.stdout)["status"], "published")

    def test_rejected_push_does_not_claim_success_or_change_remote(self):
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('"status": "published"', result.stdout)
        self.assertIsNone(self.remote_revision())


if __name__ == "__main__":
    unittest.main()
