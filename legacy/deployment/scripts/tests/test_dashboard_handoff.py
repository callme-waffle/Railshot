"""Offline boundary checks: product payloads must never execute local deployment.

The complete input is operator-completed illustrative data. No production
Dashboard converter, SSH connection, image publication, or ready node is implied.
"""
import contextlib
import copy
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import jsonschema

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from input_adapter import InputError, parse_input
from models import DeploymentResult
from runtime import main

DATA = json.loads((SCRIPTS / 'tests/fixtures/dashboard-handoff.json').read_text())


class DashboardHandoffTests(unittest.TestCase):
    def test_product_and_provider_payloads_rejected_before_engine(self):
        for key in ('dashboard_app_fields', 'dashboard_plan_input',
                    'dashboard_environment_input', 'actions_dispatch_input', 'node_descriptor'):
            with self.subTest(layer=key), patch('sys.stdin', io.StringIO(json.dumps(DATA[key]))), \
                    patch('runtime.DeploymentEngine') as engine, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['deploy']), 1)
                result = json.loads(output.getvalue())
                self.assertEqual(result['error']['code'], 'INVALID_INPUT')
                self.assertEqual(result['error']['stage'], 'INPUT_ADAPTER')
                engine.assert_not_called()

    def test_operator_completed_input_matches_current_runtime_schema(self):
        request = DATA['resolved_runtime_input']
        schema = json.loads((SCRIPTS / 'schemas/input-v0.2.schema.json').read_text())
        jsonschema.Draft202012Validator(schema).validate(request)
        adapted = parse_input(request)
        self.assertEqual(adapted.context.provider, DATA['node_descriptor']['provider_kind'])
        self.assertEqual(adapted.spec.runtime.node_ip, DATA['node_descriptor']['addresses']['private'])
        self.assertEqual(adapted.spec.workload.health_path, '/healthz')
        self.assertEqual(adapted.spec.runtime.timeout_seconds, 600)
        self.assertNotIn('provider', asdict(adapted.spec))
        self.assertNotIn('node_host', asdict(adapted.spec))

    def test_cli_preserves_runtime_result_with_mocked_execution_only(self):
        # Stop at the execution boundary: mock readiness is not an HTTP/node test.
        with patch('sys.stdin', io.StringIO(json.dumps(DATA['resolved_runtime_input']))), \
                patch('runtime.DeploymentEngine') as engine, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            engine.return_value.execute.return_value = DeploymentResult(status='ready')
            self.assertEqual(main(['deploy', '--mode', 'online']), 0)
            result = json.loads(output.getvalue())
            schema = json.loads((SCRIPTS / 'schemas/output-v0.2.schema.json').read_text())
            jsonschema.Draft202012Validator(schema).validate(result)
            self.assertEqual(result['provider'], 'gcp')
            self.assertEqual(result['environment_id'], DATA['resolved_runtime_input']['environment_id'])
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(result['workload_status'], 'unknown')
            self.assertIsNone(result['endpoint'])

    def test_ansible_receipt_is_not_a_full_deployment_result(self):
        schema = json.loads((SCRIPTS / 'schemas/output-v0.2.schema.json').read_text())
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(DATA['ansible_bootstrap_receipt'])

    def test_ui_onprem_label_must_not_bypass_provider_contract(self):
        request = copy.deepcopy(DATA['resolved_runtime_input'])
        request['provider'] = 'on-prem'
        with self.assertRaisesRegex(InputError, 'provider'):
            parse_input(request)
        request['provider'] = 'openstack'
        self.assertEqual(parse_input(request).context.provider, 'openstack')

    def test_profile_controls_and_product_status_cannot_enter_runtime_spec(self):
        for field, value in (('profile_id', 'gcp-small'), ('node_count', 1),
                             ('runtime_ready', True), ('status', 'succeeded')):
            with self.subTest(field=field):
                request = copy.deepcopy(DATA['resolved_runtime_input'])
                request['runtime'][field] = value
                with self.assertRaises(InputError):
                    parse_input(request)


if __name__ == '__main__':
    unittest.main()
