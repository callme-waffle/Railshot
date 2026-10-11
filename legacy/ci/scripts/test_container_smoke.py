"""Offline regressions for the container smoke's isolation and proxy assertions."""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('container_smoke', Path(__file__).with_name('container-smoke.py'))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class ContainerSmokeTests(unittest.TestCase):
    def test_native_probe_is_nonroot_readonly_network_none_and_cleans_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure), patch.object(smoke, 'docker') as docker, patch.object(smoke, 'remove_container') as cleanup:
                if failure:
                    docker.side_effect = subprocess.TimeoutExpired(['docker'], 180)
                    with self.assertRaises(subprocess.TimeoutExpired):
                        smoke.native_api_smoke('local-api:fixture')
                else:
                    docker.return_value = 'local runtime checks passed'
                    with patch('builtins.print'):
                        smoke.native_api_smoke('local-api:fixture')
                args = docker.call_args.args
                for flag, expected in [('--network', 'none'), ('--user', '1000:1000'), ('--entrypoint', 'python3')]:
                    self.assertEqual(args[args.index(flag) + 1], expected)
                self.assertIn('--read-only', args)
                self.assertIn('RAILSHOT_STATE_DIR=/tmp/railshot-state', args)
                self.assertEqual(args[-2:], ('local-api:fixture', '/app/apps/api/runtime-smoke.py'))
                cleanup.assert_called_once_with(args[args.index('--name') + 1])

    def test_dashboard_verifier_rejects_token_leaks_lost_origin_and_browser_auth_forwarding(self):
        token, marker, endpoint = 'synthetic-internal-token', 'local-marker', 'http://127.0.0.1:1234'
        for broken in (None, 'asset-secret', 'origin', 'authorization', 'static-secret', 'mcp-authorization'):
            calls = []
            def http(url, headers=None, data=None):
                if url == endpoint:
                    return 200, {}, b'<script src="/app.js"></script>'
                if url.endswith('/app.js'):
                    return 200, {}, token.encode() if broken == 'asset-secret' else b'public dashboard'
                if '/api/' in url:
                    self.assertEqual(headers['Host'], 'console.example.test')
                    if data is None:
                        self.assertNotIn('Authorization', headers, 'The browser must not supply the operator token')
                    calls.append({'path': url.removeprefix(endpoint), 'method': 'POST' if data else 'GET',
                                  'body': data, 'idempotency_key': headers.get('Idempotency-Key'),
                                  'authenticated': not (broken == 'authorization' and data is not None),
                                  'host': headers['Host'], 'origin': None if broken == 'origin' else headers['Origin']})
                    return 200, {}, json.dumps({'proxy_smoke': marker}).encode()
                if url.removeprefix(endpoint) in ['/mcp', '/mcp/', '/.well-known/oauth-protected-resource']:
                    calls.append({'path': url.removeprefix(endpoint), 'authenticated': broken == 'mcp-authorization',
                                  'host': headers['Host']})
                    return 401, {}, b'authentication required'
                return (200, {}, token.encode()) if broken == 'static-secret' else (404, {}, b'not found')
            with self.subTest(broken=broken), patch.object(smoke, 'http', side_effect=http):
                if broken:
                    with self.assertRaises(AssertionError):
                        smoke.verify_dashboard(endpoint, token, calls, marker)
                else:
                    smoke.verify_dashboard(endpoint, token, calls, marker)

    def test_host_fixture_rejects_missing_bearer_and_never_returns_the_token(self):
        token = 'synthetic-fixture-token'
        with smoke.mock_api(token) as (port, calls, marker):
            endpoint = f'http://127.0.0.1:{port}/api/v1/targets'
            self.assertEqual(smoke.http(endpoint)[0], 401)
            status, _, body = smoke.http(endpoint, {'Authorization': 'Bearer ' + token})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {'proxy_smoke': marker})
            self.assertNotIn(token.encode(), body)
            self.assertEqual([call['authenticated'] for call in calls], [False, True])


if __name__ == '__main__':
    unittest.main()
