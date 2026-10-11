"""Real publication preparation with native edge and authoritative DNS boundaries stubbed."""
from contextlib import contextmanager, redirect_stdout
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import application_routes as routes
import applications
import environment as runtime
import gcp_routes
import test_application_release as release_fixtures
import test_applications as registration_fixtures


class ApplicationRoutesTest(unittest.TestCase):
    @contextmanager
    def fixture(self, provider='aws', ingress_patch=None):
        original = registration_fixtures.ApplicationsTest.setUp

        def setup_registration(case):
            original(case)
            profile = case.config['environments'][case.env_id]
            profile['provider'] = provider
            profile['ingress'].update(edge_config_file=str(case.root / 'edge.json'),
                dns_config_file=str(case.root / 'dns.json'), expires_at='2099-01-01T00:00:00Z')
            profile['ingress'].update(ingress_patch or {})
            if provider == 'aws':
                case.native.side_effect = None
                case.native.return_value = json.dumps({'Reservations': [{'Instances': [{
                    'InstanceId': 'i-0123456789abcdef0',
                    'PrivateIpAddress': case.fixture.descriptor['addresses']['private'],
                    'SecurityGroups': [{'GroupId': 'sg-12345678'}],
                }]}]})
            case.fixture.write('dns.json', {'version': 1, 'base_domain': 'railshot.io', 'zone_id': 'a' * 32,
                'token_file': str(case.root / 'unused-token'), 'state_dir': str(case.root / 'dns-state')})
            if provider == 'gcp':
                descriptor = json.loads((ROOT / 'examples/ansible/gcp-node-descriptor.json').read_text())
                descriptor['target_id'] = case.env_id
                case.fixture.write('descriptor.json', descriptor)
            elif provider == 'openstack':
                case.fixture.use_openstack()
                case.fixture.write('registry.json', case.fixture.registry)
                profile['ingress'].setdefault('tunnel_config_file', str(case.root / 'tunnel.json'))
                case.fixture.write('edge.json', {'version': 1, 'provider': 'openstack', 'base_domain': 'railshot.io',
                    'runtime_private_address': '10.26.1.5', 'controller': {}, 'proxy': {}})
                case.fixture.write('tunnel.json', {'version': 1, 'state_dir': str(case.root / 'tunnel-state'),
                    'registry_file': case.config['registry_file'], 'environment_id': case.env_id,
                    'resource_id': 'server-1', 'runtime_private_address': '10.26.1.5', 'base_domain': 'railshot.io',
                    'namespace': 'railshot-edge', 'name': 'railshot-tunnel',
                    'tunnel_id': '11111111-2222-4333-8444-555555555555', 'credentials_secret': 'tunnel-credentials',
                    'origin_vip': '10.26.1.51', 'ca_configmap': 'origin-ca',
                    'configmap_uid': '22222222-2222-4333-8444-555555555555',
                    'deployment_uid': '33333333-2222-4333-8444-555555555555'})
            if provider != 'aws':
                private = descriptor['addresses']['private'] if provider == 'gcp' else '10.26.1.5'
                endpoint = 'https://' + private + ':6443'
                cm = case.fixture.control.objects['argocd', 'configmap', 'railshot-credentials']
                policy = json.loads(cm['data']['policy.json'])
                policy['targets'][0]['server'] = endpoint
                cm['data']['policy.json'] = json.dumps(policy)
                secret = case.fixture.control.objects['argocd', 'secret', 'railshot-' + case.env_id]
                secret['data']['server'] = base64.b64encode(endpoint.encode()).decode()
            case.write_config()

        case = release_fixtures.ApplicationReleaseTest(methodName='runTest')
        try:
            with patch.object(registration_fixtures.ApplicationsTest, 'setUp', setup_registration):
                case.setUp()
            case.edge.side_effect = None
            case.edge.return_value = {'reference': '/private/native/route.json',
                'hostname': case.registered['hostname'], 'node_port': case.registered['node_port']}
            case.native_ensure = case.enterContext(patch.object(routes.edge, 'ensure'))
            case.native_load = case.enterContext(patch.object(routes.edge, 'load', return_value=(
                {'config': 'native-edge'}, {'phase': 'applied', 'plan_sha256': 'd' * 64, 'route_key': 'allocated-route'})))
            case.terraform = case.enterContext(patch.object(routes.edge, 'terraform', return_value=json.dumps(
                {'alb_dns_name': {'value': 'railshot-123.ap-northeast-2.elb.amazonaws.com.'}})))
            case.registration.native.side_effect = None
            case.registration.native.return_value = json.dumps({'Reservations': [{'Instances': [{
                'InstanceId': 'i-0123456789abcdef0',
                'PrivateIpAddress': case.registration.fixture.descriptor['addresses']['private'],
                'SecurityGroups': [{'GroupId': 'sg-12345678'}],
            }]}]})
            case.gcp_result = {'hostname': case.registered['hostname'], 'frontend_ip': '34.100.10.20',
                'backend_service': 'railshot-calculator', 'dns_authorization_record': {
                    'name': '_acme-challenge_123.' + case.registered['hostname'] + '.', 'type': 'CNAME',
                    'data': 'token.project.authorize.certificatemanager.goog.'}}
            case.gcp = case.enterContext(patch.object(gcp_routes, 'ensure', return_value=case.gcp_result))
            case.order = []
            case.openstack_result = {'status': 'configured', 'https_verified': False,
                'application_id': case.registered['application_id'], 'hostname': case.registered['hostname'],
                'private_address': '10.26.1.5', 'node_port': case.registered['node_port'], 'health_path': '/alive',
                'resources': {name: '12345678-1234-1234-1234-123456789abc' for name in ('pool', 'member', 'monitor', 'policy', 'rule')},
                'network_rule_id': '12345678-1234-1234-1234-123456789abd'}
            case.openstack = case.enterContext(patch.object(routes.openstack_routes, 'ensure',
                side_effect=lambda *_: case.order.append('octavia') or case.openstack_result))
            tunnel_id = '11111111-2222-4333-8444-555555555555'
            case.connector_result = {'status': 'succeeded', 'phase': 'tunnel_configured', 'https_verified': False,
                'hostname': case.registered['hostname'], 'tunnel_id': tunnel_id,
                'dns': {'type': 'CNAME', 'content': tunnel_id + '.cfargotunnel.com', 'proxied': True}}
            case.connector = case.enterContext(patch.object(routes.tunnel, 'ensure',
                side_effect=lambda *_: case.order.append('tunnel') or case.connector_result))

            def dns_boundary(config_path, request):
                case.order.append('dns')
                # Keep the real hostname/zone/certificate ownership validator inside the boundary.
                routes.dns.request_at(routes.dns.config_at(config_path), request)
                return {'record_id': 'f' * 32}

            case.dns = case.enterContext(patch.object(routes.dns, 'ensure', side_effect=dns_boundary))
            yield case
        finally:
            case.doCleanups()

    def test_delete_fence_between_publication_and_route_prevents_external_write(self):
        with self.fixture() as case:
            prepared = routes.application_release.finalize(case.config_path, case.request)
            home = Path(case.registration.config['state_dir']) / prepared['application_id']
            runtime.save(home / 'lifecycle.json', {'status': 'deleting'})
            with patch.object(routes.application_release, 'finalize', return_value=prepared):
                with self.assertRaisesRegex(ValueError, 'APPLICATION_LIFECYCLE_BLOCKED'):
                    routes.ensure(case.config_path, case.request)
            case.native_ensure.assert_not_called(); case.dns.assert_not_called()

    def assert_configured_only(self, case, result):
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['phase'], 'route_configured')
        self.assertEqual(result['public_route_state'], 'configured')
        self.assertIs(result['https_verified'], False)
        self.assertEqual(result['application_id'], case.registered['application_id'])
        self.assertEqual(result['target_id'], case.registered['target_id'])
        self.assertEqual(result['environment_id'], case.registered['environment_id'])
        self.assertFalse({'url', 'deployed', 'public_http'} & result.keys())
        saved = case.prepared_dir / 'route-result.json'
        self.assertEqual(json.loads(saved.read_bytes()), result)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result['route_request_sha256'], hashlib.sha256(
            (case.prepared_dir / 'route-request.json').read_bytes()).hexdigest())
        case.probe.assert_not_called()

    def test_aws_publication_binds_allocated_route_and_dns_without_claiming_https(self):
        with self.fixture() as case:
            result = routes.ensure(case.config_path, case.request)
            self.assert_configured_only(case, result)
            ingress = case.registration.config['environments'][case.registered['environment_id']]['ingress']
            case.edge.assert_called_once_with(ingress['edge_config_file'], {
                'target_id': case.registered['application_id'], 'tenant': 'demo', 'app': case.registered['app'],
                'environment_id': case.registered['environment_id'], 'provider_kind': 'aws',
                'target_private_ip': case.registration.fixture.descriptor['addresses']['private'],
                'namespace': case.registered['namespace'], 'health_path': '/alive', 'expected_status': 200,
                'node_port': case.registered['node_port'], 'manage_dns': False,
                'target_security_group_id': 'sg-12345678', 'expires_at': ingress['expires_at'],
            })
            case.native_ensure.assert_called_once_with('/private/native/route.json')
            case.terraform.assert_called_once_with({'config': 'native-edge'}, 'output', '-json')
            case.dns.assert_called_once_with(ingress['dns_config_file'], {
                'application_id': case.registered['application_id'], 'hostname': case.registered['hostname'],
                'type': 'CNAME', 'content': 'railshot-123.ap-northeast-2.elb.amazonaws.com',
            })
            request = runtime.read_private(case.prepared_dir / 'route-request.json')
            self.assertEqual(request['source_commit'], case.request['publication']['source_commit'])
            self.assertEqual((request['port'], request['route'], request['health_path']), (7070, '/application', '/alive'))
            self.assertEqual(request['images'], case.request['publication']['images'])
            self.assertEqual(result['edge'], {'provider': 'aws', 'plan_sha256': 'd' * 64, 'route_key': 'allocated-route'})
            case.gcp.assert_not_called()

    def test_gcp_publication_registers_exact_certificate_cname_then_application_a(self):
        with self.fixture('gcp') as case:
            result = routes.ensure(case.config_path, case.request)
            self.assert_configured_only(case, result)
            ingress = case.registration.config['environments'][case.registered['environment_id']]['ingress']
            case.gcp.assert_called_once_with(ingress['edge_config_file'], {
                'application_id': case.registered['application_id'], 'hostname': case.registered['hostname'],
                'node_port': case.registered['node_port'], 'health_path': '/alive',
            }, dns_config_path=ingress['dns_config_file'])
            self.assertEqual([call.args for call in case.dns.call_args_list], [
                (ingress['dns_config_file'], {'application_id': case.registered['application_id'], 'purpose': 'certificate',
                    'application_hostname': case.registered['hostname'], 'hostname': '_acme-challenge_123.' + case.registered['hostname'],
                    'type': 'CNAME', 'content': 'token.project.authorize.certificatemanager.goog'}),
                (ingress['dns_config_file'], {'application_id': case.registered['application_id'],
                    'hostname': case.registered['hostname'], 'type': 'A', 'content': '34.100.10.20'}),
            ])
            case.edge.assert_not_called(); case.registration.native.assert_not_called()

    def test_gcp_shared_certificate_only_registers_application_dns(self):
        with self.fixture('gcp') as case:
            case.gcp_result['dns_authorization_record'] = None
            result = routes.ensure(case.config_path, case.request)
            self.assert_configured_only(case, result)
            self.assertEqual(case.dns.call_count, 1)
            self.assertEqual(case.dns.call_args.args[1], {
                'application_id': case.registered['application_id'],
                'hostname': case.registered['hostname'], 'type': 'A', 'content': '34.100.10.20'})

    def test_openstack_finalized_binding_runs_octavia_then_tunnel_then_proxied_dns(self):
        with self.fixture('openstack') as case:
            result = routes.ensure(case.config_path, case.request)
            self.assert_configured_only(case, result)
            self.assertIs(result['backend_verified'], False)
            self.assertEqual(case.order, ['octavia', 'tunnel', 'dns'])
            request = runtime.read_private(case.prepared_dir / 'route-request.json')
            ingress = request['ingress']
            case.openstack.assert_called_once_with(ingress['edge_config_file'], {
                'application_id': request['application_id'], 'hostname': request['hostname'],
                'private_address': request['resource']['private_address'], 'node_port': request['node_port'],
                'health_path': request['health_path']})
            case.connector.assert_called_once_with(ingress['tunnel_config_file'], {
                key: request[key] for key in ('environment_id', 'application_id', 'app', 'tenant', 'hostname')})
            case.dns.assert_called_once_with(ingress['dns_config_file'], {
                'application_id': request['application_id'], 'hostname': request['hostname'],
                'type': 'CNAME', 'content': '11111111-2222-4333-8444-555555555555.cfargotunnel.com', 'proxied': True})
            self.assertEqual(result['edge']['resources'], case.openstack_result['resources'])
            case.edge.assert_not_called(); case.gcp.assert_not_called()

    def test_openstack_operator_configs_cannot_retarget_registered_resource_or_address(self):
        for file, key, value in (
                ('tunnel.json', 'resource_id', 'other-resource'), ('tunnel.json', 'runtime_private_address', '10.26.1.99'),
                ('tunnel.json', 'environment_id', 'other-environment'), ('tunnel.json', 'base_domain', 'railshot.com'),
                ('edge.json', 'runtime_private_address', '10.26.1.99'), ('edge.json', 'base_domain', 'railshot.com')):
            with self.subTest(file=file, key=key), self.fixture('openstack') as case:
                config_file = case.registration.root / file
                config = runtime.read_private(config_file)
                runtime.save(config_file, {**config, key: value})
                with self.assertRaises(applications.RegistrationError) as raised:
                    routes.ensure(case.config_path, case.request)
                self.assertFalse(raised.exception.unknown)
                case.openstack.assert_not_called(); case.connector.assert_not_called(); case.dns.assert_not_called()

    def test_openstack_native_readback_and_tunnel_dns_must_match_before_dns(self):
        for boundary, key, value in (
                ('native', 'hostname', 'other.railshot.io'), ('native', 'node_port', 32123),
                ('native', 'private_address', '10.26.1.99'), ('native', 'https_verified', True),
                ('tunnel', 'hostname', 'other.railshot.io'), ('tunnel', 'https_verified', True),
                ('dns', 'type', 'A'), ('dns', 'content', 'other.cfargotunnel.com'), ('dns', 'proxied', False)):
            with self.subTest(boundary=boundary, key=key), self.fixture('openstack') as case:
                receipt = case.openstack_result if boundary == 'native' else case.connector_result
                if boundary == 'dns': receipt = receipt['dns']
                receipt[key] = value
                with self.assertRaises(applications.RegistrationError) as raised:
                    routes.ensure(case.config_path, case.request)
                self.assertTrue(raised.exception.unknown)
                case.dns.assert_not_called()
                if boundary == 'native': case.connector.assert_not_called()

    def test_foreign_certificate_suffix_or_challenge_hostname_never_registers_app_a(self):
        for field, value, error in (
                ('data', 'foreign.example.com.', 'APPLICATION_CERTIFICATE_DNS_INVALID'),
                ('data', 'token.authorize.certificatemanager.goog.attacker.com.', 'APPLICATION_CERTIFICATE_DNS_INVALID'),
                ('name', '_acme-challenge_123.other-app.railshot.io.', 'DNS_CERTIFICATE_HOSTNAME_INVALID')):
            with self.subTest(field=field, value=value), self.fixture('gcp') as case:
                case.gcp_result['dns_authorization_record'][field] = value
                with self.assertRaisesRegex((applications.RegistrationError, routes.dns.DNSError), error):
                    routes.ensure(case.config_path, case.request)
                self.assertFalse(any(call.args[1]['type'] == 'A' for call in case.dns.call_args_list))
                self.assertFalse((case.prepared_dir / 'route-result.json').exists())

    def test_native_route_identity_or_alb_suffix_mismatch_never_writes_dns(self):
        for provider, field in (('aws', 'hostname'), ('aws', 'node_port'), ('gcp', 'hostname'), ('aws', 'alb')):
            with self.subTest(provider=provider, field=field), self.fixture(provider) as case:
                if field == 'alb':
                    case.terraform.return_value = json.dumps({'alb_dns_name': {'value': 'foreign.example.com'}})
                elif provider == 'aws':
                    case.edge.return_value[field] = 'foreign.railshot.io' if field == 'hostname' else 12345
                else:
                    case.gcp_result['hostname'] = 'foreign.railshot.io'
                with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_ROUTE_BINDING_MISMATCH'):
                    routes.ensure(case.config_path, case.request)
                case.dns.assert_not_called()
                if provider == 'aws' and field != 'alb': case.native_ensure.assert_not_called()

    def test_changed_publication_or_prepared_request_is_rejected_before_native_calls(self):
        with self.fixture() as case:
            case.prepare()
            changed = copy.deepcopy(case.request)
            changed['publication']['source_commit'] = 'f' * 40
            with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_RELEASE_CONFLICT'):
                routes.ensure(case.config_path, changed)
            request = runtime.read_private(case.prepared_dir / 'route-request.json')
            runtime.save(case.prepared_dir / 'route-request.json', {**request, 'hostname': 'foreign.railshot.io'})
            with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_RELEASE_CHANGED'):
                routes.ensure(case.config_path, case.request)
            case.edge.assert_not_called(); case.gcp.assert_not_called(); case.dns.assert_not_called()

    def test_missing_native_or_tunnel_configuration_stop_before_mutation(self):
        for provider, ingress, error in (
                ('aws', {'edge_config_file': None}, 'APPLICATION_ROUTE_NOT_CONFIGURED'),
                ('gcp', {'dns_config_file': 'relative.json'}, 'APPLICATION_ROUTE_NOT_CONFIGURED'),
                ('openstack', {'tunnel_config_file': None}, 'APPLICATION_ROUTE_NOT_CONFIGURED'),
                ('openstack', {'tunnel_config_file': 'relative.json'}, 'APPLICATION_ROUTE_NOT_CONFIGURED')):
            with self.subTest(provider=provider), self.fixture(provider, ingress) as case:
                with self.assertRaisesRegex(applications.RegistrationError, error):
                    routes.ensure(case.config_path, case.request)
                case.edge.assert_not_called(); case.gcp.assert_not_called(); case.dns.assert_not_called()
                case.registration.native.assert_not_called()
                case.openstack.assert_not_called(); case.connector.assert_not_called()

    def test_dns_zone_must_match_registered_application_domain(self):
        with self.fixture() as case:
            config_path = case.registration.root / 'dns.json'
            config = runtime.read_private(config_path); config['base_domain'] = 'foreign.example'
            runtime.save(config_path, config)
            with self.assertRaisesRegex(applications.RegistrationError, 'APPLICATION_DNS_ZONE_MISMATCH'):
                routes.ensure(case.config_path, case.request)
            case.edge.assert_not_called(); case.dns.assert_not_called()

    def test_cli_keeps_started_native_work_unknown_and_preflight_failure_blocked(self):
        for failure, status, code in (
                ('applying', 'unknown', 'APPLICATION_ROUTE_RECONCILE_REQUIRED'),
                ('native-error', 'unknown', 'APPLICATION_ROUTE_RECONCILE_REQUIRED'),
                ('dns-unknown', 'unknown', 'DNS_API_FAILED'),
                ('config', 'blocked', 'APPLICATION_ROUTE_NOT_CONFIGURED')):
            ingress = {'edge_config_file': None} if failure == 'config' else None
            with self.subTest(failure=failure), self.fixture(ingress_patch=ingress) as case:
                if failure == 'applying':
                    case.native_load.return_value[1]['phase'] = 'applying'
                elif failure == 'native-error':
                    case.native_ensure.side_effect = OSError('private-provider-response')
                elif failure == 'dns-unknown':
                    case.dns.side_effect = routes.dns.DNSError('DNS_API_FAILED', 'UNKNOWN')
                path = case.registration.root / 'publication.json'
                runtime.save(path, case.request)
                output = io.StringIO()
                with patch.object(sys, 'argv', ['application_routes', '--config', str(case.config_path), '--request', str(path)]), \
                        patch.object(routes.os, 'umask'), redirect_stdout(output):
                    self.assertEqual(routes.main(), 3)
                result = json.loads(output.getvalue())
                self.assertEqual(result, {'status': status, 'public_route_state': 'unverified',
                    'error': {'code': code, 'retryable': False, 'outcome_unknown': status == 'unknown'}})
                self.assertNotIn('private-provider-response', output.getvalue())
                self.assertFalse((case.prepared_dir / 'route-result.json').exists())
                if failure != 'dns-unknown': case.dns.assert_not_called()

    def test_cli_preserves_worker_unknown_and_tunnel_outcome_without_diagnostics(self):
        for boundary in ('worker', 'tunnel'):
            for unknown in ((True,) if boundary == 'worker' else (False, True)):
                with self.subTest(boundary=boundary, unknown=unknown), self.fixture('openstack') as case:
                    if boundary == 'worker':
                        case.openstack.side_effect = routes.openstack_routes.RouteError(unknown=unknown)
                    else:
                        case.connector_result.clear()
                        case.connector_result.update(status='unknown' if unknown else 'blocked',
                            error={'code': 'private-remote-diagnostic', 'outcome_unknown': unknown})
                    path = case.registration.root / 'publication.json'
                    runtime.save(path, case.request)
                    output = io.StringIO()
                    with patch.object(sys, 'argv', ['application_routes', '--config', str(case.config_path), '--request', str(path)]), \
                            patch.object(routes.os, 'umask'), redirect_stdout(output):
                        self.assertEqual(routes.main(), 3)
                    result = json.loads(output.getvalue())
                    self.assertEqual(result['status'], 'unknown' if unknown else 'blocked')
                    self.assertIs(result['error']['outcome_unknown'], unknown)
                    self.assertNotIn('private-remote', output.getvalue())
                    case.dns.assert_not_called()
                    if boundary == 'worker': case.connector.assert_not_called()
                    self.assertFalse((case.prepared_dir / 'route-result.json').exists())


if __name__ == '__main__':
    unittest.main()
