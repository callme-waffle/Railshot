#!/usr/bin/env python3
"""Add one registered application to a state-bound GCP edge using the shared DNS writer."""
import argparse
from collections import Counter
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from edge import encoded, digest, read_private, locked, durable_write, native, require, http_path, lifecycle_ready

SOURCE = Path(__file__).resolve().parents[1] / 'infrastructure/terraform/gcp-edge'
KINDS = ('google_compute_network_endpoint_group', 'google_compute_network_endpoint',
         'google_compute_health_check', 'google_compute_backend_service',
         'google_certificate_manager_dns_authorization', 'google_certificate_manager_certificate',
         'google_certificate_manager_certificate_map_entry')
UPDATES = {'google_compute_url_map.app', 'google_compute_url_map.redirect', 'google_compute_firewall.gfe'}


class RouteError(ValueError):
    def __init__(self, code, *, unknown=False):
        super().__init__(code)
        self.code, self.unknown = code, unknown


def certificate_dns():
    """Terraform creation hook: publish the owned CNAME before issuing its cert."""
    import dns
    request = json.loads(os.environ['RAILSHOT_GCP_CERTIFICATE_DNS_REQUEST'])
    try:
        dns.require(request.get('purpose') == 'certificate', 'DNS_REQUEST_INVALID')
        receipt = dns.ensure(os.environ['RAILSHOT_GCP_CERTIFICATE_DNS_CONFIG'], request)
        # API readback alone does not prove that Google's resolver sees the CNAME.
        query = 'https://dns.google/resolve?' + urlencode({'name': request['hostname'], 'type': 'CNAME'})
        for attempt in range(30):
            try:
                with urlopen(query, timeout=5) as response:
                    answer = json.loads(response.read(65_537))
                if isinstance(answer, dict) and answer.get('Status') == 0 and any(isinstance(row, dict) and row.get('type') == 5 and
                        row.get('name', '').rstrip('.').lower() == request['hostname'] and
                        row.get('data', '').rstrip('.').lower() == request['content']
                        for row in (answer.get('Answer') or [])):
                    return receipt
            except (OSError, ValueError):
                pass
            if attempt < 29:
                time.sleep(2)
        raise dns.DNSError('DNS_CERTIFICATE_PROPAGATION_PENDING', 'UNKNOWN')
    except dns.DNSError as error:
        durable_write(Path.cwd() / 'certificate-dns-error.json', encoded({'code': error.code}))
        raise


def owned(state):
    result = {}
    for resource in state.get('resources', []):
        if resource.get('mode') != 'managed':
            continue
        for instance in resource['instances']:
            address = (resource.get('module', '') + '.' if resource.get('module') else '') + resource['type'] + '.' + resource['name']
            if 'index_key' in instance:
                address += '[' + json.dumps(instance['index_key']) + ']'
            value = instance.get('attributes', {}).get('id')
            require(not instance.get('deposed') and isinstance(value, str) and value and address not in result,
                    'complete unique managed resource identities required')
            result[address] = value
    require(result, 'existing managed edge state required')
    return result


def config_at(path):
    value = read_private(path)
    require(set(value) == {'version', 'provider', 'edge_kind', 'state_file', 'variables_file', 'state_dir',
                           'state_lineage', 'owned_resources', 'previous_source_sha'} and
            value['version'] == 1 and value['provider'] == 'gcp' and value['edge_kind'] == 'native',
            'exact native GCP authority required')
    require(re.fullmatch(r'[a-f0-9]{40}', value['previous_source_sha']) and
            isinstance(value['state_lineage'], str) and value['state_lineage'] and
            isinstance(value['owned_resources'], dict) and value['owned_resources'], 'bound existing state required')
    for key in ('state_file', 'variables_file', 'state_dir'):
        require(isinstance(value[key], str) and Path(value[key]).is_absolute(), 'absolute private paths required')
    return value


def bound_state(config):
    state = read_private(config['state_file'])
    require(state.get('lineage') == config['state_lineage'] and owned(state) == config['owned_resources'],
            'edge lineage or resource ownership changed')
    return state


