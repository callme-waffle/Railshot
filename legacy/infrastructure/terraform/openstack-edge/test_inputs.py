"""Offline Terraform input/routing checks. No providers, credentials or cloud calls."""
import copy
import itertools
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parent
SUBNET = '11111111-2222-3333-4444-555555555555'


def fixture():
    route = {'host': 'app.example.com', 'target_private_ip': '10.10.1.20',
             'member_subnet_id': SUBNET, 'node_port': 30080, 'health_path': '/health'}
    return {
        'name': 'railshot-edge', 'vip_subnet_id': SUBNET,
        'default_tls_container_ref': f'https://barbican.example.com:9311/v1/containers/{SUBNET}',
        'allowed_cidrs': ['192.0.2.10/32'],
        'routes': {
            'web': {**route},
            'api': {**route, 'path_prefix': '/api', 'node_port': 30081},
            'admin': {**route, 'path_prefix': '/api/admin', 'node_port': 30082},
            'other': {**route, 'host': 'other.example.com', 'node_port': 30083},
        },
    }


def evaluate(values, expression='{routes=var.routes, patterns=local.path_patterns, exclusions=local.path_exclusions, enabled=var.enabled}'):
    with tempfile.TemporaryDirectory(prefix='railshot-octavia-inputs-') as directory:
        work = Path(directory)
        for filename in ('variables.tf', 'routes.tf'):
            (work / filename).write_text((MODULE / filename).read_text())
        (work / 'fixture.tfvars.json').write_text(json.dumps(values))
        result = subprocess.run(
            ['terraform', 'console', '-no-color', '-var-file=fixture.tfvars.json'],
            input=f'jsonencode({expression})\n', text=True, capture_output=True, cwd=work,
        )
        if result.returncode or 'Error:' in result.stderr:
            raise ValueError(result.stderr)
        return json.loads(json.loads(result.stdout))


def resource_body(kind, name):
    """Extract these flat resource bodies; provider validate checks their HCL schema."""
    source = (MODULE / 'main.tf').read_text()
    return re.search(r'resource "' + kind + r'" "' + name + r'" \{\n(.*?)\n\}', source, re.S).group(1)


