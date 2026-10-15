"""Owned mutable routes for two-worker kubeadm environments."""
import copy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import time
import uuid

import applications
import cluster_edge as cluster
import environment as runtime
import dns
import application_cleanup as cleanup

require = applications.require


def scope(config, environment_id):
    profile = config['environments'][environment_id]
    ingress = profile['ingress']
    registry = runtime.read_private(config['registry_file'])
    native, _ = runtime.registered_node(registry['targets'][environment_id], environment_id, 'routes.' + environment_id)
    require(native['runtime']['profile'] == 'kubeadm-1c2w', 'CLUSTER_APPLICATION_PROFILE_REQUIRED')
    require(native['runtime']['edge'] == ingress['cluster_edge_config_file'], 'CLUSTER_APPLICATION_EDGE_MISMATCH')
    prepared = cluster.validate(native)
    root = cluster.private_directory(ingress['cluster_edge_state_dir'])
    require(root.name == environment_id, 'CLUSTER_APPLICATION_STATE_MISMATCH')
    return native, prepared, root


def state(root, prepared):
    """Transfer only the installer's exact successful state; never adopt foreign state."""
    journal = cluster.private_json(root / 'journal.json')
    staged = cluster.digest({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in root.iterdir()
                             if path.suffix == '.tf' or path.name == '.terraform.lock.hcl'})
    require(staged == cluster.source_digest(prepared['config']['provider']), 'CLUSTER_APPLICATION_MODULE_CHANGED')
    require(journal.get('phase') == 'configured' and journal.get('binding_sha256') == prepared['binding_sha256'],
            'CLUSTER_APPLICATION_INSTALLATION_UNVERIFIED')
    current = cluster.state_identity(root / 'terraform.tfstate', prepared['config']['provider'])
    marker = root / 'applications.json'
    if marker.exists():
        record = cluster.private_json(marker)
        require(record.get('version') == 1 and record.get('phase') == 'ready'
                and record['initial_binding_sha256'] == journal['binding_sha256'] and record['state'] == current
                and record['module_sha256'] == cluster.source_digest(prepared['config']['provider']),
                'CLUSTER_APPLICATION_RECONCILIATION_REQUIRED')
        return record
    require(journal['state'] == current and journal['module_sha256'] == cluster.source_digest(prepared['config']['provider']),
            'CLUSTER_APPLICATION_STATE_ADOPTION_FORBIDDEN')
    values = cluster.private_json(root / 'terraform.tfvars.json')
    require(values == prepared['values'], 'CLUSTER_APPLICATION_VARIABLES_MISMATCH')
    record = {'version': 1, 'phase': 'ready', 'initial_binding_sha256': journal['binding_sha256'],
              'module_sha256': journal['module_sha256'], 'state': current, 'values': values, 'apps': {}}
    runtime.save(marker, record)
    return record


def route_addresses(provider, app_id, values, route):
    suffix = '[' + json.dumps(app_id) + ']'
    if provider == 'aws':
        result = {kind + '.app' + suffix for kind in ('aws_lb_target_group', 'aws_lb_target_group_attachment', 'aws_lb_listener_rule', 'aws_route53_record')}
        for node_id, node in values['compute_nodes'].items():
            result.add('aws_security_group_rule.target_from_alb[' + json.dumps(node['security_group_id'] + ':' + str(route['node_port'])) + ']')
            if node['private_ip'] != route['target_private_ip']:
                result.add('aws_lb_target_group_attachment.compute[' + json.dumps(app_id + ':' + node_id) + ']')
        return result
    if provider == 'gcp':
        import gcp_routes
        result = {kind + '.routes' + suffix for kind in gcp_routes.resource_kinds(route)}
        for node_id, node in values['compute_nodes'].items():
            if node['instance_name'] != values['instance_name'] or node['availability_zone'] != values['zone']:
                result.update(kind + '.route_compute[' + json.dumps(app_id + ':' + node_id) + ']'
                              for kind in ('google_compute_network_endpoint_group', 'google_compute_network_endpoint'))
        return result
    result = {kind + '.app' + suffix for kind in ('openstack_lb_pool_v2', 'openstack_lb_member_v2', 'openstack_lb_monitor_v2', 'openstack_lb_l7policy_v2')}
    result.update('openstack_lb_l7rule_v2.' + name + suffix for name in ('host', 'path'))
    result.update('openstack_lb_member_v2.compute[' + json.dumps(app_id + ':' + node_id) + ']'
                  for node_id, node in values['compute_nodes'].items() if node['private_ip'] != route['target_private_ip'])
    return result