def checked_request(request):
    require(isinstance(request, dict) and set(request) == {'application_id', 'hostname', 'node_port', 'health_path'},
            'exact registered application route required')
    require(isinstance(request['application_id'], str) and re.fullmatch(r'app-[a-f0-9]{24}', request['application_id']),
            'registered application ID required')
    require(isinstance(request['hostname'], str) and len(request['hostname']) <= 253 and
            re.fullmatch(r'([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', request['hostname']),
            'exact lowercase hostname required')
    require(type(request['node_port']) is int and 30000 <= request['node_port'] <= 32767, 'NodePort required')
    http_path(request['health_path'])
    return {k: request[k] for k in ('hostname', 'node_port', 'health_path')}


def application_route(request, values):
    route = checked_request(request)
    certificate = values.get('application_certificate')
    if certificate is not None:
        require(isinstance(certificate, dict) and set(certificate) == {'id', 'domain'} and
                isinstance(certificate['domain'], str) and isinstance(certificate['id'], str) and
                re.fullmatch(r'[a-z0-9-]+\.' + re.escape(certificate['domain']), route['hostname']) and
                re.fullmatch(r'projects/' + re.escape(values['project_id']) +
                             r'/locations/global/certificates/[a-z0-9-]+', certificate['id']),
                'shared certificate must match the project and direct child hostname')
        route['certificate_id'] = certificate['id']
    return route


def resource_kinds(route):
    # A platform certificate is shared infrastructure, never app-owned cleanup.
    return (*KINDS[:4], KINDS[-1]) if route.get('certificate_id') else KINDS


def additions(before, after):
    old, new = Counter(map(encoded, before or [])), Counter(map(encoded, after or []))
    require(not old - new and sum((new - old).values()) == 1, 'existing route blocks must be preserved exactly')
    return json.loads(next(iter(new - old)))


def exact_block(block, required):
    require(all(block.get(k) == v for k, v in required.items()) and
            all(v in (None, [], {}) for k, v in block.items() if k not in required), 'unexpected added routing behavior')


def unknown_paths(value, path=()):
    if value is True:
        yield path
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from unknown_paths(child, (*path, key))
    elif isinstance(value, list):
        for key, child in enumerate(value):
            yield from unknown_paths(child, (*path, key))


def comparable_resource(address, value, *, sort_rules=False):
    """Compare GCP resource references and unset routing fields by their meaning."""
    result = copy.deepcopy(value)
    if not isinstance(result, dict):
        return result
    if address == 'google_compute_firewall.gfe':
        # Provider refresh can turn unset selectors into empty lists. Nonempty
        # selectors, source ranges and ports still require exact comparison.
        for field in ('source_service_accounts', 'source_tags', 'target_tags'):
            if result.get(field) is None:
                result[field] = []
    elif address in ('google_compute_url_map.app', 'google_compute_url_map.redirect'):
        for field in ('host_rule', 'path_matcher'):
            for block in result.get(field) or []:
                if block.get('description') in (None, ''):
                    block.pop('description', None)
                if field == 'path_matcher':
                    backend = block.get('default_service')
                    if backend == '':
                        block['default_service'] = None
                    elif isinstance(backend, str):
                        block['default_service'] = backend.removeprefix('https://www.googleapis.com/compute/v1/')
                    for redirect in block.get('default_url_redirect') or []:
                        for key in ('path_redirect', 'prefix_redirect'):
                            if redirect.get(key) == '':
                                redirect[key] = None
            # Google returns host rules and named matchers in canonical order
            # after apply. Their list position does not determine routing.
            if sort_rules and isinstance(result.get(field), list):
                result[field].sort(key=encoded)
    return result


