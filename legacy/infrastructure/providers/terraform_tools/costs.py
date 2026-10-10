#!/usr/bin/env python3
"""Import complete monthly billing snapshots; reserve bounded incremental spend.

Administrator-only CLI. This is not an approval service or a real-time cloud meter.
Uses stdlib SQLite and Decimal; never converts currencies implicitly.
"""
import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid money') from exc
    if not result.is_finite():
        raise ValueError('non-finite money')
    return result


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return result.astimezone(timezone.utc)


def ledger(path):
    db = sqlite3.connect(path, timeout=10)
    db.executescript('''
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS snapshots (
        scope TEXT PRIMARY KEY, provider TEXT NOT NULL, period TEXT NOT NULL,
        observed_at TEXT NOT NULL, checksum TEXT NOT NULL, rows_json TEXT NOT NULL,
        source_scope TEXT);
      CREATE TABLE IF NOT EXISTS reservations (
        operation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
        scope TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL,
        created_at TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('held','released')));
    ''')
    if 'source_scope' not in {r[1] for r in db.execute('PRAGMA table_info(snapshots)')}:
        db.execute('ALTER TABLE snapshots ADD COLUMN source_scope TEXT')
        db.commit()
    return db


def azure_fields(row):
    """EA/MCA casing varies; ambiguous duplicate headers must not choose a value silently."""
    fields = {}
    for key, value in row.items():
        normalized = key.casefold()
        if normalized in fields and fields[normalized] != value:
            raise ValueError('conflicting Azure export headers')
        fields[normalized] = value
    currencies = {fields[k] for k in ('billingcurrency', 'billingcurrencycode') if fields.get(k)}
    if len(currencies) > 1:
        raise ValueError('conflicting Azure billing currencies')
    fields['billingcurrency'] = next(iter(currencies), '')
    return fields


def verify_source_scope(provider, rows, source_scope):
    if not isinstance(source_scope, str) or not source_scope:
        raise ValueError('source scope required')
    if provider == 'aws':
        if rows['account_id'] != source_scope:
            raise ValueError('billing export source scope mismatch or missing')
        return
    if provider == 'gcp' and isinstance(rows, dict):
        if rows['project_billing_info']['projectId'] != source_scope:
            raise ValueError('billing export source scope mismatch or missing')
        return
    for row in rows:
        actual = ((row.get('project') or {}).get('id') if provider == 'gcp'
                  else azure_fields(row).get('subscriptionid'))
        if not isinstance(actual, str) or actual.casefold() != source_scope.casefold():
            raise ValueError('billing export source scope mismatch or missing')


def normalize_aws(snapshot, period):
    """One account's complete CE query; request metadata is retained by the operator.

    CE totals do not echo the account filter. Do not treat a bare response, a
    service-filtered total, or an unfinished page as an account-wide snapshot.
    """
    if not isinstance(snapshot, dict) or set(snapshot) != {'account_id', 'request', 'response'}:
        raise ValueError('AWS snapshot requires account_id, request and response')
    account, request, response = (snapshot[k] for k in ('account_id', 'request', 'response'))
    if not isinstance(account, str) or not re.fullmatch(r'[0-9]{12}', account):
        raise ValueError('AWS snapshot requires a 12-digit account')
    required = {'TimePeriod', 'Granularity', 'Metrics', 'Filter'}
    if (not isinstance(request, dict) or not required <= set(request)
            or set(request) - required - {'GroupBy'} or request.get('GroupBy')
            or request['Granularity'] not in {'DAILY', 'MONTHLY'}
            or request['Metrics'] != ['UnblendedCost']):
        raise ValueError('AWS snapshot requires an ungrouped UnblendedCost query')
    dimensions = request['Filter'].get('Dimensions') if isinstance(request['Filter'], dict) else None
    if (not isinstance(request['Filter'], dict) or set(request['Filter']) != {'Dimensions'} or not isinstance(dimensions, dict)
            or set(dimensions) - {'Key', 'Values', 'MatchOptions'}
            or dimensions.get('Key') != 'LINKED_ACCOUNT' or dimensions.get('Values') != [account]
            or dimensions.get('MatchOptions', ['EQUALS']) not in (['EQUALS'], ['CASE_SENSITIVE'], ['EQUALS', 'CASE_SENSITIVE'])):
        raise ValueError('AWS query must filter exactly the registered linked account')
    interval = request['TimePeriod']
    try:
        if set(interval) != {'Start', 'End'}:
            raise ValueError('AWS time interval requires Start and End')
        start, end = (date.fromisoformat(interval[k]) for k in ('Start', 'End'))
        month_start = date.fromisoformat(period + '-01')
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        if start != month_start or not start < end <= next_month:
            raise ValueError('AWS query must cover this month from its first day')
        if (not isinstance(response, dict) or response.get('NextPageToken') or response.get('GroupDefinitions')
                or not isinstance(response.get('ResultsByTime'), list) or not response['ResultsByTime']):
            raise ValueError('AWS response missing, grouped or incompletely paginated')
        cursor, total, estimated, currency = start, Decimal(0), False, None
        for result in response['ResultsByTime']:
            first = date.fromisoformat(result['TimePeriod']['Start'])
            last = date.fromisoformat(result['TimePeriod']['End'])
            if first != cursor or not first < last <= end:
                raise ValueError('AWS result periods must cover the query without gaps or overlaps')
            if request['Granularity'] == 'DAILY' and last - first != timedelta(days=1):
                raise ValueError('AWS daily result must cover exactly one day')
            if result.get('Groups') or set(result['Total']) != {'UnblendedCost'} or type(result['Estimated']) is not bool:
                raise ValueError('AWS response must contain explicit total and estimate status')
            metric = result['Total']['UnblendedCost']
            unit = metric['Unit']
            if not isinstance(unit, str) or not re.fullmatch('[A-Z]{3}', unit) or currency not in (None, unit):
                raise ValueError('AWS currency missing or inconsistent')
            currency = unit
            total += amount(metric['Amount'])
            estimated |= result['Estimated']
            cursor = last
        if cursor != end:
            raise ValueError('AWS result does not cover the complete query')
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('malformed AWS cost query or response') from exc
    return [{'resource_id': 'aws-account:' + account, 'currency': currency, 'cost': str(total),
             'estimated': estimated, 'coverage_end': end.isoformat()}]


