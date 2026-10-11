"""Mock AWS responses and a temporary ledger only; never uses live credentials."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import budget
import costs

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
ACCOUNT = '123456789012'


def days(currency='USD', total='0'):
    return [{'TimePeriod': {'Start': f'2026-10-0{day}', 'End': f'2026-10-0{day + 1}'},
             'Total': {'UnblendedCost': {'Amount': total, 'Unit': currency}}, 'Estimated': True} for day in (1, 2)]


def request():
    return {'TimePeriod': {'Start': '2026-10-01', 'End': '2026-10-03'}, 'Granularity': 'DAILY',
            'Metrics': ['UnblendedCost'], 'Filter': {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': [ACCOUNT]}}}


def product(attributes, family, rate, unit, suffix=''):
    sku = 'fixture-' + suffix
    return json.dumps({'product': {'sku': sku, 'productFamily': family, 'attributes': attributes},
        'terms': {'OnDemand': {'term': {'sku': sku, 'effectiveDate': '2026-09-01T00:00:00Z',
          'priceDimensions': {'rate': {'unit': unit, 'beginRange': '0', 'endRange': 'Inf', 'appliesTo': [],
                                     'pricePerUnit': {'USD': rate}}}}}}})


class AWS:
    def __init__(self):
        self.calls, self.account, self.rows = [], ACCOUNT, days()
        self.missing, self.price_paging, self.bad_price, self.loop = None, False, False, False
        self.compute_rate, self.storage_rate = '0.104', '0.0912'

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ['sts', 'get-caller-identity']:
            return {'Account': self.account}
        if args[:2] == ['ce', 'get-cost-and-usage']:
            assert json.loads(args[args.index('--cli-input-json') + 1]) == request()
            if '--next-page-token' not in args:
                return {'ResultsByTime': self.rows[:1], 'NextPageToken': 'ce-second'}
            assert args[args.index('--next-page-token') + 1] == 'ce-second'
            return {'ResultsByTime': self.rows[1:], **({'NextPageToken': 'ce-second'} if self.loop else {})}
        assert args[:2] == ['pricing', 'get-products']
        if self.price_paging and '--next-token' not in args:
            return {'PriceList': [], 'NextToken': 'price-second'}
        attributes = {row['Field']: row['Value'] for row in json.loads(args[args.index('--filters') + 1])}
        family = attributes.pop('productFamily', None)
        if family == self.missing and self.missing is not None:
            return {'PriceList': []}
        if family == 'Compute Instance':
            values = [product(attributes, family, self.compute_rate, 'Hrs', 'compute')]
        elif family == 'Storage':
            values = [product(attributes, family, self.storage_rate, 'GB-Mo', 'storage')]
        else:
            values = [product({**attributes, 'usagetype': 'APN2-PublicIPv4:' + usage}, None, '0.005', 'Hrs', usage)
                      for usage in ['InUseAddress', 'IdleAddress']]
        if self.bad_price:
            row = json.loads(values[0]); row['terms']['OnDemand']['term']['priceDimensions']['rate']['pricePerUnit'] = {'EUR': '1'}
            values[0] = json.dumps(row)
        return {'PriceList': values}


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.ledger = self.root / 'billing.sqlite3'
        self.scope = 'aws/' + ACCOUNT + '/2026-10'
        with contextlib.closing(costs.ledger(self.ledger)) as db:
            costs.import_snapshot(db, self.scope, 'aws', '2026-10', (NOW - timedelta(minutes=1)).isoformat(),
                {'account_id': ACCOUNT, 'request': request(), 'response': {'ResultsByTime': days()}}, now=NOW, source_scope=ACCOUNT)
            costs.reserve(db, scope=self.scope, operation_id='legacy-held', currency='USD', incremental_cost='11',
                          limit='30', unreported_cost='15', now=NOW)
        self.ledger.chmod(0o600)
        target = {'schema_version': 'v1', 'provider_kind': 'aws', 'execution_driver': 'terraform', 'target_id': 'test-runtime',
            'profile': {'kind': 'app_cluster', 'allowed_sizes': ['t3.large']},
            'variables': {'target_id': 'test-runtime', 'purpose': 'runtime', 'account_id': ACCOUNT,
                          'region': 'ap-northeast-2', 'instance_type': 't3.large', 'root_volume_gb': 30,
                          'data_disk_gib': 20, 'max_run_duration_seconds': 7200, 'allocate_eip': False},
            'budget': {'ledger_path': str(self.ledger), 'scope': self.scope, 'currency': 'USD', 'incremental_cost': '1',
                       'limit': '30', 'unreported_cost': '15', 'quoted_at': '2026-10-01T00:00:00Z', 'expires_at': '2026-10-01T02:00:00Z'}}
        self.value = {'version': 1, 'targets': [target], 'retained_storage_hours': 24}
        for index in range(1, 4):
            node = copy.deepcopy(target); node['target_id'] = node['variables']['target_id'] = f'test-db{index}'
            node['profile']['kind'] = 'database_cluster'; node['variables']['purpose'] = 'database'
            self.value['targets'].append(node)
        self.aws = AWS()

    def tearDown(self):
        self.temporary.cleanup()

    def collect(self, value=None):
        return budget.refresh(value or self.value, self.root / 'evidence', self.root / 'output.json', call=self.aws, clock=lambda: NOW)

    def rows(self):
        with contextlib.closing(budget.sqlite3.connect(self.ledger.as_uri() + '?mode=ro', uri=True)) as db:
            return {table: db.execute(f'SELECT * FROM {table}').fetchall() for table in ['snapshots', 'reservations']}

    def test_complete_ce_price_paging_and_existing_holds_bound_four_quotes(self):
        before = self.rows(); original = copy.deepcopy(self.value); self.aws.price_paging = True
        result = self.collect()
        self.assertEqual(result, {'currency': 'USD', 'incremental_estimate': '4', 'projected_total': '30', 'limit': '30', 'within_budget': True})
        self.assertEqual(self.rows()['reservations'], before['reservations'])
        self.assertEqual(self.value, original)
        output = json.loads((self.root / 'output.json').read_text())
        self.assertEqual(set(output), {'version', 'targets', 'summary', 'evidence'})
        self.assertEqual(output['summary'], result)
        self.assertEqual(len([call for call in self.aws.calls if call[:2] == ['pricing', 'get-products']]), 6)
        for source, target in zip(original['targets'], output['targets']):
            self.assertEqual({key: value for key, value in target.items() if key != 'budget'},
                             {key: value for key, value in source.items() if key != 'budget'})
            self.assertEqual(set(target['budget']), budget.BUDGET_KEYS)
            self.assertEqual(target['budget']['quoted_at'], NOW.isoformat())
            self.assertEqual(target['budget']['expires_at'], (NOW + timedelta(hours=2)).isoformat())
            for key in ['ledger_path', 'currency', 'limit', 'unreported_cost']:
                self.assertEqual(target['budget'][key], source['budget'][key])
        for evidence in output['evidence']:
            path = Path(evidence['path'])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), evidence['sha256'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / 'output.json').stat().st_mode & 0o777, 0o600)

    def test_default_retained_storage_until_month_end_blocks_without_erasing_holds(self):
        value = copy.deepcopy(self.value); value.pop('retained_storage_hours')
        before = self.rows()['reservations']; result = self.collect(value)
        self.assertFalse(result['within_budget'])
        self.assertEqual(result['incremental_estimate'], '20')
        self.assertEqual(result['projected_total'], '46')
        self.assertEqual(before, self.rows()['reservations'])

    def test_new_reported_spend_can_exceed_budget_and_is_still_an_actual_snapshot(self):
        self.aws.rows = days(total='1')
        result = self.collect()
        self.assertEqual(result['projected_total'], '32')
        self.assertFalse(result['within_budget'])
        with contextlib.closing(budget.sqlite3.connect(self.ledger)) as db:
            self.assertEqual(costs.report(db, self.scope, now=NOW)['totals'], {'USD': '2'})

    def test_old_quote_remains_floor_and_is_rounded_up(self):
        self.value['targets'][0]['budget']['incremental_cost'] = '2.1'
        result = self.collect()
        self.assertEqual(result['incremental_estimate'], '6')
        self.assertEqual(json.loads((self.root / 'output.json').read_text())['targets'][0]['budget']['incremental_cost'], '3')

    def test_ce_page_fees_are_included_before_rounding_an_exact_dollar_estimate(self):
        self.value['targets'] = self.value['targets'][:1]
        self.aws.compute_rate, self.aws.storage_rate = '0.47', '0.031'
        result = self.collect()
        # Compute .94 + disk .05 + IPv4 .01 = exactly 1; two CE pages add .02.
        self.assertEqual(result['incremental_estimate'], '2')
        evidence = json.loads(next((self.root / 'evidence').glob('*-estimate.json')).read_text())
        self.assertEqual(evidence['ce_request_cost'], '0.02')
        self.assertEqual(evidence['ce_request_count'], 2)

    def test_eip_is_charged_through_storage_horizon_not_only_running_hours(self):
        self.value['targets'][0]['variables']['allocate_eip'] = True
        self.collect()
        estimates = json.loads(next((self.root / 'evidence').glob('*-estimate.json')).read_text())['estimates']
        self.assertEqual(estimates[0]['ipv4_hours'], '24')
        self.assertEqual(estimates[1]['ipv4_hours'], '2')

    def test_wrong_account_missing_price_and_wrong_currency_do_not_write_ledger(self):
        mutations = [lambda: setattr(self.aws, 'account', '999999999999'),
                     lambda: setattr(self.aws, 'missing', 'Storage'), lambda: setattr(self.aws, 'bad_price', True),
                     lambda: setattr(self.aws, 'rows', days(currency='KRW'))]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.aws = AWS(); before = self.rows(); mutation()
                with self.assertRaises(budget.BudgetError): self.collect()
                self.assertEqual(self.rows(), before)
                self.assertFalse((self.root / 'output.json').exists())

    def test_ce_incomplete_coverage_and_repeating_token_fail_before_import(self):
        for change in ['gap', 'loop']:
            with self.subTest(change=change):
                self.aws = AWS(); before = self.rows()
                if change == 'gap': self.aws.rows = self.aws.rows[:1]
                else: self.aws.loop = True
                with self.assertRaises(ValueError): self.collect()
                self.assertEqual(self.rows(), before)

    def test_input_identity_policy_horizon_and_existing_authority_are_required(self):
        mutations = [lambda v: v.update(retained_storage_hours=48), lambda v: v.update(retained_storage_hours=True),
                     lambda v: v['targets'][1]['budget'].update(limit='31'),
                     lambda v: v['targets'][1]['variables'].update(account_id='999999999999'),
                     lambda v: v['targets'][0]['variables'].update(max_run_duration_seconds=None),
                     lambda v: v['targets'][0]['budget'].update(currency='KRW'),
                     lambda v: v['targets'].append(copy.deepcopy(v['targets'][0]))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = copy.deepcopy(self.value); mutate(value)
                with self.assertRaises(ValueError): self.collect(value)
        self.assertEqual(self.aws.calls, [])
        self.ledger.unlink()
        with self.assertRaises(OSError): self.collect()
        self.assertFalse(self.ledger.exists())

    def test_empty_or_public_ledger_is_not_silently_initialized(self):
        with contextlib.closing(costs.ledger(self.ledger)) as db:
            db.execute('DELETE FROM snapshots'); db.commit()
        with self.assertRaisesRegex(budget.BudgetError, 'BILLING_AUTHORITY_MISSING'): self.collect()
        self.ledger.chmod(0o644)
        with self.assertRaisesRegex(budget.BudgetError, 'PRIVATE_FILE_INVALID'): self.collect()
        self.assertEqual(self.aws.calls, [])

    def test_cli_stdout_is_only_summary_and_error_is_safe_json(self):
        path = self.root / 'input.json'; path.write_text(json.dumps(self.value)); path.chmod(0o600)
        argv = ['budget.py', '--input', str(path), '--evidence-dir', str(self.root / 'evidence'), '--output', str(self.root / 'output.json')]
        real = budget.refresh
        def offline(value, evidence, output): return real(value, evidence, output, call=self.aws, clock=lambda: NOW)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(budget.sys, 'argv', argv), patch.object(budget, 'refresh', offline), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(budget.main(), 0)
        self.assertEqual(set(json.loads(stdout.getvalue())), {'currency', 'incremental_estimate', 'projected_total', 'limit', 'within_budget'})
        self.assertEqual(stderr.getvalue(), '')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(budget.sys, 'argv', argv), patch.object(budget, 'refresh', offline), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(budget.main(), 2)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(json.loads(stderr.getvalue()), {'error': {'code': 'BUDGET_OUTPUT_EXISTS', 'outcome': 'BLOCKED'}})


if __name__ == '__main__':
    unittest.main(verbosity=2)