def checked_plan(document, record, candidate, provider, app_id, route, action):
    require(not document.get('errored') and document.get('complete', True), 'CLUSTER_APPLICATION_PLAN_INCOMPLETE')
    require(all(cleanup.subset(value, document.get('variables', {}).get(key, {}).get('value')) for key, value in candidate.items()),
            'CLUSTER_APPLICATION_PLAN_VARIABLES_CHANGED')
    owned = record['state']['owned_resources']
    permitted = route_addresses(provider, app_id, candidate, route)
    changes, seen = [], set()
    for item in document.get('resource_changes', []):
        address, change = item['address'], item['change']
        require(address not in seen and not item.get('deposed') and not item.get('previous_address')
                and not change.get('importing') and not change.get('replace_paths'), 'CLUSTER_APPLICATION_REPLACEMENT_FORBIDDEN')
        seen.add(address)
        actions, before, after = change['actions'], change.get('before'), change.get('after')
        if item.get('mode') == 'data' and actions in (['read'], ['no-op']):
            continue
        if actions == ['no-op']:
            require(address in owned and before and after and before.get('id') == after.get('id') == owned[address],
                    'CLUSTER_APPLICATION_IDENTITY_CHANGED')
            continue
        if address in permitted:
            if actions == ['create']:
                require(action in ('apply', 'start') and address not in owned and before is None,
                        'CLUSTER_APPLICATION_FOREIGN_CREATE')
                if provider == 'aws' and item['type'] == 'aws_lb_target_group_attachment':
                    require(after.get('target_id') in [node['private_ip'] for node in candidate['compute_nodes'].values()]
                            and after.get('port') == route['node_port'], 'CLUSTER_APPLICATION_NON_COMPUTE_TARGET')
                elif provider == 'aws' and item['type'] == 'aws_security_group_rule':
                    require(after.get('security_group_id') in [node['security_group_id'] for node in candidate['compute_nodes'].values()]
                            and after.get('type') == 'ingress' and after.get('protocol') == 'tcp'
                            and after.get('from_port') == after.get('to_port') == route['node_port'],
                            'CLUSTER_APPLICATION_FOREIGN_NETWORK_CHANGE')
                elif provider == 'gcp' and '.route_compute[' in address:
                    node = next((node for node_id, node in candidate['compute_nodes'].items()
                                 if address.endswith('[' + json.dumps(app_id + ':' + node_id) + ']')), None)
                    require(node and after.get('project') == candidate['project_id'] and after.get('zone') == node['availability_zone'],
                            'CLUSTER_APPLICATION_NON_COMPUTE_TARGET')
                    if item['type'] == 'google_compute_network_endpoint':
                        require(after.get('instance') == node['instance_name'] and after.get('ip_address') == node['private_ip']
                                and after.get('port') == route['node_port'], 'CLUSTER_APPLICATION_NON_COMPUTE_TARGET')
                elif provider in ('aws', 'gcp'):
                    cleanup.validate_create(provider, address, after, route, candidate, app_id)
                elif item['type'] == 'openstack_lb_member_v2':
                    require(after.get('address') in [node['private_ip'] for node in candidate['compute_nodes'].values()]
                            and after.get('protocol_port') == route['node_port'], 'CLUSTER_APPLICATION_NON_COMPUTE_TARGET')
            elif actions == ['delete']:
                require(action in ('stop', 'delete') and address in owned and before.get('id') == owned[address] and after is None,
                        'CLUSTER_APPLICATION_FOREIGN_DELETE')
            elif actions == ['update']:
                fields = {'aws_lb_target_group': 'health_check', 'openstack_lb_monitor_v2': 'url_path',
                          'google_compute_health_check': 'http_health_check'}
                field = fields.get(item['type'])
                require(action == 'apply' and field and before.get('id') == after.get('id') == owned.get(address)
                        and {k: v for k, v in before.items() if k != field} == {k: v for k, v in after.items() if k != field},
                        'CLUSTER_APPLICATION_UPDATE_FORBIDDEN')
            else:
                require(False, 'CLUSTER_APPLICATION_ACTION_FORBIDDEN')
        else:
            require(actions == ['update'] and address in owned and before and after
                    and before.get('id') == after.get('id') == owned[address], 'CLUSTER_APPLICATION_SHARED_RESOURCE_CHANGED')
            if provider == 'aws':
                require(address == 'aws_security_group.alb' and {k: v for k, v in before.items() if k != 'egress'} ==
                        {k: v for k, v in after.items() if k != 'egress'}, 'CLUSTER_APPLICATION_SHARED_RESOURCE_CHANGED')
                left, right = (before, after) if action in ('apply', 'start') else (after, before)
                old = {cluster.encoded(row) for row in left['egress']}
                new = {cluster.encoded(row) for row in right['egress']}
                require(old <= new, 'CLUSTER_APPLICATION_FOREIGN_NETWORK_CHANGE')
                for raw in new - old:
                    changed = json.loads(raw)
                    require(changed.get('protocol') == 'tcp' and changed.get('from_port') == changed.get('to_port') == route['node_port']
                            and changed.get('cidr_blocks') in [[node['private_ip'] + '/32'] for node in candidate['compute_nodes'].values()]
                            and not any(changed.get(key) for key in ('ipv6_cidr_blocks', 'prefix_list_ids', 'security_groups', 'self')),
                            'CLUSTER_APPLICATION_FOREIGN_NETWORK_CHANGE')
            elif provider == 'gcp':
                cleanup.validate_shared('gcp', address, before, after, change.get('after_unknown', {}), route, app_id,
                                        candidate, 'start' if action in ('apply', 'start') else action)
            else:
                require(False, 'CLUSTER_APPLICATION_SHARED_RESOURCE_CHANGED')
        changes.append({'address': address, 'actions': actions})
    for drift in document.get('resource_drift', []):
        require(next((item['change']['actions'] for item in document.get('resource_changes', []) if item['address'] == drift['address']), None) == ['no-op'],
                'CLUSTER_APPLICATION_DRIFT_REQUIRES_REVIEW')
    require(set(owned) <= seen, 'CLUSTER_APPLICATION_OWNED_RESOURCE_OMITTED')
    return changes