def validate_plan(plan, request, values):
    actions = {item['address']: item['change']['actions'] for item in plan.get('resource_changes', [])}
    require(not plan.get('errored'), 'reconcile writable refresh drift before route writes')
    for item in plan.get('resource_drift', []):
        address, change = item['address'], item.get('change', {})
        # A refreshed resource may also receive the requested additive update,
        # but only when refresh changed representation, not existing behavior.
        representation_only = (address in UPDATES and actions.get(address) == ['update'] and
                               isinstance(change.get('before'), dict) and isinstance(change.get('after'), dict) and
                               comparable_resource(address, change['before'], sort_rules=True) ==
                               comparable_resource(address, change['after'], sort_rules=True))
        require(actions.get(address) == ['no-op'] or representation_only,
                'reconcile writable refresh drift before route writes')
    key = request['application_id']
    route = application_route(request, values)
    expected = copy.deepcopy({**values, 'routes': {**values.get('routes', {}), key: route}})
    actual = copy.deepcopy({k: plan.get('variables', {}).get(k, {}).get('value') for k in expected})
    for collection in (actual, expected):
        for route in (collection.get('routes') or {}).values():
            if route.get('enabled') is True:
                route.pop('enabled')
            if route.get('certificate_id') is None:
                route.pop('certificate_id', None)
    require(actual == expected,
            'saved plan variables differ from bound candidate')
    provider = plan.get('configuration', {}).get('provider_config', {}).get('google', {})
    require(provider.get('full_name') == 'registry.terraform.io/hashicorp/google' and
            provider.get('expressions', {}).get('project', {}).get('references') == ['var.project_id'],
            'provider project must reference the bound project input')
    creates = {kind + '.routes[' + json.dumps(key) + ']' for kind in resource_kinds(application_route(request, values))}
    name = values.get('name', 'railshot-gcp-edge') + '-' + hashlib.sha256(key.encode()).hexdigest()[:16]
    seen = set()
    for item in plan.get('resource_changes', []):
        address, change = item['address'], item['change']
        actions = change['actions']
        if actions == ['no-op'] or (item.get('mode') == 'data' and actions == ['read']):
            continue
        require(address not in seen, 'duplicate resource change')
        seen.add(address)
        if address in creates:
            require(actions == ['create'] and change.get('before') is None, 'only fresh application resources permitted')
            after = change['after']
            kind = address.split('.')[0]
            expected = {'port': request['node_port']} if kind == 'google_compute_network_endpoint' else {'name': name}
            require(after.get('project') == values['project_id'] or
                    (after.get('project') is None and change.get('after_unknown', {}).get('project') is True),
                    'new resource project mismatch')
            if kind == 'google_compute_network_endpoint_group':
                expected.update(default_port=request['node_port'], network_endpoint_type='GCE_VM_IP_PORT', zone=values['zone'])
            if kind == 'google_compute_network_endpoint':
                expected.update(instance=values['instance_name'], ip_address=values['expected_private_ip'], zone=values['zone'])
            if kind == 'google_compute_health_check':
                require(len(after.get('http_health_check', [])) == 1, 'HTTP health check required')
                expected.update(http_health_check=after['http_health_check'])
                require(all(after['http_health_check'][0].get(k) == v for k, v in
                            {'port': request['node_port'], 'host': request['hostname'], 'request_path': request['health_path']}.items()),
                        'application health contract changed')
            if kind == 'google_certificate_manager_dns_authorization':
                expected.update(domain=request['hostname'], type='PER_PROJECT_RECORD')
            if kind == 'google_compute_backend_service':
                expected.update(load_balancing_scheme='EXTERNAL_MANAGED', protocol='HTTP')
            if kind == 'google_certificate_manager_certificate':
                require(len(after.get('managed', [])) == 1 and after['managed'][0].get('domains') == [request['hostname']],
                        'certificate hostname mismatch')
            if kind == 'google_certificate_manager_certificate_map_entry':
                expected.update(hostname=request['hostname'], map=values.get('name', 'railshot-gcp-edge'))
                if values.get('application_certificate'):
                    expected['certificates'] = [values['application_certificate']['id']]
            require(all(after.get(k) == v for k, v in expected.items()), 'new resource differs from registered route')
            continue
        require(address in UPDATES and actions == ['update'], 'unrelated create, update, delete or replacement forbidden')
        before = comparable_resource(address, change['before'])
        after = comparable_resource(address, change['after'])
        fields = {'allow', 'fingerprint'} if address.endswith('.gfe') else {'host_rule', 'path_matcher', 'fingerprint'}
        require({k: v for k, v in before.items() if k not in fields} ==
                {k: v for k, v in after.items() if k not in fields}, 'existing edge behavior changed')
        allowed_unknown = {('fingerprint',)}
        if address.endswith('.gfe'):
            require(len(before['allow']) == len(after['allow']) == 1, 'one existing GFE TCP rule required')
            old, new = before['allow'][0], after['allow'][0]
            require(old['protocol'] == new['protocol'] == 'tcp' and
                    {k: v for k, v in old.items() if k != 'ports'} == {k: v for k, v in new.items() if k != 'ports'} and
                    set(new['ports']) == set(old['ports']) | {str(request['node_port'])}, 'only the requested TCP port may be added')
        else:
            exact_block(additions(before.get('host_rule'), after.get('host_rule')),
                        {'hosts': [request['hostname']], 'path_matcher': name})
            added = additions(before.get('path_matcher'), after.get('path_matcher'))
            if address.endswith('.redirect'):
                exact_block(added, {'name': name, 'default_url_redirect': [{
                    'host_redirect': request['hostname'], 'https_redirect': True,
                    'redirect_response_code': 'MOVED_PERMANENTLY_DEFAULT', 'strip_query': False,
                    **{k: v for k, v in added.get('default_url_redirect', [{}])[0].items() if v is None}}]})
            else:
                backend = added.get('default_service')
                expected_backend = f"projects/{values['project_id']}/global/backendServices/{name}"
                require(backend in (None, expected_backend, 'https://www.googleapis.com/compute/v1/' + expected_backend),
                        'new matcher backend mismatch')
                exact_block(added, {'name': name, 'default_service': backend})
                index = after['path_matcher'].index(added)
                allowed_unknown.add(('path_matcher', index, 'default_service'))
                require(backend is not None or ('path_matcher', index, 'default_service') in
                        set(unknown_paths(change.get('after_unknown', {}))), 'unresolved backend must be provider-computed')
        require(set(unknown_paths(change.get('after_unknown', {}))) <= allowed_unknown, 'unreviewable existing edge change')
    require(seen == creates | UPDATES, 'exact application resources and three additive updates required')
    return creates


