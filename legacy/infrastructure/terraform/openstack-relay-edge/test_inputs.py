"""Offline ownership and input contracts for the imported relay and its control SG owner."""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parent
CONTROL = MODULE.parent / 'control/main.tf'


def evaluate(source, values, expression):
    with tempfile.TemporaryDirectory(prefix='railshot-relay-inputs-') as directory:
        work = Path(directory)
        (work / 'inputs.tf').write_text(source + '\nlocals {\n  evaluated = ' + expression + '\n}\n')
        (work / 'values.tfvars.json').write_text(json.dumps(values))
        result = subprocess.run(['terraform', 'console', '-no-color', '-var-file=values.tfvars.json'],
                                input='jsonencode(local.evaluated)\n', capture_output=True, text=True, cwd=work)
        if result.returncode or 'Error:' in result.stderr:
            raise ValueError(result.stderr)
        return json.loads(json.loads(result.stdout))


class RelayInputTests(unittest.TestCase):
    def test_module_owns_exactly_three_importable_resources_and_no_sg_or_attachment(self):
        source = (MODULE / 'main.tf').read_text()
        self.assertEqual(re.findall(r'resource\s+"([^\"]+)"\s+"([^\"]+)"', source), [
            ('aws_lb_target_group', 'app'), ('aws_lb_listener_rule', 'app'), ('aws_route53_record', 'app')])
        self.assertEqual(source.count('prevent_destroy = true'), 3)
        self.assertIn('version = "~> 6.66.0"', source)

    def test_relay_input_bounds(self):
        source = (MODULE / 'variables.tf').read_text()
        values = {'account_id': '123456789012', 'region': 'ap-northeast-2', 'vpc_id': 'vpc-' + 'a' * 17,
                  'target_group_name': 'existing-relay', 'backend_port': 13200, 'health_path': '/health',
                  'listener_arn': 'arn:aws:elasticloadbalancing:ap-northeast-2:123456789012:listener/app/existing/abc/def',
                  'priority': 49100, 'hostname': 'app.example.com', 'zone_id': 'ZEXAMPLE',
                  'alb_dns_name': 'existing.elb.amazonaws.com', 'alb_zone_id': 'ZALB',
                  'target_group_tags': {'owner': 'existing'}, 'listener_rule_tags': {'owner': 'existing'}}
        self.assertEqual(evaluate(source, values, 'var.backend_port'), 13200)
        for key, invalid in (('backend_port', 0), ('backend_port', 65536), ('priority', 50001),
                             ('priority', 1.5), ('hostname', '*.example.com'), ('health_path', '/bad\npath')):
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                evaluate(source, {**values, key: invalid}, 'var.backend_port')

    def test_control_inline_owner_keeps_four_build_peer_rules_and_app_rule(self):
        source = CONTROL.read_text()
        variables = 'variable "build_worker_security_group_id" {' + source.split(
            'variable "build_worker_security_group_id" {', 1)[1].split('resource "aws_security_group" "control"', 1)[0]
        group = source.split('resource "aws_security_group" "control" {', 1)[1]
        expression = 'concat(' + group.split('ingress = concat(', 1)[1].split('  dynamic "egress"', 1)[0].strip()
        values = {'build_worker_security_group_id': 'sg-' + 'b' * 17}
        before = evaluate(variables, values, expression)
        self.assertEqual(len(before), 4)
        values['openstack_app_ingress'] = {'source_security_group_id': 'sg-' + 'c' * 17, 'port': 13200,
                                         'description': 'existing-owner app backend'}
        after = evaluate(variables, values, expression)
        self.assertEqual(after[:4], before)
        self.assertEqual(after[-1], {'description': 'existing-owner app backend', 'protocol': 'tcp',
            'from_port': 13200, 'to_port': 13200, 'security_groups': ['sg-' + 'c' * 17],
            'cidr_blocks': [], 'ipv6_cidr_blocks': [], 'prefix_list_ids': [], 'self': False})
        values['openstack_app_ingress']['source_security_group_id'] = '0.0.0.0/0'
        with self.assertRaises(ValueError):
            evaluate(variables, values, expression)


if __name__ == '__main__':
    unittest.main(verbosity=2)
