"""Local manifest and native cloudflared checks; never start or register a tunnel."""
import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from render import IMAGE, render


def fixture():
    return {'namespace': 'edge-demo', 'name': 'demo-tunnel',
            'tunnel_id': '11111111-2222-4333-8444-555555555555',
            'credentials_secret': 'tunnel-credentials',
            'hostnames': ['web.example.com', 'api.example.com'], 'origin_vip': '10.20.0.50'}


def parts(values=None):
    result = render(**(values or fixture()))
    resources = {item['kind']: item for item in result['items']}
    return resources, json.loads(resources['ConfigMap']['data']['config.json'])


class RenderTest(unittest.TestCase):
    def test_exact_hosts_keep_tls_sni_host_header_and_final_404(self):
        _, config = parts()
        self.assertEqual(config['ingress'][-1], {'service': 'http_status:404'})
        self.assertEqual([r['hostname'] for r in config['ingress'][:-1]], ['api.example.com', 'web.example.com'])
        for route in config['ingress'][:-1]:
            self.assertEqual(route['service'], 'https://10.20.0.50:443')
            self.assertEqual(route['originRequest'], {'originServerName': route['hostname'],
                                                      'httpHostHeader': route['hostname'], 'noTLSVerify': False})
            self.assertNotIn('path', route)  # Octavia owns path routing, including /api vs /apix.

    def test_only_references_are_mounted_and_pod_has_no_api_privileges(self):
        resources, config = parts()
        self.assertEqual(set(resources), {'ServiceAccount', 'ConfigMap', 'Deployment'})
        self.assertFalse(resources['ServiceAccount']['automountServiceAccountToken'])
        deployment = resources['Deployment']['spec']
        self.assertEqual(deployment['replicas'], 1)
        self.assertEqual(deployment['strategy'], {'type': 'Recreate'})
        pod = deployment['template']['spec']
        for field in ('automountServiceAccountToken', 'hostNetwork', 'hostPID', 'hostIPC'):
            self.assertFalse(pod[field])
        self.assertTrue(pod['securityContext']['runAsNonRoot'])
        self.assertEqual(pod['securityContext']['runAsUser'], 65532)
        container = pod['containers'][0]
        self.assertEqual(container['image'], IMAGE)
        self.assertIn('@sha256:', container['image'])
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
        self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertNotIn('env', container)
        self.assertNotIn('ports', container)
        self.assertNotIn('livenessProbe', container)  # Do not restart on a Cloudflare network outage.
        self.assertIn('127.0.0.1:2000', container['readinessProbe']['exec']['command'])
        self.assertNotIn('--token', container['args'])
        secret = next(v['secret'] for v in pod['volumes'] if 'secret' in v)
        self.assertEqual(secret, {'secretName': 'tunnel-credentials', 'defaultMode': 0o440,
                                 'items': [{'key': 'credentials.json', 'path': 'credentials.json'}]})
        self.assertTrue(config['credentials-file'].endswith('/credentials.json'))

    def test_private_ca_is_an_existing_configmap_reference(self):
        values = fixture()
        values['ca_configmap'] = 'origin-ca'
        resources, config = parts(values)
        pod = resources['Deployment']['spec']['template']['spec']
        ca = next(v['configMap'] for v in pod['volumes'] if v['name'] == 'ca')
        self.assertEqual(ca['name'], 'origin-ca')
        self.assertEqual(ca['items'], [{'key': 'ca.pem', 'path': 'ca.pem'}])
        for route in config['ingress'][:-1]:
            self.assertEqual(route['originRequest']['caPool'], '/etc/cloudflared/ca/ca.pem')
            self.assertFalse(route['originRequest']['noTLSVerify'])

    def test_config_changes_roll_pod_and_host_order_does_not(self):
        values = fixture()
        initial = render(**values)
        values['hostnames'].reverse()
        self.assertEqual(render(**values), initial)
        values['origin_vip'] = '10.20.0.51'
        changed = render(**values)
        self.assertNotEqual(initial['items'][-1]['spec']['template']['metadata']['annotations'],
                            changed['items'][-1]['spec']['template']['metadata']['annotations'])

    def test_public_origins_injected_hosts_and_bad_references_are_rejected(self):
        mutations = [('origin_vip', value) for value in
                     ('8.8.8.8', '169.254.169.254', '100.64.0.1', '127.0.0.1', '::1',
                      'https://10.20.0.50', '10.20.0.50:443', '10.20.0.50/path', '10.999.0.1')]
        mutations += [('hostnames', [value]) for value in
                      ('*.example.com', 'https://app.example.com', 'app.example.com:443',
                       'APP.example.com', 'app.example.com.', 'app.example.com\nHost:x', '10.20.0.50')]
        mutations += [('hostnames', ['app.example.com'] * 2),
                      ('hostnames', [f'app{i}.example.com' for i in range(51)]),
                      ('credentials_secret', '../secret'), ('ca_configmap', '/ca.pem'),
                      ('tunnel_id', 'not-a-uuid'), ('namespace', 'bad/ns'), ('name', 'x' * 57)]
        for field, value in mutations:
            values = copy.deepcopy(fixture())
            values[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                render(**values)


@unittest.skipUnless(os.environ.get('CLOUDFLARED'), 'set CLOUDFLARED to a verified official binary for native validation')
class NativeCloudflaredTest(unittest.TestCase):
    def test_native_ingress_parser_and_matcher_accept_generated_json(self):
        resources, _ = parts()
        with tempfile.TemporaryDirectory(prefix='railshot-tunnel-test-') as directory:
            config = Path(directory) / 'config.json'
            config.write_text(resources['ConfigMap']['data']['config.json'])
            command = [os.environ['CLOUDFLARED'], 'tunnel', '--config', str(config), 'ingress']
            result = subprocess.run(command + ['validate'], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for url, expected in [('https://api.example.com/api', 'https://10.20.0.50:443'),
                                  ('https://web.example.com/apix', 'https://10.20.0.50:443'),
                                  ('https://unknown.example.com/', 'http_status:404')]:
                result = subprocess.run(command + ['rule', url], capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout + result.stderr)

    def test_native_localhost_ready_command_propagates_not_ready(self):
        class Handler(BaseHTTPRequestHandler):
            status = 200

            def do_GET(self):
                self.send_response(self.status if self.path == '/ready' else 404)
                self.end_headers()

            def log_message(self, *_args):
                pass

        with HTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for status in (200, 503):
                    Handler.status = status
                    result = subprocess.run([os.environ['CLOUDFLARED'], 'tunnel', '--metrics',
                                             f'127.0.0.1:{server.server_port}', 'ready'],
                                            capture_output=True, timeout=5)
                    self.assertEqual(result.returncode == 0, status == 200)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    unittest.main(verbosity=2)
