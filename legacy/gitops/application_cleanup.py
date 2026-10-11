"""Bound app lifecycle for the existing edge writers; never destroy a shared edge.

Plans contain PRIVATE state snapshots and must never be returned by the public API.
The caller holds the application registration lock. We additionally hold the same
edge/DNS locks as publication. Only execute() mutates providers; failed mutations
leave a durable unknown intent and cannot be automatically replayed.
"""
from contextlib import ExitStack
import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile

import dns
import edge
import gcp_routes
from edge import digest, encoded, read_private, durable_write, require

ROOT = Path(__file__).resolve().parents[1]
AWS_KINDS = ('aws_lb_target_group', 'aws_lb_target_group_attachment', 'aws_lb_listener_rule')
GCP_BACKENDS = gcp_routes.KINDS[:4]


class CleanupError(ValueError):
    def __init__(self, code='APPLICATION_EDGE_BLOCKED', unknown=False):
        self.code, self.unknown = code, unknown
        super().__init__(code)


def checked(binding, action):
    require(action in ('stop', 'start', 'delete'), 'invalid application edge action')
    require(isinstance(binding, dict) and binding.get('version') == 1 and
            binding.get('provider') in ('aws', 'gcp', 'openstack') and
            re.fullmatch(r'app-[a-f0-9]{24}', binding.get('application_id', '')),
            'registered application binding required')
    target = binding['registered']['target']
    require(target['id'] == target['namespace'] == binding['application_id'] and
            type(target['node_port']) is int and 30000 <= target['node_port'] <= 32767,
            'application namespace and NodePort binding required')
    for key in ('edge_config_file', 'dns_config_file'):
        require(isinstance(binding['ingress'].get(key), str) and Path(binding['ingress'][key]).is_absolute(),
                'operator edge and DNS authority required')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def equal_route(left, right):
    return {**left, 'enabled': left.get('enabled', True)} == {**right, 'enabled': right.get('enabled', True)}


def authority(binding):
    path = binding['ingress']['edge_config_file']
    provider, app = binding['provider'], binding['application_id']
    if provider == 'gcp':
        config = gcp_routes.config_at(path)
        state = gcp_routes.bound_state(config)
        values = read_private(config['variables_file'])
        key, ledger = app, None
        state_path = config['state_file']
    else:
        require(provider == 'aws', 'native Terraform edge required')
        config = edge.config_at(path)
        values = edge.base_values(config, writable=True)
        ledger_path = Path(config['state_dir']) / 'allocations.json'
        ledger = read_private(ledger_path) if ledger_path.exists() else {}
        matches = [key for key, row in ledger.items() if row['request'].get('target_id') == app]
        require(len(matches) <= 1, 'ambiguous app edge allocation')
        key = matches[0] if matches else None
        for other_key, allocation in list(ledger.items()):
            _, row = edge.load({'config_path': path,
                'allocation_path': str(Path(config['state_dir']) / (other_key + '.json')),
                'config_sha256': config['_sha256']})
            ledger[other_key] = row
            require(row['phase'] in ('reserved', 'applied', 'stopped', 'deleted')
                    and not (row['phase'] == 'reserved' and row.get('previous_route')), 'unfinished AWS edge operation')
            if row['phase'] in ('applied', 'stopped'):
                require(other_key not in values['routes'] or equal_route(edge.without_health(values['routes'][other_key]),
                        edge.without_health(row['route'])),
                        'AWS route authority conflict')
                values['routes'][other_key] = row['route']
        if key:
            row = ledger[key]
            owner = row['request']
            require(all(owner.get(k) == v for k, v in {
                'environment_id': binding['environment_id'], 'tenant': binding['registered']['tenant'],
                'app': binding['registered']['app'], 'namespace': app}.items()) and
                row['config_sha256'] == config['_sha256'], 'AWS application ownership mismatch')
        state_path = edge.local_state_file(config)
        state = read_private(state_path)
    require(isinstance(state.get('lineage'), str) and state['lineage'] and type(state.get('serial')) is int,
            'state lineage and serial required')
    route = values.get('routes', {}).get(key)
    if route:
        require(route.get('hostname', route.get('host')) == binding['hostname'] and
                route['node_port'] == binding['registered']['target']['node_port'], 'registered route changed')
    else:
        # A legacy baseline or a manually managed host is not an undeployed app.
        require(values.get('hostname') != binding['hostname'] and all(
            r.get('hostname', r.get('host')) != binding['hostname'] for r in values.get('routes', {}).values()),
            'legacy or foreign route requires explicit ownership migration')
    return {'config': config, 'state_file': str(state_path), 'state': state, 'values': values,
            'key': key, 'route': route, 'ledger': ledger, 'owned': gcp_routes.owned(state)}