def new_route(prepared, record, request):
    provider, values = prepared['config']['provider'], record['values']
    primary = prepared['workers'][0]
    require(all(key == request['application_id'] or (value.get('host', value.get('hostname')) != request['hostname']
                and value['node_port'] != request['node_port']) for key, value in values.get('routes', {}).items()),
            'CLUSTER_APPLICATION_ROUTE_CONFLICT')
    route = {'node_port': request['node_port'], 'health_path': request['health_path']}
    if provider == 'gcp':
        import gcp_routes
        return gcp_routes.application_route({**route, 'hostname': request['hostname']}, values)
    route.update(host=request['hostname'], path_prefix='/', expected_status=200, target_private_ip=primary['private_ipv4'])
    if provider == 'aws':
        used = {row['priority'] for row in values['routes'].values()}
        prior = record['apps'].get(request['application_id'], {}).get('route')
        priority = prior['priority'] if prior else next((value for value in range(100, 50000) if value not in used), None)
        require(priority is not None, 'CLUSTER_APPLICATION_ROUTE_CAPACITY')
        route.update(priority=priority, provider_kind='aws', manage_dns=True,
                     target_security_group_id=prepared['config']['worker_bindings'][primary['resource_id']]['security_group_id'], enabled=True)
    else:
        route['member_subnet_id'] = prepared['config']['worker_bindings'][primary['resource_id']]['subnet_id']
    return route


def executor(prepared, native, root):
    stack = ExitStack()
    kube = stack.enter_context(runtime.runtime_kubectl(native)) if prepared['config']['provider'] == 'openstack' else None
    runner = cluster.NativeRunner(prepared, time.monotonic() + 1800, None, kube)
    runner.directory = root
    return stack, runner, kube


def plan_native(prepared, root, record, request, action, runner):
    app_id = request['application_id']
    previous = record['apps'].get(app_id)
    require(previous is None or all(previous['request'].get(key) == request.get(key) for key in ('application_id', 'environment_id', 'hostname', 'app', 'tenant', 'node_port')),
            'CLUSTER_APPLICATION_OWNER_CHANGED')
    require(action in ('apply', 'delete') or previous is not None, 'CLUSTER_APPLICATION_ROUTE_NOT_FOUND')
    require(previous is not None or app_id not in record['values'].get('routes', {}), 'CLUSTER_APPLICATION_ROUTE_UNOWNED')
    route = new_route(prepared, record, request) if action == 'apply' or previous is None else previous['route']
    candidate = copy.deepcopy(record['values'])
    if action in ('stop', 'delete'):
        candidate['routes'].pop(app_id, None)
    else:
        candidate.setdefault('routes', {})[app_id] = route
    work = cluster.private_directory(root / 'app-plans' / uuid.uuid4().hex)
    variables = work / 'candidate.json'; runtime.save(variables, candidate)
    plan_file = work / 'change.tfplan'
    runner(['terraform', 'plan', '-input=false', '-lock-timeout=5s', '-var-file=' + str(variables), '-out=' + str(plan_file)], cwd=root)
    document = json.loads(runner(['terraform', 'show', '-json', str(plan_file)], cwd=root))
    changes = checked_plan(document, record, candidate, prepared['config']['provider'], app_id, route, action)
    result = {'version': 1, 'application_id': app_id, 'provider': prepared['config']['provider'], 'action': action,
              'resources': [{'kind': change['address'].split('.')[0], 'name': request['hostname']} for change in changes],
              'retained': [{'kind': 'shared_load_balancer', 'name': request['environment_id']}, {'kind': 'shared_runtime', 'name': request['environment_id']}],
              'private': {'work': str(work), 'state': record['state'], 'candidate': candidate, 'request': request,
                          'route': route, 'no_route': previous is None and action == 'delete',
                          'health_path': route['health_path'], 'plan_sha256': hashlib.sha256(plan_file.read_bytes()).hexdigest()}}
    result['plan_sha256'] = cluster.digest(result)
    runtime.save(work / 'review.json', result)
    runtime.save(work / 'intent.json', {'phase': 'planned', 'plan_sha256': result['plan_sha256']})
    return result


