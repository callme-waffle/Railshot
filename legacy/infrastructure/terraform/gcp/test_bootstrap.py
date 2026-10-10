"""Offline render and temporary apt-source checks; no credentials, cloud API or VM.

Run directly: python3 test_bootstrap.py; evaluates a fresh source-only directory.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


MODULE = Path(__file__).resolve().parent
def evaluate(expression, overrides=None):
    inputs = {
        "project_id": "railshot-offline-test",
        "region": "asia-northeast3", "zone": "asia-northeast3-a",
        "target_id": "gcp-offline-test", "owner_ref": "terraform:offline:gcp",
        # Syntactic fixture, not a claim that this image exists.
        "boot_image": "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20000101",
    }
    inputs.update(overrides or {})
    # Never run console against a module directory that could hold real state/tfvars.
    with tempfile.TemporaryDirectory(prefix="railshot-gcp-render-") as directory:
        work = Path(directory)
        for name in ("variables.tf", "cloud-init.yaml.tftpl", "bootstrap.sh.tftpl"):
            (work / name).write_text((MODULE / name).read_text())
        pure_locals = (MODULE / "main.tf").read_text().split('resource "google_compute_network"', 1)[0]
        (work / "locals.tf").write_text(pure_locals)
        fixture = work / "fixture.tfvars.json"
        fixture.write_text(json.dumps(inputs))
        result = subprocess.run(
            ["terraform", "console", "-no-color", "-var-file=" + str(fixture)],
            input="jsonencode(" + expression + ")\n", text=True, capture_output=True,
            check=True, cwd=work,
        )
    # Terraform 1.5 console can return status 0 despite input-validation diagnostics.
    if "Error:" in result.stderr:
        raise ValueError(result.stderr)
    return json.loads(json.loads(result.stdout))


def render():
    return yaml.safe_load(evaluate("local.cloud_init"))


class BootstrapRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.git = render()

    def file(self, config, path):
        return next(item for item in config["write_files"] if item["path"] == path)

    def test_host_only_script_parses_without_execution(self):
        script = self.file(self.git, "/usr/local/sbin/railshot-bootstrap")["content"]
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        for retired in ("ansible", "argocd", "get.k3s.io", "gitops"):
            self.assertNotIn(retired, json.dumps(self.git))
        self.assertFalse(any('/systemd/system/k3s' in item['path'] for item in self.git['write_files']))

    def test_cloud_config_contains_only_host_handoff(self):
        node = yaml.safe_load(self.file(self.git, "/etc/railshot/host.yml")["content"])
        self.assertEqual(node["cloud_provider"], "gcp")
        self.assertEqual(node["runtime_status"], "not_configured")
        self.assertEqual(set(node), {"name", "node_name", "region", "cloud_provider", "runtime_status"})

    def test_data_mount_is_required_before_host_completion(self):
        script = self.file(self.git, "/usr/local/sbin/railshot-bootstrap")["content"]
        self.assertLess(script.index("mounted_uuid="), script.index("host_prepared"))
        self.assertIn("[ 'false' = true ]", script)
        self.assertIn("existing non-ext4 filesystem; refusing format", script)
        self.assertIn('runtime_ready":"not_configured', script)
        self.assertNotIn("mkfs.ext4 -F", script)

    def test_database_mount_and_private_ports(self):
        config = yaml.safe_load(evaluate('local.cloud_init', overrides={'purpose': 'database'}))
        script = self.file(config, '/usr/local/sbin/railshot-bootstrap')['content']
        self.assertIn('mount_path=/var/lib/postgresql', script)
        self.assertNotIn('/var/lib/rancher', script)
        self.assertIn('RAILSHOT_SSH_HOST_KEY', script)
        self.assertIn('/etc/ssh/ssh_host_ed25519_key.pub', script)
        self.assertIn('> /dev/ttyS0', script)
        self.assertIn('runtime_ready":"not_configured', script)
        subprocess.run(['bash', '-n'], input=script, text=True, check=True)
        rules = [{'port': port, 'cidr': '10.42.0.1/32'} for port in (5432, 2379, 2380, 8008)]
        self.assertEqual(evaluate('var.database_ingress', overrides={'database_ingress': rules}), rules)
        for rule in ({'port': 5432, 'cidr': '0.0.0.0/0'}, {'port': 5432, 'cidr': '10.0.0.0/1'},
                     {'port': 22, 'cidr': '10.1.0.0/16'}, {'port': 5432, 'cidr': '192.168.999.1/32'}):
            with self.subTest(rule=rule), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                evaluate('var.database_egress', overrides={'database_egress': [rule]})
        self.assertEqual(evaluate('local.web_ports', overrides={'purpose': 'database', 'allow_http': True}), [])

    def test_shared_network_references_and_legacy_moves(self):
        values = {'existing_network_name': 'shared-vpc', 'existing_subnetwork_name': 'shared-subnet'}
        self.assertEqual(evaluate('[var.existing_network_name, var.existing_subnetwork_name]', overrides=values),
                         ['shared-vpc', 'shared-subnet'])
        source = (MODULE / 'main.tf').read_text()
        self.assertIn('subnetwork = local.subnetwork_ref', source)
        for name in ('web', 'iap_ssh', 'host_https', 'host_deny_other', 'database_ingress', 'database_egress'):
            rule = source.split(f'resource "google_compute_firewall" "{name}" {{', 1)[1].split('\n}', 1)[0]
            self.assertRegex(rule, r'network\s*=\s*local.network_ref')
        for kind in ('network', 'subnetwork'):
            self.assertIn(f'to   = google_compute_{kind}.node[0]', source)
        self.assertIn('data.google_compute_subnetwork.existing[0].network == data.google_compute_network.existing[0].self_link', source)

    def test_optional_iap_and_runtime_limit_are_off_by_default(self):
        policy = evaluate("{iap = local.iap_ssh, runtime = local.runtime_limit}")
        self.assertEqual(policy, {"iap": None, "runtime": None})

    def test_management_api_accepts_only_bounded_ipv4_hosts(self):
        sources = [(MODULE / 'control-api.tf', 'k3s_control_source_cidrs'),
                   (MODULE.parent / 'control' / 'external-api.tf', 'external_k3s_api_cidrs')]
        cases = [([], True), (['192.0.2.1/32'], True), (['0.0.0.0/0'], False),
                 (['192.0.2.0/24'], False), (['2001:db8::1/128'], False),
                 (['invalid/32'], False), ([f'192.0.2.{n}/32' for n in range(1, 22)], False)]
        for source, variable in sources:
            with tempfile.TemporaryDirectory(prefix='railshot-api-cidrs-') as directory:
                work = Path(directory)
                # Evaluate the real variable validation without cloud resources or state.
                (work / 'main.tf').write_text(source.read_text().split('\nresource ', 1)[0])
                fixture = work / 'fixture.tfvars.json'
                for cidrs, valid in cases:
                    with self.subTest(variable=variable, cidrs=cidrs):
                        fixture.write_text(json.dumps({variable: cidrs}))
                        result = subprocess.run(
                            ['terraform', 'console', '-no-color', '-var-file=' + str(fixture)],
                            input=f'jsonencode(var.{variable})\n', cwd=work, text=True, capture_output=True,
                        )
                        accepted = result.returncode == 0 and 'Error:' not in result.stderr
                        self.assertEqual(accepted, valid, result.stderr)
                        if accepted:
                            self.assertEqual(set(json.loads(json.loads(result.stdout))), set(cidrs))

    def test_registry_oauth_scope_is_explicit_and_read_only(self):
        self.assertEqual(evaluate("local.node_oauth_scopes"), [])
        self.assertEqual(evaluate("local.node_oauth_scopes", overrides={"enable_gcp_registry_pull": True}),
                         ["https://www.googleapis.com/auth/devstorage.read_only"])

    def test_opt_in_uses_only_iap_ssh_and_stops_without_restart(self):
        policy = evaluate(
            "{iap = local.iap_ssh, runtime = local.runtime_limit}",
            overrides={"allow_iap_ssh": True, "max_run_duration_seconds": 7200},
        )
        self.assertEqual(policy["iap"]["source_ranges"], ["35.235.240.0/20"])
        self.assertEqual(policy["iap"]["ports"], ["22"])
        self.assertEqual(policy["iap"]["transport_ref"], "iap:railshot-offline-test/asia-northeast3-a/railshot-gcp")
        self.assertEqual(policy["runtime"], {
            "seconds": 7200, "automatic_restart": False, "instance_termination_action": "STOP",
        })

    def test_optional_operator_key_is_bounded(self):
        key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFixtureOnlyTestNotARealKey offline'
        self.assertNotIn('users', self.git)
        configured = yaml.safe_load(evaluate('local.cloud_init', overrides={'operator_ssh_public_key': key}))
        self.assertEqual(configured['users'][1], {
            'name': 'railshot-operator', 'lock_passwd': True, 'shell': '/bin/bash',
            'sudo': 'ALL=(ALL) NOPASSWD:ALL', 'ssh_authorized_keys': [key]})
        for invalid in ('-----BEGIN OPENSSH PRIVATE KEY-----', key + '\nroot: injected'):
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                evaluate('local.cloud_init', overrides={'operator_ssh_public_key': invalid})

    def test_wireguard_is_rejected_and_existing_firewalls_are_retained(self):
        self.assertEqual(evaluate('var.wireguard_peer_public_cidrs'), [])
        with self.assertRaisesRegex(ValueError, 'WireGuard is retired'):
            evaluate('var.wireguard_peer_public_cidrs', overrides={
                'wireguard_peer_public_cidrs': ['192.0.2.1/32']})
        source = (MODULE / 'main.tf').read_text()
        for name in ('wireguard_ingress', 'wireguard_egress'):
            self.assertNotIn(f'resource "google_compute_firewall" "{name}"', source)
            self.assertRegex(source, rf'removed\s*{{\s*from\s*=\s*google_compute_firewall\.{name}\s+lifecycle\s*{{\s*destroy\s*=\s*false\s*}}\s*}}')
        self.assertNotIn('wireguard', (MODULE / 'outputs.tf').read_text())

    def test_node_identity_is_explicit_and_existing_fqdn_can_be_preserved(self):
        config = yaml.safe_load(self.file(self.git, "/etc/railshot/host.yml")["content"])
        self.assertEqual(config["node_name"], "railshot-gcp")
        original = "railshot-gcp-poc.asia-northeast3-a.c.example.internal"
        rendered = yaml.safe_load(evaluate("local.cloud_init", overrides={"node_name": original}))
        self.assertEqual(yaml.safe_load(self.file(rendered, "/etc/railshot/host.yml")["content"])["node_name"], original)
        for invalid in ("UPPER.invalid", "node..invalid", "node/other", "a" * 64):
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                evaluate("local.cloud_init", overrides={"node_name": invalid})

    def test_invalid_runtime_limits_are_rejected_by_terraform(self):
        for duration in (29, 10368001, 30.5):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ValueError, "max_run_duration_seconds must be null or an integer"):
                    evaluate("local.runtime_limit", overrides={"max_run_duration_seconds": duration})

    def test_host_egress_allows_https_before_denying_other_public_traffic(self):
        source = (MODULE / 'main.tf').read_text()
        self.assertRegex(source, r'https_ports\s*=\s*\["443"\]')
        self.assertIn('google-platform-always-allowed', source)
        for name, priority in (('host_https', 1000), ('host_deny_other', 2000)):
            rule = source.split(f'resource "google_compute_firewall" "{name}" {{', 1)[1].split('\n}', 1)[0]
            self.assertRegex(rule, r'direction\s*=\s*"EGRESS"')
            self.assertRegex(rule, rf'priority\s*=\s*{priority}\b')
            self.assertRegex(rule, r'destination_ranges\s*=\s*\["0.0.0.0/0"\]')
            self.assertRegex(rule, r'target_service_accounts\s*=\s*\[google_service_account.node.email\]')
            if name == 'host_https':
                self.assertRegex(rule, r'allow\s*\{\s*protocol\s*=\s*"tcp"\s+ports\s*=\s*local.host_egress.https_ports')
            else:
                self.assertRegex(rule, r'deny\s*\{\s*protocol\s*=\s*"all"')

    def test_apt_boot_payload_rewrites_approved_mirrors_and_rejects_unknown_http(self):
        self.assertTrue(self.git['apt']['preserve_sources_list'])
        code = self.git['bootcmd'][0].split("<<'PYBOOT'\n", 1)[1].rsplit('\nPYBOOT', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            apt = Path(directory) / 'apt'
            (apt / 'sources.list.d').mkdir(parents=True)
            fixtures = {'sources.list': 'deb http://security.ubuntu.com/ubuntu noble-security main\n',
                        'sources.list.d/ubuntu.sources': 'URIs: http://region.cloud.archive.ubuntu.com/ubuntu\n',
                        'sources.list.d/extra.list': 'deb https://approved.example/repo noble main\n'}
            for path, content in fixtures.items():
                (apt / path).write_text(content)
            script = code.replace('/etc/apt', str(apt))
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            for path, content in fixtures.items():
                expected = content.replace('http://security.', 'https://security.').replace(
                    'http://region.cloud.archive.', 'https://archive.')
                self.assertEqual((apt / path).read_text(), expected)
            for content in ('URIs: http://unreviewed.example/repo\n',
                            'deb http://unreviewed.example/repo noble main\n'):
                with self.subTest(source=content):
                    path = apt / 'sources.list.d/ubuntu.sources'
                    path.write_text(content)
                    failed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=5)
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn('bootstrap blocked', failed.stderr)
                    self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