def normalize_gcp_console(snapshot, period):
    """Native Korean/KRW Console CSV, bound to the operator's browser observation.

    Required envelope: format=gcp_console_report_v1, billing_account_id,
    project_billing_info (the unedited gcloud billing projects describe response),
    observed_at, csv (original text), browser_evidence (original observation text),
    report={url,start,end,time_basis:'usage_date',group_by:'project',filters:{},currency:'KRW'}.
    Dates cover the whole month, inclusive. The private original envelope remains
    with the operator; hashes in the ledger bind this receipt to that evidence.
    Browser metadata is operator-observed, not an independently verified API query.
    Account-wide totals are conservative estimates, never project-attributed costs.
    """
    required = {'format', 'billing_account_id', 'project_billing_info', 'observed_at',
                'csv', 'browser_evidence', 'report'}
    if set(snapshot) != required or snapshot['format'] != 'gcp_console_report_v1':
        raise ValueError('GCP Console snapshot requires the complete v1 envelope')
    account, binding, metadata = (snapshot[k] for k in ('billing_account_id', 'project_billing_info', 'report'))
    if not isinstance(account, str) or not re.fullmatch(r'[0-9A-F]{6}(?:-[0-9A-F]{6}){2}', account):
        raise ValueError('invalid GCP billing account')
    if (not isinstance(binding, dict) or set(binding) != {'billingAccountName', 'billingEnabled', 'name', 'projectId'}
            or not isinstance(binding.get('projectId'), str)
            or not re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', binding['projectId'])
            or binding['billingEnabled'] is not True
            or binding['billingAccountName'] != 'billingAccounts/' + account
            or binding['name'] != 'projects/' + binding['projectId'] + '/billingInfo'):
        raise ValueError('GCP project billing linkage missing or inconsistent')
    expected_url = ('https://console.cloud.google.com/billing/' + account
                    + '/reports;grouping=GROUP_BY_PROJECT?project=' + binding['projectId'])
    if (not isinstance(metadata, dict)
            or set(metadata) != {'url', 'start', 'end', 'time_basis', 'group_by', 'filters', 'currency'}
            or metadata['url'] != expected_url or metadata['time_basis'] != 'usage_date'
            or metadata['group_by'] != 'project' or metadata['filters'] != {}
            or metadata['currency'] != 'KRW'):
        raise ValueError('GCP Console report must be unfiltered, project-grouped, account-wide KRW usage')
    if not isinstance(period, str) or not re.fullmatch(r'\d{4}-\d{2}', period):
        raise ValueError('period must be YYYY-MM')
    first = date.fromisoformat(period + '-01')
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if metadata['start'] != first.isoformat() or metadata['end'] != last.isoformat():
        raise ValueError('GCP Console report must cover the complete usage month')
    if not isinstance(snapshot['observed_at'], str) or not first <= timestamp(snapshot['observed_at']).date() <= last:
        raise ValueError('GCP Console observation must fall within the report month')
    for key in ('csv', 'browser_evidence'):
        if not isinstance(snapshot[key], str) or not snapshot[key].strip():
            raise ValueError('GCP Console original CSV and browser evidence required')
    header = ['프로젝트 이름', '프로젝트 ID', '프로젝트 번호', '비용(₩)', '절감 프로그램(₩)',
              '기타 절감(₩)', '반올림되지 않은 소계(₩)', '소계(₩)', '이전 기간 대비 소계 변동률']
    try:
        rows = list(csv.reader(io.StringIO(snapshot['csv'].lstrip('\ufeff')), strict=True))
    except csv.Error as exc:
        raise ValueError('malformed GCP Console CSV') from exc
    if not rows or rows[0] != header:
        raise ValueError('unsupported GCP Console CSV header or currency')

    def money(value):
        if not re.fullmatch(r'-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,6})?', value):
            raise ValueError('invalid GCP Console money')
        return amount(value.replace(',', ''))

    seen, footers, total, positive_total = set(), {}, Decimal(0), Decimal(0)
    for row in rows[1:]:
        if not row or not any(row):
            continue
        if len(row) in (8, 9) and not any(row[:5]):
            if row[5] not in {'소계', '세금', '합계'} or row[5] in footers or (len(row) == 9 and row[8]):
                raise ValueError('unknown or duplicate GCP Console footer')
            footers[row[5]] = money(row[6])
            money(row[7])
        else:
            if (footers or len(row) != 9 or not row[0] or not row[1]
                    or not re.fullmatch(r'[0-9]+', row[2]) or row[1] in seen):
                raise ValueError('malformed or duplicate GCP Console project row')
            seen.add(row[1])
            values = [money(value) for value in row[3:8]]
            total += values[3]
            positive_total += max(Decimal(0), values[3])
    if not seen or set(footers) != {'소계', '세금', '합계'}:
        raise ValueError('empty or incomplete GCP Console export is unknown, not zero spend')
    selected = max(Decimal(0), positive_total, footers['소계'], footers['합계'])
    # A negative project's credit cannot offset another project's known spend.
    difference = total - footers['소계']
    return [{'resource_id': 'gcp-billing-account:' + account, 'currency': 'KRW', 'cost': str(selected),
             'estimated': True, 'basis': 'account_wide_reported_estimate',
             'scope_binding': 'billing_account_link_verified', 'project_cost_attribution': 'not_project_attributed',
             'billing_account_id': account, 'binding_project_id': binding['projectId'],
             'report_metadata_basis': 'operator_browser_observation', 'report_url': expected_url,
             'coverage_start': metadata['start'], 'coverage_end': metadata['end'],
             'selection_policy': 'max_positive_project_rows_and_footer_totals',
             'reported_project_rows_sum': str(total), 'reported_positive_project_rows_sum': str(positive_total),
             'reported_subtotal': str(footers['소계']), 'reported_tax': str(footers['세금']),
             'reported_total': str(footers['합계']), 'rows_subtotal_difference': str(difference),
             'discrepancy': difference != 0 or footers['소계'] + footers['세금'] != footers['합계'],
             'csv_sha256': hashlib.sha256(snapshot['csv'].encode()).hexdigest(),
             'browser_evidence_sha256': hashlib.sha256(snapshot['browser_evidence'].encode()).hexdigest(),
             'project_billing_info_sha256': hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()}]