def addresses(provider, key, route, values, action):
    if not route:
        return set()
    active = route.get('enabled', True)
    kinds = AWS_KINDS if provider == 'aws' else GCP_BACKENDS
    suffix = ('.app[' if provider == 'aws' else '.routes[') + json.dumps(key) + ']'
    result = {kind + suffix for kind in kinds} if active or action == 'start' else set()
    if provider == 'gcp' and action == 'delete':
        result |= {kind + suffix for kind in gcp_routes.resource_kinds(route)[4:]}
    if provider == 'aws':
        pair = route['target_security_group_id'] + ':' + str(route['node_port'])
        if (active or action == 'start') and not any(k != key and r.get('enabled', True) and
                r['target_security_group_id'] + ':' + str(r['node_port']) == pair for k, r in values['routes'].items()):
            result.add('aws_security_group_rule.target_from_alb[' + json.dumps(pair) + ']')
        if action == 'delete' and route.get('manage_dns', True):
            result.add('aws_route53_record' + suffix)
    return result


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    return expected == actual


def refresh_metadata(document):
    """Allow only AWS's empty-tag normalization and the known ALB association."""
    changes = {row['address']: row['change'] for row in document.get('resource_changes', [])}
    alb = changes.get('aws_lb.app', {})
    alb_id = (alb.get('before') or {}).get('id')
    for row in document.get('resource_drift', []):
        address, change = row.get('address', ''), row.get('change', {})
        kind = row.get('type')
        before, after = change.get('before'), change.get('after')
        planned = changes.get(address, {})
        require(kind in ('aws_lb_listener_rule', 'aws_lb_target_group') and
                address.startswith(kind + '.app[') and row.get('mode') == 'managed' and
                not row.get('deposed') and not change.get('importing') and not change.get('replace_paths') and
                not change.get('after_unknown') and change['actions'] == ['update'] and
                isinstance(before, dict) and isinstance(after, dict) and before.get('id') and
                before['id'] == after.get('id') and planned.get('before') == after and
                planned.get('actions') in (['no-op'], ['delete']), 'edge drift must be reconciled first')
        normalized = dict(before)
        if before.get('tags') is None and after.get('tags') == {}:
            normalized['tags'] = {}
        if (kind == 'aws_lb_target_group' and before.get('load_balancer_arns') == [] and alb_id and
                alb.get('actions') == ['no-op'] and (alb.get('after') or {}).get('id') == alb_id and
                after.get('load_balancer_arns') == [alb_id]):
            normalized['load_balancer_arns'] = [alb_id]
        require(normalized == after, 'edge drift must be reconciled first')