class OctaviaInputsTest(unittest.TestCase):
    def test_longest_prefix_and_host_isolation_are_independent_of_policy_order(self):
        model = evaluate(fixture())
        self.assertFalse(model['enabled'])
        cases = [
            ('app.example.com', '/', 'web'),
            ('app.example.com', '/api', 'api'),
            ('app.example.com', '/api/items', 'api'),
            ('app.example.com', '/api/admin', 'admin'),
            ('app.example.com', '/api/admin/users', 'admin'),
            ('app.example.com', '/api/adminx', 'api'),
            ('app.example.com', '/apix', 'web'),
            ('app.example.com', '/api-v2', 'web'),
            ('other.example.com', '/api/admin', 'other'),
            ('unknown.example.com', '/', None),
            ('unknown.example.com', '/api', None),
        ]
        for host, path, expected in cases:
            matches = []
            for key, route in model['routes'].items():
                excluded = any(
                    e['parent'] == key and re.search(model['patterns'][e['child']], path)
                    for e in model['exclusions'].values()
                )
                if route['host'] == host and re.search(model['patterns'][key], path) and not excluded:
                    matches.append(key)
            with self.subTest(host=host, path=path):
                self.assertEqual(matches, [] if expected is None else [expected])
                for order in itertools.permutations(model['routes']):
                    self.assertEqual(next((key for key in order if key in matches), None), expected)

    def test_nonroot_route_never_matches_adjacent_prefix(self):
        values = fixture()
        values['routes'] = {'api': values['routes']['api']}
        pattern = evaluate(values)['patterns']['api']
        for path in ('/', '/apix', '/api-v2', '/API'):
            self.assertIsNone(re.search(pattern, path), path)
        for path in ('/api', '/api/', '/api/items'):
            self.assertIsNotNone(re.search(pattern, path), path)

    def test_existing_pkcs12_secret_reference_is_accepted_without_reading_payload(self):
        values = fixture()
        values['default_tls_container_ref'] = f'https://barbican.example.com/v1/secrets/{SUBNET}'
        self.assertEqual(evaluate(values, 'var.default_tls_container_ref'), values['default_tls_container_ref'])

    def test_invalid_addresses_ports_paths_and_hosts_are_rejected(self):
        changes = [('target_private_ip', value) for value in
                   ('8.8.8.8', '169.254.169.254', '100.64.0.1', '::1', '10.999.0.1')]
        changes += [('node_port', 80), ('node_port', 32768), ('node_port', 30080.5),
                    ('host', '*.example.com'), ('host', 'example.com\r\nx: y'),
                    ('member_subnet_id', 'not-a-subnet')]
        changes += [(field, value) for field in ('path_prefix', 'health_path')
                    for value in ('//api', '/api/', '/api.*', '/a?x=1', '/a#x', '/a%2fb', '/a b', '/a\nHost:x')]
        for field, value in changes:
            values = fixture()
            values['routes']['api'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                evaluate(values)

    def test_duplicate_routes_empty_routes_and_implicit_access_are_rejected(self):
        values = fixture()
        values['routes']['duplicate'] = copy.deepcopy(values['routes']['api'])
        with self.assertRaises(ValueError):
            evaluate(values)
        for field, value in [('routes', {}), ('allowed_cidrs', []),
                             ('allowed_cidrs', ['0.0.0.0/0']), ('allowed_cidrs', ['::/0']),
                             ('vip_subnet_id', 'not-a-subnet'),
                             ('default_tls_container_ref', '-----BEGIN PRIVATE KEY-----'),
                             ('default_tls_container_ref', f'https://user:password@barbican.example.com/v1/containers/{SUBNET}')]:
            values = fixture()
            values[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                evaluate(values)

    def test_route_limit_bounds_exclusion_generation(self):
        values = fixture()
        route = values['routes']['web']
        values['routes'] = {f'route-{i}': {**route, 'path_prefix': '/' + '/'.join(['x'] * i)} for i in range(50)}
        self.assertEqual(len(evaluate(values)['exclusions']), 1225)
        values['routes']['overflow'] = {**route, 'host': 'overflow.example.com'}
        with self.assertRaises(ValueError):
            evaluate(values)

    def test_native_resource_wiring_has_no_default_backend_or_priority(self):
        listener = resource_body('openstack_lb_listener_v2', 'https')
        pool = resource_body('openstack_lb_pool_v2', 'app')
        policy = resource_body('openstack_lb_l7policy_v2', 'app')
        host = resource_body('openstack_lb_l7rule_v2', 'host')
        path = resource_body('openstack_lb_l7rule_v2', 'path')
        exclusion = resource_body('openstack_lb_l7rule_v2', 'exclude_child')
        self.assertRegex(resource_body('openstack_lb_loadbalancer_v2', 'app'), r'loadbalancer_provider\s*=\s*"amphora"')
        self.assertRegex(listener, r'protocol\s*=\s*"TERMINATED_HTTPS"')
        self.assertRegex(listener, r'protocol_port\s*=\s*443')
        self.assertNotRegex(listener, r'(?m)^\s*default_pool_id\s*=')
        self.assertNotRegex(pool, r'(?m)^\s*listener_id\s*=')
        self.assertNotRegex(policy, r'(?m)^\s*position\s*=')
        self.assertRegex(policy, r'action\s*=\s*"REDIRECT_TO_POOL"')
        self.assertRegex(host, r'type\s*=\s*"HOST_NAME"')
        self.assertRegex(host, r'compare_type\s*=\s*"EQUAL_TO"')
        for body in (path, exclusion):
            self.assertRegex(body, r'type\s*=\s*"PATH"')
            self.assertRegex(body, r'compare_type\s*=\s*"REGEX"')
        self.assertRegex(exclusion, r'invert\s*=\s*true')
        self.assertIn('local.path_patterns[each.value.child]', exclusion)
        self.assertIn('openstack_lb_l7policy_v2.app[each.value.parent].id', exclusion)
        monitor = resource_body('openstack_lb_monitor_v2', 'app')
        self.assertRegex(monitor, r'type\s*=\s*"HTTP"')
        self.assertRegex(monitor, r'http_method\s*=\s*"GET"')
        self.assertRegex(monitor, r'http_version\s*=\s*"1.1"')
        self.assertRegex(monitor, r'domain_name\s*=\s*each.value.host')
        self.assertRegex(monitor, r'expected_codes\s*=\s*"200"')


if __name__ == '__main__':
    unittest.main(verbosity=2)
