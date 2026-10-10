"""Offline policy-scope regressions, not a substitute for live AWS evaluation.

The evaluator intentionally supports only the operators present in this document;
new operators fail the tests instead of being silently treated as permissive.
"""
from fnmatch import fnmatchcase
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
POLICY = ROOT / 'infrastructure/terraform/control/product-edge-policy.json'
REGION = 'ap-northeast-2'
ACCOUNT = '721622471953'
ELB = f'arn:aws:elasticloadbalancing:{REGION}:{ACCOUNT}:'
EC2 = f'arn:aws:ec2:{REGION}:{ACCOUNT}:'
VPC = EC2 + 'vpc/vpc-085e5a8268cf2b206'
ZONE = 'arn:aws:route53:::hostedzone/Z01500273BKOZ113O8L34'
LISTENER = 'app/railshot-apps/4363b7c639aaab7e/a3dee055c140eb3d'


def values(value):
    return value if isinstance(value, list) else [value]


def decision(policy, action, resource, context):
    allowed = False
    for statement in policy['Statement']:
        if not any(fnmatchcase(action.lower(), pattern.lower()) for pattern in values(statement['Action'])):
            continue
        if not any(fnmatchcase(resource, pattern) for pattern in values(statement['Resource'])):
            continue
        matches = True
        for operator, conditions in statement.get('Condition', {}).items():
            if operator not in ('StringEquals', 'StringLike', 'ForAllValues:StringEquals', 'ForAllValues:StringLike', 'Null'):
                raise AssertionError('unsupported policy operator: ' + operator)
            for key, patterns in conditions.items():
                found = context.get(key)
                if operator == 'Null':
                    matched = (found is None or found == []) == (patterns == 'true')
                else:
                    match = fnmatchcase if operator.endswith('StringLike') else lambda a, b: a == b
                    if operator.startswith('ForAllValues:'):
                        matched = all(any(match(value, p) for p in values(patterns)) for value in ([] if found is None else values(found)))
                    else:
                        matched = found is not None and any(match(found, p) for p in values(patterns))
                matches = matches and matched
        if matches:
            if statement['Effect'] == 'Deny':
                return 'explicitDeny'
            allowed = True
    return 'allowed' if allowed else 'implicitDeny'


class ProductEdgePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads(POLICY.read_bytes())
        self.context = {'aws:RequestedRegion': REGION, 'ec2:Vpc': VPC}

    def assertDecision(self, expected, action, resource, context=None):
        self.assertEqual(decision(self.policy, action, resource, self.context if context is None else context), expected)

    def test_delete_rule_is_limited_to_children_of_the_exact_existing_listener(self):
        action = 'elasticloadbalancing:DeleteRule'
        self.assertDecision('allowed', action, ELB + 'listener-rule/' + LISTENER + '/new-app-rule')
        for resource in (ELB + 'listener/' + LISTENER, ELB + 'listener-rule/' + LISTENER.replace('a3dee055c140eb3d', 'other-listener') + '/rule',
                         ELB + 'listener-rule/' + LISTENER.replace('railshot-apps', 'other-lb') + '/rule',
                         (ELB + 'listener-rule/' + LISTENER + '/rule').replace(ACCOUNT, '123456789012')):
            with self.subTest(resource=resource):
                self.assertDecision('implicitDeny', action, resource)
        self.assertDecision('implicitDeny', action, ELB + 'listener-rule/' + LISTENER + '/rule', {'aws:RequestedRegion': 'us-east-1'})

    def test_target_lifecycle_keeps_rsapp_prefix_and_explicit_bootstrap_denials(self):
        bootstrap = next(s['Resource'] for s in self.policy['Statement'] if s['Sid'] == 'PreserveBootstrapTargets')
        self.assertEqual(len(bootstrap), 4)
        for action in ('DeleteTargetGroup', 'DeregisterTargets', 'RegisterTargets', 'ModifyTargetGroupAttributes'):
            action = 'elasticloadbalancing:' + action
            self.assertDecision('allowed', action, ELB + 'targetgroup/rsapp-new-application/1234')
            for resource in bootstrap:
                with self.subTest(action=action, bootstrap=resource):
                    self.assertDecision('explicitDeny', action, resource)
            for resource in (ELB + 'targetgroup/foreign-service/1234',
                             (ELB + 'targetgroup/rsapp-new/1234').replace(REGION, 'us-east-1'),
                             (ELB + 'targetgroup/rsapp-new/1234').replace(ACCOUNT, '123456789012')):
                self.assertDecision('implicitDeny', action, resource)

    def test_sg_revoke_scopes_existing_ids_or_owned_tagged_target_and_vpc(self):
        alb = EC2 + 'security-group/sg-0716e6b46f1a845f4'
        self.assertDecision('allowed', 'ec2:RevokeSecurityGroupEgress', alb)
        self.assertDecision('implicitDeny', 'ec2:RevokeSecurityGroupIngress', alb)
        for group in ('sg-02925a97753d8d3e9', 'sg-0ad2a18168c4eac2b'):
            arn = EC2 + 'security-group/' + group
            self.assertDecision('allowed', 'ec2:RevokeSecurityGroupIngress', arn)
            self.assertDecision('implicitDeny', 'ec2:RevokeSecurityGroupEgress', arn)
            self.assertDecision('implicitDeny', 'ec2:RevokeSecurityGroupIngress', arn, {**self.context, 'ec2:Vpc': 'foreign-vpc'})
        owned = EC2 + 'security-group/sg-0123456789abcdef0'
        context = {**self.context, 'ec2:ResourceTag/ProjectOwner': 'railshot-product', 'ec2:ResourceTag/Target': 'registered-runtime'}
        self.assertDecision('allowed', 'ec2:RevokeSecurityGroupIngress', owned, context)
        self.assertDecision('implicitDeny', 'ec2:RevokeSecurityGroupEgress', owned, context)
        for key, wrong in (('ec2:Vpc', 'foreign-vpc'), ('ec2:ResourceTag/ProjectOwner', 'other-owner'),
                           ('ec2:ResourceTag/Target', ''), ('aws:RequestedRegion', 'us-east-1')):
            self.assertDecision('implicitDeny', 'ec2:RevokeSecurityGroupIngress', owned, {**context, key: wrong})
            self.assertDecision('implicitDeny', 'ec2:RevokeSecurityGroupIngress', owned, {k: v for k, v in context.items() if k != key})

    def test_dns_create_delete_only_owned_name_type_and_complete_batch(self):
        action = 'route53:ChangeResourceRecordSets'
        context = {'route53:ChangeResourceRecordSetsActions': ['DELETE'],
                   'route53:ChangeResourceRecordSetsRecordTypes': ['A'],
                   'route53:ChangeResourceRecordSetsNormalizedRecordNames': ['calculator-012345abcdef.railshot.io']}
        self.assertDecision('allowed', action, ZONE, context)
        self.assertDecision('allowed', action, ZONE, {**context, 'route53:ChangeResourceRecordSetsActions': ['CREATE', 'DELETE']})
        for key, wrong in (('route53:ChangeResourceRecordSetsActions', ['DELETE', 'UPSERT']),
                           ('route53:ChangeResourceRecordSetsRecordTypes', ['A', 'AAAA']),
                           ('route53:ChangeResourceRecordSetsNormalizedRecordNames', ['calculator-012345abcdef.railshot.io', 'railshot.io']),
                           ('route53:ChangeResourceRecordSetsNormalizedRecordNames', ['calculator-012345abcdef.foreign.io']),
                           ('route53:ChangeResourceRecordSetsNormalizedRecordNames', ['calculator-012345abcde.railshot.io'])):
            with self.subTest(key=key, wrong=wrong):
                self.assertDecision('implicitDeny', action, ZONE, {**context, key: wrong})
        for key in context:
            self.assertDecision('implicitDeny', action, ZONE, {k: v for k, v in context.items() if k != key})
        self.assertDecision('implicitDeny', action, ZONE + 'FOREIGN', context)

    def test_policy_grants_no_shared_infrastructure_delete_or_arbitrary_writes(self):
        for action, resource in (
            ('elasticloadbalancing:DeleteLoadBalancer', ELB + 'loadbalancer/app/railshot-apps/4363b7c639aaab7e'),
            ('elasticloadbalancing:DeleteListener', ELB + 'listener/' + LISTENER),
            ('elasticloadbalancing:ModifyRule', ELB + 'listener-rule/' + LISTENER + '/rule'),
            ('ec2:TerminateInstances', EC2 + 'instance/i-example'), ('ec2:DeleteSecurityGroup', EC2 + 'security-group/sg-0716e6b46f1a845f4'),
            ('ec2:DeleteVpc', VPC), ('ec2:DeleteRoute', EC2 + 'route-table/rtb-0bc498243a6f73e04'),
            ('route53:DeleteHostedZone', ZONE), ('iam:CreatePolicyVersion', '*')):
            with self.subTest(action=action):
                self.assertDecision('implicitDeny', action, resource)
        for statement in self.policy['Statement']:
            if statement['Effect'] == 'Allow':
                self.assertTrue(all('*' not in action for action in values(statement['Action'])))
        self.assertLessEqual(len(json.dumps(self.policy, separators=(',', ':'))), 6144)
        source = (ROOT / 'infrastructure/terraform/control/main.tf').read_text()
        self.assertIn('policy      = file("${path.module}/product-edge-policy.json")', source)


if __name__ == '__main__':
    unittest.main()