def apply_native(prepared, root, record, plan, runner):
    private = plan['private']; work = Path(private['work'])
    require(work.parent == root / 'app-plans' and runtime.read_private(work / 'review.json') == plan
            and hashlib.sha256((work / 'change.tfplan').read_bytes()).hexdigest() == private['plan_sha256'],
            'CLUSTER_APPLICATION_PLAN_CHANGED')
    intent = runtime.read_private(work / 'intent.json')
    if intent['phase'] == 'native_applied':
        require(record['state'] == intent['state'] and record['values'] == private['candidate'], 'CLUSTER_APPLICATION_PLAN_CHANGED')
        return record
    require(private['state'] == record['state'], 'CLUSTER_APPLICATION_PLAN_STALE')
    require(intent['phase'] == 'planned', 'CLUSTER_APPLICATION_RECONCILIATION_REQUIRED')
    runtime.save(work / 'intent.json', {'phase': 'applying', 'plan_sha256': plan['plan_sha256']})
    runtime.save(root / 'applications.json', {**record, 'phase': 'applying', 'pending': str(work)})
    runner(['terraform', 'apply', '-input=false', '-auto-approve', str(work / 'change.tfplan')], cwd=root)
    runtime.save(root / 'terraform.tfvars.json', private['candidate'])
    runner(['terraform', 'plan', '-input=false', '-lock-timeout=5s', '-out=' + str(work / 'verify.tfplan')], cwd=root)
    verified = json.loads(runner(['terraform', 'show', '-json', str(work / 'verify.tfplan')], cwd=root))
    require(all(item['change']['actions'] in (['no-op'], ['read']) for item in verified.get('resource_changes', [])),
            'CLUSTER_APPLICATION_APPLY_UNVERIFIED')
    return {**record, 'state': cluster.state_identity(root / 'terraform.tfstate', prepared['config']['provider']),
            'values': private['candidate']}


def recover(native, prepared, root):
    """Read back a completed native candidate; never replay an uncertain TF apply."""
    marker = root / 'applications.json'
    if not marker.exists() or runtime.read_private(marker).get('phase') != 'applying':
        return
    record = runtime.read_private(marker); work = Path(record['pending'])
    require(work.parent == root / 'app-plans', 'CLUSTER_APPLICATION_PLAN_CHANGED')
    plan = runtime.read_private(work / 'review.json'); private = plan['private']
    require(private['state'] == record['state'] and hashlib.sha256((work / 'change.tfplan').read_bytes()).hexdigest() == private['plan_sha256'],
            'CLUSTER_APPLICATION_PLAN_CHANGED')
    current = cluster.state_identity(root / 'terraform.tfstate', prepared['config']['provider'])
    stack, runner, _ = executor(prepared, native, root)
    with stack:
        original = json.loads(runner(['terraform', 'show', '-json', str(work / 'change.tfplan')], cwd=root))
        changes = checked_plan(original, record, private['candidate'], plan['provider'], plan['application_id'], private['route'], plan['action'])
        expected = set(record['state']['owned_resources'])
        for change in changes:
            if change['actions'] == ['create']: expected.add(change['address'])
            if change['actions'] == ['delete']: expected.discard(change['address'])
        require(set(current['owned_resources']) == expected and all(current['owned_resources'].get(key) == value
            for key, value in record['state']['owned_resources'].items() if key in expected),
            'CLUSTER_APPLICATION_PARTIAL_NATIVE_REQUIRES_REVIEW')
        readback = work / 'reconcile.tfplan'
        runner(['terraform', 'plan', '-input=false', '-lock-timeout=5s', '-var-file=' + str(work / 'candidate.json'), '-out=' + str(readback)], cwd=root)
        document = json.loads(runner(['terraform', 'show', '-json', str(readback)], cwd=root))
        cluster.validate_plan(document, plan['provider'], current['owned_resources'])
        runtime.save(root / 'terraform.tfvars.json', private['candidate'])
        changed = {**record, 'state': current, 'values': private['candidate'], 'phase': 'ready'}
        changed.pop('pending', None)
        changed['apps'][plan['application_id']] = {'request': private['request'], 'route': private['route'], 'status': 'partial'}
        runtime.save(marker, changed)
        runtime.save(work / 'intent.json', {'phase': 'native_applied', 'state': current, 'plan_sha256': plan['plan_sha256']})
        if plan['action'] in ('apply', 'start'):
            try:
                public_route(prepared, root, private['request'], runner, observe=True)
            except Exception:
                return  # A new owned cleanup plan is possible; public readiness stays false.
            finish(root, changed, plan, 'started')