def validate_plan(document, snapshot, candidate, provider, action):
    """No configuration drift/replacement/import; exact app addresses and shared edits."""
    require(not document.get('errored'), 'edge drift must be reconciled first')
    refresh_metadata(document)
    require(all(subset(v, document.get('variables', {}).get(k, {}).get('value')) for k, v in candidate.items()),
            'saved plan variables changed')
    route, key = snapshot['route'], snapshot['key']
    targets = addresses(provider, key, route, snapshot['values'], action)
    owned = snapshot['owned']
    changes, seen = [], set()
    for item in document.get('resource_changes', []):
        address, change = item['address'], item['change']
        require(address not in seen and not item.get('deposed') and not change.get('importing') and
                not change.get('replace_paths'), 'duplicate, import or replacement forbidden')
        seen.add(address)
        actions, before, after = change['actions'], change.get('before'), change.get('after')
        if item.get('mode') == 'data' and actions in (['read'], ['no-op']):
            continue
        if actions == ['no-op']:
            require(address in owned and before and before.get('id') == owned[address] and
                    after and after.get('id') == owned[address], 'unchanged resource identity mismatch')
            continue
        if address in targets:
            if action == 'start':
                require(actions == ['create'] and address not in owned and before is None, 'only stopped app recreation allowed')
                validate_create(provider, address, after, route, snapshot['values'], key)
            else:
                require(actions == ['delete'] and address in owned and before and
                        before.get('id') == owned[address] and after is None, 'exact owned deletion required')
        else:
            require(route and actions == ['update'] and address in owned and before and after and
                    before.get('id') == after.get('id') == owned[address], 'shared identity or action changed')
            validate_shared(provider, address, before, after, change.get('after_unknown', {}), route, key,
                            snapshot['values'], action)
        changes.append({'address': address, 'actions': actions})
    changed_targets = {r['address'] for r in changes} & targets
    require(changed_targets == targets, 'complete exact application resource set required')
    return changes


def validate_create(provider, address, after, route, values, key):
    require(isinstance(after, dict), 'reviewable new application resource required')
    kind = address.split('.')[0]
    if provider == 'gcp':
        require(after.get('project') == values['project_id'], 'application project changed')
        name = values.get('name', 'railshot-gcp-edge') + '-' + hashlib.sha256(key.encode()).hexdigest()[:16]
        if kind != 'google_compute_network_endpoint':
            require(after.get('name') == name, 'application resource name changed')
        if kind == 'google_compute_network_endpoint':
            require(subset({'instance': values['instance_name'], 'ip_address': values['expected_private_ip'],
                            'port': route['node_port'], 'zone': values['zone']}, after), 'application endpoint changed')
        if kind == 'google_compute_network_endpoint_group':
            require(subset({'network_endpoint_type': 'GCE_VM_IP_PORT', 'default_port': route['node_port'],
                            'zone': values['zone']}, after), 'application NEG changed')
        if kind == 'google_compute_health_check':
            require(len(after.get('http_health_check', [])) == 1 and subset({
                'port': route['node_port'], 'host': route['hostname'], 'request_path': route['health_path']},
                after['http_health_check'][0]), 'application health check changed')
        if kind == 'google_compute_backend_service':
            require(subset({'protocol': 'HTTP', 'load_balancing_scheme': 'EXTERNAL_MANAGED'}, after), 'application backend changed')
    elif kind == 'aws_lb_target_group':
        require(subset({'vpc_id': values['vpc_id'], 'port': route['node_port'], 'protocol': 'HTTP', 'target_type': 'ip'}, after)
                and len(after.get('health_check', [])) == 1 and after['health_check'][0]['path'] == route['health_path'],
                'application target group changed')
    elif kind == 'aws_lb_target_group_attachment':
        require(subset({'target_id': route['target_private_ip'], 'port': route['node_port']}, after), 'application target changed')
    elif kind == 'aws_security_group_rule':
        require(subset({'security_group_id': route['target_security_group_id'], 'protocol': 'tcp', 'type': 'ingress',
                        'from_port': route['node_port'], 'to_port': route['node_port']}, after), 'application SG rule changed')
    elif kind == 'aws_lb_listener_rule':
        require(after.get('priority') == route['priority'] and len(after.get('condition', [])) == 1 and
                after['condition'][0].get('host_header') == [{'values': [route['host']]}], 'application listener changed')


