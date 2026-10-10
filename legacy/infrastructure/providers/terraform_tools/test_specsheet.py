"""Run: python3 -m unittest discover -s platform/infra -p test_specsheet.py"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from specsheet import build, markdown


class SpecsheetTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        self.gcp = {
            'schema_version': 'v1', 'provider_kind': 'gcp', 'target_id': 'gcp-test',
            'resource_id': 'projects/test/zones/zone/instances/node', 'instance_id': '123',
            'execution_driver': 'terraform', 'owner_ref': 'terraform:test',
            'location': {'project_id': 'test-project', 'region': 'asia-northeast3', 'zone': 'asia-northeast3-a'},
            'compute': {'machine_type': 'e2-standard-4', 'source': 'configured'}, 'architecture': 'x86_64',
            'addresses': {'private': '10.0.0.2', 'public': '192.0.2.1'},
            'data_disk': {'resource_id': 'disk-1', 'size_gib': 100, 'mount_path': '/var/lib/rancher', 'preservation': 'retain'},
            'bootstrap': {'status': 'unverified', 'revision': 'a' * 40},
            'gitops': {'repo': 'https://github.com/example/gitops', 'path': 'clusters/gcp/platform', 'revision': 'main'},
            'runtime_limit': {'seconds': 7200, 'instance_termination_action': 'STOP', 'automatic_restart': False},
        }
        self.cost = {'provider': 'gcp', 'scope': 'gcp/test/2026-10', 'source_scope': 'test-project',
                     'scope_binding': 'row_verified', 'period': '2026-10', 'status': 'reported', 'basis': 'reported_actual',
                     'observed_at': self.now.isoformat(), 'totals': {'USD': '1.25', 'KRW': '1000'},
                     'reserved_estimate': {'USD': '0.50'}}

    def test_gcp_configuration_never_invents_capacity_or_price(self):
        sheet = build(self.gcp, now=self.now)
        self.assertEqual(sheet['compute']['machine_type'], 'e2-standard-4')
        self.assertIsNone(sheet['compute']['vcpu'])
        self.assertIsNone(sheet['compute']['memory_mib'])
        self.assertEqual(sheet['cost']['status'], 'unknown')
        self.assertIsNone(sheet['cost']['list_price_estimate'])
        self.assertEqual(sheet['runtime_limit']['seconds'], 7200)
        self.assertEqual(sheet['live_verification'], 'unverified')
        self.assertEqual(sheet['capabilities']['node_scale_out']['implementation'], 'not_implemented')
        self.assertIn('| vCPU | unknown |', markdown(sheet))

    def test_aws_guest_timer_does_not_claim_provider_restart_configuration(self):
        descriptor = deepcopy(self.gcp)
        descriptor['provider_kind'] = 'aws'
        descriptor['runtime_limit'] = {'seconds': 7200, 'instance_termination_action': 'STOP',
                                       'implementation': 'guest_systemd_timer', 'readiness': 'unverified'}
        sheet = build(descriptor, now=self.now)
        self.assertEqual(sheet['runtime_limit']['action'], 'STOP')
        self.assertEqual(sheet['runtime_limit']['implementation'], 'guest_systemd_timer')
        self.assertIsNone(sheet['runtime_limit']['automatic_restart'])
        self.assertEqual(sheet['runtime_limit']['verification'], 'unverified')

    def test_host_only_descriptor_has_no_runtime_or_gitops_readiness(self):
        descriptor = deepcopy(self.gcp)
        descriptor['runtime'] = {'configuration_status': 'not_configured', 'readiness': 'not_configured'}
        descriptor['bootstrap'] = {'profile': 'ubuntu-host-v1', 'revision': None, 'status': 'unverified'}
        descriptor['gitops'] = {'repo': None, 'path': None, 'revision': None}
        sheet = build(descriptor, now=self.now)
        self.assertEqual(sheet['bootstrap']['runtime_verification'], 'not_configured')
        self.assertEqual(sheet['gitops'], {'repo': None, 'path': None, 'revision': None})
        self.assertIn('| Runtime readiness | not_configured |', markdown(sheet))
        descriptor['runtime']['readiness'] = 'ready'
        self.assertEqual(build(descriptor, now=self.now)['bootstrap']['runtime_verification'], 'unverified')
        self.assertEqual(sheet['live_verification'], 'unverified')

    def test_azure_current_descriptor_aliases_and_detached_data(self):
        azure = {'schema_version': 'v1', 'provider_kind': 'azure', 'target_id': 'azure-test',
                 'resource_id': 'node', 'instance_id': None, 'execution_driver': 'terraform',
                 'owner_ref': 'terraform:azure:test', 'region': 'koreacentral', 'architecture': 'amd64',
                 'addresses': {'private_ipv4': '10.0.0.4', 'public_ipv4': '192.0.2.2'},
                 'storage': {'data_disk_id': 'retained-disk', 'size_gib': 100, 'mount_path': '/var/lib/rancher', 'retention': 'retain'},
                 'bootstrap': {'readiness': 'unverified', 'source_revision': 'b' * 40},
                 'gitops': {'repository': 'https://github.com/example/gitops', 'path': 'clusters/azure/platform', 'revision': 'main'}}
        sheet = build({'node_descriptor': {'value': azure, 'sensitive': False}}, now=self.now)
        self.assertEqual(sheet['data_disk']['resource_id'], 'retained-disk')
        self.assertEqual(sheet['addresses']['private'], '10.0.0.4')
        self.assertEqual(sheet['compute']['architecture'], 'x86_64')
        self.assertIsNone(sheet['instance_id'])
        self.assertIsNone(sheet['runtime_limit'])

    def test_costs_are_explicit_scope_totals_and_staleness_is_recomputed(self):
        sheet = build(self.gcp, cost_report=self.cost, cost_scope=self.cost['scope'], now=self.now)
        self.assertEqual(sheet['cost']['status'], 'reported')
        self.assertEqual(sheet['cost']['totals'], {'USD': '1.25', 'KRW': '1000'})
        self.assertIsNone(sheet['cost']['node_attributed_cost'])
        old = build(self.gcp, cost_report=self.cost, cost_scope=self.cost['scope'], now=self.now + timedelta(days=2))
        self.assertEqual(old['cost']['status'], 'stale')

    def test_unbound_mismatched_and_empty_costs_remain_unknown(self):
        reports = [self.cost, {**self.cost, 'source_scope': 'other-project'},
                   {**self.cost, 'totals': {}}, {**self.cost, 'scope_binding': 'operator_asserted'},
                   {**self.cost, 'basis': 'estimate'}]
        for index, report in enumerate(reports):
            with self.subTest(index=index):
                sheet = build(self.gcp, cost_report=report, cost_scope=None if index == 0 else self.cost['scope'], now=self.now)
                self.assertEqual(sheet['cost']['status'], 'unknown')

    def test_unrecognized_fields_are_not_copied_and_invalid_money_fails(self):
        descriptor = deepcopy(self.gcp)
        descriptor['private_credential'] = 'must-not-copy'
        self.assertNotIn('must-not-copy', json.dumps(build(descriptor, now=self.now)))
        with self.assertRaises(ValueError):
            build(self.gcp, cost_report={**self.cost, 'totals': {'USD': 'NaN'}}, cost_scope=self.cost['scope'], now=self.now)

    def test_cli_writes_markdown_from_node_descriptor_json(self):
        with tempfile.TemporaryDirectory() as directory:
            descriptor, output = Path(directory) / 'node.json', Path(directory) / 'spec.md'
            descriptor.write_text(json.dumps(self.gcp))
            subprocess.run(['python3', str(Path(__file__).with_name('specsheet.py')), str(descriptor),
                            '--format', 'markdown', '--output', str(output)], check=True)
            self.assertIn('Billing status: **unknown**', output.read_text())

    def test_aws_descriptor_works_without_inventing_aws_billing_support(self):
        aws = deepcopy(self.gcp)
        aws.update(provider_kind='aws', target_id='aws-test', instance_id='i-offline',
                   location={'account_id': '000000000000', 'region': 'ap-northeast-2'}, runtime_limit=None)
        sheet = build(aws, now=self.now)
        self.assertEqual(sheet['provider_kind'], 'aws')
        self.assertEqual(sheet['location']['account_id'], '000000000000')
        self.assertEqual(sheet['cost']['status'], 'unknown')
        self.assertIsNone(sheet['compute']['vcpu'])

    def test_azure_billing_subscription_must_match_descriptor(self):
        azure = deepcopy(self.gcp)
        azure.update(provider_kind='azure', location={'subscription_id': 'subscription-one', 'region': 'koreacentral'})
        cost = {**self.cost, 'provider': 'azure', 'source_scope': 'subscription-two'}
        sheet = build(azure, cost_report=cost, cost_scope=cost['scope'], now=self.now)
        self.assertEqual(sheet['cost']['status'], 'unknown')
        cost['source_scope'] = 'subscription-one'
        self.assertEqual(build(azure, cost_report=cost, cost_scope=cost['scope'], now=self.now)['cost']['status'], 'reported')


if __name__ == '__main__':
    unittest.main()
