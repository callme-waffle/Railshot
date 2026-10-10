import copy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import traceback
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import dns


class DNSTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.token = self.root / 'token'
        dns.durable_write(self.token, b'synthetic-secret-not-a-real-token\n')
        self.config = {'version': 1, 'zone_id': 'a' * 32, 'base_domain': 'example.com',
                       'token_file': str(self.token), 'state_dir': str(self.root / 'state')}
        self.config_path = self.root / 'config.json'
        self.write_config()
        self.request = {'application_id': 'app-123', 'hostname': 'demo.example.com',
                        'type': 'A', 'content': '192.0.2.4'}
        self.records, self.calls = [], []
        self.post_failure = None
        self.patcher = patch('dns.transport', side_effect=self.transport)
        self.mock_transport = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def write_config(self):
        dns.durable_write(self.config_path, dns.encoded(self.config))

    def record(self, request=None, **changes):
        request = request or self.request
        return {'id': 'b' * 32, 'name': request['hostname'], 'type': request['type'],
                'content': request['content'], 'comment': 'railshot:' + request['application_id'],
                'proxied': False, 'ttl': 300, **changes}

    def response(self, records, page=1):
        start = (page - 1) * dns.PAGE_SIZE
        values = records[start:start + dns.PAGE_SIZE]
        return {'success': True, 'errors': [], 'result': copy.deepcopy(values), 'result_info': {
            'count': len(values), 'page': page, 'per_page': dns.PAGE_SIZE, 'total_count': len(records),
            'total_pages': max(1, (len(records) + dns.PAGE_SIZE - 1) // dns.PAGE_SIZE)}}

    def transport(self, config, method, *, query=None, body=None):
        self.calls.append((method, copy.deepcopy(query), copy.deepcopy(body)))
        if method == 'GET':
            self.assertEqual(set(query), {'name.exact', 'page', 'per_page'})
            values = [r for r in self.records if r['name'] == query['name.exact']]
            return self.response(values, query['page'])
        self.assertEqual(method, 'POST')
        # The create intent must already be durable when the first write is sent.
        intents = [json.loads(p.read_bytes()) for p in (self.root / 'state').glob('*.json')
                   if not p.name.endswith('.error.json')]
        self.assertTrue(any(row['phase'] == 'creating' and row['request']['hostname'] == body['name']
                            for row in intents))
        if self.post_failure == 'before':
            raise dns.DNSError('DNS_API_FAILED')
        if self.post_failure == 'crash':
            raise KeyboardInterrupt()
        self.records.append({'id': 'b' * 32, **body})
        if self.post_failure == 'after':
            raise dns.DNSError('DNS_API_FAILED')
        return {'success': True, 'result': copy.deepcopy(self.records[-1])}

    def ensure(self, request=None):
        return dns.ensure(self.config_path, self.request if request is None else request)

    def assert_error(self, code, request=None, outcome='BLOCKED'):
        with self.assertRaises(dns.DNSError) as caught:
            self.ensure(request)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.outcome, outcome)

    def test_create_readback_and_repeat_are_idempotent(self):
        first = self.ensure()
        self.assertEqual(first['status'], 'verified')
        self.assertEqual([c[0] for c in self.calls], ['GET', 'POST', 'GET'])
        self.assertEqual(self.calls[1][2], {
            'name': 'demo.example.com', 'type': 'A', 'content': '192.0.2.4',
            'comment': 'railshot:app-123', 'proxied': False, 'ttl': 300})
        second = self.ensure()
        self.assertEqual(first['record_id'], second['record_id'])
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)
        row = json.loads(Path(first['receipt_path']).read_bytes())
        self.assertEqual(row['receipt'], second)
        for path in (self.root / 'state').iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / 'state').stat().st_mode), 0o700)

    def test_existing_owned_exact_record_is_verified_read_only(self):
        self.records = [self.record()]
        self.ensure()
        self.assertEqual([c[0] for c in self.calls], ['GET'])

    def test_application_cname_create_and_readback(self):
        request = {**self.request, 'type': 'CNAME', 'content': 'edge.example.net'}
        self.assertEqual(self.ensure(request)['content'], 'edge.example.net')
        self.assertEqual(self.ensure(request)['type'], 'CNAME')
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)

    def test_tunnel_cname_uses_proxy_and_auto_ttl_and_reconciles_read_only(self):
        request = {**self.request, 'type': 'CNAME', 'proxied': True,
                   'content': '11111111-2222-4333-8444-555555555555.cfargotunnel.com'}
        receipt = self.ensure(request)
        self.assertTrue(receipt['proxied'])
        self.assertEqual(receipt['status'], 'verified')
        self.assertEqual(self.calls[1][2], {
            'name': request['hostname'], 'type': 'CNAME', 'content': request['content'],
            'comment': 'railshot:app-123', 'proxied': True, 'ttl': 1})
        self.ensure(request)
        self.assertEqual([c[0] for c in self.calls], ['GET', 'POST', 'GET', 'GET'])

    def test_tunnel_readback_requires_proxy_and_auto_ttl(self):
        request = {**self.request, 'type': 'CNAME', 'proxied': True,
                   'content': '11111111-2222-4333-8444-555555555555.cfargotunnel.com'}
        for changes in ({'proxied': False, 'ttl': 1}, {'proxied': True, 'ttl': 300}):
            with self.subTest(changes=changes):
                self.records = [self.record(request, **changes)]
                self.assert_error('DNS_RECORD_CONFLICT', request)
        self.records = [self.record(request, proxied=True, ttl=1)]
        self.assertEqual(self.ensure(request)['status'], 'verified')
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))

    def test_proxy_validation_rejects_other_targets_types_and_certificate_requests(self):
        request = {**self.request, 'type': 'CNAME', 'proxied': True,
                   'content': '11111111-2222-4333-8444-555555555555.cfargotunnel.com'}
        invalid = [{'content': target} for target in (
            'edge.example.net', 'not-a-uuid.cfargotunnel.com',
            '11111111222243338444555555555555.cfargotunnel.com',
            '11111111-2222-4333-8444-555555555555.cfargotunnel.com.evil.com',
            '11111111-2222-4333-8444-555555555555.cfargotunnel.com.',
            'https://11111111-2222-4333-8444-555555555555.cfargotunnel.com')]
        invalid += [{'type': 'A', 'content': '192.0.2.4'},
                    {'purpose': 'certificate', 'application_hostname': self.request['hostname'],
                     'hostname': '_acme-challenge.demo.example.com'}]
        invalid += [{'proxied': value} for value in (1, 0, 'true', 'false', None, [], {})]
        for change in invalid:
            with self.subTest(change=change):
                self.assert_error('DNS_PROXY_INVALID', {**request, **change})
        self.mock_transport.assert_not_called()

    def test_explicit_false_preserves_dns_only_ttl(self):
        request = {**self.request, 'proxied': False}
        self.assertFalse(self.ensure(request)['proxied'])
        self.assertEqual(self.calls[1][2]['proxied'], False)
        self.assertEqual(self.calls[1][2]['ttl'], 300)

    def test_preexisting_legacy_request_binding_does_not_gain_false_field(self):
        expected = {**self.request, 'purpose': 'application', 'application_hostname': self.request['hostname']}
        config = dns.config_at(self.config_path)
        self.assertEqual(dns.request_at(config, self.request), expected)
        # This is the exact intent shape written before proxied was supported.
        with dns.locked(config) as root:
            key = dns.digest({'zone_id': config['zone_id'], 'hostname': self.request['hostname']})
            path = root / (key + '.json')
            dns.save(path, {'version': 1, 'phase': 'creating', 'request': expected,
                            'config_sha256': config['_sha256']})
        self.records = [self.record()]
        receipt = self.ensure()
        self.assertNotIn('proxied', receipt)
        self.assertEqual(json.loads(path.read_bytes())['request'], expected)
        self.assertEqual([c[0] for c in self.calls], ['GET'])

    def test_post_success_without_matching_readback_does_not_verify(self):
        def mutate_then_change(config, method, **kwargs):
            result = self.transport(config, method, **kwargs)
            if method == 'POST':
                self.records[0]['proxied'] = True
            return result
        with patch('dns.transport', side_effect=mutate_then_change):
            self.assert_error('DNS_RECORD_CONFLICT', outcome='UNKNOWN')
            self.assert_error('DNS_RECORD_CONFLICT', outcome='UNKNOWN')
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)

    def test_foreign_records_of_every_type_are_not_adopted(self):
        for record_type in ('A', 'CNAME', 'AAAA', 'TXT', 'NS', 'MX'):
            with self.subTest(record_type=record_type):
                self.records = [self.record(type=record_type, comment='someone-else')]
                self.assert_error('DNS_RECORD_FOREIGN_OWNER')
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))

    def test_owned_mismatch_or_duplicate_never_overwrites(self):
        for change in ({'content': '192.0.2.9'}, {'type': 'CNAME'}, {'ttl': 1}, {'proxied': True}):
            with self.subTest(change=change):
                self.records = [self.record(**change)]
                self.assert_error('DNS_RECORD_CONFLICT')
        self.records = [self.record(), self.record(id='c' * 32)]
        self.assert_error('DNS_RECORD_CONFLICT')
        self.assertFalse(any(c[0] == 'POST' for c in self.calls))

    def test_all_pages_are_read_before_any_create(self):
        self.records = [self.record(type='TXT', comment='foreign', id=f'{n:032x}') for n in range(101)]
        self.assert_error('DNS_RECORD_CONFLICT')
        self.assertEqual([c[1]['page'] for c in self.calls], [1, 2])

    def test_incomplete_or_unrelated_list_cannot_prove_absence(self):
        for field, value in (('total_count', 1), ('total_pages', 2), ('page', 2), ('per_page', 20)):
            response = self.response([])
            response['result_info'][field] = value
            with self.subTest(field=field), patch('dns.transport', return_value=response) as call:
                self.assert_error('DNS_LIST_INCOMPLETE')
                self.assertEqual(call.call_args.args[1], 'GET')
        with patch('dns.transport', return_value=self.response([self.record(name='other.example.com')])):
            self.assert_error('DNS_LIST_RESPONSE_INVALID')

    def test_empty_zero_page_list_is_valid(self):
        response = self.response([])
        response['result_info']['total_pages'] = 0
        with patch('dns.transport', return_value=response):
            self.assertEqual(dns.records_at(self.config, self.request['hostname']), [])

    def test_timeout_after_write_can_reconcile_without_a_second_post(self):
        self.post_failure = 'after'
        self.assertEqual(self.ensure()['status'], 'verified')
        self.ensure()
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)

    def test_unknown_write_stays_unknown_and_only_queries_on_resume(self):
        self.post_failure = 'before'
        self.assert_error('DNS_CREATE_OUTCOME_UNKNOWN', outcome='UNKNOWN')
        self.assert_error('DNS_CREATE_OUTCOME_UNKNOWN', outcome='UNKNOWN')
        self.assertEqual([c[0] for c in self.calls], ['GET', 'POST', 'GET', 'GET'])
        self.records = [self.record()]
        self.assertEqual(self.ensure()['status'], 'verified')
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)

    def test_process_crash_after_intent_never_retries_create(self):
        self.post_failure = 'crash'
        with self.assertRaises(KeyboardInterrupt):
            self.ensure()
        self.post_failure = None
        self.assert_error('DNS_CREATE_OUTCOME_UNKNOWN', outcome='UNKNOWN')
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)

    def test_reconcile_read_failure_preserves_unknown_outcome(self):
        self.post_failure = 'before'
        self.assert_error('DNS_CREATE_OUTCOME_UNKNOWN', outcome='UNKNOWN')
        with patch('dns.transport', side_effect=dns.DNSError('DNS_API_FAILED')):
            self.assert_error('DNS_API_FAILED', outcome='UNKNOWN')

    def test_record_removed_after_verification_is_not_recreated(self):
        self.ensure()
        self.records.clear()
        self.assert_error('DNS_VERIFIED_RECORD_MISSING')
        self.assertEqual([c[0] for c in self.calls].count('POST'), 1)

    def test_application_and_certificate_have_distinct_receipts(self):
        application = self.ensure()
        certificate = {**self.request, 'purpose': 'certificate', 'application_hostname': 'demo.example.com',
                       'hostname': '_acme-challenge_abc123.demo.example.com', 'type': 'CNAME',
                       'content': 'token.authorize.certificatemanager.goog'}
        cert = self.ensure(certificate)
        self.assertNotEqual(application['receipt_path'], cert['receipt_path'])
        self.assertEqual(self.ensure(certificate)['record_id'], cert['record_id'])
        for change in ({'hostname': '_acme-challenge_other.other.example.com'}, {'type': 'A'},
                       {'application_hostname': 'evil.com'}, {'hostname': '_acme-challenge.demo.example.com.evil.com'}):
            with self.subTest(change=change), self.assertRaises(dns.DNSError):
                self.ensure({**certificate, **change})

    def test_unsafe_requests_fail_before_transport(self):
        for change in ({'hostname': 'example.com'}, {'hostname': 'two.demo.example.com'},
                       {'hostname': 'demo.example.com.evil.com'}, {'hostname': 'demo.example.com.'},
                       {'hostname': '*.example.com'}, {'hostname': 'https://demo.example.com'},
                       {'type': 'AAAA'}, {'content': '::1'}, {'content': '192.0.2.4/path'},
                       {'application_id': 'bad\ncomment'}, {'type': 'CNAME', 'content': 'https://evil.com'},
                       {'type': 'CNAME', 'content': 'demo.example.com'}, {'url': 'https://evil.com'}):
            with self.subTest(change=change), self.assertRaises(dns.DNSError):
                self.ensure({**self.request, **change})
        self.mock_transport.assert_not_called()

    def test_intent_binding_blocks_owner_or_payload_change_before_transport(self):
        self.ensure()
        self.calls.clear()
        self.assert_error('DNS_INTENT_BINDING_CONFLICT', {**self.request, 'application_id': 'another-app'})
        self.assert_error('DNS_INTENT_BINDING_CONFLICT', {**self.request, 'content': '192.0.2.9'})
        self.assertEqual(self.calls, [])

    def test_private_file_symlink_modes_and_state_lock(self):
        self.config_path.chmod(0o644)
        self.assert_error('DNS_PRIVATE_FILE_REQUIRED')
        self.config_path.chmod(0o600)
        alias = self.root / 'alias.json'
        alias.symlink_to(self.config_path)
        with self.assertRaisesRegex(dns.DNSError, 'DNS_PRIVATE_FILE_REQUIRED'):
            dns.ensure(alias, self.request)
        (self.root / 'state').mkdir(mode=0o700)
        (self.root / 'state').chmod(0o755)
        self.assert_error('DNS_PRIVATE_STATE_REQUIRED')
        (self.root / 'state').chmod(0o700)
        with dns.locked(dns.config_at(self.config_path)):
            self.assert_error('DNS_WRITER_BUSY')
        self.mock_transport.assert_not_called()

    def test_exact_config_keys_and_no_endpoint_override(self):
        for key, value in (('url', 'https://evil.com'), ('zone_id', '../evil'), ('version', True),
                           ('base_domain', 'https://example.com')):
            config = {**self.config, key: value}
            dns.durable_write(self.config_path, dns.encoded(config))
            self.assert_error('DNS_CONFIG_INVALID')
        self.mock_transport.assert_not_called()

    def test_real_transport_fixed_origin_header_only_and_bounded_read(self):
        self.patcher.stop()
        response = Mock()
        response.status = 200
        response.read.return_value = dns.encoded(self.response([]))
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch('dns.build_opener', return_value=opener) as build:
            dns.transport(dns.config_at(self.config_path), 'GET', query={
                'name.exact': 'demo.example.com', 'page': 1, 'per_page': 100})
        request = opener.open.call_args.args[0]
        url = urlsplit(request.full_url)
        self.assertEqual(url.scheme, 'https')
        self.assertEqual(url.netloc, 'api.cloudflare.com')
        self.assertEqual(url.path, '/client/v4/zones/' + 'a' * 32 + '/dns_records')
        self.assertEqual(parse_qs(url.query)['name.exact'], ['demo.example.com'])
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-secret-not-a-real-token')
        self.assertNotIn('synthetic-secret', request.full_url)
        self.assertEqual(response.read.call_args.args, (dns.MAX_RESPONSE + 1,))
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 15})
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], dns.NoRedirect)
        self.assertIsNone(build.call_args.args[1].redirect_request(None, None, 302, None, None, 'https://evil.com'))

    def test_token_privacy_and_provider_errors_are_not_exposed_or_persisted(self):
        self.patcher.stop()
        secret = 'synthetic-secret-not-a-real-token'
        with patch('dns.build_opener') as build:
            build.return_value.open.side_effect = HTTPError('https://api.cloudflare.com', 401, secret, {}, io.BytesIO(secret.encode()))
            try:
                self.ensure()
            except dns.DNSError as error:
                self.assertEqual(error.code, 'DNS_API_FAILED')
                self.assertNotIn(secret, ''.join(traceback.format_exception(type(error), error, error.__traceback__)))
            else:
                self.fail('provider error must fail closed')
        for path in (self.root / 'state').glob('*.json'):
            self.assertNotIn(secret, path.read_text())
        self.token.chmod(0o644)
        with patch('dns.build_opener') as build:
            self.assert_error('DNS_PRIVATE_FILE_REQUIRED')
            build.assert_not_called()
        self.token.chmod(0o600)
        dns.durable_write(self.token, b'token\r\nInjected: value')
        with patch('dns.build_opener') as build:
            self.assert_error('DNS_TOKEN_INVALID')
            build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