def validate_shared(provider, address, before, after, unknown, route, key, values, action):
    adding = action == 'start'
    left, right = (before, after) if adding else (after, before)
    if provider == 'aws':
        require(address == 'aws_security_group.alb', 'unowned shared AWS resource')
        require({k: v for k, v in before.items() if k != 'egress'} ==
                {k: v for k, v in after.items() if k != 'egress'} and not list(gcp_routes.unknown_paths(unknown)),
                'shared ALB SG changed outside app egress')
        block = gcp_routes.additions(left.get('egress'), right.get('egress'))
        require(subset({'protocol': 'tcp', 'from_port': route['node_port'], 'to_port': route['node_port'],
                        'cidr_blocks': [route['target_private_ip'] + '/32']}, block) and
                not any(block.get(k) for k in ('ipv6_cidr_blocks', 'prefix_list_ids', 'security_groups', 'self')),
                'unowned ALB egress change')
        return
    require(address in gcp_routes.UPDATES, 'unowned shared GCP resource')
    fields = {'allow', 'fingerprint'} if address.endswith('.gfe') else {'host_rule', 'path_matcher', 'fingerprint'}
    require({k: v for k, v in before.items() if k not in fields} ==
            {k: v for k, v in after.items() if k not in fields}, 'shared GCP behavior changed')
    allowed_unknown = {('fingerprint',)}
    if address.endswith('.gfe'):
        require(len(left['allow']) == len(right['allow']) == 1, 'one GFE firewall rule required')
        old, new = left['allow'][0], right['allow'][0]
        require({k: v for k, v in old.items() if k != 'ports'} == {k: v for k, v in new.items() if k != 'ports'} and
                old['protocol'] == 'tcp' and set(new['ports']) == set(old['ports']) | {str(route['node_port'])},
                'unowned GFE port change')
    else:
        name = values.get('name', 'railshot-gcp-edge') + '-' + hashlib.sha256(key.encode()).hexdigest()[:16]
        gcp_routes.exact_block(gcp_routes.additions(left.get('host_rule'), right.get('host_rule')),
                               {'hosts': [route['hostname']], 'path_matcher': name})
        block = gcp_routes.additions(left.get('path_matcher'), right.get('path_matcher'))
        if address.endswith('.redirect'):
            require(block.get('name') == name and len(block.get('default_url_redirect', [])) == 1 and
                    block['default_url_redirect'][0].get('host_redirect') == route['hostname'] and
                    block['default_url_redirect'][0].get('https_redirect') is True, 'unowned HTTP redirect')
        else:
            backend = block.get('default_service')
            expected = 'projects/' + values['project_id'] + '/global/backendServices/' + name
            require(backend in (expected, 'https://www.googleapis.com/compute/v1/' + expected) or
                    (adding and backend is None), 'unowned backend matcher')
            gcp_routes.exact_block(block, {'name': name, 'default_service': backend})
            allowed_unknown.add(('path_matcher', right['path_matcher'].index(block), 'default_service'))
    require(set(gcp_routes.unknown_paths(unknown)) <= allowed_unknown, 'unreviewable shared change')


def terraform(work, *args):
    return edge.native(['terraform', '-chdir=' + str(work), *args])


def saved_plan(work, values, label):
    variables, saved = work / (label + '.json'), work / (label + '.tfplan')
    durable_write(variables, encoded(values))
    terraform(work, 'plan', '-input=false', '-lock=true', '-lock-timeout=5s',
              '-var-file=' + str(variables), '-out=' + str(saved))
    saved.chmod(0o600)
    return json.loads(terraform(work, 'show', '-json', str(saved)))


def unchanged(document):
    refresh_metadata(document)
    require(not document.get('errored') and all(
        row['change']['actions'] == ['no-op'] or (row.get('mode') == 'data' and row['change']['actions'] == ['read'])
        for row in document.get('resource_changes', [])), 'fresh edge plan contains drift or unrelated changes')


