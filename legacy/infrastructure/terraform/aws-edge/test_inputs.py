"""Offline input/routing contracts. No provider, state, credentials or cloud calls."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parent


def fixture():
    return {
        'name': 'railshot-edge', 'account_id': '123456789012', 'region': 'ap-northeast-2',
        'vpc_id': 'vpc-0123456789abcdef0',
        'public_subnet_ids': ['subnet-0123456789abcdef0', 'subnet-0123456789abcdef1'],
        'zone_id': 'ZEXAMPLE', 'base_domain': 'example.com',
        'routes': {
            'aws-demo-a1b2': {'host': 'aws-demo-a1b2.example.com', 'provider_kind': 'aws',
                'target_private_ip': '172.31.10.20', 'node_port': 30080,
                'health_path': '/healthz', 'priority': 100, 'target_security_group_id': 'sg-0123456789abcdef1'},
            'aws-other-c3d4': {'host': 'aws-other-c3d4.example.com', 'provider_kind': 'aws',
                'target_private_ip': '10.66.0.2', 'node_port': 30081, 'health_path': '/ready', 'priority': 200, 'target_security_group_id': 'sg-0123456789abcdef2'},
        },
    }


def evaluate(values, expression='local.aws_target_rules'):
    with tempfile.TemporaryDirectory(prefix='railshot-edge-inputs-') as directory:
        work = Path(directory)
        (work / 'variables.tf').write_text((MODULE / 'variables.tf').read_text())
        source = (MODULE / 'main.tf').read_text()
        (work / 'locals.tf').write_text('locals {' + source.split('locals {', 1)[1].split('\ndata "aws_subnet"', 1)[0])
        (work / 'fixture.tfvars.json').write_text(json.dumps(values))
        result = subprocess.run(['terraform', 'console', '-no-color', '-var-file=fixture.tfvars.json'],
                                input='jsonencode(' + expression + ')\n', text=True, capture_output=True, cwd=work)
        if result.returncode or 'Error:' in result.stderr:
            raise ValueError(result.stderr)
        return json.loads(json.loads(result.stdout))


class EdgeInputsTest(unittest.TestCase):
    def test_stopped_apps_keep_dns_but_no_backend_networking_and_last_app_can_delete(self):
        values = fixture()
        values['routes']['aws-demo-a1b2']['enabled'] = False
        self.assertEqual(evaluate(values, 'keys(local.active_routes)'), ['aws-other-c3d4'])
        self.assertEqual(evaluate(values, 'keys(local.dns_routes)'), ['aws-demo-a1b2', 'aws-other-c3d4'])
        self.assertEqual(evaluate(values), {'sg-0123456789abcdef2:30081': {'security_group_id': 'sg-0123456789abcdef2', 'port': 30081}})
        values['routes'] = {}
        self.assertEqual(evaluate(values, '{routes=local.active_routes,dns=local.dns_routes,rules=local.aws_target_rules}'),
                         {'routes': {}, 'dns': {}, 'rules': {}})

    def test_idle_timeout_preserves_default_and_bounds_long_api_waits(self):
        values = fixture()
        self.assertEqual(evaluate(values, 'var.idle_timeout'), 60)
        values['idle_timeout'] = 610
        self.assertEqual(evaluate(values, 'var.idle_timeout'), 610)
        for invalid in (0, 4001, 1.5):
            values['idle_timeout'] = invalid
            with self.subTest(idle_timeout=invalid), self.assertRaises(ValueError):
                evaluate(values, 'var.idle_timeout')

    def test_apex_requires_explicit_certificate_and_preserves_child_routes(self):
        values = fixture()
        self.assertTrue(evaluate(values, 'local.route_hosts_valid'))
        values['routes']['aws-demo-a1b2']['host'] = 'example.com'
        self.assertFalse(evaluate(values, 'local.route_hosts_valid'))
        values['apex_certificate_arn'] = 'arn:aws:acm:ap-northeast-2:123456789012:certificate/1234abcd-1234-1234-1234-123456789abc'
        self.assertTrue(evaluate(values, 'local.route_hosts_valid'))
        values['routes']['aws-demo-a1b2']['host'] = 'nested.app.example.com'
        self.assertFalse(evaluate(values, 'local.route_hosts_valid'))

    def test_aws_apps_need_only_target_sg_and_no_gateway_inputs(self):
        rules = evaluate(fixture())
        self.assertEqual(rules, {
            'sg-0123456789abcdef1:30080': {'security_group_id': 'sg-0123456789abcdef1', 'port': 30080},
            'sg-0123456789abcdef2:30081': {'security_group_id': 'sg-0123456789abcdef2', 'port': 30081}})
        self.assertIsNone(evaluate(fixture(), 'var.certificate_arn'))
        self.assertFalse(evaluate(fixture(), 'var.http_redirect'))

    def test_reused_node_and_duplicate_sg_port_do_not_duplicate_network_rules(self):
        values = fixture()
        alias = copy.deepcopy(values['routes']['aws-demo-a1b2'])
        alias.update(host='aws-alias-e5f6.example.com', priority=300)
        values['routes']['aws-alias-e5f6'] = alias
        self.assertEqual(len(evaluate(values)), 2)

    def test_external_dns_preserves_existing_alb_route_keys(self):
        values = fixture()
        self.assertEqual(set(evaluate(values, 'keys(local.dns_routes)')), set(values['routes']))
        before = evaluate(values, 'keys(var.routes)')
        values['routes']['aws-other-c3d4']['manage_dns'] = False
        self.assertEqual(evaluate(values, 'keys(local.dns_routes)'), ['aws-demo-a1b2'])
        self.assertEqual(evaluate(values, 'keys(var.routes)'), before)
        source = (MODULE / 'main.tf').read_text()
        for resource in ('aws_lb_target_group', 'aws_lb_target_group_attachment', 'aws_lb_listener_rule'):
            block = source.split('resource "' + resource + '" "app" {', 1)[1].split('\n}', 1)[0]
            self.assertRegex(block, r'for_each\s*= local.active_routes')
        dns = source.split('resource "aws_route53_record" "app" {', 1)[1].split('\n}', 1)[0]
        self.assertIn('for_each = local.dns_routes', dns)

    def test_existing_proxy_egress_is_optional_and_restricted_to_private_nodeport(self):
        values = fixture()
        self.assertIsNone(evaluate(values, 'var.openstack_proxy_egress'))
        values['openstack_proxy_egress'] = {'target_private_ip': '172.31.0.10', 'node_port': 31081}
        self.assertEqual(evaluate(values, 'var.openstack_proxy_egress'), values['openstack_proxy_egress'])
        for key, invalid in (('target_private_ip', '8.8.8.8'), ('target_private_ip', '169.254.169.254'),
                             ('target_private_ip', '100.64.0.1'), ('target_private_ip', '10.999.0.1'),
                             ('node_port', 443), ('node_port', 32768), ('node_port', 31081.5)):
            rejected = copy.deepcopy(values)
            rejected['openstack_proxy_egress'][key] = invalid
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                evaluate(rejected, 'var.openstack_proxy_egress')

    def test_shared_service_egress_is_explicit_and_independent_of_app_routes(self):
        values = fixture()
        self.assertEqual(evaluate(values, 'var.shared_service_egress'), {})
        rules = {'observability': {'target_private_ip': '172.31.2.249', 'port': 3000,
                                   'description': 'shared-observer'}}
        values['shared_service_egress'] = rules
        values['routes'] = {}
        self.assertEqual(evaluate(values, '{rules=var.shared_service_egress, apps=local.active_routes}'),
                         {'rules': rules, 'apps': {}})
        for key, invalid in (('target_private_ip', '8.8.8.8'), ('target_private_ip', '169.254.169.254'),
                             ('target_private_ip', '172.31.2.249/24'), ('target_private_ip', '10.999.0.1'),
                             ('port', 0), ('port', 65536), ('port', 3.5),
                             ('description', ''), ('description', 'bad\nline')):
            rejected = copy.deepcopy(values)
            rejected['shared_service_egress']['observability'][key] = invalid
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                evaluate(rejected, 'var.shared_service_egress')

    def test_app_relay_and_skyline_egress_coexist_without_new_routes(self):
        values = fixture()
        self.assertIsNone(evaluate(values, 'var.openstack_app_egress'))
        routes = evaluate(values, 'keys(var.routes)')
        values['openstack_proxy_egress'] = {'target_private_ip': '172.31.0.10', 'node_port': 31081}
        values['openstack_app_egress'] = {'target_private_ip': '172.31.0.10', 'port': 13200,
                                        'description': 'existing-owner app relay'}
        expression = '{skyline=var.openstack_proxy_egress, app=var.openstack_app_egress, routes=keys(var.routes)}'
        self.assertEqual(evaluate(values, expression), {'skyline': values['openstack_proxy_egress'],
                         'app': values['openstack_app_egress'], 'routes': routes})
        for key, invalid in (('target_private_ip', '8.8.8.8'), ('target_private_ip', '169.254.169.254'),
                             ('port', 0), ('port', 65536), ('port', 13200.5),
                             ('description', ''), ('description', 'wrong\nline')):
            rejected = copy.deepcopy(values)
            rejected['openstack_app_egress'][key] = invalid
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                evaluate(rejected, 'var.openstack_app_egress')

    def test_legacy_wireguard_inputs_fail_instead_of_being_ignored(self):
        for key, value in {'wireguard_network_interface_id': 'eni-0123456789abcdef0',
                           'wireguard_security_group_id': 'sg-0123456789abcdef0',
                           'wireguard_peer_cidrs': ['192.0.2.20/32'],
                           'wireguard_route_table_ids': ['rtb-0123456789abcdef0']}.items():
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'WireGuard is retired'):
                evaluate({**fixture(), key: value})

    def test_zone_can_be_created_before_delegation_or_reused_by_id(self):
        values = fixture()
        self.assertEqual(evaluate(values, 'var.zone_id'), 'ZEXAMPLE')
        del values['zone_id']
        self.assertIsNone(evaluate(values, 'var.zone_id'))
        for invalid in ('', 'example.com', '/hostedzone/ZEXAMPLE'):
            values['zone_id'] = invalid
            with self.subTest(zone_id=invalid), self.assertRaises(ValueError):
                evaluate(values)

    def test_public_metadata_cgnat_ipv6_and_invalid_routes_fail_closed(self):
        changes = [('target_private_ip', ip) for ip in ('8.8.8.8', '169.254.169.254', '100.64.0.1', '::1', '10.999.0.1')]
        changes += [('node_port', 80), ('node_port', 32768), ('priority', 0), ('priority', 1.5),
                    ('health_path', '/ok\ninjected'), ('host', '*.example.com'), ('provider_kind', 'azure'), ('provider_kind', 'gcp')]
        for field, value in changes:
            values = fixture()
            values['routes']['aws-other-c3d4'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                evaluate(values)

    def test_host_priority_and_sg_ownership_validation(self):
        for mutation in ('host', 'priority', 'aws-sg'):
            values = fixture()
            if mutation == 'host': values['routes']['aws-other-c3d4']['host'] = values['routes']['aws-demo-a1b2']['host']
            elif mutation == 'priority': values['routes']['aws-other-c3d4']['priority'] = 100
            elif mutation == 'aws-sg': del values['routes']['aws-demo-a1b2']['target_security_group_id']
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                evaluate(values)


if __name__ == '__main__':
    unittest.main(verbosity=2)