def output_route(state, request, values):
    outputs = state['outputs']
    routes = outputs.get('application_routes', {}).get('value', {})
    require(set(routes) == set(values.get('routes', {})), 'complete application output set required')
    for key, row in routes.items():
        require(set(row) == {'frontend_ip', 'hostname', 'backend_service', 'dns_authorization_record'} and
                row['frontend_ip'] == outputs['frontend_ip']['value'] and
                row['hostname'] == values['routes'][key]['hostname'] and
                row['backend_service'] == values.get('name', 'railshot-gcp-edge') + '-' + hashlib.sha256(key.encode()).hexdigest()[:16],
                'application output does not match configured binding')
        require(ipaddress.IPv4Address(row['frontend_ip']).is_global, 'public GCP frontend required')
        record = row['dns_authorization_record']
        if values['routes'][key].get('certificate_id'):
            require(record is None, 'shared certificate must not allocate per-app authorization')
            continue
        require(isinstance(record, dict) and record.get('type') == 'CNAME' and
                isinstance(record.get('name'), str) and isinstance(record.get('data'), str) and
                re.fullmatch(r'_acme-challenge(?:_[a-z0-9-]+)?\.' + re.escape(row['hostname']) + r'\.?', record['name']) and
                re.fullmatch(r'[a-z0-9.-]+\.authorize\.certificatemanager\.goog\.?', record['data']), 'bound certificate DNS record required')
    return {'status': 'configured', 'phase': 'applied', 'application_id': request['application_id'],
            **routes[request['application_id']], 'public_route_state': 'pending_verification'}