def verify_absence(provider, values, document):
    """Provider list readback proves removal even when deleted objects left state."""
    deleted = [row for row in document.get('resource_changes', []) if row['change']['actions'] == ['delete']]
    if not deleted:
        return
    def read(args):
        return json.loads(edge.native(args))
    if provider == 'aws':
        prefix = ['aws', '--region', values['region']]
        require(read([*prefix, 'sts', 'get-caller-identity', '--output', 'json']).get('Account') == values['account_id'],
                'AWS absence reader account changed')
        for row in deleted:
            before, kind = row['change']['before'], row['address'].split('.')[0]
            if kind == 'aws_lb_target_group':
                rows = read([*prefix, 'elbv2', 'describe-target-groups', '--output', 'json'])['TargetGroups']
                require(all(item.get('TargetGroupArn') != before.get('arn', before['id']) for item in rows), 'target group deletion unverified')
            elif kind == 'aws_lb_listener_rule':
                rows = read([*prefix, 'elbv2', 'describe-rules', '--listener-arn', before['listener_arn'], '--output', 'json'])['Rules']
                require(all(item.get('RuleArn') != before.get('arn', before['id']) for item in rows), 'listener deletion unverified')
            elif kind == 'aws_security_group_rule':
                rows = read([*prefix, 'ec2', 'describe-security-group-rules', '--filters',
                             'Name=group-id,Values=' + before['security_group_id'], '--output', 'json'])['SecurityGroupRules']
                require(not any(item.get('IsEgress') is False and item.get('IpProtocol') == before['protocol'] and
                        item.get('FromPort') == before['from_port'] and item.get('ToPort') == before['to_port'] and
                        item.get('ReferencedGroupInfo', {}).get('GroupId') == before['source_security_group_id'] for item in rows),
                        'application SG rule deletion unverified')
            elif kind == 'aws_route53_record':
                rows = read([*prefix, 'route53', 'list-resource-record-sets', '--hosted-zone-id', before['zone_id'],
                             '--output', 'json'])['ResourceRecordSets']
                require(not any(item.get('Name', '').rstrip('.') == before['name'].rstrip('.') and
                                item.get('Type') == before['type'] for item in rows), 'Route53 deletion unverified')
            else:
                # An attachment cannot outlive its target group. Require both to
                # be deleted in this plan; do not infer absence for a live group.
                require(kind == 'aws_lb_target_group_attachment' and any(
                    other['address'].split('.')[0] == 'aws_lb_target_group' and
                    other['change']['before'].get('arn', other['change']['before']['id']) == before['target_group_arn']
                    for other in deleted), 'attachment deletion requires exact parent group deletion')
        return
    commands = {'google_compute_network_endpoint_group': ['compute', 'network-endpoint-groups'],
                'google_compute_health_check': ['compute', 'health-checks'],
                'google_compute_backend_service': ['compute', 'backend-services'],
                'google_certificate_manager_dns_authorization': ['certificate-manager', 'dns-authorizations'],
                'google_certificate_manager_certificate': ['certificate-manager', 'certificates'],
                'google_certificate_manager_certificate_map_entry': ['certificate-manager', 'maps', 'entries']}
    for row in deleted:
        before, kind = row['change']['before'], row['address'].split('.')[0]
        if kind == 'google_compute_network_endpoint':
            require(any(other['address'].split('.')[0] == 'google_compute_network_endpoint_group' and
                        other['change']['before']['name'] == before['network_endpoint_group'] for other in deleted),
                    'endpoint deletion requires exact parent NEG deletion')
            continue
        require(kind in commands, 'unrecognized application deletion')
        extra = ['--location=global'] if kind.startswith('google_certificate_manager_') else []
        if kind == 'google_certificate_manager_certificate_map_entry':
            extra.append('--map=' + before['map'])
        rows = read(['gcloud', *commands[kind], 'list', '--project=' + values['project_id'], *extra, '--format=json', '--quiet'])
        require(isinstance(rows, list) and all(isinstance(item, dict) for item in rows), 'provider absence list invalid')
        require(not any(item.get('name') in (before['name'], before['id']) or
                        item.get('selfLink', '').removeprefix('https://www.googleapis.com/compute/v1/') == before['id']
                        for item in rows), 'GCP resource deletion unverified')


def source_files(provider):
    source = ROOT / 'infrastructure/terraform' / (provider + '-edge')
    return {path.name: path.read_bytes() for path in sorted(source.iterdir())
            if path.is_file() and (path.suffix == '.tf' or path.name == '.terraform.lock.hcl')}