def normalize(provider, rows, period):
    if provider == 'aws':
        return normalize_aws(rows, period)
    if provider == 'gcp' and isinstance(rows, dict):
        return normalize_gcp_console(rows, period)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('export must be a complete JSON/CSV row list')
    if not re.fullmatch(r'\d{4}-\d{2}', period):
        raise ValueError('period must be YYYY-MM')
    datetime.strptime(period, '%Y-%m')
    totals = {}
    for row in rows:
        if provider == 'gcp':
            # Cloud Billing detailed export, BigQuery JSON output. Credits are signed.
            day = str(row['usage_start_time'])[:10]
            currency = row['currency']
            resource = (row.get('resource') or {}).get('global_name') or 'UNALLOCATED'
            cost = amount(row['cost']) + sum((amount(c['amount']) for c in (row.get('credits') or [])), Decimal(0))
        elif provider == 'azure':
            # ActualCost CSV: official EA uses BillingCurrencyCode, MCA BillingCurrency.
            fields = azure_fields(row)
            raw_day = fields['date']
            day = (datetime.strptime(raw_day, '%m/%d/%Y').strftime('%Y-%m-%d')
                   if '/' in raw_day else raw_day[:10])
            currency = fields['billingcurrency']
            resource = fields.get('resourceid') or 'UNALLOCATED'
            cost = amount(fields['costinbillingcurrency'])
        else:
            raise ValueError('unsupported billing export provider')
        datetime.strptime(day, '%Y-%m-%d')
        if not day.startswith(period + '-'):
            raise ValueError('export contains a different billing month')
        if not re.fullmatch('[A-Z]{3}', currency):
            raise ValueError('invalid currency')
        key = (resource, currency)
        totals[key] = totals.get(key, Decimal(0)) + cost
    if not totals:
        raise ValueError('empty export is unknown, not zero spend')
    return [{'resource_id': r, 'currency': c, 'cost': str(v)} for (r, c), v in sorted(totals.items())]