def tunnel_binding(prepared, root, request):
    import application_routes
    tunnel = application_routes.tunnel
    original = runtime.read_private(prepared['config']['tunnel']['config_file'])
    identities = runtime.read_private(root / 'tunnel-objects.json')
    config = {key: original.get(key) for key in tunnel.RENDER_FIELDS}
    config.update(version=1, state_dir=str(root / 'application-tunnel'),
                  registry_file=request['resource']['registry_file'], environment_id=request['environment_id'],
                  resource_id=request['resource']['resource_id'], runtime_private_address=request['resource']['private_address'],
                  base_domain=request['ingress']['base_domain'],
                  configmap_uid=identities['ConfigMap/' + original['name'] + '-config'],
                  deployment_uid=identities['Deployment/' + original['name']])
    path = root / 'application-tunnel.json'
    if path.exists():
        require(runtime.read_private(path) == config, 'CLUSTER_APPLICATION_TUNNEL_CHANGED')
    else:
        runtime.save(path, config)
    return tunnel, str(path), {key: request[key] for key in ('environment_id', 'application_id', 'app', 'tenant', 'hostname')}


def public_route(prepared, root, request, runner, *, observe=False):
    provider = prepared['config']['provider']
    outputs = {key: value['value'] for key, value in json.loads(runner(['terraform', 'output', '-json'], cwd=root)).items()}
    if provider == 'aws':
        require(request['application_id'] in outputs['target_group_arns'], 'CLUSTER_APPLICATION_READBACK_MISSING')
        return {'provider': provider, 'dns': 'route53-managed'}
    config_file = request['ingress']['dns_config_file']
    if provider == 'gcp':
        route = outputs['application_routes'][request['application_id']]
        auth = route.get('dns_authorization_record')
        if auth:
            require(auth['type'] == 'CNAME' and auth['data'].rstrip('.').endswith('.authorize.certificatemanager.goog'),
                    'CLUSTER_APPLICATION_CERTIFICATE_SCOPE_INVALID')
            cluster.dns_readback(runner, config_file, {'application_id': request['application_id'], 'purpose': 'certificate',
                'application_hostname': request['hostname'], 'hostname': auth['name'].rstrip('.'),
                'type': 'CNAME', 'content': auth['data'].rstrip('.')}, observe=observe)
        desired = {'type': 'A', 'content': outputs['frontend_ip']}
    else:
        tunnel, path, tunnel_request = tunnel_binding(prepared, root, request)
        if observe:
            with tunnel.lifecycle_session(path, tunnel_request) as (config, kube):
                snapshot = tunnel.lifecycle_snapshot(config, kube, tunnel_request)
                require(snapshot['owners'].get(request['hostname']) == request['application_id'], 'CLUSTER_APPLICATION_TUNNEL_MISSING')
            result = {'status': 'succeeded', 'tunnel_id': config['tunnel_id']}
        else:
            result = tunnel.ensure(path, tunnel_request)
        require(result.get('status') == 'succeeded', 'CLUSTER_APPLICATION_TUNNEL_UNVERIFIED')
        desired = {'type': 'CNAME', 'content': result['tunnel_id'] + '.cfargotunnel.com', 'proxied': True}
    proof = cluster.dns_readback(runner, config_file, {'application_id': request['application_id'],
        'hostname': request['hostname'], **desired}, observe=observe)
    return {'provider': provider, 'dns_record_id': proof['record_id']}