def module_hash(files):
    return digest({name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})


def native_plan(binding, action, config):
    snapshot = authority(binding)
    route, key = snapshot['route'], snapshot['key']
    if action == 'start':
        require(route is not None and route.get('enabled', True) is False, 'only a stopped owned app can start')
    candidate = copy.deepcopy(snapshot['values'])
    if route:
        if action == 'delete':
            del candidate['routes'][key]
        else:
            candidate['routes'][key]['enabled'] = action == 'start'
    work = Path(tempfile.mkdtemp(prefix='application-lifecycle-', dir=config['state_dir']))
    files = source_files(binding['provider'])
    for name, raw in files.items():
        durable_write(work / name, raw)
    durable_write(work / 'backend.tf.json', encoded({'terraform': {'backend': {'local': {'path': snapshot['state_file']}}}}))
    terraform(work, 'init', '-input=false', '-lockfile=readonly')
    unchanged(saved_plan(work, snapshot['values'], 'before'))
    document = saved_plan(work, candidate, 'change')
    changes = validate_plan(document, snapshot, candidate, binding['provider'], action)
    require(authority(binding) == snapshot, 'edge authority changed during planning')
    return {'snapshot': snapshot, 'candidate': candidate, 'work': str(work), 'changes': changes,
            'module_sha256': module_hash(files), 'plan_sha256': sha(work / 'change.tfplan')}


def config_at(binding):
    path = binding['ingress']['edge_config_file']
    if binding['provider'] == 'aws':
        return edge.config_at(path)
    if binding['provider'] == 'gcp':
        return gcp_routes.config_at(path)
    # The OpenStack adapter supplies its own worker/tunnel journals and locks.
    import openstack_routes
    return openstack_routes.lifecycle_config(binding)


def check_journals(root):
    for path in root.glob('application-lifecycle-*/intent.json'):
        require(read_private(path).get('phase') in ('planned', 'succeeded'), 'edge lifecycle reconciliation required')


def plan(binding, action):
    checked(binding, action)
    config = config_at(binding)
    with ExitStack() as stack:
        root = stack.enter_context(edge.locked(config))
        dns_config = dns.config_at(binding['ingress']['dns_config_file'])
        stack.enter_context(dns.locked(dns_config))
        check_journals(root)
        records = dns.application_snapshot(dns_config, binding['application_id'], binding['hostname'])
        if binding['provider'] == 'openstack':
            import openstack_routes
            native = openstack_routes.lifecycle_plan(binding, action)
        else:
            native = native_plan(binding, action, config)
        work = Path(native['work'])
        health_path = (native.get('health_path') if binding['provider'] == 'openstack' else
                       (native['snapshot']['route'] or {}).get('health_path'))
        if health_path is not None:
            edge.http_path(health_path)
        require(action != 'start' or health_path is not None, 'registered health path required to resume')
        resources = [{'kind': row['address'].split('.')[0], 'name': binding['hostname']}
                     for row in native['changes']]
        if action == 'delete':
            resources += [{'kind': 'cloudflare_dns_record', 'name': r['row']['request']['hostname']} for r in records]
        retained = [{'kind': 'shared_load_balancer', 'name': binding['provider']},
                    {'kind': 'shared_runtime', 'name': binding['environment_id']}]
        if action == 'stop':
            retained += [{'kind': 'cloudflare_dns_record', 'name': r['row']['request']['hostname']} for r in records]
            if health_path is not None:
                retained.append({'kind': 'certificate', 'name': binding['hostname']})
        result = {'version': 1, 'application_id': binding['application_id'], 'provider': binding['provider'],
                  'action': action, 'resources': resources, 'retained': retained, 'private': {
                      'binding_sha256': digest(binding), 'native': native, 'dns_config': dns_config, 'dns': records,
                      'health_path': health_path}}
        result['plan_sha256'] = digest(result)
        durable_write(work / 'review.json', encoded(result))
        durable_write(work / 'intent.json', encoded({'phase': 'planned', 'plan_sha256': result['plan_sha256']}))
        return result


