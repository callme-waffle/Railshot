"""Offline Terraform template rendering and fail-closed mount contract. No cloud or formatting."""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import yaml

MODULE = Path(__file__).resolve().parent


def evaluate(expression):
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(['terraform', 'console', '-no-color'], cwd=directory,
                                input='jsonencode(' + expression + ')\n', capture_output=True, text=True, check=True)
    if 'Error:' in result.stderr:
        raise ValueError(result.stderr)
    return json.loads(json.loads(result.stdout))


def render(operator_ssh_public_key=None, purpose="runtime", max_run_duration_seconds=None):
    args = {'device': '/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol01234567890123456',
            'initialize_empty_data_disk': 'false',
            'data_mount_path': '/var/lib/postgresql' if purpose == 'database' else '/var/lib/rancher'}
    script = evaluate('templatefile(' + json.dumps(str(MODULE / 'bootstrap.sh.tftpl')) + ',' + json.dumps(args) + ')')
    config = {'name': 'offline', 'cloud_provider': 'aws', 'region': 'ap-northeast-2',
              'runtime_status': 'not_configured'}
    data = {'host_config': yaml.safe_dump(config), 'bootstrap_script': script,
            'operator_ssh_public_key': operator_ssh_public_key, 'max_run_duration_seconds': max_run_duration_seconds,
            'bootstrap_manifest': json.dumps({'method': 'host-preparation-only', 'runtime_status': 'not_configured'})}
    return yaml.safe_load(evaluate('templatefile(' + json.dumps(str(MODULE / 'cloud-init.yaml.tftpl')) + ',' + json.dumps(data) + ')'))


class AWSBootstrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = render()
        cls.files = {x['path']: x for x in cls.config['write_files']}
        cls.script = cls.files['/usr/local/sbin/railshot-bootstrap']['content']

    def test_host_handoff_has_no_runtime_installer(self):
        node = yaml.safe_load(self.files['/etc/railshot/host.yml']['content'])
        self.assertEqual(node['cloud_provider'], 'aws')
        self.assertEqual(node['runtime_status'], 'not_configured')
        for retired in ('ansible', 'get.k3s.io', 'argocd', 'gitops'):
            self.assertNotIn(retired, json.dumps(self.config))
        self.assertFalse(any('/systemd/system/k3s' in path for path in self.files))
        subprocess.run(['bash', '-n'], input=self.script, text=True, check=True)

    def test_database_mount_is_distinct_and_keeps_readiness_unverified(self):
        config = render(purpose="database")
        script = next(row['content'] for row in config['write_files'] if row['path'].endswith('railshot-bootstrap'))
        self.assertIn('mount_path=/var/lib/postgresql', script)
        self.assertNotIn('/var/lib/rancher', script)
        self.assertIn('runtime_ready":"not_configured', script)
        subprocess.run(['bash', '-n'], input=script, text=True, check=True)

    def test_optional_runtime_timer_stops_without_removing_retained_data(self):
        self.assertFalse(any('runtime-limit' in path for path in self.files))
        configured = render(max_run_duration_seconds=7200)
        files = {row['path']: row['content'] for row in configured['write_files']}
        self.assertIn('OnActiveSec=7200s', files['/etc/systemd/system/railshot-runtime-limit.timer'])
        self.assertIn('ExecStart=/sbin/poweroff', files['/etc/systemd/system/railshot-runtime-limit.service'])
        self.assertEqual(configured['runcmd'], [['systemctl', 'daemon-reload'],
                         ['systemctl', 'enable', '--now', 'railshot-runtime-limit.timer'],
                         ['bash', '/usr/local/sbin/railshot-bootstrap']])

    def test_public_operator_key_bootstraps_only_a_locked_ssh_user(self):
        key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureOnlyTestNotARealKey offline'
        self.assertNotIn('users', self.config)
        configured = render(key)
        self.assertEqual(configured['users'][1], {
            'name': 'railshot-operator', 'lock_passwd': True, 'shell': '/bin/bash',
            'sudo': 'ALL=(ALL) NOPASSWD:ALL', 'ssh_authorized_keys': [key]})
        self.assertEqual(configured['runcmd'], self.config['runcmd'])

    def test_disk_identity_and_fail_closed_mount_precede_completion(self):
        self.assertIn('nvme-Amazon_Elastic_Block_Store_vol01234567890123456', self.script)
        self.assertLess(self.script.index('mounted_uuid='), self.script.index('host_prepared'))
        for guard in ('required data device missing', 'partitioned disk rejected',
                      'empty disk initialization not authorized', 'unknown disk signature',
                      'existing non-ext4 filesystem', 'refusing to cover existing local node data', 'wrong data disk mounted'):
            self.assertIn(guard, self.script)
        self.assertNotIn('mkfs.ext4 -F', self.script)
        self.assertNotIn('nofail', self.script)

    def test_runtime_does_not_invent_mount_or_bootstrap_readiness(self):
        self.assertIn('runtime_ready":"not_configured', self.script)
        self.assertEqual(self.config['runcmd'], [['bash', '/usr/local/sbin/railshot-bootstrap']])

    def test_native_variable_validation_rejects_unknown_images_and_sizes(self):
        values = {'target_id': 'aws-offline', 'owner_ref': 'terraform:offline:test',
                  'account_id': '000000000000', 'ami_id': 'ami-' + '0' * 17}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'variables.tf').write_text((MODULE / 'variables.tf').read_text())
            for key, bad in (('ami_id', 'stable/current'), ('instance_type', 'unreviewed-size'),
                             ('operator_ssh_public_key', '-----BEGIN OPENSSH PRIVATE KEY-----'),
                             ('operator_ssh_public_key', 'ssh-ed25519 AAAA\nroot: injected'),
                             ('existing_instance_profile', ''), ('existing_instance_profile', 'node/role'),
                             ('existing_instance_profile', 'arn:aws:iam::000000000000:instance-profile/node'),
                             ('existing_instance_profile', 'n' * 129),
                             ('vpc_id', 'unknown'), ('subnet_id', 'unknown'),
                             ('additional_security_group_ids', ['0.0.0.0/0']),
                             ('purpose', 'arbitrary'), ('max_run_duration_seconds', 1799),
                             ('max_run_duration_seconds', 604801), ('max_run_duration_seconds', 7200.5), ('database_ingress', [{'port': 5432, 'cidr': '0.0.0.0/0'}]),
                             ('database_egress', [{'port': 22, 'cidr': '10.1.0.0/16'}]),
                             ('database_ingress', [{'port': 5432, 'cidr': '10.0.0.0/1'}]),
                             ('database_ingress', [{'port': 5432, 'cidr': '192.168.999.1/32'}])):
                with self.subTest(key=key):
                    inputs = root / 'inputs.tfvars.json'; inputs.write_text(json.dumps({**values, key: bad}))
                    result = subprocess.run(['terraform', 'console', '-no-color', '-var-file=' + str(inputs)], cwd=root,
                                            input='jsonencode(var.' + key + ')\n', capture_output=True, text=True)
                    self.assertIn('Error:', result.stderr)

    def test_reused_profile_skips_all_node_iam_and_preserves_default_addresses(self):
        source = (MODULE / 'main.tf').read_text()
        expressions = []
        for resource_type, name in (('aws_iam_role', 'node'), ('aws_iam_role_policy_attachment', 'ssm'),
                                    ('aws_iam_role_policy', 'node_params'), ('aws_iam_instance_profile', 'node')):
            block = source.split(f'resource "{resource_type}" "{name}" {{', 1)[1].split('\n}', 1)[0]
            expressions.append(re.search(r'\bcount\s*=\s*([^\n]+)', block).group(1).strip())
            address = resource_type + '.' + name
            self.assertRegex(source, rf'moved\s*\{{\s*from\s*=\s*{re.escape(address)}\s+to\s*=\s*{re.escape(address)}\[0\]\s*\}}')
        # Evaluate the actual four count expressions, without a provider, backend or real state.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'variables.tf').write_text((MODULE / 'variables.tf').read_text())
            base = {'target_id': 'aws-offline', 'owner_ref': 'terraform:offline:test',
                    'account_id': '000000000000', 'ami_id': 'ami-' + '0' * 17}
            for existing, expected in ((None, [1] * 4), ('railshot-user-aws-node', [0] * 4)):
                inputs = root / 'inputs.tfvars.json'
                inputs.write_text(json.dumps({**base, 'existing_instance_profile': existing}))
                result = subprocess.run(['terraform', 'console', '-no-color', '-var-file=' + str(inputs)], cwd=root,
                    input='jsonencode([' + ','.join(expressions) + '])\n', capture_output=True, text=True, check=True)
                self.assertNotIn('Error:', result.stderr)
                self.assertEqual(json.loads(json.loads(result.stdout)), expected)
        self.assertRegex(source, r'iam_instance_profile\s*=\s*var\.existing_instance_profile == null \? aws_iam_instance_profile\.node\[0\]\.name : var\.existing_instance_profile')

    def test_product_tags_are_opt_in_and_use_provider_creation_tags(self):
        source = (MODULE / 'versions.tf').read_text()
        locals_block = 'locals {' + source.split('locals {', 1)[1].split('\nprovider ', 1)[0]
        tags_expression = re.search(r'default_tags[^\S\n]*\{[^\S\n]*tags[^\S\n]*=[^\S\n]*(.+?)[^\S\n]*\}$', source, re.MULTILINE).group(1).strip()
        legacy = {'Project': 'railshot', 'ManagedBy': 'terraform'}
        product = {'ProjectOwner': 'railshot-product', 'Target': 'aws-offline'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'variables.tf').write_text((MODULE / 'variables.tf').read_text())
            (root / 'locals.tf').write_text(locals_block)
            base = {'target_id': 'aws-offline', 'owner_ref': 'terraform:offline:test',
                    'account_id': '000000000000', 'ami_id': 'ami-' + '0' * 17}
            for selection, expected in (({}, {}), ({'product_environment': False}, {}),
                                        ({'product_environment': True}, product)):
                with self.subTest(selection=selection):
                    inputs = root / 'inputs.tfvars.json'
                    inputs.write_text(json.dumps({**base, **selection}))
                    result = subprocess.run(['terraform', 'console', '-no-color', '-var-file=' + str(inputs)], cwd=root,
                        input='jsonencode([local.product_tags,' + tags_expression + '])\n',
                        capture_output=True, text=True, check=True)
                    self.assertNotIn('Error:', result.stderr)
                    self.assertEqual(json.loads(json.loads(result.stdout)), [expected, {**legacy, **expected}])
        # Pinned 6.66.0 sends provider defaults in RunInstances volume TagSpecifications.
        # Do not add root tags (post-create API), or volume_tags (conflicts with the separate data volume).
        main = (MODULE / 'main.tf').read_text()
        root_device = main.split('root_block_device {', 1)[1].split('\n  }', 1)[0]
        self.assertNotRegex(root_device, r'\btags\s*=')
        self.assertNotRegex(main, r'\bvolume_tags\s*=')

    def test_managed_ssm_policy_cannot_restore_parameter_access(self):
        # Actual pure locals are evaluated in a fresh directory: no backend, provider, or real state.
        source = (MODULE / 'main.tf').read_text()
        locals_block = 'locals {' + source.split('locals {', 1)[1].split('\nresource ', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'locals.tf').write_text(locals_block)
            process = subprocess.run(['terraform', 'console', '-no-color'], cwd=root,
                input='jsonencode(local.node_parameter_policy)\n', capture_output=True, text=True, check=True)
        self.assertNotIn('Error:', process.stderr)
        policy = json.loads(json.loads(process.stdout))
        self.assertEqual(policy['Statement'], [{
            'Effect': 'Deny', 'Resource': '*',
            'Action': ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath', 'ssm:GetParameterHistory'],
        }])


if __name__ == '__main__':
    unittest.main(verbosity=2)