def import_snapshot(db, scope, provider, period, observed_at, rows, now=None, *, source_scope=None):
    now = now or datetime.now(timezone.utc)
    observed = timestamp(observed_at)
    if observed > now:
        raise ValueError('future observation')
    normalized = normalize(provider, rows, period)
    console = provider == 'gcp' and isinstance(rows, dict)
    if console:
        if source_scope is None:
            raise ValueError('GCP Console source scope required')
        if timestamp(rows['observed_at']) != observed:
            raise ValueError('GCP Console observation timestamp mismatch')
    if provider == 'aws':
        if source_scope is None:
            raise ValueError('AWS source scope required')
        if date.fromisoformat(normalized[0]['coverage_end']) < observed.date():
            raise ValueError('AWS query coverage is stale at observation time')
    if source_scope is not None:
        verify_source_scope(provider, rows, source_scope)
    payload = json.dumps(normalized, sort_keys=True)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    with db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT provider,period,observed_at,checksum,source_scope,rows_json FROM snapshots WHERE scope=?', (scope,)).fetchone()
        if previous and (previous[0:2] != (provider, period) or timestamp(previous[2]) > observed):
            raise ValueError('scope is bound to one provider/month; older snapshots are rejected')
        if previous and previous[4] is not None and previous[4] != source_scope:
            raise ValueError('source scope binding cannot change')
        if previous and console:
            previous_accounts = {r['billing_account_id'] for r in json.loads(previous[5]) if r.get('billing_account_id')}
            if previous_accounts and previous_accounts != {rows['billing_account_id']}:
                raise ValueError('GCP billing account binding cannot change')
        if previous and timestamp(previous[2]) == observed and previous[3] != digest:
            raise ValueError('conflicting snapshot at the same observation time')
        db.execute('INSERT OR REPLACE INTO snapshots (scope,provider,period,observed_at,checksum,rows_json,source_scope) VALUES (?,?,?,?,?,?,?)',
                   (scope, provider, period, observed.isoformat(), digest, payload, source_scope))
    basis = normalized[0].get('basis') or ('reported_estimate' if any(r.get('estimated') for r in normalized) else 'reported_actual')
    return {'scope': scope, 'basis': basis, 'checksum': digest,
            'observed_at': observed.isoformat(), 'resource_count': len(normalized), 'source_scope': source_scope}


def report(db, scope, *, now=None, max_age_hours=24):
    row = db.execute('SELECT provider,period,observed_at,rows_json,source_scope FROM snapshots WHERE scope=?', (scope,)).fetchone()
    if not row:
        return {'scope': scope, 'status': 'unknown', 'totals': {}}
    resources = json.loads(row[3])
    totals = {}
    for item in resources:
        c = item['currency']
        totals[c] = totals.get(c, Decimal(0)) + amount(item['cost'])
    held = {}
    for currency, value in db.execute("SELECT currency,amount FROM reservations WHERE scope=? AND state='held'", (scope,)):
        held[currency] = held.get(currency, Decimal(0)) + amount(value)
    age = ((now or datetime.now(timezone.utc)) - timestamp(row[2])).total_seconds() / 3600
    binding = ('query_verified' if row[0] == 'aws' else resources[0].get('scope_binding', 'row_verified')) if row[4] else 'operator_asserted'
    basis = resources[0].get('basis') or ('reported_estimate' if any(r.get('estimated') for r in resources) else 'reported_actual')
    return {'scope': scope, 'provider': row[0], 'period': row[1], 'observed_at': row[2],
            'source_scope': row[4], 'scope_binding': binding,
            'freshness': 'fresh' if 0 <= age <= max_age_hours else 'stale',
            'age_hours': age, 'max_age_hours': max_age_hours,
            'status': 'reported', 'basis': basis,
            'totals': {c: str(v) for c, v in totals.items()},
            'reserved_estimate': {c: str(v) for c, v in held.items()}, 'resources': resources}


