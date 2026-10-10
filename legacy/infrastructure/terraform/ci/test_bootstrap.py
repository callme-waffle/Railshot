"""Render and parse the exact cloud-init; compile embedded code without executing it."""
import base64
import gzip
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml


class BootstrapTests(unittest.TestCase):
    def test_existing_bootstrap_preserves_compressed_bytes_and_rejects_invalid_input(self):
        here = Path(__file__).resolve().parent
        source = (here/'main.tf').read_text()
        variable = source.split('variable "existing_user_data_base64" {', 1)[1].split('variable "admin_ssh_public_key"', 1)[0]
        expression = source.split('  user_data_base64 = ', 1)[1].split('\n  user_data_replace_on_change', 1)[0]
        pins = {'platform_ref': 'a'*40, 'archive_sha256': 'b'*64,
                'admin_ssh_public_key': 'ssh-ed25519 AAAATEST', 'stop_at': '2030-01-01T00:00:00Z'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path/'cloud-init.yaml.tftpl').write_bytes((here/'cloud-init.yaml.tftpl').read_bytes())
            declarations = '\n'.join(f'variable "{key}" {{ default = {json.dumps(value)} }}' for key, value in pins.items())
            (path/'main.tf').write_text('variable "existing_user_data_base64" {' + variable + declarations
                                       + '\nlocals { bootstrap = ' + expression + ' }\n')

            def evaluate(value=None):
                args = ['terraform', 'console']
                if value is not None:
                    (path/'existing.tfvars.json').write_text(json.dumps({'existing_user_data_base64': value}))
                    args += ['-var-file=existing.tfvars.json']
                return subprocess.run(args, cwd=tmp, input='local.bootstrap\n', text=True, capture_output=True)

            generated = evaluate()
            self.assertEqual(generated.returncode, 0, generated.stderr)
            rendered = json.loads(generated.stdout)
            payload = gzip.decompress(base64.b64decode(rendered))
            # Same cloud-init, deliberately different gzip header/compressor bytes.
            existing = base64.b64encode(gzip.compress(payload, mtime=123)).decode()
            self.assertNotEqual(existing, rendered)
            preserved = evaluate(existing)
            self.assertEqual(preserved.returncode, 0, preserved.stderr)
            self.assertEqual(json.loads(preserved.stdout), existing)
            evaluate('not-base64-or-gzip')
            invalid = subprocess.run(['terraform', 'plan', '-input=false', '-refresh=false',
                                      '-var-file=existing.tfvars.json'], cwd=tmp, text=True, capture_output=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn('verified existing gzip/base64', invalid.stderr)

    def test_pinned_public_source_ssm_only_and_absolute_deadline(self):
        here = Path(__file__).resolve().parent
        variables = {'platform_ref': 'a'*40, 'archive_sha256': 'b'*64,
                     'public_key': 'ssh-ed25519 AAAATEST', 'stop_at': '2030-01-01T00:00:00Z'}
        expression = 'base64encode(templatefile(' + json.dumps(str(here/'cloud-init.yaml.tftpl')) + ',' + json.dumps(variables) + '))'
        with tempfile.TemporaryDirectory() as tmp:
            process = subprocess.run(['terraform','console'],cwd=tmp,input=expression+'\n',text=True,capture_output=True,check=True)
        config = yaml.safe_load(base64.b64decode(json.loads(process.stdout)))
        boot = config['bootcmd'][0]
        code = boot.split("<<'PY'\n",1)[1].rsplit('\nPY',1)[0]
        compile(code,'cloud-init bootcmd','exec')
        self.assertIn("['systemctl','start','--no-block'",code)
        self.assertIn('datetime.now(timezone.utc) >= deadline',code)
        self.assertNotIn('enable --now',boot)
        script = next(f['content'] for f in config['write_files'] if f['path']=='/usr/local/sbin/railshot-ci-bootstrap')
        subprocess.run(['bash','-n'],input=script,text=True,check=True)
        self.assertIn('sha256sum --check --status',script)
        self.assertIn('https://codeload.github.com/Jasmin-Softbank/Railshot/tar.gz/' + variables['platform_ref'], script)
        self.assertIn('/infrastructure/ansible/ci.yml',script)
        self.assertIn('DOCKER_CONFIG=/var/lib/railshot-console/docker-config',script)
        self.assertNotIn('platform/control',json.dumps(config))
        self.assertNotIn('auth.json',json.dumps(config))
        self.assertNotIn('CODEX',json.dumps(config))
        policy=(here/'main.tf').read_text()
        self.assertIn('default     = null',policy)
        self.assertIn('ingress = [for rule in local.build_peer_health',policy)
        self.assertIn('security_groups  = [var.control_security_group_id]',policy)
        self.assertIn('"ssm:GetParameter", "ssm:GetParameters"',policy)
        self.assertIn('Effect = "Deny"',policy)
        self.assertIn('delete_on_termination = false',policy)
        self.assertIn('http_put_response_hop_limit = 1',policy)


if __name__=='__main__':unittest.main()
