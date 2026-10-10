#!/usr/bin/env python3
"""Private, single-writer app route allocation and saved-plan execution for aws-edge."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from handoff import require, http_path
from service_name import service_name
from storage import durable_write


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read_private(path):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
            not info.st_mode & 0o077 and info.st_size < 2_000_000, 'private owned JSON file required')
    return json.loads(path.read_bytes())


def config_at(path):
    config = read_private(path)
    require(set(config) == {'version', 'state_dir', 'terraform_dir', 'variables_file', 'auto_apply'} and
            config['version'] == 1 and type(config['auto_apply']) is bool, 'edge config v1 required')
    for key in ('state_dir', 'terraform_dir', 'variables_file'):
        require(isinstance(config[key], str) and Path(config[key]).is_absolute(), 'absolute operator paths required')
    config['_sha256'] = digest(config)
    return config


@contextmanager
def locked(config):
    root = Path(config['state_dir'])
    require(not root.is_symlink(), 'regular edge state directory required')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.stat()
    require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'private edge state directory required')
    # ponytail: one local writer for the shared ALB; move this state/lock with the sole executor.
    with os.fdopen(os.open(root / 'edge.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield root


def base_values(config, *, writable=False):
    values = read_private(config['variables_file'])
    require(values.get('base_domain') == 'railshot.io' and isinstance(values.get('routes'), dict),
            'existing railshot.io route map required')
    if writable:
        require(all(route.get('provider_kind') == 'aws' for route in values['routes'].values()) and
                not any(key.startswith('wireguard_') for key in values),
                'complete the reviewed GCP/WireGuard migration before AWS edge writes')
    return values


def free_number(seed, start, end, used):
    for offset in range(end - start + 1):
        candidate = start + (seed + offset) % (end - start + 1)
        if candidate not in used:
            return candidate
    raise ValueError('edge allocation capacity exhausted')


def without_health(value):
    return {k: v for k, v in value.items() if k != 'health_path'}


def prepare(config_path, request):
    """Only the environment registrar calls this with its private provider/profile snapshot."""
    config = config_at(config_path)
    fields = {'target_id', 'tenant', 'app', 'environment_id', 'provider_kind', 'target_private_ip',
              'namespace', 'health_path', 'expires_at'}
    require(isinstance(request, dict) and fields <= set(request) <= fields | {
        'target_security_group_id', 'expected_json', 'expected_status', 'node_port', 'manage_dns'},
            'exact registered route input required')
    for key in ('target_id', 'app', 'namespace'):
        require(isinstance(request[key], str) and re.fullmatch(r'[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]', request[key]),
                'registered DNS identity required')
    require(isinstance(request['tenant'], str) and re.fullmatch(r'[a-z0-9]{1,20}', request['tenant']),
            'registered CI tenant required')
    require(isinstance(request['environment_id'], str) and
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', request['environment_id']), 'registered environment identity required')
    require(request['namespace'] not in ('default', 'kube-system', 'kube-public', 'kube-node-lease', 'argocd'),
            'dedicated registered namespace required')
    require(request['provider_kind'] == 'aws', 'AWS target required; GCP must use its native L7 entrypoint')
    address = ipaddress.ip_address(request['target_private_ip'])
    require(address.version == 4 and any(address in ipaddress.ip_network(c) for c in
            ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')), 'RFC1918 target required')
    sg = request.get('target_security_group_id')
    require(isinstance(sg, str) and re.fullmatch(r'sg-[0-9a-f]{8,17}', sg), 'AWS target security group required')
    http_path(request['health_path'])
    require(('expected_json' in request) != ('expected_status' in request), 'one app health contract required')
    if 'expected_json' in request:
        require(isinstance(request['expected_json'], dict) and len(encoded(request['expected_json'])) <= 8192,
                'bounded app health contract required')
    else:
        require(type(request['expected_status']) is int and request['expected_status'] == 200, 'exact HTTP 200 contract required')
    require(type(request.get('manage_dns', True)) is bool, 'explicit DNS ownership required')
    if 'node_port' in request:
        require(type(request['node_port']) is int and 30000 <= request['node_port'] <= 32767, 'registered NodePort required')
    require(isinstance(request['expires_at'], str), 'resource expiry required')
    expires = datetime.fromisoformat(request['expires_at'].replace('Z', '+00:00'))
    require(expires.tzinfo is not None and expires > datetime.now(timezone.utc), 'future owned resource expiry required')
    identity = {key: request[key] for key in ('tenant', 'app', 'environment_id')}
    key = 'app-' + digest(identity)[:24]
    with locked(config) as root:
        lifecycle_ready(root)
        values = base_values(config, writable=True)
        ledger_path = root / 'allocations.json'
        ledger = read_private(ledger_path) if ledger_path.exists() else {}
        if key in ledger:
            allocation = root / (key + '.json')
            if not allocation.exists():  # Recover a crash between reservation and writing its reference.
                durable_write(allocation, encoded(ledger[key]))
            reference = {'config_path': str(config_path), 'allocation_path': str(allocation),
                         'config_sha256': config['_sha256']}
            _, current = load(reference)
            require(without_health(current['request']) == without_health(request),
                    'service identity belongs to another target or contract')
            if current['request'] != request:
                require(current['phase'] == 'applied', 'observe unfinished route before changing health contract')
                # The reservation remains immutable; one atomic journal write records
                # the new desired health path and the previously applied route.
                current = {**current, 'request': request, 'previous_route': current['route'],
                           'route': {**current['route'], 'health_path': request['health_path']},
                           'public_http': {**current['public_http'],
                               'url': 'https://' + current['hostname'] + request['health_path']},
                           'phase': 'reserved'}
                for field in ('plan_sha256', 'changes', 'verification'):
                    current.pop(field, None)
                save(config, current)
            return {**current, 'reference': reference}
        require(len(values['routes']) + len(ledger) < 50, 'shared edge route capacity exhausted')
        routes = [*values['routes'].values(), *(row['route'] for row in ledger.values())]
        require(not any(row['request']['target_private_ip'] == request['target_private_ip'] and
                        row['request']['namespace'] == request['namespace'] and row['request']['app'] == request['app']
                        for row in ledger.values()), 'application namespace already allocated to another environment')
        name = service_name(request['app'], request['tenant'], request['environment_id'], values['base_domain'])
        require(key not in values['routes'] and name['hostname'] not in {r['host'] for r in routes}, 'hostname collision')
        seed = int(digest(identity), 16)
        node_port = request.get('node_port')
        if node_port is not None:
            require(not any(r['node_port'] == node_port and r['target_private_ip'] == str(address) for r in routes),
                    'registered NodePort already routed')
        route = {'host': name['hostname'], 'provider_kind': request['provider_kind'],
                 'target_private_ip': str(address), 'health_path': request['health_path'],
                 'node_port': node_port if node_port is not None else free_number(seed, 30000, 32767, {r['node_port'] for r in routes}),
                 'priority': free_number(seed, 1000, 49999, {r['priority'] for r in routes})}
        if request.get('manage_dns') is False:
            route['manage_dns'] = False
        if sg:
            route['target_security_group_id'] = sg
        row = {'version': 1, 'route_key': key, 'request': request, 'route': route,
               'hostname': route['host'], 'node_port': route['node_port'], 'priority': route['priority'],
               'public_http': {'url': 'https://' + route['host'] + request['health_path'],
                               **{k: request[k] for k in ('expected_json', 'expected_status') if k in request}},
               'phase': 'reserved', 'config_sha256': config['_sha256']}
        ledger[key] = row
        durable_write(ledger_path, encoded(ledger))
        durable_write(root / (key + '.json'), encoded(row))
        return {**row, 'reference': {'config_path': str(config_path), 'allocation_path': str(root / (key + '.json')),
                                    'config_sha256': config['_sha256']}}


def load(reference):
    require(set(reference) == {'config_path', 'allocation_path', 'config_sha256'}, 'exact edge reference required')
    config = config_at(reference['config_path'])
    require(config['_sha256'] == reference['config_sha256'], 'edge config changed after registration')
    path = Path(reference['allocation_path'])
    require(path.parent == Path(config['state_dir']) and re.fullmatch(r'app-[a-f0-9]{24}\.json', path.name),
            'owned allocation path required')
    row = read_private(path)
    ledger = read_private(path.parent / 'allocations.json')
    require(without_health(row['request']) == without_health(ledger[row['route_key']]['request']) and
            without_health(row['route']) == without_health(ledger[row['route_key']]['route']) and
            row['config_sha256'] == config['_sha256'], 'allocation ownership conflict')
    return config, row


def native(args, *, timeout=110, env=None):
    environment = {key: value for key, value in {**os.environ, **(env or {})}.items()
                   if not key.startswith(('TF_CLI_ARGS', 'TF_VAR_')) and key not in ('TF_DATA_DIR', 'TF_WORKSPACE')}
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
                                env={**environment, 'AWS_PAGER': '', 'TF_IN_AUTOMATION': '1'})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('edge native outcome requires observation') from exc
    require(result.returncode == 0, 'edge native command did not succeed')
    return result.stdout


def lifecycle_ready(root):
    for path in Path(root).glob('application-lifecycle-*/intent.json'):
        require(read_private(path).get('phase') in ('planned', 'succeeded'),
                'application lifecycle requires manual reconciliation')


def local_state_file(config):
    """Bind explicit or implicit local state without ever inventing a new backend."""
    directory = Path(config['terraform_dir'])
    metadata = directory / '.terraform/terraform.tfstate'
    backend = {}
    if metadata.exists():
        require(metadata.is_file() and not metadata.is_symlink(), 'regular initialized backend metadata required')
        backend = json.loads(metadata.read_bytes()).get('backend') or {}
        require(not backend or backend.get('type') == 'local', 'local edge backend required')
    for path in directory.glob('*.tf'):
        kinds = re.findall(r'backend\s+"([^"]+)"\s*\{', path.read_text())
        require(all(kind == 'local' for kind in kinds) and (not kinds or backend),
                'uninitialized or remote edge backend forbidden')
    for path in directory.glob('*.tf.json'):
        declared = json.loads(path.read_bytes()).get('terraform', {}).get('backend', {})
        require(not declared or (set(declared) == {'local'} and backend), 'uninitialized or remote edge backend forbidden')
    state_path = backend.get('config', {}).get('path') or str(directory / 'terraform.tfstate')
    require(Path(state_path).is_absolute(), 'absolute existing local state required')
    state = read_private(state_path)
    require(isinstance(state.get('lineage'), str) and state['lineage'] and type(state.get('serial')) is int,
            'existing state lineage and serial required')
    return str(state_path)


def terraform(config, *args):
    # Every app writer uses the bundled module, including after lifecycle changes.
    # The operator directory remains the state authority, never a second source
    # that can silently ignore enabled=false on the next app publication.
    source = Path(__file__).resolve().parents[1] / 'infrastructure/terraform/aws-edge'
    files = {path.name: path.read_bytes() for path in sorted(source.iterdir())
             if path.is_file() and (path.suffix == '.tf' or path.name == '.terraform.lock.hcl')}
    state_file = local_state_file(config)
    files['backend.tf.json'] = encoded({'terraform': {'backend': {'local': {'path': state_file}}}})
    fingerprint = digest({name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})
    work = Path(config['state_dir']) / ('aws-source-' + fingerprint)
    require(not work.is_symlink(), 'private source directory required')
    work.mkdir(mode=0o700, exist_ok=True)
    require(work.stat().st_uid == os.geteuid() and not work.stat().st_mode & 0o077, 'private source directory required')
    for name, raw in files.items():
        path = work / name
        if path.exists():
            require(not path.is_symlink() and path.read_bytes() == raw, 'bundled source cache changed')
        else:
            durable_write(path, raw)
    native(['terraform', '-chdir=' + str(work), 'init', '-input=false', '-lockfile=readonly'])
    return native(['terraform', '-chdir=' + str(work), *args])


def save(config, row):
    durable_write(Path(config['state_dir']) / (row['route_key'] + '.json'), encoded(row))


def has_unknown(value):
    if isinstance(value, dict):
        return any(has_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(has_unknown(item) for item in value)
    return value is True


def validate_plan(plan, row):
    """Allow this route creation or its exact health-path update, plus additive networking."""
    require(row['route']['provider_kind'] == 'aws', 'only AWS routes can be written by this executor')
    require(not plan.get('errored'), 'successful edge plan required')
    actions_by_address = {item['address']: item['change']['actions'] for item in plan.get('resource_changes', [])}
    # Provider refresh can fill computed ALB associations/null collections. No write may accompany that drift.
    require(all(actions_by_address.get(item['address']) == ['no-op'] for item in plan.get('resource_drift', [])),
            'reconcile writable edge drift before adding a route')
    key, route = row['route_key'], row['route']
    exact = {kind + '.app[' + json.dumps(key) + ']' for kind in
             ('aws_lb_target_group', 'aws_lb_target_group_attachment', 'aws_lb_listener_rule', 'aws_route53_record')}
    exact.add('aws_security_group_rule.target_from_alb[' + json.dumps(
        str(route.get('target_security_group_id')) + ':' + str(route['node_port'])) + ']')
    changed = []
    for item in plan.get('resource_changes', []):
        change, address = item['change'], item['address']
        actions = change['actions']
        if actions == ['no-op'] or (item.get('mode') == 'data' and actions == ['read']):
            continue
        allowed = address in exact and actions == ['create']
        if address == 'aws_lb_target_group.app[' + json.dumps(key) + ']' and actions == ['update']:
            before, after = change['before'], change['after']
            prior = row.get('previous_route')
            old_health, new_health = before.get('health_check'), after.get('health_check')
            allowed = bool(prior and without_health(prior) == without_health(route)
                and {k: v for k, v in before.items() if k != 'health_check'} ==
                    {k: v for k, v in after.items() if k != 'health_check'}
                and isinstance(old_health, list) and isinstance(new_health, list)
                and len(old_health) == len(new_health) == 1
                and old_health[0].get('path') == prior['health_path']
                and new_health[0] == {**old_health[0], 'path': route['health_path']}
                and not has_unknown(change.get('after_unknown', {})))
        if address == 'aws_security_group.alb' and actions == ['update']:
            before, after = change['before'], change['after']
            allowed = {k: v for k, v in before.items() if k != 'egress'} == {k: v for k, v in after.items() if k != 'egress'}
            prior, following = {encoded(v) for v in before['egress']}, {encoded(v) for v in after['egress']}
            allowed = allowed and prior <= following and all(
                v.get('cidr_blocks') == [route['target_private_ip'] + '/32'] and v.get('protocol') == 'tcp' and
                v.get('from_port') == route['node_port'] == v.get('to_port') and
                not any(v.get(k) for k in ('ipv6_cidr_blocks', 'prefix_list_ids', 'security_groups', 'self'))
                for v in after['egress'] if encoded(v) not in prior)
        require(allowed, 'edge plan changes an unowned or existing resource: ' + address)
        changed.append({'address': address, 'actions': actions})
    return changed


def plan_route(reference):
    config, _ = load(reference)
    with locked(config):
        lifecycle_ready(config['state_dir'])
        config, row = load(reference)
        require(row['phase'] in ('reserved', 'planned'), 'observe interrupted or already applied routes; never replan automatically')
        values = base_values(config, writable=True)
        for key in read_private(Path(config['state_dir']) / 'allocations.json'):
            require(re.fullmatch(r'app-[a-f0-9]{24}', key), 'registered allocation key required')
            other = read_private(Path(config['state_dir']) / (key + '.json'))
            require(other['phase'] != 'applying', 'another edge apply needs reconciliation')
            if other['phase'] == 'applied' or other['route_key'] == row['route_key'] or other.get('previous_route'):
                require(other['route']['provider_kind'] == 'aws', 'migrate the legacy GCP allocation before AWS edge writes')
                key = other['route_key']
                desired = (other['previous_route'] if other['phase'] != 'applied'
                           and key != row['route_key'] and other.get('previous_route') else other['route'])
                require(key not in values['routes'] or
                        without_health(values['routes'][key]) == without_health(desired), 'base route ownership conflict')
                values['routes'][key] = desired
        prefix = Path(config['state_dir']) / row['route_key']
        variables, saved = str(prefix) + '.tfvars.json', str(prefix) + '.tfplan'
        durable_write(variables, encoded(values))
        terraform(config, 'plan', '-input=false', '-lock-timeout=5s', '-var-file=' + variables, '-out=' + saved)
        os.chmod(saved, 0o600)
        plan = json.loads(terraform(config, 'show', '-json', saved))
        changes = validate_plan(plan, row)
        row.update(phase='planned', plan_sha256=hashlib.sha256(Path(saved).read_bytes()).hexdigest(), changes=changes)
        save(config, row)
        return {'phase': row['phase'], 'route_key': row['route_key'], 'plan_sha256': row['plan_sha256'],
                'saved_plan': saved, 'changes': changes}


def apply_route(reference, plan_sha256):
    config, _ = load(reference)
    with locked(config):
        lifecycle_ready(config['state_dir'])
        config, row = load(reference)
        require(row['phase'] == 'planned' and row['plan_sha256'] == plan_sha256, 'reviewed saved plan required')
        base_values(config, writable=True)
        for key in read_private(Path(config['state_dir']) / 'allocations.json'):
            require(re.fullmatch(r'app-[a-f0-9]{24}', key), 'registered allocation key required')
            require(read_private(Path(config['state_dir']) / (key + '.json'))['phase'] != 'applying',
                    'another edge apply needs reconciliation')
        require(datetime.fromisoformat(row['request']['expires_at'].replace('Z', '+00:00')) > datetime.now(timezone.utc),
                'route allocation has expired')
        saved = Path(config['state_dir']) / (row['route_key'] + '.tfplan')
        require(hashlib.sha256(saved.read_bytes()).hexdigest() == plan_sha256, 'saved plan changed')
        validate_plan(json.loads(terraform(config, 'show', '-json', str(saved))), row)
        row['phase'] = 'applying'
        save(config, row)  # Durable intent: failure/kill never causes a second apply.
        terraform(config, 'apply', '-input=false', '-lock-timeout=5s', str(saved))
        row['phase'] = 'applied'
        save(config, row)
    return row


def validate_binding(reference, registered):
    _, row = load(reference)
    target, owner = registered['target'], row['request']
    require(all(registered[k] == owner[k] for k in ('tenant', 'app')) and target['id'] == owner['target_id'] and
            target['namespace'] == owner['namespace'] and target['node_port'] == row['node_port'] and
            registered['public_http'] == row['public_http'], 'CD allocation binding differs')


def observe(reference, request, revision, public_probe, site_probe, app_route='/'):
    config, row = load(reference)
    expected = row['request']
    require(all(expected[k] == request['publication'].get(k) for k in ('tenant', 'app', 'target_id')),
            'edge publication identity mismatch')
    result = {'state': 'unverified', 'verified_at': None, 'url': None}
    if row['phase'] not in ('applying', 'applied'):
        return result
    values = base_values(config)
    aws = lambda *args: json.loads(native(['aws', '--region', values['region'], *args, '--output', 'json']))
    state = json.loads(terraform(config, 'show', '-json'))
    resources = {r['address']: r['values'] for r in state['values']['root_module']['resources']}
    suffix = '.app[' + json.dumps(row['route_key']) + ']'
    group = resources['aws_lb_target_group' + suffix]['arn']
    rule = resources['aws_lb_listener_rule' + suffix]['arn']
    outputs = state['values']['outputs']
    route = row['route']
    live_group = aws('elbv2', 'describe-target-groups', '--target-group-arns', group)['TargetGroups'][0]
    require(live_group['TargetType'] == 'ip' and live_group['Port'] == route['node_port'] and
            live_group['HealthCheckPath'] == route['health_path'] and live_group['Protocol'] == 'HTTP', 'target group differs')
    live_rule = aws('elbv2', 'describe-rules', '--rule-arns', rule)['Rules'][0]
    require(live_rule['Priority'] == str(route['priority']) and
            len(live_rule['Conditions']) == 1 and live_rule['Conditions'][0]['Field'] == 'host-header' and
            live_rule['Conditions'][0]['HostHeaderConfig']['Values'] == [route['host']] and
            len(live_rule['Actions']) == 1 and live_rule['Actions'][0]['Type'] == 'forward' and
            live_rule['Actions'][0]['TargetGroupArn'] == group, 'listener ownership differs')
    records = aws('route53', 'list-resource-record-sets', '--hosted-zone-id', outputs['zone_id']['value'],
                  '--start-record-name', route['host'], '--start-record-type', 'A', '--max-items', '1')['ResourceRecordSets']
    require(records and records[0]['Name'].rstrip('.') == route['host'] and records[0]['Type'] == 'A' and
            records[0]['AliasTarget']['DNSName'].rstrip('.').removeprefix('dualstack.') ==
            outputs['alb_dns_name']['value'].rstrip('.').removeprefix('dualstack.'), 'DNS alias differs')
    health = aws('elbv2', 'describe-target-health', '--target-group-arn', group)['TargetHealthDescriptions']
    require(len(health) == 1 and health[0]['Target']['Id'] == route['target_private_ip'] and
            health[0]['Target']['Port'] == route['node_port'], 'registered target differs')
    if health[0]['TargetHealth']['State'] != 'healthy':
        return result
    public = public_probe(row['public_http'], route['health_path'])
    site_url = 'https://' + route['host'] + http_path(app_route)
    if public['state'] == 'succeeded' and site_probe(site_url):
        result = public
        result.update(site_url=site_url,
                      receipt={'deployment_id': request['deployment_id'], 'target_id': expected['target_id'],
                               'tenant': expected['tenant'], 'app': expected['app'], 'environment_id': expected['environment_id'],
                               'source_commit': request['publication']['source_commit'], 'revision': revision,
                               'namespace': expected['namespace'], 'expires_at': expected['expires_at'],
                               'images_sha256': digest(request['publication'].get('images', {})),
                               'plan_sha256': row.get('plan_sha256'),
                               'route_key': row['route_key'], 'route_sha256': digest(route), 'target_health': 'healthy',
                               'dns_alias': 'verified', 'tls_http': 'verified'})
        with locked(config):
            _, latest = load(reference)
            latest.update(phase='applied', verification=result)
            save(config, latest)
    return result


def ensure(reference):
    config, row = load(reference)
    if config['auto_apply'] and row['phase'] in ('reserved', 'planned'):
        planned = plan_route(reference) if row['phase'] == 'reserved' else row
        apply_route(reference, planned['plan_sha256'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'plan', 'apply'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--request', type=Path)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--plan-sha256')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            result = prepare(args.config, read_private(args.request))
        elif args.action == 'plan':
            result = plan_route(read_private(args.reference))
        else:
            result = apply_route(read_private(args.reference), args.plan_sha256)
        durable_write(args.out, encoded(result))
        print(json.dumps({'status': 'succeeded', 'phase': result['phase']}))
        return 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        print(json.dumps({'status': 'blocked', 'code': 'EDGE_RECONCILE_REQUIRED'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
