import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dns
import gcp_routes as routes


def app(index=1):
    return {'application_id': 'app-' + str(index) * 24, 'hostname': f'app{index}.railshot.io',
            'node_port': 31000 + index, 'health_path': '/ready'}


def name(request, values):
    return values.get('name', 'railshot-gcp-edge') + '-' + hashlib.sha256(request['application_id'].encode()).hexdigest()[:16]


def matcher(request, values, redirect=False):
    return {'name': name(request, values), **({'default_url_redirect': [{
        'host_redirect': request['hostname'], 'https_redirect': True, 'strip_query': False,
        'redirect_response_code': 'MOVED_PERMANENTLY_DEFAULT', 'path_redirect': None}]} if redirect else {
            'default_service': 'projects/' + values['project_id'] + '/global/backendServices/' + name(request, values)})}


def plan_for(request, values):
    changes = []
    for kind in routes.resource_kinds(routes.application_route(request, values)):
        after = {'name': name(request, values), 'project': values['project_id']}
        if kind == 'google_compute_network_endpoint_group':
            after.update(default_port=request['node_port'], network_endpoint_type='GCE_VM_IP_PORT', zone=values['zone'])
        if kind == 'google_compute_network_endpoint':
            after.update(port=request['node_port'], instance=values['instance_name'], zone=values['zone'], ip_address=values['expected_private_ip'])
        if kind == 'google_compute_health_check':
            after['http_health_check'] = [{'port': request['node_port'], 'host': request['hostname'], 'request_path': request['health_path']}]
        if kind == 'google_compute_backend_service':
            after.update(load_balancing_scheme='EXTERNAL_MANAGED', protocol='HTTP')
        if kind == 'google_certificate_manager_dns_authorization':
            after.update(domain=request['hostname'], type='PER_PROJECT_RECORD')
        if kind == 'google_certificate_manager_certificate':
            after['managed'] = [{'domains': [request['hostname']]}]
        if kind == 'google_certificate_manager_certificate_map_entry':
            after.update(hostname=request['hostname'], map=values.get('name', 'railshot-gcp-edge'))
            if values.get('application_certificate'):
                after['certificates'] = [values['application_certificate']['id']]
        changes.append({'address': kind + '.routes[' + json.dumps(request['application_id']) + ']', 'mode': 'managed',
                        'change': {'actions': ['create'], 'before': None, 'after': after}})
    existing = [{'application_id': k, **v} for k, v in values.get('routes', {}).items()]
    for redirect in (False, True):
        hosts = [{'hosts': [r['hostname']], 'path_matcher': name(r, values)} for r in existing]
        matchers = [matcher(r, values, redirect) for r in existing]
        if not redirect:
            hosts += [{'hosts': [values['hostname']], 'path_matcher': 'app'}]
            matchers += [{'name': 'app', 'default_service': 'original-backend'}]
        before = {'id': 'original-redirect' if redirect else 'original-app', 'fingerprint': 'a',
                  'host_rule': hosts, 'path_matcher': matchers, 'default_url_redirect': [{'host_redirect': values['hostname']}]}
        after = copy.deepcopy(before)
        after['fingerprint'] = None
        after['host_rule'].append({'hosts': [request['hostname']], 'path_matcher': name(request, values)})
        after['path_matcher'].append(matcher(request, values, redirect))
        changes.append({'address': 'google_compute_url_map.' + ('redirect' if redirect else 'app'),
                        'change': {'actions': ['update'], 'before': before, 'after': after,
                                   'after_unknown': {'fingerprint': True}}})
    before = {'id': 'gfe', 'source_ranges': ['35.191.0.0/16', '130.211.0.0/22'],
              'target_service_accounts': ['original@project.iam.gserviceaccount.com'],
              'allow': [{'protocol': 'tcp', 'ports': [str(values.get('node_port', 30080)), *[str(r['node_port']) for r in existing]]}]}
    after = copy.deepcopy(before); after['allow'][0]['ports'].append(str(request['node_port']))
    changes.append({'address': 'google_compute_firewall.gfe', 'change': {'actions': ['update'], 'before': before, 'after': after}})
    candidate = {**values, 'routes': {**values.get('routes', {}), request['application_id']: routes.application_route(request, values)}}
    return {'resource_changes': changes, 'variables': {k: {'value': v} for k, v in candidate.items()},
            'configuration': {'provider_config': {'google': {'full_name': 'registry.terraform.io/hashicorp/google',
                                'expressions': {'project': {'references': ['var.project_id']}}}}}}


class GcpRoutesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.values = {'project_id': 'fixture-project', 'zone': 'asia-northeast3-a', 'instance_name': 'existing-vm',
                       'expected_private_ip': '10.1.0.2', 'hostname': 'original.railshot.io'}
        self.state = {'version': 4, 'serial': 1, 'lineage': 'lineage-one', 'resources': [],
                      'outputs': {'frontend_ip': {'value': '34.1.2.3'}}}
        for address in sorted(routes.UPDATES | {'google_compute_global_address.app'}):
            kind, resource_name = address.split('.')
            self.state['resources'].append({'mode': 'managed', 'type': kind, 'name': resource_name,
                                           'instances': [{'attributes': {'id': address + '-id'}}]})
        self.config = {'version': 1, 'provider': 'gcp', 'edge_kind': 'native', 'state_file': str(self.root / 'terraform.tfstate'),
                       'variables_file': str(self.root / 'vars.json'), 'state_dir': str(self.root / 'journal'),
                       'state_lineage': 'lineage-one', 'owned_resources': routes.owned(self.state), 'previous_source_sha': 'a' * 40}
        self.config_path = self.root / 'config.json'
        self.write(self.config_path, self.config); self.write(self.config['state_file'], self.state)
        self.write(self.config['variables_file'], self.values)
        self.calls = []
        self.interrupt = False
        self.interrupt_after = False
        self.drift = False
        self.plans = {}
        self.corrupt_output = False

    def write(self, path, data):
        routes.durable_write(path, routes.encoded(data))

    def native(self, argv, **kwargs):
        self.calls.append(argv)
        work = Path(argv[1].split('=', 1)[1]); command = argv[2]
        if command == 'init':
            backend = json.loads((work / 'backend.tf.json').read_text())
            self.assertEqual(backend['terraform']['backend']['local']['path'], self.config['state_file'])
            return ''
        candidate = routes.read_private(work / 'candidate.json')
        current = routes.read_private(self.config['variables_file'])
        before = routes.read_private(work / 'before.json')
        current = before['values']
        key = next(k for k in candidate['routes'] if k not in current.get('routes', {}))
        request = {'application_id': key, **{k: candidate['routes'][key][k] for k in ('hostname', 'node_port', 'health_path')}}
        if command == 'plan':
            self.assertEqual({k: v for k, v in candidate.items() if k != 'routes'}, {k: v for k, v in current.items() if k != 'routes'})
            self.assertEqual({k: candidate['routes'][k] for k in current.get('routes', {})}, current.get('routes', {}))
            target = Path(next(a[5:] for a in argv if a.startswith('-out=')))
            routes.durable_write(target, b'synthetic saved plan')
            self.plans[str(target)] = plan_for(request, current) if target.name == 'plan' else {
                'resource_changes': [{'address': 'google_compute_firewall.gfe', 'change': {'actions': ['update']}}] if self.drift else [],
                'output_changes': {}}
            return ''
        if command == 'show':
            return json.dumps(self.plans[argv[-1]])
        self.assertEqual(command, 'apply')
        self.assertEqual(kwargs.get('timeout'), 900)
        self.assertEqual(kwargs['env']['RAILSHOT_GCP_CERTIFICATE_DNS_CONFIG'], str(self.root / 'dns.json'))
        self.assertEqual(kwargs['env']['PYTHONPATH'], str(Path(routes.__file__).resolve().parent))
        self.assertEqual(routes.read_private(Path(self.config['state_dir']) / ('gcp-route-' + key + '.json'))['phase'], 'applying')
        if self.interrupt:
            raise RuntimeError('synthetic uncertain apply')
        state = routes.read_private(self.config['state_file'])
        for kind in routes.resource_kinds(candidate['routes'][key]):
            resource = next((r for r in state['resources'] if r['type'] == kind and r['name'] == 'routes'), None)
            if resource is None:
                resource = {'mode': 'managed', 'type': kind, 'name': 'routes', 'instances': []}; state['resources'].append(resource)
            resource['instances'].append({'index_key': key, 'attributes': {'id': kind + '/' + key}})
        state['serial'] += 1
        state['outputs']['application_routes'] = {'value': {k: {
            'hostname': v['hostname'], 'frontend_ip': '34.1.2.3', 'backend_service': name({'application_id': k}, current),
            'dns_authorization_record': None if v.get('certificate_id') else {'name': '_acme-challenge_abc.' + v['hostname'] + '.', 'type': 'CNAME',
                                         'data': 'abc.authorize.certificatemanager.goog.'}}
            for k, v in candidate['routes'].items()}}
        if self.corrupt_output:
            state['outputs']['application_routes']['value'][key]['hostname'] = 'foreign.railshot.io'
        self.write(self.config['state_file'], state)
        if self.interrupt_after:
            raise RuntimeError('synthetic lost apply reply')
        return ''

    def test_new_route_requires_dns_authority_before_any_cloud_mutation(self):
        with patch('gcp_routes.native') as native, self.assertRaisesRegex(routes.RouteError, 'GCP_CERTIFICATE_DNS_NOT_CONFIGURED'):
            routes.ensure(self.config_path, app())
        native.assert_not_called()

    def test_shared_certificate_skips_new_issuance_and_preserves_legacy_route(self):
        with patch('gcp_routes.native', side_effect=self.native):
            legacy = routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
            values = routes.read_private(self.config['variables_file'])
            values['application_certificate'] = {'domain': 'railshot.io',
                'id': 'projects/fixture-project/locations/global/certificates/shared-apps'}
            self.write(self.config['variables_file'], values)
            result = routes.ensure(self.config_path, app(2), dns_config_path=str(self.root / 'dns.json'))
            self.assertIsNone(result['dns_authorization_record'])
            self.assertEqual(routes.output_route(routes.read_private(self.config['state_file']), app(),
                routes.read_private(self.config['variables_file'])), legacy)
            owned = routes.owned(routes.read_private(self.config['state_file']))
            self.assertNotIn('google_certificate_manager_certificate.routes["' + app(2)['application_id'] + '"]', owned)
            import application_cleanup
            saved = routes.read_private(self.config['variables_file'])
            removed = application_cleanup.addresses('gcp', app(2)['application_id'], saved['routes'][app(2)['application_id']], saved, 'delete')
            self.assertEqual(len(removed), 5)
            self.assertFalse(any('certificate.routes' in key or 'dns_authorization.routes' in key for key in removed))
            wrong = copy.deepcopy(values)
            wrong['application_certificate']['domain'] = 'another.test'
            with self.assertRaises(ValueError):
                routes.application_route(app(2), wrong)

    def test_certificate_dns_failure_keeps_partial_route_unknown_and_specific_reason(self):
        def native(argv, **kwargs):
            if argv[2] == 'apply':
                work = Path(argv[1].split('=', 1)[1])
                self.write(work / 'certificate-dns-error.json', {'code': 'DNS_CERTIFICATE_PROPAGATION_PENDING'})
                raise ValueError('provisioner failed')
            return self.native(argv, **kwargs)
        with patch('gcp_routes.native', side_effect=native), self.assertRaises(routes.RouteError) as error:
            routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
        self.assertEqual(error.exception.code, 'GCP_CERTIFICATE_DNS_FAILED')
        self.assertTrue(error.exception.unknown)
        journal = routes.read_private(Path(self.config['state_dir']) / ('gcp-route-' + app()['application_id'] + '.json'))
        self.assertEqual(journal['phase'], 'unknown')
        self.assertEqual(journal['error']['code'], 'GCP_CERTIFICATE_DNS_FAILED')

    def test_certificate_hook_publishes_then_waits_for_matching_public_cname(self):
        request = {'application_id': app()['application_id'], 'purpose': 'certificate',
                   'application_hostname': app()['hostname'], 'hostname': '_acme-challenge.test.railshot.io',
                   'type': 'CNAME', 'content': 'token.authorize.certificatemanager.goog'}
        env = {'RAILSHOT_GCP_CERTIFICATE_DNS_CONFIG': str(self.root / 'dns.json'),
               'RAILSHOT_GCP_CERTIFICATE_DNS_REQUEST': json.dumps(request)}
        receipt = {'status': 'verified'}
        responses = [io.BytesIO(json.dumps({'Status': 0, 'Answer': [{'type': 5, 'name': request['hostname'] + '.',
                     'data': target + '.'}]}).encode()) for target in ('foreign.authorize.certificatemanager.goog', request['content'])]
        def resolve(*args, **kwargs):
            publish.assert_called_once_with(env['RAILSHOT_GCP_CERTIFICATE_DNS_CONFIG'], request)
            return responses.pop(0)
        with patch.dict(os.environ, env), patch.object(dns, 'ensure', return_value=receipt) as publish, \
                patch('gcp_routes.urlopen', side_effect=resolve) as lookup, patch('gcp_routes.time.sleep') as sleep:
            self.assertEqual(routes.certificate_dns(), receipt)
        self.assertEqual(lookup.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_certificate_hook_fails_before_issuance_when_dns_write_or_propagation_fails(self):
        request = {'purpose': 'certificate', 'hostname': 'app1.railshot.io', 'content': 'token.authorize.certificatemanager.goog'}
        env = {'RAILSHOT_GCP_CERTIFICATE_DNS_CONFIG': str(self.root / 'dns.json'),
               'RAILSHOT_GCP_CERTIFICATE_DNS_REQUEST': json.dumps(request)}
        for write_error in (True, False):
            with self.subTest(write_error=write_error), patch.dict(os.environ, env), \
                    patch.object(dns, 'ensure', side_effect=dns.DNSError('DNS_RECORD_CONFLICT') if write_error else None), \
                    patch('gcp_routes.urlopen', side_effect=OSError('unreachable')) as lookup, \
                    patch('gcp_routes.time.sleep'), patch('gcp_routes.Path.cwd', return_value=self.root):
                code = 'DNS_RECORD_CONFLICT' if write_error else 'DNS_CERTIFICATE_PROPAGATION_PENDING'
                with self.assertRaisesRegex(dns.DNSError, code):
                    routes.certificate_dns()
                self.assertEqual(lookup.call_count, 0 if write_error else 30)
                self.assertEqual(routes.read_private(self.root / 'certificate-dns-error.json'), {'code': code})

    def test_two_apps_preserve_existing_ids_routes_defaults_and_replay_reads_only(self):
        with patch('gcp_routes.native', side_effect=self.native):
            first = routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
            second = routes.execute(self.config_path, app(2), dns_config_path=str(self.root / 'dns.json'))
            count = len(self.calls)
            self.assertEqual(routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json')), first)
            self.assertEqual(len(self.calls), count)
        self.assertEqual(second['public_route_state'], 'pending_verification')
        self.assertEqual(first['frontend_ip'], second['frontend_ip'])
        config = routes.read_private(self.config_path)
        self.assertEqual(config['previous_source_sha'], 'a' * 40)
        self.assertEqual(len(config['owned_resources']), len(self.config['owned_resources']) + 14)
        self.assertTrue(all(config['owned_resources'][k] == v for k, v in self.config['owned_resources'].items()))
        journal = routes.read_private(Path(config['state_dir']) / ('gcp-route-' + app()['application_id'] + '.json'))
        self.assertRegex(journal['module_sha256'], r'^[a-f0-9]{64}$')
        self.assertEqual(journal['phase'], 'applied')
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)

    def test_uncertain_apply_never_retries_same_or_different_app(self):
        self.interrupt = True
        with patch('gcp_routes.native', side_effect=self.native):
            with self.assertRaisesRegex(routes.RouteError, 'GCP_ROUTE_APPLY_INCOMPLETE'):
                routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
            count = len(self.calls)
            for request in (app(), app(2)):
                with self.assertRaisesRegex(routes.RouteError, 'GCP_ROUTE_RECONCILE_REQUIRED'):
                    routes.ensure(self.config_path, request, dns_config_path=str(self.root / 'dns.json'))
            self.assertFalse(any(c[2] == 'apply' for c in self.calls[count:]))
        self.assertEqual(routes.read_private(self.config['variables_file']), self.values)

    def test_post_apply_output_mismatch_is_unknown_and_does_not_commit_authority(self):
        self.corrupt_output = True
        with patch('gcp_routes.native', side_effect=self.native), self.assertRaisesRegex(routes.RouteError, 'GCP_ROUTE_APPLY_INCOMPLETE'):
            routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
        self.assertEqual(routes.read_private(self.config_path), self.config)
        with patch('gcp_routes.native', side_effect=self.native), self.assertRaisesRegex(routes.RouteError, 'GCP_ROUTE_RECONCILE_REQUIRED'):
            routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
        self.assertEqual(sum(c[2] == 'apply' for c in self.calls), 1)

    def test_storage_failure_after_apply_keeps_unknown_without_second_apply(self):
        write = routes.durable_write
        def failing_write(path, data, *args, **kwargs):
            if Path(path) == self.config_path:
                raise OSError('synthetic authority commit interruption')
            return write(path, data, *args, **kwargs)
        with patch('gcp_routes.native', side_effect=self.native), patch('gcp_routes.durable_write', side_effect=failing_write):
            with self.assertRaises(routes.RouteError): routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
        self.assertEqual(sum(command[2] == 'apply' for command in self.calls), 1)
        with patch('gcp_routes.native', side_effect=self.native):
            self.assertEqual(routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))['phase'], 'applied')
        self.assertEqual(sum(c[2] == 'apply' for c in self.calls), 1)

    def test_completed_apply_recovers_by_observation_without_second_apply(self):
        self.interrupt_after = True
        with patch('gcp_routes.native', side_effect=self.native):
            with self.assertRaises(routes.RouteError): routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
            self.assertEqual(routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))['phase'], 'applied')
        self.assertEqual(sum(c[2] == 'apply' for c in self.calls), 1)

    def test_partial_cloud_apply_is_not_adopted_or_replayed(self):
        self.interrupt_after = True
        self.drift = True
        with patch('gcp_routes.native', side_effect=self.native):
            with self.assertRaises(routes.RouteError): routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
            with self.assertRaisesRegex(routes.RouteError, 'GCP_ROUTE_RECONCILE_REQUIRED'):
                routes.ensure(self.config_path, app(2), dns_config_path=str(self.root / 'dns.json'))
        self.assertEqual(sum(c[2] == 'apply' for c in self.calls), 1)
        self.assertEqual(routes.read_private(self.config_path), self.config)

    def test_saved_plan_tamper_after_intent_never_applies(self):
        write = routes.durable_write
        def tamper(path, data, *args, **kwargs):
            result = write(path, data, *args, **kwargs)
            if Path(path).name == 'gcp-route-' + app()['application_id'] + '.json' and json.loads(data)['phase'] == 'applying':
                for plan in Path(self.config['state_dir']).glob('gcp-route-*/plan'):
                    plan.write_bytes(b'tampered plan')
            return result
        with patch('gcp_routes.native', side_effect=self.native), patch('gcp_routes.durable_write', side_effect=tamper):
            with self.assertRaisesRegex(routes.RouteError, 'GCP_ROUTE_APPLY_INCOMPLETE'): routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
        self.assertFalse(any(command[2] == 'apply' for command in self.calls))

    def test_plan_rejection_never_applies_or_changes_authority(self):
        def native(argv):
            if argv[2] == 'show':
                plan = plan_for(app(), self.values)
                plan['resource_changes'][0]['change']['actions'] = ['delete', 'create']
                return json.dumps(plan)
            return self.native(argv)
        with patch('gcp_routes.native', side_effect=native), self.assertRaises(ValueError):
            routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
        self.assertFalse(any(command[2] == 'apply' for command in self.calls))
        self.assertEqual(routes.read_private(self.config_path), self.config)
        self.assertEqual(routes.read_private(self.config['variables_file']), self.values)

    def test_failed_preflight_can_retry_with_new_workspace_without_replaying_apply(self):
        with patch('gcp_routes.native', side_effect=RuntimeError('synthetic init failure')), self.assertRaisesRegex(routes.RouteError, 'GCP_ROUTE_PREPARATION_FAILED'):
            routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
        with patch('gcp_routes.native', side_effect=self.native):
            self.assertEqual(routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))['phase'], 'applied')
        self.assertEqual(sum(command[2] == 'apply' for command in self.calls), 1)
        self.assertEqual(len([p for p in Path(self.config['state_dir']).iterdir() if p.is_dir()]), 2)

    def test_bound_lineage_or_foreign_identity_rejected_before_terraform(self):
        for field in ('lineage', 'id'):
            changed = copy.deepcopy(self.state)
            if field == 'lineage': changed['lineage'] = 'foreign'
            else: changed['resources'][0]['instances'][0]['attributes']['id'] = 'foreign'
            self.write(self.config['state_file'], changed)
            with patch('gcp_routes.native') as native, self.assertRaisesRegex(routes.RouteError, 'GCP_ROUTE_PREPARATION_FAILED'):
                routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
            native.assert_not_called()

    def test_existing_app_mutation_and_collision_rejected(self):
        with patch('gcp_routes.native', side_effect=self.native): routes.ensure(self.config_path, app(), dns_config_path=str(self.root / 'dns.json'))
        for request in ({**app(), 'health_path': '/different'}, {**app(2), 'node_port': app()['node_port']},
                        {**app(2), 'hostname': self.values['hostname']}):
            with patch('gcp_routes.native') as native, self.assertRaises(ValueError): routes.ensure(self.config_path, request, dns_config_path=str(self.root / 'dns.json'))
            native.assert_not_called()

    def test_strict_request_and_exact_seven_creates(self):
        for request in ({**app(), 'node_port': True}, {**app(), 'health_path': '/..'},
                        {**app(), 'hostname': '*.railshot.io'}, {**app(), 'extra': 'untrusted'}):
            with self.assertRaises(ValueError): routes.checked_request(request)
        plan = plan_for(app(), self.values)
        self.assertEqual(len(routes.validate_plan(plan, app(), self.values)), 7)
        plan['resource_changes'].pop(0)
        with self.assertRaisesRegex(ValueError, 'exact application resources'): routes.validate_plan(plan, app(), self.values)

    def test_refreshed_host_rule_order_does_not_block_new_application(self):
        values = {**self.values, 'routes': {app()['application_id']: routes.checked_request(app())}}
        plan = plan_for(app(2), values)
        before = copy.deepcopy(plan['resource_changes'][7]['change']['before'])
        after = copy.deepcopy(before)
        after['host_rule'].reverse()
        after['path_matcher'].reverse()
        plan['resource_drift'] = [{'address': 'google_compute_url_map.app',
            'change': {'before': before, 'after': after}}]
        self.assertEqual(len(routes.validate_plan(plan, app(2), values)), 7)
        after['host_rule'][0]['hosts'] = ['foreign.railshot.io']
        with self.assertRaisesRegex(ValueError, 'drift'):
            routes.validate_plan(plan, app(2), values)

    def test_existing_backend_self_link_and_empty_redirect_are_equivalent(self):
        values = {**self.values, 'routes': {app()['application_id']: routes.checked_request(app())}}
        plan = plan_for(app(2), values)
        web = next(r['change'] for r in plan['resource_changes'] if r['address'] == 'google_compute_url_map.app')
        web['before']['path_matcher'][0]['default_service'] = 'https://www.googleapis.com/compute/v1/' + web['before']['path_matcher'][0]['default_service']
        redirect = next(r['change'] for r in plan['resource_changes'] if r['address'] == 'google_compute_url_map.redirect')
        for side, empty in [('before', ''), ('after', None)]:
            block = redirect[side]['path_matcher'][0]
            block['default_service'] = empty
            block['default_url_redirect'][0].update(path_redirect=empty, prefix_redirect=empty)
        self.assertEqual(len(routes.validate_plan(plan, app(2), values)), 7)
        changed = copy.deepcopy(plan)
        row = next(r['change'] for r in changed['resource_changes'] if r['address'] == 'google_compute_url_map.app')
        row['after']['path_matcher'][0]['default_service'] += '-foreign'
        with self.assertRaises(ValueError): routes.validate_plan(changed, app(2), values)
        redirect['after']['path_matcher'][0]['default_url_redirect'][0]['prefix_redirect'] = '/changed'
        with self.assertRaises(ValueError): routes.validate_plan(plan, app(2), values)

    def test_mutation_delete_replacement_network_expansion_and_unknown_existing_blocked(self):
        for mutation in ('delete', 'replace', 'unrelated', 'old_host', 'old_matcher', 'default', 'port', 'source', 'unknown', 'new_health'):
            plan = plan_for(app(), self.values); changes = plan['resource_changes']
            if mutation == 'delete': changes[0]['change']['actions'] = ['delete']
            elif mutation == 'replace': changes[0]['change']['actions'] = ['delete', 'create']
            elif mutation == 'unrelated': changes[0]['address'] = 'google_compute_instance.foreign'
            elif mutation == 'old_host': changes[7]['change']['after']['host_rule'][0]['hosts'] = ['foreign.railshot.io']
            elif mutation == 'old_matcher': changes[7]['change']['after']['path_matcher'][0]['default_service'] = 'foreign'
            elif mutation == 'default': changes[8]['change']['after']['default_url_redirect'] = []
            elif mutation == 'port': changes[9]['change']['after']['allow'][0]['ports'].append('22')
            elif mutation == 'source': changes[9]['change']['after']['source_ranges'] = ['0.0.0.0/0']
            elif mutation == 'unknown': changes[7]['change']['after_unknown']['default_service'] = True
            elif mutation == 'new_health': changes[2]['change']['after']['http_health_check'][0]['request_path'] = '/foreign'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): routes.validate_plan(plan, app(), self.values)

    def test_second_application_preserves_first_and_accepts_only_new_computed_backend(self):
        values = {**self.values, 'routes': {app()['application_id']: routes.checked_request(app())}}
        plan = plan_for(app(2), values)
        change = plan['resource_changes'][7]['change']; index = len(change['after']['path_matcher']) - 1
        change['after']['path_matcher'][index]['default_service'] = None
        change['after_unknown']['path_matcher'] = [{} for _ in change['after']['path_matcher']]
        change['after_unknown']['path_matcher'][index] = {'default_service': True}
        self.assertEqual(len(routes.validate_plan(plan, app(2), values)), 7)
        change['after']['path_matcher'][0]['default_service'] = 'foreign'
        with self.assertRaises(ValueError): routes.validate_plan(plan, app(2), values)

    def test_computed_project_requires_exact_bound_provider_and_variables(self):
        plan = plan_for(app(), self.values)
        change = plan['resource_changes'][0]['change']
        del change['after']['project']; change['after_unknown'] = {'project': True}
        self.assertEqual(len(routes.validate_plan(plan, app(), self.values)), 7)
        for key in ('variables', 'configuration'):
            changed = copy.deepcopy(plan); changed[key] = {}
            with self.assertRaises(ValueError): routes.validate_plan(changed, app(), self.values)
        change['after']['project'] = 'foreign-project'
        with self.assertRaises(ValueError): routes.validate_plan(plan, app(), self.values)

    def test_refresh_only_for_unchanged_resources_is_permitted(self):
        plan = plan_for(app(), self.values)
        plan['resource_drift'] = [{'address': 'google_compute_global_address.app'}]
        plan['resource_changes'].append({'address': 'google_compute_global_address.app', 'change': {'actions': ['no-op']}})
        self.assertEqual(len(routes.validate_plan(plan, app(), self.values)), 7)
        plan['resource_drift'].append({'address': 'google_compute_url_map.app'})
        with self.assertRaisesRegex(ValueError, 'writable refresh'): routes.validate_plan(plan, app(), self.values)

    def test_empty_gcp_fields_allow_additive_plan_without_mutating_evidence(self):
        # Reproduce the failed clock plan: refresh emits [] for unset firewall
        # selectors and the URL map plan emits null for an empty description.
        plan = plan_for(app(), self.values)
        firewall = plan['resource_changes'][9]
        before = copy.deepcopy(firewall['change']['before'])
        after = copy.deepcopy(before)
        for field in ('source_service_accounts', 'source_tags', 'target_tags'):
            before[field] = None
            after[field] = []
            firewall['change']['before'][field] = []
            firewall['change']['after'][field] = []
        plan['resource_drift'] = [{'address': firewall['address'], 'change': {
            'actions': ['update'], 'before': before, 'after': after}}]
        for item in plan['resource_changes'][7:9]:
            change = item['change']
            for field in ('host_rule', 'path_matcher'):
                for block in change['before'][field]:
                    block['description'] = ''
                for block in change['after'][field]:
                    block['description'] = None
                change['after'][field][-1]['description'] = ''
        original = copy.deepcopy(plan)
        self.assertEqual(len(routes.validate_plan(plan, app(), self.values)), 7)
        self.assertEqual(plan, original)

    def test_real_firewall_drift_is_rejected_even_with_empty_field_normalization(self):
        for field, value in (('source_tags', ['foreign']), ('target_tags', ['foreign']),
                             ('source_service_accounts', ['foreign@example.com']),
                             ('source_ranges', ['0.0.0.0/0']), ('description', 'changed')):
            with self.subTest(field=field):
                plan = plan_for(app(), self.values)
                firewall = plan['resource_changes'][9]
                before = copy.deepcopy(firewall['change']['before'])
                after = copy.deepcopy(before)
                before[field] = None
                after[field] = value
                plan['resource_drift'] = [{'address': firewall['address'], 'change': {
                    'actions': ['update'], 'before': before, 'after': after}}]
                with self.assertRaisesRegex(ValueError, 'writable refresh'):
                    routes.validate_plan(plan, app(), self.values)

    def test_route_descriptions_and_unknown_values_are_not_generally_ignored(self):
        for mutation in ('changed_description', 'new_description', 'unknown_description'):
            with self.subTest(mutation=mutation):
                plan = plan_for(app(), self.values)
                change = plan['resource_changes'][7]['change']
                if mutation == 'changed_description':
                    change['before']['host_rule'][0]['description'] = 'keep this'
                    change['after']['host_rule'][0]['description'] = None
                elif mutation == 'new_description':
                    change['after']['host_rule'][-1]['description'] = 'unexpected'
                else:
                    change['after_unknown']['host_rule'] = [{'description': True}]
                with self.assertRaises(ValueError):
                    routes.validate_plan(plan, app(), self.values)


if __name__ == '__main__':
    unittest.main()