def finish(root, record, plan, status):
    request = plan['private']['request']
    record['apps'][request['application_id']] = {'request': request, 'route': plan['private']['route'], 'status': status}
    record.update(phase='ready'); record.pop('pending', None)
    runtime.save(root / 'applications.json', record)
    receipt = {'status': 'succeeded', 'application_id': request['application_id'], 'provider': plan['provider'],
               'phase': status, 'plan_sha256': plan['plan_sha256'], 'resources': plan['resources'], 'https_verified': False}
    runtime.save(Path(plan['private']['work']) / 'intent.json', {'phase': 'succeeded', 'receipt': receipt})
    return receipt


def ensure(config, publication_request, result, request):
    native, prepared, root = scope(config, request['environment_id'])
    with cluster.locked(root):
        recover(native, prepared, root)
        record = state(root, prepared)
        stack, runner, _ = executor(prepared, native, root)
        with stack:
            plan = plan_native(prepared, root, record, request, 'apply', runner)
            changed = apply_native(prepared, root, record, plan, runner)
            evidence = public_route(prepared, root, request, runner)
            finish(root, changed, plan, 'started')
    directory = Path(config['state_dir']) / request['application_id'] / 'deployments' / publication_request['deployment_id']
    output = {**result, 'phase': 'route_configured', 'public_route_state': 'configured', 'https_verified': False,
              'edge': {**evidence, 'compute_ids': [node['resource_id'] for node in prepared['workers']]},
              'route_request_sha256': hashlib.sha256((directory / 'route-request.json').read_bytes()).hexdigest()}
    runtime.save(directory / 'route-result.json', output)
    return output


def binding_scope(binding):
    ingress = binding['ingress']
    native, _ = runtime.registered_node({'profile': 'kubeadm-1c2w', 'cluster_file': ingress['cluster_file'], 'purpose': 'runtime'},
                                        binding['environment_id'], 'lifecycle.' + binding['application_id'])
    require(native['runtime']['edge'] == ingress['cluster_edge_config_file'], 'CLUSTER_APPLICATION_EDGE_MISMATCH')
    prepared = cluster.validate(native)
    root = cluster.private_directory(ingress['cluster_edge_state_dir'])
    require(root.name == binding['environment_id'], 'CLUSTER_APPLICATION_STATE_MISMATCH')
    return native, prepared, root


def lifecycle_plan(binding, action):
    native, prepared, root = binding_scope(binding)
    with cluster.locked(root):
        recover(native, prepared, root)
        record = state(root, prepared)
        previous = record['apps'].get(binding['application_id'])
        require(previous is not None or action == 'delete', 'CLUSTER_APPLICATION_ROUTE_NOT_FOUND')
        request = previous['request'] if previous else {'version': 1, 'environment_id': binding['environment_id'],
            'application_id': binding['application_id'], 'hostname': binding['hostname'], 'app': binding['registered']['app'],
            'tenant': binding['registered']['tenant'], 'node_port': binding['registered']['target']['node_port'],
            'health_path': '/', 'provider': binding['provider'], 'ingress': binding['ingress']}
        require(request['hostname'] == binding['hostname'], 'CLUSTER_APPLICATION_OWNER_CHANGED')
        stack, runner, _ = executor(prepared, native, root)
        with stack:
            plan = plan_native(prepared, root, record, request, action, runner)
        private = plan['private']; private['binding_sha256'] = cluster.digest(binding)
        if binding['provider'] != 'aws':
            dns_config = dns.config_at(request['ingress']['dns_config_file'])
            with dns.locked(dns_config):
                private['dns'] = dns.application_snapshot(dns_config, binding['application_id'], binding['hostname'])
            private['dns_config'] = dns_config
        if binding['provider'] == 'openstack' and not private['no_route']:
            tunnel, path, tunnel_request = tunnel_binding(prepared, root, request)
            private['tunnel'] = tunnel.lifecycle_plan(path, tunnel_request, action)
        plan.pop('plan_sha256'); plan['plan_sha256'] = cluster.digest(plan)
        runtime.save(Path(private['work']) / 'review.json', plan)
        runtime.save(Path(private['work']) / 'intent.json', {'phase': 'planned', 'plan_sha256': plan['plan_sha256']})
        return plan


def reviewed(binding, action, plan, root):
    private = plan['private']; work = Path(private['work'])
    require(work.parent == root / 'app-plans' and runtime.read_private(work / 'review.json') == plan
            and private['binding_sha256'] == cluster.digest(binding) and plan['action'] == action,
            'CLUSTER_APPLICATION_PLAN_CHANGED')
    intent = runtime.read_private(work / 'intent.json')
    require(intent['phase'] in ('planned', 'native_applied', 'succeeded'), 'CLUSTER_APPLICATION_RECONCILIATION_REQUIRED')
    return intent


