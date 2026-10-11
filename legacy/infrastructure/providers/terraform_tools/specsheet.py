#!/usr/bin/env python3
"""Render nonsecret Terraform NodeDescriptor JSON as JSON/Markdown; no cloud calls.

Configuration is not a live observation. Missing sizes and prices remain unknown.
"""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import sys

from costs import amount, timestamp


def text(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError('invalid descriptor text field')
    return value


def positive_number(value):
    if value is not None and (type(value) not in (int, float) or not 0 < value < 10**9):
        raise ValueError('invalid descriptor capacity')
    return value


def address(value):
    return str(ipaddress.ip_address(value)) if value is not None else None


def cost_summary(report, descriptor, scope, now, max_age_hours):
    unknown = {'status': 'unknown', 'totals': {}, 'reason': 'No bound billing report supplied',
               'node_attributed_cost': None, 'list_price_estimate': None}
    if report is None or report.get('status') == 'unknown':
        return unknown
    if report.get('status') != 'reported' or report.get('basis') != 'reported_actual':
        return {**unknown, 'reason': 'Input is not a reported actual-cost snapshot'}
    if not scope or report.get('scope') != scope or report.get('provider') != descriptor['provider_kind']:
        return {**unknown, 'reason': 'Billing scope/provider was not explicitly bound to this report'}
    if report.get('scope_binding') != 'row_verified' or not report.get('source_scope'):
        return {**unknown, 'reason': 'Billing export source scope is unverified'}
    scope_field = {'gcp': 'project_id', 'azure': 'subscription_id'}.get(descriptor['provider_kind'])
    source = (descriptor.get('location') or {}).get(scope_field)
    if not scope_field or not source or source.casefold() != str(report['source_scope']).casefold():
        return {**unknown, 'reason': 'Billing source scope is unsupported, missing or different from this node'}
    totals = report.get('totals')
    if not isinstance(totals, dict) or not totals:
        return {**unknown, 'reason': 'Empty billing totals do not establish zero cost'}
    def currencies(values):
        result = {}
        for currency, value in values.items():
            if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
                raise ValueError('invalid cost currency')
            result[currency] = str(amount(value))
        return result
    observed = timestamp(report['observed_at'])
    age = (now - observed).total_seconds() / 3600
    if age < 0:
        raise ValueError('future billing observation')
    return {
        'status': 'reported' if age <= max_age_hours else 'stale',
        'basis': 'reported_actual', 'scope': text(scope), 'source_scope': text(report['source_scope']),
        'period': text(report.get('period')), 'observed_at': observed.isoformat(),
        'age_hours': age, 'max_age_hours': max_age_hours, 'totals': currencies(totals),
        'reserved_estimate': currencies(report.get('reserved_estimate', {})),
        'node_attributed_cost': None, 'list_price_estimate': None,
        'attribution': 'Billing scope total; not the price of this VM. Retained resources may be included.',
        'collection': 'manual_export_import', 'fx_conversion': 'not_applied',
    }


def build(descriptor, *, cost_report=None, cost_scope=None, now=None, max_age_hours=24):
    if not isinstance(descriptor, dict):
        raise ValueError('descriptor must be an object')
    # Also accept `terraform output -json`, but never carry other outputs into this sheet.
    if 'node_descriptor' in descriptor:
        descriptor = descriptor['node_descriptor']['value']
    if descriptor.get('schema_version') != 'v1' or descriptor.get('provider_kind') not in {'aws', 'gcp', 'azure'}:
        raise ValueError('supported NodeDescriptor schema is v1 for aws/gcp/azure')
    if not 0 < max_age_hours <= 72:
        raise ValueError('max_age_hours must be greater than 0 and at most 72')
    for required in ('target_id', 'resource_id', 'execution_driver', 'owner_ref'):
        if not text(descriptor.get(required)):
            raise ValueError('required descriptor identity is missing')
    now = now or datetime.now(timezone.utc)
    compute = descriptor.get('compute') or {}
    disk = descriptor.get('data_disk') or descriptor.get('storage') or {}
    addresses = descriptor.get('addresses') or {}
    bootstrap = descriptor.get('bootstrap') or {}
    gitops = descriptor.get('gitops') or {}
    location = descriptor.get('location') or {'region': descriptor.get('region')}
    architecture = descriptor.get('architecture')
    runtime = descriptor.get('runtime_limit')
    if runtime is not None:
        guest_timer = runtime.get('implementation') == 'guest_systemd_timer'
        if runtime.get('instance_termination_action') != 'STOP' or (not guest_timer and runtime.get('automatic_restart') is not False):
            raise ValueError('only the implemented STOP runtime policy is supported')
        runtime = {'seconds': positive_number(runtime.get('seconds')), 'action': 'STOP',
                   'automatic_restart': None if guest_timer else False,
                   'implementation': 'guest_systemd_timer' if guest_timer else 'provider_scheduling',
                   'verification': 'unverified', 'resets_on_start': True}
    sheet = {
        'schema_version': 'v1', 'document': 'node_specsheet', 'generated_at': now.isoformat(),
        'source': 'terraform_node_descriptor', 'live_verification': 'unverified',
        'target_id': descriptor['target_id'], 'provider_kind': descriptor['provider_kind'],
        'resource_id': descriptor['resource_id'], 'instance_id': text(descriptor.get('instance_id')),
        'owner_ref': descriptor['owner_ref'], 'execution_driver': descriptor['execution_driver'],
        'location': {k: text(location.get(k)) for k in ('account_id', 'project_id', 'subscription_id', 'region', 'zone')},
        'compute': {
            'machine_type': text(compute.get('machine_type')), 'source': text(compute.get('source')) or 'unknown',
            'vcpu': positive_number(compute.get('vcpu')), 'memory_mib': positive_number(compute.get('memory_mib')),
            'architecture': 'x86_64' if architecture in {'amd64', 'x86_64'} else text(architecture),
            'image_ref': text(descriptor.get('image_ref')), 'power_status': 'unknown',
        },
        'addresses': {
            'private': address(addresses.get('private', addresses.get('private_ipv4'))),
            'public': address(addresses.get('public', addresses.get('public_ipv4'))),
        },
        'transport_ref': text(descriptor.get('transport_ref')),
        'data_disk': {
            'resource_id': text(disk.get('resource_id', disk.get('data_disk_id'))),
            'size_gib': positive_number(disk.get('size_gib')), 'mount_path': text(disk.get('mount_path')),
            'preservation': text(disk.get('preservation', disk.get('retention'))) or 'unknown',
            'mount_verification': 'unverified', 'restore_verification': 'unverified',
        },
        'bootstrap': {
            'profile': text(bootstrap.get('profile')),
            'revision': text(bootstrap.get('revision', bootstrap.get('source_revision'))),
            'bundle_sha256': text(bootstrap.get('bundle_sha256')),
            'configuration_status': text(bootstrap.get('status', bootstrap.get('readiness'))) or 'unknown',
            'runtime_verification': 'not_configured' if (descriptor.get('runtime') or {}).get('readiness') == 'not_configured' else 'unverified',
        },
        'gitops': {'repo': text(gitops.get('repo', gitops.get('repository'))),
                   'path': text(gitops.get('path')), 'revision': text(gitops.get('revision'))},
        'runtime_limit': runtime,
        'host_egress': {
            'profile': text((descriptor.get('host_egress') or {}).get('profile')),
            'runtime_verification': 'unverified', 'tenant_isolation': 'separate_guest_policy_required',
        },
        'capabilities': {
            'node_provisioning': {'implementation': 'terraform_module', 'verification': 'unverified'},
            'data_retention': {'implementation': 'separate_disk_and_terraform_guards', 'verification': 'unverified'},
            'app_replica_autoscaling': {'implementation': 'team_runtime_not_configured', 'verification': 'unverified'},
            'node_scale_out': {'implementation': 'not_implemented', 'verification': 'not_run'},
            'vm_resize': {'implementation': 'manual_terraform_plan_maintenance', 'verification': 'unverified'},
            'cloud_cost_collector': {'implementation': 'not_implemented', 'verification': 'not_run'},
            'budget_approval_integration': {'implementation': 'not_implemented', 'verification': 'not_run'},
        },
        'cost': cost_summary(cost_report, descriptor, cost_scope, now, max_age_hours),
    }
    return sheet


def markdown(sheet):
    def cell(value):
        if value is None:
            return 'unknown'
        return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '\\|').replace('`', '\\`').replace('\n', ' ')
    compute, disk, cost = sheet['compute'], sheet['data_disk'], sheet['cost']
    rows = [
        ('Target / provider', f"{sheet['target_id']} / {sheet['provider_kind']}"),
        ('Instance', sheet['instance_id']), ('Source / live verification', 'Terraform output / unverified'),
        ('Machine type', compute['machine_type']), ('vCPU', compute['vcpu']), ('Memory MiB', compute['memory_mib']),
        ('Compute source', compute['source']), ('Architecture', compute['architecture']),
        ('Region', sheet['location']['region']), ('Zone', sheet['location']['zone']),
        ('Private IP', sheet['addresses']['private']), ('Public IP', sheet['addresses']['public']),
        ('Transport', sheet['transport_ref']), ('Power observation', compute['power_status']),
        ('Data disk', disk['resource_id']), ('Data GiB', disk['size_gib']), ('Data mount', disk['mount_path']),
        ('Data policy / restore', f"{disk['preservation']} / unverified"),
        ('Bootstrap revision', sheet['bootstrap']['revision']), ('GitOps path', sheet['gitops']['path']),
        ('Runtime readiness', sheet['bootstrap']['runtime_verification']),
        ('Runtime limit', f"{sheet['runtime_limit']['seconds']} seconds / STOP / unverified" if sheet['runtime_limit'] else 'not configured'),
    ]
    lines = ['# Node specification', '', '| Field | Value |', '|---|---|']
    lines += [f'| {key} | {cell(value)} |' for key, value in rows]
    lines += ['', 'Configuration does not establish current power, readiness, mount, or restore health.', '',
              '| Capability | Implementation | Live verification |', '|---|---|---|']
    lines += [f"| {name} | {cap['implementation']} | {cap['verification']} |" for name, cap in sheet['capabilities'].items()]
    lines += ['', f"Billing status: **{cost['status']}**. VM-attributed cost and list-price estimate: **unknown**.", '']
    if cost['totals']:
        lines += [f"Scope: {cell(cost['scope'])}; period: {cell(cost['period'])}; observed: {cell(cost['observed_at'])}.",
                  '', '| Currency | Reported scope total | Held estimate |', '|---|---|---|']
        lines += [f"| {currency} | {cell(value)} | {cell(cost['reserved_estimate'].get(currency, '0'))} |" for currency, value in cost['totals'].items()]
        lines += ['', cost['attribution'], 'Currencies are separate; no FX conversion. Billing may arrive late or be corrected.']
    else:
        lines += [cost['reason']]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('descriptor', type=Path)
    parser.add_argument('--cost-report', type=Path)
    parser.add_argument('--cost-scope', help='Explicit ledger scope; scope totals are not VM-only costs')
    parser.add_argument('--max-age-hours', type=float, default=24)
    parser.add_argument('--format', choices=['json', 'markdown'], default='json')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        sheet = build(json.loads(args.descriptor.read_text()),
                      cost_report=json.loads(args.cost_report.read_text()) if args.cost_report else None,
                      cost_scope=args.cost_scope, max_age_hours=args.max_age_hours)
        rendered = json.dumps(sheet, indent=2, ensure_ascii=False) + '\n' if args.format == 'json' else markdown(sheet)
        if args.output:
            args.output.write_text(rendered)
        else:
            print(rendered, end='')
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        print('Invalid descriptor or billing input; no specification was produced.', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
