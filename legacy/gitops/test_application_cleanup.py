import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import application_cleanup as cleanup
import dns
import gcp_routes
from test_gcp_routes import app, plan_for


def state_resources(rows):
    resources = []
    for address, attributes in rows.items():
        kind, rest = address.split('.', 1)
        name, _, index = rest.partition('[')
        item = {'attributes': attributes}
        if index:
            item['index_key'] = json.loads(index[:-1])
        resources.append({'mode': 'managed', 'type': kind, 'name': name, 'instances': [item]})
    return resources


def state_rows(state):
    rows = {}
    for resource in state['resources']:
        for item in resource['instances']:
            address = resource['type'] + '.' + resource['name']
            if 'index_key' in item:
                address += '[' + json.dumps(item['index_key']) + ']'
            rows[address] = item['attributes']
    return rows


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.request = app()
        self.base = {'project_id': 'fixture-project', 'zone': 'test-a', 'instance_name': 'existing-vm',
                     'expected_private_ip': '10.1.0.2', 'hostname': 'legacy.example.com', 'routes': {}}
        self.creation = plan_for(self.request, self.base)
        neg_name = self.creation['resource_changes'][0]['change']['after']['name']
        self.creation['resource_changes'][1]['change']['after']['network_endpoint_group'] = neg_name
        rows = {'google_compute_global_address.app': {'id': 'shared-public-ip', 'address': '34.1.2.3'}}
        for change in self.creation['resource_changes']:
            values = copy.deepcopy(change['change']['after'])
            values['id'] = values.get('id') or change['address'] + '-original-id'
            values.pop('fingerprint', None)
            rows[change['address']] = values
        self.state = {'version': 4, 'lineage': 'original-lineage', 'serial': 10, 'resources': state_resources(rows)}
        self.values = {**self.base, 'routes': {self.request['application_id']: gcp_routes.checked_request(self.request)}}
        self.config = {'version': 1, 'provider': 'gcp', 'edge_kind': 'native', 'state_file': str(self.root / 'state.json'),
                       'variables_file': str(self.root / 'vars.json'), 'state_dir': str(self.root / 'edge'),
                       'state_lineage': self.state['lineage'], 'owned_resources': gcp_routes.owned(self.state),
                       'previous_source_sha': 'a' * 40}
        self.config_path = self.root / 'config.json'
        self.write(self.config_path, self.config); self.write(self.config['state_file'], self.state)
        self.write(self.config['variables_file'], self.values)
        self.dns_config = {'version': 1, 'zone_id': 'a' * 32, 'base_domain': 'railshot.io',
                           'token_file': str(self.root / 'token'), 'state_dir': str(self.root / 'dns')}
        self.write(self.root / 'dns.json', self.dns_config)
        self.binding = {'version': 1, 'environment_id': 'environment-one', 'application_id': self.request['application_id'],
                        'provider': 'gcp', 'registered': {'tenant': 'team', 'app': 'user-app', 'target': {
                            'id': self.request['application_id'], 'namespace': self.request['application_id'],
                            'node_port': self.request['node_port']}}, 'hostname': self.request['hostname'],
                        'ingress': {'edge_config_file': str(self.config_path), 'dns_config_file': str(self.root / 'dns.json')}}
        self.calls, self.documents = [], {}
        self.fail_apply = False
        self.mutate_plan = None
        self.after_drift = False
        self.applied = False
        self.native_patch = patch('edge.native', side_effect=self.native)
        self.native_patch.start(); self.addCleanup(self.native_patch.stop)
        self.dns_patch = patch('dns.records_at', return_value=[])
        self.dns_patch.start(); self.addCleanup(self.dns_patch.stop)

    def write(self, path, value):
        cleanup.durable_write(path, cleanup.encoded(value))

    def document(self, candidate):
        current = cleanup.read_private(self.config['variables_file'])
        state = cleanup.read_private(self.config['state_file'])
        rows = state_rows(state)
        old = current['routes'].get(self.request['application_id'])
        new = candidate['routes'].get(self.request['application_id'])
        was = any(address.startswith('google_compute_backend_service.routes[') for address in rows)
        now = new is not None and new.get('enabled', True)
        changes = []
        for source in self.creation['resource_changes']:
            address = source['address']; kind = address.split('.')[0]
            if source['change']['actions'] == ['create']:
                should = new is not None if kind in gcp_routes.KINDS[4:] else now
                exists = address in rows
                if exists and not should:
                    change = {'actions': ['delete'], 'before': rows[address], 'after': None}
                elif should and not exists:
                    change = copy.deepcopy(source['change'])
                elif exists:
                    change = {'actions': ['no-op'], 'before': rows[address], 'after': rows[address]}
                else:
                    continue
            elif was != now:
                after = copy.deepcopy(source['change']['after' if now else 'before'])
                after.pop('fingerprint', None)
                after['id'] = rows[address]['id']
                change = {'actions': ['update'], 'before': rows[address], 'after': after}
            else:
                change = {'actions': ['no-op'], 'before': rows[address], 'after': rows[address]}
            changes.append({'address': address, 'change': change})
        address = 'google_compute_global_address.app'
        changes.append({'address': address, 'change': {'actions': ['no-op'], 'before': rows[address], 'after': rows[address]}})
        return {'resource_changes': changes, 'variables': {k: {'value': v} for k, v in candidate.items()}}

    def native(self, argv):
        self.calls.append(argv)
        if argv[0] == 'gcloud':
            self.assertIn('--project=' + self.values['project_id'], argv)
            return '[]'
        if argv[0] == 'aws':
            if 'get-caller-identity' in argv:
                return json.dumps({'Account': self.values['account_id']})
            return json.dumps({'TargetGroups': [], 'Rules': [], 'SecurityGroupRules': [], 'ResourceRecordSets': []})
        work, command = Path(argv[1].split('=', 1)[1]), argv[2]
        if command == 'init':
            self.assertEqual(json.loads((work / 'backend.tf.json').read_text())['terraform']['backend']['local']['path'], self.config['state_file'])
            return ''
        if command == 'plan':
            variables = next(a.split('=', 1)[1] for a in argv if a.startswith('-var-file='))
            path = next(a.split('=', 1)[1] for a in argv if a.startswith('-out='))
            document = self.document(cleanup.read_private(variables))
            if self.mutate_plan and Path(variables).name == 'change.json':
                self.mutate_plan(document)
            if self.after_drift and self.applied:
                document['resource_drift'] = [{'address': 'google_compute_global_address.app'}]
            self.documents[path] = document
            cleanup.durable_write(path, b'fake-saved-plan-' + Path(path).name.encode())
            return ''
        if command == 'show':
            return json.dumps(self.documents[argv[-1]])
        self.assertEqual(command, 'apply')
        self.assertEqual(cleanup.read_private(work / 'intent.json')['phase'], 'applying')
        if self.fail_apply:
            raise RuntimeError('uncertain fake provider response')
        state = cleanup.read_private(self.config['state_file']); rows = state_rows(state)
        for item in self.documents[argv[-1]]['resource_changes']:
            action = item['change']['actions']
            if action == ['delete']:
                del rows[item['address']]
            elif action in (['update'], ['create']):
                rows[item['address']] = copy.deepcopy(item['change']['after'])
                rows[item['address']]['id'] = rows[item['address']].get('id') or item['address'] + '-restored-id'
        state['resources'] = state_resources(rows); state['serial'] += 1
        self.write(self.config['state_file'], state)
        # Provider refresh after apply should use the candidate as the effective state.
        self.applied = True
        return ''

    def test_stop_start_delete_preserve_shared_ids_and_certificates(self):
        original = gcp_routes.owned(self.state)
        plan = cleanup.plan(self.binding, 'stop')
        self.assertEqual(sum(r['actions'] == ['delete'] for r in plan['private']['native']['changes']), 4)
        receipt = cleanup.execute(self.binding, 'stop', plan)
        self.assertEqual(receipt['phase'], 'stopped')
        calls = list(self.calls)
        self.assertEqual(cleanup.execute(self.binding, 'stop', plan), receipt)
        self.assertEqual(self.calls, calls)
        with self.assertRaises(ValueError):
            cleanup.execute(self.binding, 'delete', plan)
        stopped = gcp_routes.owned(cleanup.read_private(self.config['state_file']))
        for kind in gcp_routes.KINDS[4:]:
            address = kind + '.routes[' + json.dumps(self.request['application_id']) + ']'
            self.assertEqual(stopped[address], original[address])
        resumed = cleanup.plan(self.binding, 'start')
        self.assertEqual(cleanup.execute(self.binding, 'start', resumed)['phase'], 'started')
        deleting = cleanup.plan(self.binding, 'delete')
        self.assertEqual(sum(r['actions'] == ['delete'] for r in deleting['private']['native']['changes']), 7)
        self.assertEqual(cleanup.execute(self.binding, 'delete', deleting)['phase'], 'deleted')
        after = gcp_routes.owned(cleanup.read_private(self.config['state_file']))
        self.assertEqual(after['google_compute_global_address.app'], 'shared-public-ip')
        self.assertEqual(len(after), 4)
        with self.assertRaisesRegex(ValueError, 'stopped'):
            cleanup.plan(self.binding, 'start')

    def test_partial_apply_cannot_be_replayed_or_bypassed_by_new_plan(self):
        plan = cleanup.plan(self.binding, 'delete'); self.fail_apply = True
        with self.assertRaises(cleanup.CleanupError) as caught:
            cleanup.execute(self.binding, 'delete', plan)
        self.assertTrue(caught.exception.unknown)
        calls = len(self.calls)
        for run in (lambda: cleanup.execute(self.binding, 'delete', plan), lambda: cleanup.plan(self.binding, 'stop')):
            with self.assertRaisesRegex(ValueError, 'reconciliation'):
                run()
        self.assertEqual(calls, len(self.calls))
        self.assertEqual(cleanup.read_private(self.config['variables_file']), self.values)

    def test_stale_serial_source_plan_and_foreign_dns_fail_before_apply(self):
        plan = cleanup.plan(self.binding, 'delete')
        for mutation in ('serial', 'saved', 'dns'):
            with self.subTest(mutation=mutation):
                state = copy.deepcopy(self.state)
                if mutation == 'serial':
                    state['serial'] += 1
                self.write(self.config['state_file'], state)
                saved = Path(plan['private']['native']['work']) / 'change.tfplan'
                saved.write_bytes(b'tampered' if mutation == 'saved' else b'fake-saved-plan-change.tfplan')
                context = patch('dns.application_snapshot', return_value=[{'foreign': True}]) if mutation == 'dns' else patch('dns.records_at', return_value=[])
                with context, self.assertRaises(ValueError):
                    cleanup.execute(self.binding, 'delete', plan)
                self.assertFalse(any(call[2] == 'apply' for call in self.calls))

    def test_shared_lb_delete_replacement_unrelated_egress_and_unknown_are_rejected(self):
        def mutate(document, mode):
            change = document['resource_changes'][-1]['change']
            if mode == 'foreign-delete':
                change.update(actions=['delete'], after=None)
            elif mode == 'replace':
                document['resource_changes'][0]['change']['actions'] = ['delete', 'create']
            elif mode == 'unknown':
                document['resource_changes'][-2]['change']['after_unknown'] = {'source_ranges': True}
            else:
                document['resource_changes'][-2]['change']['after']['source_ranges'] = ['0.0.0.0/0']
        for mode in ('foreign-delete', 'replace', 'unknown', 'firewall'):
            self.mutate_plan = lambda document: mutate(document, mode)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                cleanup.plan(self.binding, 'delete')
        self.assertFalse(any(call[2] == 'apply' for call in self.calls))

    def test_drift_after_apply_is_unknown_and_does_not_commit_variables(self):
        plan = cleanup.plan(self.binding, 'stop'); self.after_drift = True
        with self.assertRaises(cleanup.CleanupError):
            cleanup.execute(self.binding, 'stop', plan)
        self.assertEqual(cleanup.read_private(self.config['variables_file']), self.values)