def native_execute(binding, action, expected):
    snapshot, candidate, work = expected['snapshot'], expected['candidate'], Path(expected['work'])
    require(authority(binding) == snapshot and module_hash(source_files(binding['provider'])) == expected['module_sha256'],
            'edge authority or source changed after review')
    require(sha(work / 'change.tfplan') == expected['plan_sha256'] and
            module_hash({name: (work / name).read_bytes() for name in source_files(binding['provider'])}) == expected['module_sha256'],
            'saved plan or copied source changed')
    document = json.loads(terraform(work, 'show', '-json', str(work / 'change.tfplan')))
    validate_plan(document, snapshot, candidate, binding['provider'], action)
    terraform(work, 'apply', '-input=false', '-lock-timeout=5s', str(work / 'change.tfplan'))
    after = read_private(snapshot['state_file'])
    identities = gcp_routes.owned(after)
    targets = addresses(binding['provider'], snapshot['key'], snapshot['route'], snapshot['values'], action)
    kept = {k: v for k, v in snapshot['owned'].items() if k not in targets}
    require(after['lineage'] == snapshot['state']['lineage'] and after['serial'] >= snapshot['state']['serial'] and
            all(identities.get(k) == v for k, v in kept.items()) and
            set(identities) == (set(kept) | targets if action == 'start' else set(kept)),
            'applied state resource ownership mismatch')
    unchanged(saved_plan(work, candidate, 'after'))
    verify_absence(binding['provider'], snapshot['values'], document)
    config = snapshot['config']
    durable_write(config['variables_file'], encoded(candidate))
    if binding['provider'] == 'gcp':
        durable_write(binding['ingress']['edge_config_file'], encoded({**config, 'owned_resources': identities}))
    elif snapshot['key']:
        ledger = snapshot['ledger']; row = ledger[snapshot['key']]
        row['phase'] = {'start': 'applied', 'stop': 'stopped', 'delete': 'deleted'}[action]
        if action != 'delete':
            row['route'] = candidate['routes'][snapshot['key']]
        durable_write(Path(config['state_dir']) / (snapshot['key'] + '.json'), encoded(row))
        durable_write(Path(config['state_dir']) / 'allocations.json', encoded(ledger))
    return {'state_lineage': after['lineage'], 'state_serial': after['serial'], 'owned_resources': identities}


def reviewed_plan(binding, action, expected_plan, root):
    private = expected_plan['private']; native = private['native']; work = Path(native['work'])
    require(work.parent == root and re.fullmatch(r'application-lifecycle-[A-Za-z0-9_-]+', work.name) and
            not work.is_symlink(), 'private lifecycle plan path required')
    require(expected_plan == read_private(work / 'review.json') and
            expected_plan['plan_sha256'] == digest({k: v for k, v in expected_plan.items() if k != 'plan_sha256'}) and
            expected_plan['action'] == action and private['binding_sha256'] == digest(binding), 'reviewed application plan required')
    return work, read_private(work / 'intent.json')


def validate_locked(binding, action, expected_plan, config, dns_config, root):
    private = expected_plan['private']; native = private['native']
    work, intent = reviewed_plan(binding, action, expected_plan, root)
    check_journals(root)
    require(intent['phase'] == 'planned' and dns_config == private['dns_config'] and
            dns.application_snapshot(dns_config, binding['application_id'], binding['hostname']) == private['dns'],
            'DNS authority or application records changed after review')
    if binding['provider'] != 'openstack':
        require(authority(binding) == native['snapshot'] and
                module_hash(source_files(binding['provider'])) == native['module_sha256'] and
                sha(work / 'change.tfplan') == native['plan_sha256'], 'edge plan became stale')
        require(module_hash({name: (work / name).read_bytes() for name in source_files(binding['provider'])}) == native['module_sha256'],
                'copied source changed after review')
        validate_plan(json.loads(terraform(work, 'show', '-json', str(work / 'change.tfplan'))), native['snapshot'],
                      native['candidate'], binding['provider'], action)
        unchanged(saved_plan(work, native['snapshot']['values'], 'preflight'))
        require(authority(binding) == native['snapshot'], 'edge authority changed during preflight')
    else:
        import openstack_routes
        openstack_routes.lifecycle_validate(binding, action, native)
    return work