def recover(config_path, config, journal):
    """Adopt a completed interrupted apply only after a refreshed no-change plan."""
    row = read_private(journal)
    request = row['request']; checked_request(request)
    root = Path(config['state_dir'])
    matches = [work for work in root.glob('gcp-route-' + request['application_id'] + '-*')
               if (work / 'plan').is_file() and hashlib.sha256((work / 'plan').read_bytes()).hexdigest() == row['plan_sha256']]
    require(len(matches) == 1, 'original saved route plan required')
    work = matches[0]
    before = read_private(work / 'before.json') if (work / 'before.json').exists() else {
        'config': config, 'values': read_private(config['variables_file'])}
    old_config, values = before['config'], before['values']
    require(digest(old_config) == row['config_sha256'] and digest(values) == row['variables_sha256'],
            'original edge authority required')
    require(digest({name: hashlib.sha256((work / name).read_bytes()).hexdigest()
                    for name in ('main.tf', 'variables.tf', '.terraform.lock.hcl')}) == row['module_sha256'],
            'original route module required')
    candidate = read_private(work / 'candidate.json')
    require(candidate == {**values, 'routes': {**values.get('routes', {}), request['application_id']: application_route(request, values)}},
            'original route candidate required')
    command = lambda *args: native(['terraform', '-chdir=' + str(work), *args])
    plan = json.loads(command('show', '-json', str(work / 'plan')))
    creates = validate_plan(plan, request, values)
    state = read_private(config['state_file']); identities = owned(state)
    require(state['lineage'] == old_config['state_lineage'] and set(identities) == set(old_config['owned_resources']) | creates
            and all(identities[k] == v for k, v in old_config['owned_resources'].items()), 'incomplete applied route')
    updated_config = {**old_config, 'owned_resources': identities}
    require(config in (old_config, updated_config) and read_private(config['variables_file']) in (values, candidate),
            'edge authority changed since interrupted apply')
    result = output_route(state, request, candidate)
    # Terraform refresh is read-only here. A partial apply or actual cloud drift
    # remains an operator repair; never replay the original stale apply plan.
    command('plan', '-input=false', '-lock-timeout=5s', '-var-file=' + str(work / 'candidate.json'),
            '-out=' + str(work / 'recovery-plan'))
    observed = json.loads(command('show', '-json', str(work / 'recovery-plan')))
    require(not observed.get('errored') and all(item['change']['actions'] == ['no-op']
            or item.get('mode') == 'data' and item['change']['actions'] == ['read']
            for item in observed.get('resource_changes', []))
            and all(change['actions'] == ['no-op'] for change in observed.get('output_changes', {}).values()),
            'interrupted route still requires cloud reconciliation')
    require(read_private(config['state_file']) == state and config_at(config_path) == config,
            'edge changed during recovery observation')
    durable_write(config['variables_file'], encoded(candidate))
    durable_write(config_path, encoded(updated_config))
    durable_write(journal, encoded({**row, 'phase': 'applied', 'result': result, 'state_sha256_after': digest(state)}))
    return updated_config


def ensure(config_path, request, *, dns_config_path=None):
    try:
        return _ensure(config_path, request, dns_config_path=dns_config_path)
    except RouteError:
        raise
    except (ValueError, OSError, RuntimeError) as error:
        raise RouteError('GCP_ROUTE_PREPARATION_FAILED') from error


