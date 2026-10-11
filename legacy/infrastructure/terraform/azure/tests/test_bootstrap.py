"""Offline render and temporary apt-source checks; no provider or guest execution."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml


class BootstrapRenderTest(unittest.TestCase):
    def test_rendered_handoff_and_shell_syntax(self):
        module = Path(__file__).resolve().parents[1]
        terraform = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")
        self.assertIsNotNone(terraform, "terraform must be on PATH")
        config = {
            "name": "railshot-azure",
            "cloud_provider": "azure",
            "runtime_status": "not_configured",
        }
        inputs = {
            "host_config": yaml.safe_dump(config),
            "initialize_empty_data_disk": "false",
        }
        expression = (
            "jsonencode(yamldecode(templatefile("
            + json.dumps(str(module / "cloud-init.yaml.tftpl"))
            + ", "
            + json.dumps(inputs)
            + ")))\n"
        )
        with tempfile.TemporaryDirectory(prefix="railshot-azure-render-") as work:
            rendered = subprocess.run(
                [terraform, "console", "-no-color"],
                cwd=work,
                input=expression,
                text=True,
                capture_output=True,
                check=True,
            )
        cloud_config = json.loads(json.loads(rendered.stdout))
        files = {entry["path"]: entry for entry in cloud_config["write_files"]}
        node_file = files["/etc/railshot/host.yml"]
        self.assertEqual(yaml.safe_load(node_file["content"]), config)
        self.assertEqual(node_file["permissions"], "0600")
        script_path = "/usr/local/sbin/railshot-bootstrap"
        self.assertEqual(cloud_config["runcmd"], [[script_path]])
        script = files[script_path]["content"]
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        self.assertIn("test 'false' = true", script)
        self.assertIn("partitioned disk rejected", script)
        self.assertIn("unexpected disk signature; refusing format", script)
        self.assertIn('runtime_ready":"not_configured', script)
        self.assertLess(script.index('findmnt'), script.index('host_prepared'))
        for retired in ('ansible', 'argocd', 'get.k3s.io', 'gitops'):
            self.assertNotIn(retired, json.dumps(cloud_config))
        self.assertFalse(any('/systemd/system/k3s' in path for path in files))
        self.assertTrue(cloud_config['apt']['preserve_sources_list'])
        code = cloud_config['bootcmd'][0].split("<<'PYBOOT'\n", 1)[1].rsplit('\nPYBOOT', 1)[0]
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

    def test_host_egress_overrides_default_vnet_and_internet_access(self):
        source = (Path(__file__).resolve().parents[1] / 'main.tf').read_text()
        self.assertIn('for_each = local.host_egress_rules', source)
        rule = re.split(r'name\s*=\s*"deny-other-outbound"', source, maxsplit=1)[1].split('\n  }', 1)[0]
        for key, value in (('direction', 'Outbound'), ('access', 'Deny'), ('protocol', '*'),
                           ('destination_port_range', '*'), ('destination_address_prefix', '*')):
            self.assertRegex(rule, rf'{key}\s*=\s*"{re.escape(value)}"')
        self.assertRegex(rule, r'priority\s*=\s*4096\b')
        rules = source.split('host_egress_rules = {', 1)[1].split('\n  }', 1)[0]
        for name in ('https', 'dns_udp', 'dns_tcp', 'ntp', 'agent'):
            self.assertRegex(rules, name + r'\s*=')
        self.assertRegex(rules, r'https\s*=\s*\{[^\n]*ports = \["443"\], destination = "Internet"')
        self.assertNotIn('"22"', rules)
        self.assertNotIn('"VirtualNetwork"', rules)
        self.assertIn('"AzurePlatformDNS"', rules)
        self.assertIn('"168.63.129.16/32"', rules)


if __name__ == "__main__":
    unittest.main()