def validate(binding, action, expected_plan):
    """Provider read-only validation of the SAME reviewed plan before caller writes."""
    checked(binding, action)
    config = config_at(binding)
    with ExitStack() as stack:
        root = stack.enter_context(edge.locked(config))
        dns_config = dns.config_at(binding['ingress']['dns_config_file'])
        stack.enter_context(dns.locked(dns_config))
        validate_locked(binding, action, expected_plan, config, dns_config, root)


def inspect_execution(binding, action, expected_plan):
    """Read a reviewed route intent; an uncertain provider write is never replayed."""
    checked(binding, action)
    config = config_at(binding)
    with ExitStack() as stack:
        root = stack.enter_context(edge.locked(config))
        dns_config = dns.config_at(binding['ingress']['dns_config_file'])
        stack.enter_context(dns.locked(dns_config))
        _, intent = reviewed_plan(binding, action, expected_plan, root)
        if intent['phase'] == 'succeeded':
            receipt = intent['receipt']
            require(receipt.get('status') == 'succeeded' and receipt.get('application_id') == binding['application_id']
                    and receipt.get('plan_sha256') == expected_plan['plan_sha256']
                    and receipt.get('phase') == {'stop': 'stopped', 'start': 'started', 'delete': 'deleted'}[action],
                    'completed lifecycle receipt differs')
            return 'succeeded'
        validate_locked(binding, action, expected_plan, config, dns_config, root)
        return 'not_started'


def execute(binding, action, expected_plan):
    checked(binding, action)
    config = config_at(binding)
    with ExitStack() as stack:
        root = stack.enter_context(edge.locked(config))
        dns_config = dns.config_at(binding['ingress']['dns_config_file'])
        stack.enter_context(dns.locked(dns_config))
        private = expected_plan['private']; native = private['native']
        work, intent = reviewed_plan(binding, action, expected_plan, root)
        if intent['phase'] == 'succeeded':
            receipt = intent['receipt']
            require(receipt.get('status') == 'succeeded' and receipt.get('application_id') == binding['application_id']
                    and receipt.get('plan_sha256') == expected_plan['plan_sha256']
                    and receipt.get('phase') == {'stop': 'stopped', 'start': 'started', 'delete': 'deleted'}[action],
                    'completed lifecycle receipt differs')
            return receipt  # Historical receipt only; never replay a completed mutation.
        work = validate_locked(binding, action, expected_plan, config, dns_config, root)
        durable_write(work / 'intent.json', encoded({'phase': 'applying', 'plan_sha256': expected_plan['plan_sha256']}))
        try:
            if binding['provider'] == 'openstack':
                import openstack_routes
                result = openstack_routes.lifecycle_execute(binding, action, native)
            else:
                result = native_execute(binding, action, native)
            if action == 'delete':
                dns.remove_application(dns_config, binding['application_id'], binding['hostname'], private['dns'])
            receipt = {'status': 'succeeded', 'phase': {'stop': 'stopped', 'start': 'started', 'delete': 'deleted'}[action],
                       'application_id': binding['application_id'], 'provider': binding['provider'],
                       'plan_sha256': expected_plan['plan_sha256'], 'resources': expected_plan['resources'],
                       'https_verified': False, 'private': result}
            durable_write(work / 'intent.json', encoded({'phase': 'succeeded', 'receipt': receipt}))
            return receipt
        except BaseException:
            durable_write(work / 'intent.json', encoded({'phase': 'unknown', 'plan_sha256': expected_plan['plan_sha256']}))
            raise CleanupError('APPLICATION_EDGE_RECONCILE_REQUIRED', unknown=True) from None