def lifecycle_validate(binding, action, plan):
    native, prepared, root = binding_scope(binding)
    with cluster.locked(root):
        recover(native, prepared, root)
        intent = reviewed(binding, action, plan, root)
        if intent['phase'] == 'succeeded':
            return 'succeeded'
        record = state(root, prepared); private = plan['private']
        require(record['state'] == (intent['state'] if intent['phase'] == 'native_applied' else private['state']), 'CLUSTER_APPLICATION_PLAN_STALE')
        if binding['provider'] != 'aws':
            config = dns.config_at(private['request']['ingress']['dns_config_file'])
            with dns.locked(config):
                if intent['phase'] == 'native_applied' and action == 'delete':
                    dns.reconcile_application_deletions(config, binding['application_id'], binding['hostname'])
                observed = dns.application_snapshot(config, binding['application_id'], binding['hostname'])
                require(config == private['dns_config'] and (observed == private['dns'] or action == 'delete'
                        and intent['phase'] == 'native_applied' and all(row in private['dns'] for row in observed)),
                        'CLUSTER_APPLICATION_DNS_CHANGED')
        if binding['provider'] == 'openstack' and not private['no_route']:
            tunnel, path, request = tunnel_binding(prepared, root, private['request'])
            tunnel.lifecycle_validate(path, request, action, private['tunnel'])
        return 'not_started'


def lifecycle_execute(binding, action, plan):
    lifecycle_validate(binding, action, plan)
    native, prepared, root = binding_scope(binding)
    with cluster.locked(root):
        intent = reviewed(binding, action, plan, root)
        if intent['phase'] == 'succeeded':
            return intent['receipt']
        record = state(root, prepared); private = plan['private']; request = private['request']
        stack, runner, _ = executor(prepared, native, root)
        with stack:
            changed = apply_native(prepared, root, record, plan, runner)
            if binding['provider'] == 'openstack' and not private['no_route']:
                tunnel, path, tunnel_request = tunnel_binding(prepared, root, request)
                tunnel.lifecycle_execute(path, tunnel_request, action, private['tunnel'])
            if action == 'delete' and binding['provider'] != 'aws':
                with dns.locked(private['dns_config']):
                    remaining = dns.application_snapshot(private['dns_config'], binding['application_id'], binding['hostname'])
                    require(all(row in private['dns'] for row in remaining), 'CLUSTER_APPLICATION_DNS_CHANGED')
                    dns.remove_application(private['dns_config'], binding['application_id'], binding['hostname'], remaining)
            if action == 'start':
                public_route(prepared, root, request, runner, observe=True)
            return finish(root, changed, plan, {'start': 'started', 'stop': 'stopped', 'delete': 'deleted'}[action])


