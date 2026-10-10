#!/usr/bin/env python3
"""Refresh one AWS operator budget from complete CE and current Price List evidence.

This writes the existing authoritative ledger, never reserves or applies resources.
Retained storage defaults to month end. Explicit 24 hours is an operator's test
cleanup assumption, not a storage deletion or lifetime-spend guarantee.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING

import costs
from capabilities import validate_profile, timestamp

REPO = Path(__file__).resolve().parents[3]
BUDGET_KEYS = {'ledger_path', 'scope', 'currency', 'incremental_cost', 'limit', 'unreported_cost', 'quoted_at', 'expires_at'}


class BudgetError(ValueError):
    pass


def require(condition, code='AWS_BUDGET_INPUT_INVALID'):
    if not condition:
        raise BudgetError(code)


def private_file(path):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and path.resolve() == path and REPO not in path.parents
            and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077 and info.st_size > 0, 'PRIVATE_FILE_INVALID')
    return info


def private_directory(path):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and path.resolve() == path and REPO not in path.parents
            and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, 'PRIVATE_DIRECTORY_INVALID')


def save(path, value):
    data = (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}


def aws(args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('AWS_ENDPOINT_URL')}
    env.update(AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off')
    result = subprocess.run(['aws', *args, '--output', 'json', '--no-cli-pager', '--no-paginate'],
                            env=env, capture_output=True, text=True, timeout=90)
    require(result.returncode == 0 and len(result.stdout) <= 8 * 1024 * 1024, 'AWS_BUDGET_OBSERVATION_FAILED')
    value = json.loads(result.stdout)
    require(isinstance(value, dict), 'AWS_BUDGET_OBSERVATION_INVALID')
    return value


def money(value):
    require(not isinstance(value, bool))
    value = costs.amount(value)
    require(value >= 0)
    return value


def validate(value):
    require(isinstance(value, dict) and set(value) <= {'version', 'targets', 'retained_storage_hours'}
            and value.get('version') == 1 and type(value['version']) is int)
    if 'retained_storage_hours' in value:
        require(type(value['retained_storage_hours']) is int and value['retained_storage_hours'] == 24)
    targets = value.get('targets')
    require(isinstance(targets, list) and 1 <= len(targets) <= 257)
    ids, policy = set(), None
    for target in targets:
        require(isinstance(target, dict) and not set(target) - {'schema_version', 'provider_kind', 'target_id',
                'execution_driver', 'owner_ref', 'variables', 'profile', 'budget'}
                and target.get('schema_version') == 'v1' and target.get('provider_kind') == 'aws'
                and target.get('execution_driver', 'terraform') == 'terraform')
        target_id, variables, budget = target.get('target_id'), target.get('variables'), target.get('budget')
        require(isinstance(target_id, str) and re.fullmatch('[a-z][a-z0-9-]{2,39}', target_id) and target_id not in ids)
        ids.add(target_id)
        require(isinstance(variables, dict) and variables.get('target_id') == target_id)
        validate_profile(target)
        account, region = variables.get('account_id'), variables.get('region')
        require(isinstance(account, str) and re.fullmatch('[0-9]{12}', account)
                and isinstance(region, str) and re.fullmatch('[a-z]{2}(?:-[a-z]+)+-\d', region))
        for key, low, high in [('max_run_duration_seconds', 1800, 604800), ('root_volume_gb', 1, 16384), ('data_disk_gib', 20, 1000)]:
            require(type(variables.get(key)) is int and low <= variables[key] <= high)
        require(type(variables.get('allocate_eip', True)) is bool)
        if 'retained_storage_hours' in value:
            require(variables['max_run_duration_seconds'] <= 24 * 3600)
        require(isinstance(budget, dict) and set(budget) == BUDGET_KEYS and budget['currency'] == 'USD')
        require(isinstance(budget['scope'], str) and re.fullmatch('aws/' + account + r'/\d{4}-\d{2}', budget['scope']))
        money(budget['incremental_cost'])
        timestamp(budget['quoted_at']); timestamp(budget['expires_at'])
        selected = (account, budget['ledger_path'], budget['currency'], money(budget['limit']), money(budget['unreported_cost']))
        require(policy is None or policy == selected, 'AWS_BUDGET_POLICY_MISMATCH')
        policy = selected
    path = Path(policy[1])
    info = private_file(path)
    # Do not call costs.ledger until a real, populated authority was verified.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        require(db.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'BILLING_LEDGER_INVALID')
        require({'snapshots', 'reservations'} <= {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}, 'BILLING_LEDGER_INVALID')
        require(db.execute("SELECT 1 FROM snapshots WHERE provider='aws' AND source_scope=? LIMIT 1", (policy[0],)).fetchone(), 'BILLING_AUTHORITY_MISSING')
    return policy, (info.st_dev, info.st_ino)


def pages(call, args, token_key, flag, records_key):
    rows, evidence, seen = [], [], set()
    for _ in range(32):
        response = call(args)
        require(isinstance(response, dict) and isinstance(response.get(records_key), list), 'AWS_BUDGET_OBSERVATION_INVALID')
        evidence.append({'arguments': args, 'response': response})
        rows.extend(response[records_key])
        token = response.get(token_key)
        if not token:
            return rows, evidence
        require(isinstance(token, str) and token not in seen and len(token) <= 16384, 'AWS_BUDGET_PAGINATION_INVALID')
        seen.add(token)
        base = args[:args.index(flag)] if flag in args else args
        args = [*base, flag, token]
    raise BudgetError('AWS_BUDGET_PAGINATION_LIMIT')


def price(products, attributes, family, unit, now):
    matches = []
    for raw in products:
        product = json.loads(raw) if isinstance(raw, str) else raw
        info = product.get('product', {})
        if (family is not None and info.get('productFamily') != family) or any(info.get('attributes', {}).get(k) != v for k, v in attributes.items()):
            continue
        terms = [term for term in product.get('terms', {}).get('OnDemand', {}).values()
                 if timestamp(term['effectiveDate']) <= now]
        require(bool(terms), 'AWS_PRICE_MISSING')
        latest = max(timestamp(term['effectiveDate']) for term in terms)
        current = [term for term in terms if timestamp(term['effectiveDate']) == latest]
        require(len(current) == 1 and current[0].get('sku') == info.get('sku'), 'AWS_PRICE_AMBIGUOUS')
        dimensions = list(current[0].get('priceDimensions', {}).values())
        require(len(dimensions) == 1, 'AWS_PRICE_AMBIGUOUS')
        dimension = dimensions[0]
        require(dimension.get('unit') == unit and dimension.get('beginRange') == '0'
                and dimension.get('endRange') == 'Inf' and not dimension.get('appliesTo')
                and set(dimension.get('pricePerUnit', {})) == {'USD'}, 'AWS_PRICE_INVALID')
        rate = money(dimension['pricePerUnit']['USD'])
        require(rate > 0, 'AWS_PRICE_INVALID')
        matches.append(rate)
    require(len(matches) == 1, 'AWS_PRICE_MISSING_OR_AMBIGUOUS')
    return matches[0]


def refresh(value, evidence_dir, output, *, call=aws, clock=lambda: datetime.now(timezone.utc)):
    policy, ledger_identity = validate(value)
    account, ledger_path, currency, limit, unreported = policy
    evidence_dir, output = Path(evidence_dir), Path(output)
    private_directory(evidence_dir.parent)
    evidence_dir.mkdir(mode=0o700, exist_ok=True)
    private_directory(evidence_dir); private_directory(output.parent)
    require(not any(evidence_dir.iterdir()) and not output.exists() and not output.is_symlink(), 'BUDGET_OUTPUT_EXISTS')
    started = clock().astimezone(timezone.utc)
    identity = call(['sts', 'get-caller-identity', '--region', 'us-east-1'])
    require(identity.get('Account') == account, 'AWS_BUDGET_ACCOUNT_MISMATCH')
    first = started.date().replace(day=1)
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    request = {'TimePeriod': {'Start': first.isoformat(), 'End': (started.date() + timedelta(days=1)).isoformat()},
               'Granularity': 'DAILY', 'Metrics': ['UnblendedCost'],
               'Filter': {'Dimensions': {'Key': 'LINKED_ACCOUNT', 'Values': [account]}}}
    rows, ce_evidence = pages(call, ['ce', 'get-cost-and-usage', '--region', 'us-east-1', '--cli-input-json', json.dumps(request)],
                             'NextPageToken', '--next-page-token', 'ResultsByTime')
    require(all(not page['response'].get('GroupDefinitions') for page in ce_evidence), 'AWS_BILLING_GROUPED')
    snapshot = {'account_id': account, 'request': request, 'response': {'ResultsByTime': rows}}
    period, scope = started.strftime('%Y-%m'), 'aws/' + account + '/' + started.strftime('%Y-%m')
    normalized = costs.normalize('aws', snapshot, period)  # Gaps and incomplete coverage fail before any ledger write.
    require({row['currency'] for row in normalized} == {currency}, 'BILLING_CURRENCY_MISMATCH')
    raw_evidence = [('identity', identity), ('billing-pages', ce_evidence), ('billing-snapshot', snapshot)]
    cache = {}

    def get_prices(service, attributes, family=None):
        filters = dict(attributes, **({'productFamily': family} if family else {}))
        key = json.dumps([service, filters], sort_keys=True)
        if key not in cache:
            filters = [{'Type': 'TERM_MATCH', 'Field': name, 'Value': value} for name, value in filters.items()]
            products, observed = pages(call, ['pricing', 'get-products', '--region', 'us-east-1', '--service-code', service,
                                              '--filters', json.dumps(filters), '--max-results', '100'],
                                       'NextToken', '--next-token', 'PriceList')
            raw_evidence.append(('prices-' + str(len(cache)), observed))
            cache[key] = products
        return cache[key]

    month_hours = Decimal((next_month - first).days * 24)
    # Cost Explorer charges per paginated API request; the fresh snapshot may not include this call yet.
    ce_request_cost = Decimal('0.01') * len(ce_evidence)
    ce_cost_per_node = ce_request_cost / len(value['targets'])
    retained = (Decimal(value['retained_storage_hours']) if 'retained_storage_hours' in value else
                Decimal(str((datetime.combine(next_month, datetime.min.time(), timezone.utc) - started).total_seconds())) / 3600)
    refreshed, estimates = copy.deepcopy(value['targets']), []
    for target in refreshed:
        variables = target['variables']; region = variables['region']
        attributes = {'regionCode': region, 'instanceType': variables['instance_type'], 'operatingSystem': 'Linux',
                      'tenancy': 'Shared', 'preInstalledSw': 'NA', 'capacitystatus': 'Used', 'marketoption': 'OnDemand'}
        compute = price(get_prices('AmazonEC2', attributes, 'Compute Instance'), attributes, 'Compute Instance', 'Hrs', started)
        attributes = {'regionCode': region, 'volumeApiName': 'gp3'}
        storage = price(get_prices('AmazonEC2', attributes, 'Storage'), attributes, 'Storage', 'GB-Mo', started)
        attributes = {'regionCode': region, 'group': 'VPCPublicIPv4Address'}
        products = get_prices('AmazonVPC', attributes)
        # AmazonVPC IPv4 products have no productFamily; identify the observed usage suffix instead.
        parsed = [json.loads(row) if isinstance(row, str) else row for row in products]
        in_use = [row for row in parsed if row.get('product', {}).get('attributes', {}).get('usagetype', '').endswith('PublicIPv4:InUseAddress')]
        ipv4 = price(in_use, attributes, None, 'Hrs', started)
        hours = Decimal(variables['max_run_duration_seconds']) / 3600
        storage_hours = max(retained, hours)
        address_hours = hours
        if variables.get('allocate_eip', True):
            idle = [row for row in parsed if row.get('product', {}).get('attributes', {}).get('usagetype', '').endswith('PublicIPv4:IdleAddress')]
            ipv4 = max(ipv4, price(idle, attributes, None, 'Hrs', started))
            address_hours = storage_hours
        estimate = compute * hours + storage * (variables['root_volume_gb'] + variables['data_disk_gib']) * storage_hours / month_hours + ipv4 * address_hours + ce_cost_per_node
        quote = max(Decimal(1), money(target['budget']['incremental_cost']), estimate).to_integral_value(rounding=ROUND_CEILING)
        target['budget'].update(scope=scope, incremental_cost=format(quote, 'f'))
        estimates.append({'target_id': target['target_id'], 'compute_hours': format(hours, 'f'),
                          'retained_storage_hours': format(storage_hours, 'f'), 'ipv4_hours': format(address_hours, 'f'),
                          'ce_query_share': format(ce_cost_per_node, 'f'),
                          'raw_estimate': format(estimate, 'f'), 'incremental_quote': format(quote, 'f')})
    observed = clock().astimezone(timezone.utc)
    require(observed.date() == started.date() and observed >= started, 'AWS_BUDGET_DATE_CHANGED')
    for target in refreshed:
        target['budget'].update(quoted_at=observed.isoformat(), expires_at=(observed + timedelta(hours=2)).isoformat())
    raw_evidence.append(('estimate', {'observed_at': observed.isoformat(), 'estimates': estimates,
        'ce_request_cost': format(ce_request_cost, 'f'), 'ce_request_count': len(ce_evidence),
        'ce_price_source': 'https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/',
        'storage_horizon': 'explicit_operator_test_cleanup_24h' if 'retained_storage_hours' in value else 'month_end',
        'ipv4_assumption': 'one_public_ipv4_per_node_conservative',
        'limitations': ['not_a_lifetime_spend_cap', 'retained_disks_and_EIPs_require_operator_cleanup',
                        'guest_timer_stop_requires_verification', 'transfer_tax_and_other_usage_not_itemized']}))
    evidence = [save(evidence_dir / (str(index).zfill(2) + '-' + name + '.json'), payload)
                for index, (name, payload) in enumerate(raw_evidence)]
    info = private_file(ledger_path)
    require((info.st_dev, info.st_ino) == ledger_identity, 'BILLING_AUTHORITY_CHANGED')
    with closing(costs.ledger(ledger_path)) as db:
        costs.import_snapshot(db, scope, 'aws', period, observed.isoformat(), snapshot, now=observed, source_scope=account)
        current = costs.report(db, scope, now=observed)
    require(set(current['totals']) == {currency} and not set(current['reserved_estimate']) - {currency}, 'BILLING_CURRENCY_MISMATCH')
    incremental = sum((money(target['budget']['incremental_cost']) for target in refreshed), Decimal(0))
    projected = max(Decimal(0), costs.amount(current['totals'][currency])) + money(current['reserved_estimate'].get(currency, 0)) + unreported + incremental
    summary = {'currency': currency, 'incremental_estimate': format(incremental, 'f'), 'projected_total': format(projected, 'f'),
               'limit': format(limit, 'f'), 'within_budget': projected <= limit}
    save(output, {'version': 1, 'targets': refreshed, 'summary': summary, 'evidence': evidence})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        require(private_file(args.input).st_size <= 4 * 1024 * 1024)
        summary = refresh(json.loads(args.input.read_text()), args.evidence_dir, args.output)
        print(json.dumps(summary))
    except (ValueError, OSError, KeyError, TypeError, AttributeError, sqlite3.Error, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, BudgetError) else 'AWS_BUDGET_REFRESH_FAILED'
        print(json.dumps({'error': {'code': code, 'outcome': 'BLOCKED'}}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