class AWSLifecycleTest(unittest.TestCase):
    write = LifecycleTest.write
    native = LifecycleTest.native

    def setUp(self):
        LifecycleTest.setUp(self)
        self.binding['provider'] = 'aws'
        self.key = 'app-' + '7' * 24
        self.route = {'host': self.binding['hostname'], 'node_port': self.request['node_port'], 'health_path': '/ready',
                      'provider_kind': 'aws', 'target_private_ip': '10.1.0.2', 'priority': 1000,
                      'target_security_group_id': 'sg-0123456789abcdef0', 'manage_dns': False}
        self.values = {'base_domain': 'railshot.io', 'region': 'test-region', 'account_id': '123456789012',
                       'vpc_id': 'vpc-0123456789abcdef0', 'routes': {self.key: self.route}}
        tfdir = self.root / 'module'; (tfdir / '.terraform').mkdir(parents=True)
        self.write(tfdir / '.terraform/terraform.tfstate', {'backend': {'type': 'local', 'config': {'path': str(self.root / 'state.json')}}})
        self.config = {'version': 1, 'state_dir': str(self.root / 'edge'), 'terraform_dir': str(tfdir),
                       'variables_file': str(self.root / 'vars.json'), 'auto_apply': True}
        # Existing creation writers leave the reservation ledger at reserved, and
        # update only the app file to applied. Lifecycle must understand that.
        Path(self.config['state_dir']).mkdir(mode=0o700)
        row = {'route_key': self.key, 'phase': 'reserved', 'config_sha256': cleanup.digest(self.config), 'route': self.route,
               'request': {'target_id': self.binding['application_id'], 'environment_id': self.binding['environment_id'],
                           'app': self.binding['registered']['app'], 'tenant': self.binding['registered']['tenant'],
                           'namespace': self.binding['application_id']}}
        self.write(Path(self.config['state_dir']) / 'allocations.json', {self.key: row})
        self.write(Path(self.config['state_dir']) / (self.key + '.json'), {**row, 'phase': 'applied'})
        self.config['state_file'] = str(self.root / 'state.json')  # test runner access only
        self.write(self.config_path, {k: v for k, v in self.config.items() if k != 'state_file'})
        self.write(self.config['variables_file'], self.values)
        suffix = '.app[' + json.dumps(self.key) + ']'
        self.definitions = {
            'aws_lb_target_group' + suffix: {'vpc_id': self.values['vpc_id'], 'port': self.route['node_port'], 'protocol': 'HTTP',
                                           'target_type': 'ip', 'health_check': [{'path': '/ready'}], 'arn': 'exact-owned-target-group'},
            'aws_lb_target_group_attachment' + suffix: {'target_id': '10.1.0.2', 'port': self.route['node_port'],
                                                       'target_group_arn': 'exact-owned-target-group'},
            'aws_lb_listener_rule' + suffix: {'priority': 1000, 'listener_arn': 'existing-shared-listener',
                                             'condition': [{'host_header': [{'values': [self.route['host']]}]}]},
            'aws_security_group_rule.target_from_alb[' + json.dumps(self.route['target_security_group_id'] + ':' + str(self.route['node_port'])) + ']': {
                'security_group_id': self.route['target_security_group_id'], 'type': 'ingress', 'protocol': 'tcp',
                'from_port': self.route['node_port'], 'to_port': self.route['node_port'], 'source_security_group_id': 'shared-sg'}}
        self.egress = {'from_port': self.route['node_port'], 'to_port': self.route['node_port'], 'protocol': 'tcp',
                       'cidr_blocks': ['10.1.0.2/32'], 'description': '', 'ipv6_cidr_blocks': [],
                       'prefix_list_ids': [], 'security_groups': [], 'self': False}
        rows = {k: {**v, 'id': k + '-original'} for k, v in self.definitions.items()}
        rows.update({'aws_lb.app': {'id': 'shared-alb'}, 'aws_security_group.alb': {
            'id': 'shared-sg', 'egress': [self.egress], 'ingress': [{'from_port': 443}]}})
        self.state = {'version': 4, 'lineage': 'original-lineage', 'serial': 10, 'resources': state_resources(rows)}
        self.write(self.config['state_file'], self.state)

    def document(self, candidate):
        rows = state_rows(cleanup.read_private(self.config['state_file']))
        route = candidate['routes'].get(self.key)
        active = route is not None and route.get('enabled', True)
        changes = []
        for address, definition in self.definitions.items():
            if active and address not in rows:
                change = {'actions': ['create'], 'before': None, 'after': definition}
            elif not active and address in rows:
                change = {'actions': ['delete'], 'before': rows[address], 'after': None}
            elif address in rows:
                change = {'actions': ['no-op'], 'before': rows[address], 'after': rows[address]}
            else:
                continue
            changes.append({'address': address, 'change': change})
        for address in ('aws_security_group.alb', 'aws_lb.app'):
            after = copy.deepcopy(rows[address])
            if address.endswith('.alb'):
                after['egress'] = [self.egress] if active else []
            changes.append({'address': address, 'change': {'actions': ['no-op'] if after == rows[address] else ['update'],
                                                           'before': rows[address], 'after': after}})
        return {'variables': {k: {'value': v} for k, v in candidate.items()}, 'resource_changes': changes}

    def test_stop_start_delete_last_app_from_real_writer_ledger_shape(self):
        for action, phase in (('stop', 'stopped'), ('start', 'started'), ('delete', 'deleted')):
            plan = cleanup.plan(self.binding, action)
            before = cleanup.read_private(self.config['state_file'])
            cleanup.validate(self.binding, action, plan)
            self.assertEqual(cleanup.read_private(self.config['state_file']), before)
            self.assertEqual(cleanup.execute(self.binding, action, plan)['phase'], phase)
        self.assertEqual(cleanup.read_private(self.config['variables_file'])['routes'], {})
        rows = state_rows(cleanup.read_private(self.config['state_file']))
        self.assertEqual(set(rows), {'aws_lb.app', 'aws_security_group.alb'})
        self.assertEqual(rows['aws_lb.app']['id'], 'shared-alb')
        self.assertEqual(rows['aws_security_group.alb']['ingress'], [{'from_port': 443}])

    def refreshed_document(self, candidate):
        document = self.document(candidate)
        document['resource_drift'] = []
        for row in document['resource_changes']:
            kind = row['address'].split('.')[0]
            change = row['change']
            if kind not in ('aws_lb_listener_rule', 'aws_lb_target_group') or not change.get('before'):
                continue
            stale = {**change['before'], 'tags': None}
            observed = {**stale, 'tags': {}}
            if kind == 'aws_lb_target_group':
                stale['load_balancer_arns'] = []
                observed['load_balancer_arns'] = ['shared-alb']
            document['resource_drift'].append({'address': row['address'], 'type': kind, 'mode': 'managed',
                'change': {'actions': ['update'], 'before': stale, 'after': observed}})
            change['before'] = observed
            if change['actions'] == ['no-op']:
                change['after'] = observed
        return document

    def test_aws_metadata_refresh_does_not_block_stop_start_delete(self):
        original = self.document
        refreshed = self.refreshed_document
        def observed(candidate):
            with patch.object(self, 'document', original):
                return refreshed(candidate)
        with patch.object(self, 'document', side_effect=observed):
            self.test_stop_start_delete_last_app_from_real_writer_ledger_shape()

    def test_refresh_still_rejects_configuration_identity_and_foreign_alb_drift(self):
        base = self.refreshed_document(self.values)
        cleanup.unchanged(base)
        mutations = {
            'tag value': lambda row: row['change']['after'].update(tags={'Owner': 'someone-else'}),
            'port': lambda row: row['change']['after'].update(port=9999),
            'identity': lambda row: row['change']['after'].update(id='foreign-resource'),
            'foreign ALB': lambda row: row['change']['after'].update(load_balancer_arns=['other-alb']),
            'second ALB': lambda row: row['change']['after'].update(load_balancer_arns=['shared-alb', 'other-alb']),
            'replacement': lambda row: row['change'].update(replace_paths=[['port']]),
            'import': lambda row: row['change'].update(importing={'id': 'foreign-resource'}),
            'deposed': lambda row: row.update(deposed='old-instance'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                document = copy.deepcopy(base)
                drift = next(row for row in document['resource_drift'] if row['type'] == 'aws_lb_target_group')
                mutate(drift)
                planned = next(row['change'] for row in document['resource_changes'] if row['address'] == drift['address'])
                planned['before'] = planned['after'] = copy.deepcopy(drift['change']['after'])
                with self.assertRaises(ValueError):
                    cleanup.unchanged(document)

    def test_implicit_backend_and_next_writer_use_current_bundle_not_old_module(self):
        import edge
        config = cleanup.read_private(self.config_path)
        directory = Path(config['terraform_dir'])
        (directory / '.terraform/terraform.tfstate').unlink()
        self.write(directory / 'terraform.tfstate', self.state)
        (directory / 'main.tf').write_text('locals { old_module_ignores_enabled = true }')
        self.assertEqual(edge.local_state_file(config), str(directory / 'terraform.tfstate'))
        calls = []
        with patch('edge.native', side_effect=lambda argv: calls.append(argv) or '{}'):
            edge.terraform(config, 'plan', '-var-file=' + self.config['variables_file'])
        work = Path(calls[-1][1].split('=', 1)[1])
        self.assertNotEqual(work, directory)
        self.assertIn('local.active_routes', (work / 'main.tf').read_text())
        self.assertIn('old_module_ignores_enabled', (directory / 'main.tf').read_text())
        self.assertEqual(json.loads((work / 'backend.tf.json').read_text())['terraform']['backend']['local']['path'],
                         str(directory / 'terraform.tfstate'))

    def test_provider_presence_after_delete_cannot_be_reported_as_success(self):
        plan = cleanup.plan(self.binding, 'delete')
        native = self.native
        def retained_group(argv):
            if 'describe-target-groups' in argv:
                return json.dumps({'TargetGroups': [{'TargetGroupArn': 'exact-owned-target-group'}]})
            return native(argv)
        with patch('edge.native', side_effect=retained_group), self.assertRaises(cleanup.CleanupError) as caught:
            cleanup.execute(self.binding, 'delete', plan)
        self.assertTrue(caught.exception.unknown)
        self.assertEqual(cleanup.read_private(self.config['variables_file']), self.values)


class DNSDeleteTest(unittest.TestCase):
    def test_exact_id_and_owner_preflight_all_records_then_delete_and_readback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); config = {'version': 1, 'zone_id': 'a' * 32, 'base_domain': 'example.com',
                'token_file': str(root / 'token'), 'state_dir': str(root)}
            config['_sha256'] = dns.digest(config)
            requests = [{'application_id': 'app-123', 'hostname': 'demo.example.com', 'type': 'A', 'content': '34.1.2.3'},
                        {'application_id': 'app-123', 'purpose': 'certificate', 'application_hostname': 'demo.example.com',
                         'hostname': '_acme-challenge_a.demo.example.com', 'type': 'CNAME', 'content': 'a.authorize.certificatemanager.goog'}]
            records = {}
            for n, request in enumerate(requests):
                record = {'id': str(n + 1) * 32, 'name': request['hostname'], 'type': request['type'], 'content': request['content'],
                          'comment': 'railshot:app-123', 'proxied': False, 'ttl': 300}
                records[request['hostname']] = [record]
                key = dns.digest({'zone_id': config['zone_id'], 'hostname': request['hostname']})
                dns.save(root / (key + '.json'), {'phase': 'verified', 'request': request,
                    'config_sha256': config['_sha256'], 'receipt': {'record_id': record['id']}})
            calls = []
            def transport(config, method, *, record_id=None):
                calls.append((method, record_id))
                self.assertEqual(method, 'DELETE')
                self.assertTrue(any(dns.private_json(p).get('phase') == 'deleting' for p in root.glob('*.json')))
                for name, rows in records.items():
                    if rows and rows[0]['id'] == record_id:
                        records[name] = []
            with patch('dns.records_at', side_effect=lambda config, host: copy.deepcopy(records.get(host, []))), patch('dns.transport', side_effect=transport):
                expected = dns.application_snapshot(config, 'app-123', 'demo.example.com')
                records[requests[1]['hostname']][0]['id'] = 'f' * 32
                with self.assertRaises(dns.DNSError):
                    dns.remove_application(config, 'app-123', 'demo.example.com', expected)
                self.assertFalse(calls)
                records[requests[1]['hostname']][0]['id'] = '2' * 32
                dns.remove_application(config, 'app-123', 'demo.example.com', expected)
                self.assertEqual(set(calls), {('DELETE', '1' * 32), ('DELETE', '2' * 32)})
                self.assertEqual(dns.application_snapshot(config, 'app-123', 'demo.example.com'), [])


if __name__ == '__main__':
    unittest.main()