def _ensure(config_path, request, *, dns_config_path=None):
    route, config = checked_request(request), config_at(config_path)
    with locked(config) as root:
        lifecycle_ready(root)
        require(config_at(config_path) == config, 'authority changed before locking')
        for existing in root.glob('gcp-route-*.json'):
            if read_private(existing).get('phase') != 'applied':
                try:
                    config = recover(config_path, config, existing)
                except Exception as error:
                    raise RouteError('GCP_ROUTE_RECONCILE_REQUIRED', unknown=True) from error
        state, values = bound_state(config), read_private(config['variables_file'])
        routes = values.get('routes', {})
        require(isinstance(routes, dict), 'route map required')
        key = request['application_id']
        if key in routes:
            require(routes[key].get('enabled', True) and
                    {k: routes[key][k] for k in ('hostname', 'node_port', 'health_path')} == route,
                    'existing application route changes require separate approval')
            return output_route(state, request, values)
        if not values.get('application_certificate') and (not isinstance(dns_config_path, str) or not Path(dns_config_path).is_absolute()):
            raise RouteError('GCP_CERTIFICATE_DNS_NOT_CONFIGURED')
        require(all(route['hostname'] != r['hostname'] and route['node_port'] != r['node_port'] for r in
                    [{**values, 'node_port': values.get('node_port', 30080)}, *routes.values()]),
                'hostname or NodePort already allocated')
        candidate = copy.deepcopy(values); candidate.setdefault('routes', {})[key] = application_route(request, values)
        files = {name: (SOURCE / name).read_bytes() for name in ('main.tf', 'variables.tf', '.terraform.lock.hcl')}
        module_sha = digest({name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})
        work = Path(tempfile.mkdtemp(prefix='gcp-route-' + key + '-', dir=root))
        for name, raw in files.items():
            durable_write(work / name, raw)
        durable_write(work / 'backend.tf.json', encoded({'terraform': {'backend': {'local': {'path': config['state_file']}}}}))
        durable_write(work / 'candidate.json', encoded(candidate))
        durable_write(work / 'before.json', encoded({'config': config, 'values': values}))
        command = lambda *args: native(['terraform', '-chdir=' + str(work), *args])
        command('init', '-input=false', '-lockfile=readonly')
        command('plan', '-input=false', '-lock=true', '-var-file=' + str(work / 'candidate.json'), '-out=' + str(work / 'plan'))
        plan = json.loads(command('show', '-json', str(work / 'plan')))
        creates = validate_plan(plan, request, values)
        row = {'phase': 'applying', 'request': request, 'config_sha256': digest(config), 'variables_sha256': digest(values),
               'state_sha256': digest(state), 'module_sha256': module_sha,
               'plan_sha256': hashlib.sha256((work / 'plan').read_bytes()).hexdigest()}
        require(config_at(config_path) == config and read_private(config['variables_file']) == values and
                bound_state(config) == state, 'authority changed during planning')
        journal = root / ('gcp-route-' + key + '.json'); durable_write(journal, encoded(row))
        try:
            require(hashlib.sha256((work / 'plan').read_bytes()).hexdigest() == row['plan_sha256'] and
                    digest({name: hashlib.sha256((work / name).read_bytes()).hexdigest() for name in files}) == module_sha,
                    'saved plan or copied module changed before apply')
            # Native GCP URL map/firewall operations routinely exceed 110 seconds.
            # Keep this below the route executor's 30-minute deadline.
            native(['terraform', '-chdir=' + str(work), 'apply', '-input=false', str(work / 'plan')], timeout=900,
                   env={'RAILSHOT_GCP_CERTIFICATE_DNS_CONFIG': dns_config_path or '',
                        'PYTHONPATH': str(Path(__file__).resolve().parent)})
            after = read_private(config['state_file']); identities = owned(after)
            require(after['lineage'] == config['state_lineage'] and set(identities) == set(config['owned_resources']) | creates and
                    all(identities[k] == v for k, v in config['owned_resources'].items()), 'applied state ownership mismatch')
            require(all(after.get('outputs', {}).get(k) == v for k, v in state.get('outputs', {}).items()
                        if k != 'application_routes'), 'existing edge outputs changed')
            result = output_route(after, request, candidate)
            durable_write(config['variables_file'], encoded(candidate))
            durable_write(config_path, encoded({**config, 'owned_resources': identities}))
            durable_write(journal, encoded({**row, 'phase': 'applied', 'result': result, 'state_sha256_after': digest(after)}))
            return result
        except Exception as error:
            timed_out = isinstance(error.__cause__, subprocess.TimeoutExpired)
            code = 'GCP_ROUTE_APPLY_TIMEOUT' if timed_out else 'GCP_ROUTE_APPLY_INCOMPLETE'
            if (work / 'certificate-dns-error.json').exists():
                code = 'GCP_CERTIFICATE_DNS_FAILED'
            durable_write(journal, encoded({**row, 'phase': 'unknown', 'error': {'code': code}}))
            raise RouteError(code, unknown=True) from error


execute = ensure


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--config', required=True); parser.add_argument('--request', required=True)
    parser.add_argument('--dns-config')
    args = parser.parse_args()
    print(json.dumps(ensure(args.config, read_private(args.request), dns_config_path=args.dns_config)))