def teardown(config, environment_id):
    """Destroy the environment-owned edge only after all app routes are revoked."""
    native, prepared, root = scope(config, environment_id)
    with cluster.locked(root):
        receipt_path = root / 'teardown.json'
        previous = None
        if receipt_path.exists():
            previous = runtime.read_private(receipt_path)
            require(previous.get('environment_id') == environment_id, 'CLUSTER_EDGE_TEARDOWN_SCOPE_CHANGED')
            if previous.get('status') == 'succeeded':
                return previous
            require(previous.get('binding_sha256') == prepared['binding_sha256'], 'CLUSTER_EDGE_TEARDOWN_SCOPE_CHANGED')
        record = previous['record'] if previous else state(root, prepared)
        require(all(row['status'] == 'deleted' for row in record['apps'].values()), 'CLUSTER_EDGE_APPLICATIONS_REMAIN')
        stack, runner, kube = executor(prepared, native, root)
        with stack:
            plan_path = root / 'teardown.tfplan'
            runner(['terraform', 'plan', '-destroy', '-input=false', '-lock-timeout=5s', '-out=' + str(plan_path)], cwd=root)
            document = json.loads(runner(['terraform', 'show', '-json', str(plan_path)], cwd=root))
            require(not document.get('errored') and document.get('complete', True), 'CLUSTER_EDGE_TEARDOWN_PLAN_INVALID')
            changes = [item for item in document.get('resource_changes', []) if item.get('mode') != 'data']
            owned = record['state']['owned_resources']
            # Fresh refresh/plan after an interruption may contain only the
            # remaining subset of previously owned IDs. Replacements are refused.
            require(({item['address'] for item in changes} <= set(owned) if previous else
                     {item['address'] for item in changes} == set(owned)) and all(
                item['change']['actions'] == ['delete'] and item['change']['before'].get('id') == owned[item['address']]
                and item['change'].get('after') is None and not item.get('deposed') and not item.get('previous_address')
                for item in changes), 'CLUSTER_EDGE_TEARDOWN_OWNERSHIP_CHANGED')
            private_dns = None
            if prepared['config']['provider'] != 'aws':
                cf = dns.config_at(prepared['config']['dns_config_file'])
                initial = {**prepared['fixed_routes'][0], 'application_id': prepared['config']['cluster_id']}
                if prepared['config']['provider'] == 'openstack':
                    initial = prepared['config']['tunnel']['request']
                with dns.locked(cf):
                    if previous:
                        dns.reconcile_application_deletions(cf, initial['application_id'], initial['hostname'])
                    private_dns = (cf, initial, dns.application_snapshot(cf, initial['application_id'], initial['hostname']))
                    if previous:
                        require(all(item in previous['dns'] for item in private_dns[2]), 'CLUSTER_EDGE_DNS_OWNER_CHANGED')
            tunnel_namespace = None
            if prepared['config']['provider'] == 'openstack':
                import lifecycle_runtime
                tunnel = runtime.read_private(prepared['config']['tunnel']['config_file'])
                identities = runtime.read_private(root / 'tunnel-objects.json')
                ns = tunnel['namespace']; namespace = kube(ns, 'get', 'namespace', ns, '--ignore-not-found', '-o', 'json')
                require((not namespace and previous is not None) or (namespace and namespace['metadata']['uid'] == identities['Namespace/' + ns]
                        and namespace['metadata']['labels'].get('railshot.io/cluster-edge-owner') == environment_id),
                        'CLUSTER_EDGE_TUNNEL_OWNER_CHANGED')
                objects = lifecycle_runtime.discover(kube, ns) if namespace else []
                by_uid = {item['metadata']['uid']: item for _, item in objects}
                def owned_descendant(uid, seen):
                    if uid in identities.values():
                        return True
                    if uid in seen or uid not in by_uid:
                        return False
                    owners = by_uid[uid]['metadata'].get('ownerReferences', [])
                    return bool(owners) and all(owned_descendant(owner['uid'], seen | {uid}) for owner in owners)
                for _, item in objects:
                    meta = item['metadata']; key = item['kind'] + '/' + meta['name']
                    if key in identities:
                        require(meta['uid'] == identities[key] and meta.get('labels', {}).get('railshot.io/cluster-edge-owner') == environment_id,
                                'CLUSTER_EDGE_TUNNEL_OWNER_CHANGED')
                    elif key not in ('ServiceAccount/default', 'ConfigMap/kube-root-ca.crt'):
                        owners = meta.get('ownerReferences', [])
                        require(owners and all(owned_descendant(owner['uid'], {meta['uid']}) for owner in owners),
                                'CLUSTER_EDGE_TUNNEL_FOREIGN_RESOURCE')
                tunnel_namespace = (ns, namespace) if namespace else None
            runtime.save(receipt_path, {'status': 'unknown', 'environment_id': environment_id,
                                       'binding_sha256': prepared['binding_sha256'], 'record': record,
                                       'dns': previous['dns'] if previous else private_dns[2] if private_dns else [],
                                       'plan_sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest()})
            if tunnel_namespace:
                ns, namespace = tunnel_namespace
                if not namespace['metadata'].get('deletionTimestamp'):
                    kube(ns, 'delete', '--raw', '/api/v1/namespaces/' + ns, '-f', '-', document={
                        'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Foreground',
                        'preconditions': {key: namespace['metadata'][key] for key in ('uid', 'resourceVersion')}})
                deadline = time.monotonic() + 120
                while kube(ns, 'get', 'namespace', ns, '--ignore-not-found', '-o', 'json'):
                    require(time.monotonic() < deadline, 'CLUSTER_EDGE_TUNNEL_DELETE_UNVERIFIED'); time.sleep(1)
            if private_dns:
                cf, initial, snapshot = private_dns
                with dns.locked(cf):
                    dns.remove_application(cf, initial['application_id'], initial['hostname'], snapshot)
            runner(['terraform', 'apply', '-input=false', '-auto-approve', str(plan_path)], cwd=root)
            observed = runtime.read_private(root / 'terraform.tfstate')
            require(not any(item.get('mode') == 'managed' and item.get('instances') for item in observed.get('resources', [])),
                    'CLUSTER_EDGE_TEARDOWN_UNVERIFIED')
            result = {'status': 'succeeded', 'environment_id': environment_id, 'data_policy': 'retain',
                      'retained_resources': [{'kind': 'persistent_storage', 'id': environment_id, 'status': 'retained'}]}
            runtime.save(receipt_path, result)
            return result