def reserve(db, *, scope, operation_id, currency, incremental_cost, limit,
            unreported_cost, max_age_hours=24, now=None):
    """Conservative hold. Caller supplies reviewed incremental quote and metering gap.

    Reservations never auto-expire: reconcile provider completion before releasing.
    A held estimate may overlap reported spend; this intentionally overcounts.
    """
    now = now or datetime.now(timezone.utc)
    values = [amount(v) for v in (incremental_cost, limit, unreported_cost)]
    if any(v < 0 for v in values) or not re.fullmatch('[A-Z]{3}', currency) or not (0 < max_age_hours <= 72):
        raise ValueError('invalid budget input')
    request = json.dumps([scope, currency, *map(str, values), max_age_hours])
    digest = hashlib.sha256(request.encode()).hexdigest()
    with db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT request_hash,state FROM reservations WHERE operation_id=?', (operation_id,)).fetchone()
        if old:
            if old[0] != digest or old[1] != 'held':
                raise ValueError('idempotency conflict or already released operation')
        current = report(db, scope, now=now, max_age_hours=max_age_hours)
        if current['status'] == 'unknown':
            raise ValueError('billing snapshot missing')
        if current['scope_binding'] not in {'row_verified', 'query_verified', 'billing_account_link_verified'}:
            raise ValueError('billing source scope unverified')
        if current['period'] != now.strftime('%Y-%m'):
            raise ValueError('billing snapshot is not the current usage month')
        age = (now - timestamp(current['observed_at'])).total_seconds()
        if age < 0 or age > max_age_hours * 3600:
            raise ValueError('billing snapshot stale')
        if set(current['totals']) != {currency} or set(current['reserved_estimate']) - {currency}:
            raise ValueError('currency mismatch; explicit reviewed FX conversion required')
        # Recheck freshness and limits even for a held idempotent retry. Its own hold is already included.
        projected = max(Decimal(0), amount(current['totals'][currency])) + amount(current['reserved_estimate'].get(currency, 0)) + (Decimal(0) if old else values[0]) + values[2]
        if projected > values[1]:
            raise ValueError('budget exceeded')
        if old:
            return {'operation_id': operation_id, 'state': 'held', 'duplicate': True}
        db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?,?)',
                   (operation_id, digest, scope, currency, str(values[0]), now.isoformat(), 'held'))
    return {'operation_id': operation_id, 'state': 'held', 'projected': str(projected), 'currency': currency}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db', required=True)
    sub = p.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import')
    imp.add_argument('--provider', choices=['aws', 'gcp', 'azure'], required=True)
    imp.add_argument('--scope', required=True)
    imp.add_argument('--period', required=True)
    imp.add_argument('--observed-at', required=True)
    imp.add_argument('--source-scope', required=True, help='AWS account, GCP project or Azure subscription; the query/export must match')
    imp.add_argument('export')
    rep = sub.add_parser('report'); rep.add_argument('--scope', required=True)
    res = sub.add_parser('reserve')
    for key in ('scope', 'operation-id', 'currency', 'incremental-cost', 'limit', 'unreported-cost'):
        res.add_argument('--' + key, required=True)
    rel = sub.add_parser('release'); rel.add_argument('--operation-id', required=True)
    args = p.parse_args(); db = ledger(args.db)
    if args.command == 'import':
        with Path(args.export).open(encoding='utf-8-sig') as f:
            rows = json.load(f, parse_float=Decimal) if args.provider in {'aws', 'gcp'} else list(csv.DictReader(f))
        result = import_snapshot(db, args.scope, args.provider, args.period, args.observed_at, rows, source_scope=args.source_scope)
    elif args.command == 'report':
        result = report(db, args.scope)
    elif args.command == 'reserve':
        result = reserve(db, **{k: getattr(args, k) for k in ('scope','operation_id','currency','incremental_cost','limit','unreported_cost')})
    else:
        with db:
            changed = db.execute("UPDATE reservations SET state='released' WHERE operation_id=? AND state='held'", (args.operation_id,)).rowcount
        result = {'operation_id': args.operation_id, 'released': bool(changed)}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
