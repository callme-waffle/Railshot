#!/usr/bin/env python3
"""Bind an already prepared customer VM to a personal environment; never deploy an app."""
import argparse
import base64
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

import applications as apps
import environment as runtime
import openstack_routes

_renderer_spec = importlib.util.spec_from_file_location(
    'railshot_personal_tunnel_render', runtime.ROOT / 'deployment/cloudflared/render.py')
tunnel_renderer = importlib.util.module_from_spec(_renderer_spec)
_renderer_spec.loader.exec_module(tunnel_renderer)
_tunnel_spec = importlib.util.spec_from_file_location(
    'railshot_personal_tunnel_registration', runtime.ROOT / 'deployment/cloudflared/register.py')
tunnel_registration = importlib.util.module_from_spec(_tunnel_spec)
_tunnel_spec.loader.exec_module(tunnel_registration)


def require(value, code='RUNTIME_CONFIGURATION_INVALID'):
    if not value:
        raise ValueError(code)


def digest(value):
    return hashlib.sha256(runtime.bridge.encoded(value)).hexdigest()


def configured(path, code, *, raw=False):
    try:
        return runtime.read_private(path, raw=raw)
    except (OSError, TypeError, ValueError):
        raise ValueError(code) from None


def load_operator_config(config_path):
    common = runtime.read_private(config_path)
    require(apps.exact(common, ('version', 'application_template', 'template_environment', 'state_dir', 'route_profiles'))
            and common['version'] == 1 and isinstance(common['route_profiles'], dict) and common['route_profiles'],
            'RUNTIME_OPERATOR_NOT_CONFIGURED')
    template = apps.load_config(common['application_template'])
    require(common['template_environment'] in template['environments'], 'RUNTIME_APPLICATION_TEMPLATE_INVALID')
    generic = template['environments'][common['template_environment']]
    require(generic['provider'] == 'openstack', 'RUNTIME_APPLICATION_TEMPLATE_INVALID')
    pull = configured(generic['pull_secret_file'], 'RUNTIME_PULL_SECRET_INVALID')
    require(isinstance(pull, dict) and isinstance(pull.get('auths'), dict) and pull['auths'],
            'RUNTIME_PULL_SECRET_INVALID')
    profiles = {}
    for profile_id, value in common['route_profiles'].items():
        require(isinstance(profile_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', profile_id)
                and apps.exact(value, ('management_network', 'placement', 'edge_template',
                                       'worker_base_file', 'tunnel_template', 'dns_config_file')),
                'RUNTIME_ROUTE_PROFILE_INVALID')
        for key in ('management_network', 'placement'):
            require(isinstance(value[key], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value[key]),
                    'RUNTIME_ROUTE_PROFILE_INVALID')
        edge = configured(value['edge_template'], 'RUNTIME_EDGE_TEMPLATE_INVALID')
        require(apps.exact(edge, ('version', 'provider', 'base_domain', 'controller', 'proxy'))
                and edge['version'] == 1 and edge['provider'] == 'openstack', 'RUNTIME_EDGE_TEMPLATE_INVALID')
        try:
            openstack_routes.ssh_prefix(edge['controller'])
            openstack_routes.ssh_prefix(edge['proxy'])
        except (KeyError, OSError, TypeError, ValueError):
            raise ValueError('RUNTIME_EDGE_TEMPLATE_INVALID') from None
        worker_base = configured(value['worker_base_file'], 'RUNTIME_EDGE_WORKER_BASE_INVALID')
        try:
            worker_base = openstack_routes.worker.operator_base(worker_base)
        except (TypeError, ValueError):
            raise ValueError('RUNTIME_EDGE_WORKER_BASE_INVALID') from None
        tunnel = configured(value['tunnel_template'], 'RUNTIME_TUNNEL_TEMPLATE_INVALID')
        require(apps.exact(tunnel, ('version', 'base_domain', 'name', 'tunnel_id', 'credentials_file',
                                    'origin_vip', 'ca_file')) and tunnel['version'] == 1
                and tunnel['name'] == 'railshot-tunnel',
                'RUNTIME_TUNNEL_TEMPLATE_INVALID')
        try:
            tunnel_renderer.render(namespace='personal-00000000-0000-4000-8000-000000000000',
                name=tunnel['name'], tunnel_id=tunnel['tunnel_id'], credentials_secret=tunnel['name'] + '-credentials',
                hostnames=[], origin_vip=tunnel['origin_vip'], ca_configmap=tunnel['name'] + '-ca')
        except (TypeError, ValueError):
            raise ValueError('RUNTIME_TUNNEL_TEMPLATE_INVALID') from None
        credentials = configured(tunnel['credentials_file'], 'RUNTIME_TUNNEL_TEMPLATE_INVALID', raw=True)
        try:
            credential = json.loads(credentials)
        except (TypeError, ValueError, UnicodeError):
            raise ValueError('RUNTIME_TUNNEL_TEMPLATE_INVALID') from None
        require(isinstance(credential, dict) and credential.get('TunnelID') == tunnel['tunnel_id']
                and isinstance(credential.get('TunnelSecret'), str) and credential['TunnelSecret'],
                'RUNTIME_TUNNEL_TEMPLATE_INVALID')
        ca = configured(tunnel['ca_file'], 'RUNTIME_TUNNEL_TEMPLATE_INVALID', raw=True)
        require(isinstance(ca, bytes) and ca.startswith(b'-----BEGIN CERTIFICATE-----')
                and ca.rstrip().endswith(b'-----END CERTIFICATE-----'), 'RUNTIME_TUNNEL_TEMPLATE_INVALID')
        dns = configured(value['dns_config_file'], 'RUNTIME_DNS_CONFIGURATION_INVALID')
        require(apps.exact(dns, ('version', 'zone_id', 'base_domain', 'token_file', 'state_dir'))
                and dns['version'] == 1 and edge['base_domain'] == worker_base.get('base_domain') == tunnel['base_domain'] == dns['base_domain'],
                'RUNTIME_DNS_CONFIGURATION_INVALID')
        dns_token = configured(dns['token_file'], 'RUNTIME_DNS_CONFIGURATION_INVALID', raw=True)
        require(isinstance(dns_token, bytes) and 16 <= len(dns_token.strip()) <= 4096,
                'RUNTIME_DNS_CONFIGURATION_INVALID')
        profiles[profile_id] = {'selection': copy.deepcopy(value), 'edge': edge, 'worker_base': worker_base,
                                'tunnel': tunnel, 'dns': dns,
                                'credentials_sha256': hashlib.sha256(credentials).hexdigest(),
                                'ca_sha256': hashlib.sha256(ca).hexdigest()}
    return common, template, generic, profiles


def check_config(config_path):
    try:
        _, _, _, profiles = load_operator_config(config_path)
        require(all(callable(getattr(openstack_routes, name, None))
                    for name in ('verify_runtime', 'register_runtime', 'unregister_runtime')),
                'RUNTIME_EDGE_REGISTRATION_UNSUPPORTED')
        return {'ready': True, 'blockers': [], 'profile_count': len(profiles)}
    except Exception as error:
        code = str(error) if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', str(error)) else 'RUNTIME_OPERATOR_NOT_CONFIGURED'
        return {'ready': False, 'blockers': [{'code': code}]}


def select_route_profile(profiles, request, claim_root):
    evidence = request['evidence']
    candidates = [(name, row) for name, row in profiles.items()
                  if row['selection']['management_network'] == evidence['management_network']
                  and row['selection']['placement'] == evidence['placement']]
    if request['profile_id'] is not None:
        candidates = [(name, row) for name, row in candidates if name == request['profile_id']]
    available = []
    for name, row in candidates:
        path = claim_root / (digest(row['tunnel']['tunnel_id']) + '.json')
        if not path.exists():
            available.append((name, row))
            continue
        claim = runtime.read_private(path)
        expected = {'environment_id': request['target_id'], 'project_id': request['project_id'],
                    'resource_id': evidence['resource_id'], 'profile_id': name,
                    'tunnel_id': row['tunnel']['tunnel_id']}
        if claim == expected or (isinstance(claim, dict) and claim.get('status') == 'released'
                and claim.get('profile_id') == name and claim.get('tunnel_id') == row['tunnel']['tunnel_id']):
            available.append((name, row))
    # A profile is a pre-approved operator capacity slot. Pick a free slot
    # deterministically; an explicit profile_id remains an exact binding.
    require(available, 'ENVIRONMENT_OWNERSHIP_CONFLICT' if candidates else 'RUNTIME_ROUTE_PROFILE_MISMATCH')
    return sorted(available, key=lambda item: item[0])[0]


def tunnel_documents(profile, request):
    base = profile['tunnel']
    namespace, name = request['target_id'], base['name']
    credentials = runtime.read_private(base['credentials_file'], raw=True)
    ca = runtime.read_private(base['ca_file'], raw=True)
    labels = {'app.kubernetes.io/managed-by': 'railshot', 'railshot.io/registration': request['target_id']}
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name + '-credentials',
              'namespace': namespace, 'labels': labels}, 'type': 'Opaque',
              'data': {'credentials.json': base64.b64encode(credentials).decode()}}
    ca_document = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name + '-ca',
                   'namespace': namespace, 'labels': labels}, 'data': {'ca.pem': ca.decode()}}
    rendered = tunnel_renderer.render(namespace=namespace, name=name, tunnel_id=base['tunnel_id'],
        credentials_secret=name + '-credentials', hostnames=[], origin_vip=base['origin_vip'], ca_configmap=name + '-ca')
    for document in rendered['items']:
        document['metadata'].setdefault('labels', {}).update(labels)
        if document['kind'] == 'ConfigMap' and document['metadata']['name'] == name + '-config':
            document['metadata'].setdefault('annotations', {})[tunnel_registration.OWNERS] = '{}'
        if document['kind'] == 'Deployment':
            document['spec']['template']['metadata'].setdefault('labels', {}).update(labels)
    return [secret, ca_document, *rendered['items']]


def edge_inputs(profile, request):
    evidence, native = request['evidence'], request['evidence']['provider_binding']
    edge = {key: copy.deepcopy(profile['edge'][key])
            for key in ('version', 'provider', 'base_domain', 'controller', 'proxy')}
    edge['runtime_private_address'] = evidence['private_ipv4']
    binding = {'project_id': request['project_id'], 'server_id': evidence['resource_id'], 'port_id': native['port_id'],
               'security_group_id': native['security_group_id'], 'private_address': evidence['private_ipv4']}
    return edge, binding


def checked_worker_receipt(binding, request):
    require(apps.exact(binding, ('environment_id', 'generation', 'base_sha256', 'binding_sha256'))
            and binding['environment_id'] == request['target_id'] and binding['generation'] == request['generation']
            and all(isinstance(binding[key], str) and re.fullmatch(r'[a-f0-9]{64}', binding[key])
                    for key in ('base_sha256', 'binding_sha256')), 'RUNTIME_EDGE_WORKER_BINDING_INVALID')
    return binding


def materialize_route(profile, request, registry_file, home, kube):
    """Create target-bound route inputs from allowlisted operator fields only."""
    evidence, source = request['evidence'], profile['selection']
    base_edge, base_tunnel = profile['edge'], profile['tunnel']
    edge, native = edge_inputs(profile, request)
    worker_binding = checked_worker_receipt(openstack_routes.register_runtime(edge,
        environment_id=request['target_id'], generation=request['generation'], base=profile['worker_base'], runtime=native), request)
    edge['worker_binding'] = worker_binding
    namespace, name = request['target_id'], base_tunnel['name']
    configmap = kube(namespace, 'get', 'configmap', name + '-config', '-o', 'json')
    deployment = kube(namespace, 'get', 'deployment', name, '-o', 'json')
    require(configmap and deployment and configmap.get('metadata', {}).get('uid')
            and deployment.get('metadata', {}).get('uid'), 'RUNTIME_ROUTE_BASE_RESOURCES_UNAVAILABLE')
    tunnel = {key: copy.deepcopy(base_tunnel[key]) for key in ('version', 'name', 'tunnel_id', 'origin_vip', 'base_domain')}
    tunnel.update(namespace=namespace, credentials_secret=name + '-credentials', ca_configmap=name + '-ca')
    tunnel.update(state_dir=str(home / 'tunnel-state'), registry_file=registry_file,
                  environment_id=request['target_id'], resource_id=evidence['resource_id'],
                  runtime_private_address=evidence['private_ipv4'], configmap_uid=configmap['metadata']['uid'],
                  deployment_uid=deployment['metadata']['uid'])
    edge_path, tunnel_path = home / 'edge.json', home / 'tunnel.json'
    ingress = {'base_domain': base_edge['base_domain'], 'edge_config_file': str(edge_path),
               'tunnel_config_file': str(tunnel_path), 'dns_config_file': source['dns_config_file'],
               'runtime_binding': {'project_id': request['project_id'], 'resource_id': evidence['resource_id'],
                                   'private_ipv4': evidence['private_ipv4']}}
    return edge, tunnel, ingress, edge_path, tunnel_path


def observed_server(raw, evidence, project):
    require(isinstance(raw, dict), 'RUNTIME_RESOURCE_UNVERIFIED')
    data = {key.lower().replace(' ', '_'): value for key, value in raw.items()}
    require(data.get('id') == evidence['resource_id'] and data.get('project_id', data.get('tenant_id')) == project
            and data.get('status') == 'ACTIVE', 'RUNTIME_RESOURCE_MISMATCH')
    addresses = data.get('addresses')
    rows = []
    if isinstance(addresses, list):
        rows = addresses
    elif isinstance(addresses, dict):
        for network, values in addresses.items():
            require(isinstance(values, list), 'RUNTIME_ADDRESS_INVALID')
            for value in values:
                if isinstance(value, str):
                    address = ipaddress.ip_address(value)
                    rows.append({'network': network, 'address': str(address), 'version': address.version})
                else:
                    require(isinstance(value, dict), 'RUNTIME_ADDRESS_INVALID')
                    rows.append({'network': network, 'address': value.get('addr', value.get('address')), 'version': value.get('version')})
    elif isinstance(addresses, str):
        # OSC can render address maps as "network=ip, ip; other=ip".
        for section in addresses.split(';'):
            require(section.count('=') == 1, 'RUNTIME_ADDRESS_AMBIGUOUS')
            network, values = section.strip().split('=', 1)
            for value in values.split(','):
                address = ipaddress.ip_address(value.strip())
                rows.append({'network': network, 'address': str(address), 'version': address.version})
    selected = [row for row in rows if isinstance(row, dict) and row.get('network') == evidence['management_network'] and row.get('version') == 4]
    require(len(selected) == 1 and selected[0]['address'] == evidence['private_ipv4'], 'RUNTIME_ADDRESS_MISMATCH')
    return {'id': evidence['resource_id'], 'project_id': project, 'status': 'ACTIVE', 'addresses': rows}


def validate_request(request):
    require(apps.exact(request, ('action', 'target_id', 'generation', 'project_id', 'profile_id', 'expected_resource_id',
                                 'address', 'identity_file', 'known_hosts_file', 'evidence')))
    require(request['action'] in ('prepare', 'verify', 'reconcile', 'delete', 'inspect-delete', 'resume-delete') and re.fullmatch(r'personal-[a-f0-9-]{36}', request['target_id']))
    require(type(request['generation']) is int and request['generation'] > 0)
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', request['project_id']))
    require(request['profile_id'] is None or isinstance(request['profile_id'], str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', request['profile_id']))
    require(request['expected_resource_id'] is None or isinstance(request['expected_resource_id'], str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', request['expected_resource_id']))
    require(ipaddress.ip_address(request['address']).version == 4)
    evidence = request['evidence']
    require(apps.exact(evidence, ('resource_id', 'private_ipv4', 'management_network', 'placement', 'architecture',
                                  'initialization', 'ssh_user', 'ssh_port', 'server', 'provider_binding')))
    require(evidence['architecture'] == 'amd64' and evidence['initialization'] in ('cloud-init', 'preconfigured')
            and evidence['ssh_user'] == 'railshot-runtime' and evidence['ssh_port'] == 2223)
    require(ipaddress.ip_address(evidence['private_ipv4']).version == 4)
    require(apps.exact(evidence['provider_binding'], ('port_id', 'security_group_id'))
            and isinstance(evidence['provider_binding']['port_id'], str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', evidence['provider_binding']['port_id'])
            and isinstance(evidence['provider_binding']['security_group_id'], str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', evidence['provider_binding']['security_group_id']),
            'RUNTIME_NETWORK_BINDING_INVALID')
    for key in ('resource_id', 'management_network', 'placement'):
        require(isinstance(evidence[key], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', evidence[key]))
    require(request['expected_resource_id'] is None or request['expected_resource_id'] == evidence['resource_id'],
            'RUNTIME_RESOURCE_MISMATCH')
    identity = runtime.read_private(request['identity_file'], raw=True)
    hosts = runtime.read_private(request['known_hosts_file'], raw=True)
    require(identity and hosts)
    public = subprocess.run(['ssh-keygen', '-y', '-f', request['identity_file']], capture_output=True, check=True, timeout=10).stdout
    require(public.startswith(b'ssh-ed25519 '))
    return observed_server(evidence['server'], evidence, request['project_id']), hashlib.sha256(public.strip()).hexdigest(), hashlib.sha256(hosts).hexdigest()


def health(kube, private_ip):
    nodes = kube('default', 'get', 'nodes', '-o', 'json')['items']
    selected = [node for node in nodes if any(row.get('type') == 'InternalIP' and row.get('address') == private_ip for row in node.get('status', {}).get('addresses', []))]
    require(len(selected) == 1 and any(row.get('type') == 'Ready' and row.get('status') == 'True' for row in selected[0]['status'].get('conditions', [])), 'RUNTIME_NODE_NOT_READY')
    system = kube('default', 'get', 'namespace', 'kube-system', '-o', 'json')
    dns = kube('kube-system', 'get', 'deployment', 'coredns', '-o', 'json')
    require(dns.get('spec', {}).get('replicas', 1) > 0 and dns.get('status', {}).get('readyReplicas', 0) >= dns['spec'].get('replicas', 1), 'RUNTIME_DNS_NOT_READY')
    require(system['metadata'].get('uid'), 'RUNTIME_CLUSTER_IDENTITY_MISSING')
    return system['metadata']['uid']


def route_binding(profile, request, registry_file):
    """Existing public-route references must belong to this runtime, never the template's old VM."""
    try:
        ingress, evidence = profile['ingress'], request['evidence']
        tunnel = runtime.read_private(ingress['tunnel_config_file'])
        edge = runtime.read_private(ingress['edge_config_file'])
        runtime.read_private(ingress['dns_config_file'])
        binding = ingress.get('runtime_binding')
        require(binding == {'project_id': request['project_id'], 'resource_id': evidence['resource_id'],
                            'private_ipv4': evidence['private_ipv4']}, 'RUNTIME_ROUTE_CONFIGURATION_REQUIRED')
        require(tunnel.get('registry_file') == registry_file and tunnel.get('environment_id') == request['target_id']
                and tunnel.get('resource_id') == evidence['resource_id'] and tunnel.get('runtime_private_address') == evidence['private_ipv4']
                and tunnel.get('base_domain') == ingress['base_domain'] and edge.get('provider') == 'openstack'
                and edge.get('runtime_private_address') == evidence['private_ipv4'] and edge.get('base_domain') == ingress['base_domain'], 'RUNTIME_ROUTE_CONFIGURATION_REQUIRED')
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError('RUNTIME_ROUTE_CONFIGURATION_REQUIRED') from None


def readback(kube, cd, registered, ident, private_ip, tunnel_config_path, *, require_empty_routes=False):
    cluster_uid = health(kube, private_ip)
    _, _, policy, secret, config, ca = runtime.shared_cluster_snapshot(cd, registered, ident)
    require(policy['service_account']['namespace'] == ident and policy['service_account']['name'] == runtime.SA, 'RUNTIME_SERVICE_ACCOUNT_MISMATCH')
    namespace = kube('default', 'get', 'namespace', ident, '-o', 'json')
    sa = kube(ident, 'get', 'serviceaccount', runtime.SA, '-o', 'json')
    require(sa['metadata']['uid'] == policy['service_account']['uid'], 'RUNTIME_SERVICE_ACCOUNT_REPLACED')
    for obj in (namespace, sa):
        require(not obj['metadata'].get('ownerReferences') and not obj['metadata'].get('deletionTimestamp')
                and obj['metadata'].get('labels', {}).get('railshot.io/registration') == ident, 'RUNTIME_OBJECT_OWNERSHIP_MISMATCH')
    project = runtime.argo.kubectl(cd['context'], 'argocd', 'get', 'appproject', ident, '-o', 'json')
    require(project['metadata'].get('labels', {}).get('railshot.io/registration') == ident
            and project['spec']['sourceRepos'] == [registered['target']['repo_url']]
            and project['spec']['clusterResourceWhitelist'] == [], 'RUNTIME_ARGO_SCOPE_MISMATCH')
    for ns, resource, group, verb, allowed in [(ident, 'deployments', 'apps', 'create', True),
            (ident, 'secrets', '', 'get', False), ('kube-system', 'deployments', 'apps', 'create', False),
            ('', 'clusterroles', 'rbac.authorization.k8s.io', 'create', False)]:
        response = runtime.credentials.customer(policy['server'], ca, config['bearerToken'],
            '/apis/authorization.k8s.io/v1/selfsubjectaccessreviews', {'apiVersion': 'authorization.k8s.io/v1', 'kind': 'SelfSubjectAccessReview',
            'spec': {'resourceAttributes': {'namespace': ns, 'resource': resource, 'group': group, 'verb': verb}}}, server_name=private_ip)
        require(response['status']['allowed'] is allowed, 'RUNTIME_DEPLOYMENT_PERMISSION_MISMATCH')
    objects = {}
    for kind, name in [('serviceaccount', runtime.SA), ('role', runtime.SA), ('rolebinding', runtime.SA), ('secret', 'ghcr-pull')]:
        obj = kube(ident, 'get', kind, name, '-o', 'json')
        require(obj['metadata'].get('labels', {}).get('railshot.io/registration') == ident, 'RUNTIME_OBJECT_OWNERSHIP_MISMATCH')
        objects[kind + '/' + name] = obj['metadata']['uid']
    tunnel = runtime.read_private(tunnel_config_path)
    cm = kube(ident, 'get', 'configmap', tunnel['name'] + '-config', '-o', 'json')
    deployment = kube(ident, 'get', 'deployment', tunnel['name'], '-o', 'json')
    owners = tunnel_registration.inspect(tunnel, cm, deployment)
    require(not require_empty_routes or not owners, 'RUNTIME_APPLICATION_ROUTES_REMAIN')
    route_objects = []
    for kind, name in [('deployment', tunnel['name']), ('serviceaccount', tunnel['name']),
            ('configmap', tunnel['name'] + '-config'), ('configmap', tunnel['ca_configmap']),
            ('secret', tunnel['credentials_secret'])]:
        obj = kube(ident, 'get', kind, name, '-o', 'json')
        require(obj and obj['metadata'].get('uid') and not obj['metadata'].get('ownerReferences')
                and obj['metadata'].get('labels', {}).get('railshot.io/registration') == ident,
                'RUNTIME_ROUTE_OBJECT_OWNERSHIP_MISMATCH')
        route_objects.append({'namespace': ident, 'kind': kind, 'name': name, 'uid': obj['metadata']['uid']})
    return {'cluster_uid': cluster_uid, 'namespace_uid': namespace['metadata']['uid'], 'service_account_uid': sa['metadata']['uid'],
            'secret_uid': secret['metadata']['uid'], 'project_uid': project['metadata']['uid'], 'ca_sha256': policy['ca_sha256'],
            'objects': objects, 'route_objects': route_objects}


def delete_exact(call, namespace, kind, name, uid):
    obj = call(namespace, 'get', kind, name, '--ignore-not-found', '-o', 'json')
    if not obj:
        return
    require(obj['metadata']['uid'] == uid, 'RUNTIME_DELETE_IDENTITY_CHANGED')
    plural = {'serviceaccount': 'serviceaccounts', 'role': 'roles', 'rolebinding': 'rolebindings', 'secret': 'secrets',
              'configmap': 'configmaps', 'deployment': 'deployments', 'appproject': 'appprojects'}[kind]
    prefix = '/apis/rbac.authorization.k8s.io/v1' if kind in ('role', 'rolebinding') else '/apis/apps/v1' if kind == 'deployment' \
        else '/apis/argoproj.io/v1alpha1' if kind == 'appproject' else '/api/v1'
    call(namespace, 'delete', '--raw', prefix + '/namespaces/' + namespace + '/' + plural + '/' + name, '-f', '-',
         document={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid, 'resourceVersion': obj['metadata']['resourceVersion']}})
    require(not call(namespace, 'get', kind, name, '--ignore-not-found', '-o', 'json'), 'RUNTIME_DELETE_RESIDUAL')


def inspect_removal(kube, cd, registered, ident, binding, shared):
    control = lambda ns, *args, **kwargs: runtime.argo.kubectl(cd['context'], ns, *args, **kwargs)
    for path in shared.glob('app-*/registration.json'):
        registration = runtime.read_private(path)
        if registration.get('environment_id') == ident:
            lifecycle = path.parent / 'lifecycle.json'
            require(lifecycle.exists() and runtime.read_private(lifecycle).get('status') == 'deleted', 'RUNTIME_APPLICATIONS_REMAIN')
    namespace = kube('default', 'get', 'namespace', ident, '-o', 'json')
    require(namespace and namespace['metadata']['uid'] == binding['namespace_uid'], 'RUNTIME_DELETE_IDENTITY_CHANGED')
    cm = control('argocd', 'get', 'configmap', 'railshot-credentials', '-o', 'json')
    policy = runtime.credentials.validate_policy(json.loads(cm['data']['policy.json']))
    selected = [row for row in policy['targets'] if row['target_id'] == ident]
    require(len(selected) <= 1 and (not selected or selected[0]['namespaces'] == [ident]
            and selected[0]['service_account']['uid'] == binding['service_account_uid']
            and selected[0]['server'] == registered['target']['cluster_server']), 'RUNTIME_SHARED_NAMESPACES_REMAIN')
    require(not any(ident in row['namespaces'] for row in policy['targets'] if row['target_id'] != ident), 'RUNTIME_SHARED_NAMESPACES_REMAIN')
    items = [*binding['route_objects'], *[{'namespace': ident, 'kind': key.split('/')[0], 'name': key.split('/')[1], 'uid': uid}
                                        for key, uid in binding['objects'].items()]]
    for call, entries in [(kube, items), (control, [
            {'namespace': 'argocd', 'kind': 'secret', 'name': 'railshot-' + ident, 'uid': binding['secret_uid']},
            {'namespace': 'argocd', 'kind': 'appproject', 'name': ident, 'uid': binding['project_uid']}])]:
        for item in entries:
            obj = call(item['namespace'], 'get', item['kind'], item['name'], '--ignore-not-found', '-o', 'json')
            require(not obj or obj['metadata']['uid'] == item['uid'] and not obj['metadata'].get('ownerReferences')
                    and not obj['metadata'].get('finalizers'), 'RUNTIME_DELETE_IDENTITY_CHANGED')
            if obj and item['kind'] == 'configmap' and item['name'].endswith('-config'):
                require(json.loads(obj['metadata'].get('annotations', {}).get(tunnel_registration.OWNERS, 'null')) == {},
                        'RUNTIME_APPLICATION_ROUTES_REMAIN')
    return cm, policy


def remove_environment(kube, cd, registered, ident, binding, shared):
    control = lambda ns, *args, **kwargs: runtime.argo.kubectl(cd['context'], ns, *args, **kwargs)
    cm, policy = inspect_removal(kube, cd, registered, ident, binding, shared)
    for item in binding['route_objects']:
        delete_exact(kube, item['namespace'], item['kind'], item['name'], item['uid'])
    for key, uid in binding['objects'].items():
        kind, name = key.split('/')
        delete_exact(kube, ident, kind, name, uid)
    # Remove only this registration's renewal row and secret access name.
    policy['targets'] = [row for row in policy['targets'] if row['target_id'] != ident]
    cm['data']['policy.json'] = json.dumps(policy)
    control('argocd', 'replace', '-f', '-', '-o', 'json', document=cm)
    renewal_role = control('argocd', 'get', 'role', 'railshot-credentials', '-o', 'json')
    for rule in renewal_role['rules']:
        rule['resourceNames'] = [name for name in rule.get('resourceNames', []) if name != 'railshot-' + ident]
    renewal_role['rules'] = [rule for rule in renewal_role['rules'] if rule.get('resourceNames')]
    control('argocd', 'replace', '-f', '-', '-o', 'json', document=renewal_role)
    grants = {('argoproj.io', 'appprojects'): ident, ('', 'secrets'): 'railshot-' + ident}
    role = control('argocd', 'get', 'role', 'railshot-product-registrations', '-o', 'json')
    for (group, resource), name in grants.items():
        rule = next((row for row in role['rules'] if row['apiGroups'] == [group] and row['resources'] == [resource] and row['verbs'] == ['delete']), None)
        if rule is None:
            rule = {'apiGroups': [group], 'resources': [resource], 'verbs': ['delete'], 'resourceNames': []}
            role['rules'].append(rule)
        if name not in rule['resourceNames']:
            rule['resourceNames'].append(name)
    control('argocd', 'replace', '-f', '-', '-o', 'json', document=role)
    try:
        delete_exact(control, 'argocd', 'secret', 'railshot-' + ident, binding['secret_uid'])
        delete_exact(control, 'argocd', 'appproject', ident, binding['project_uid'])
    finally:
        role = control('argocd', 'get', 'role', 'railshot-product-registrations', '-o', 'json')
        names = {ident, 'railshot-' + ident, runtime.application_name(ident, ident, 'runtime-anchor')}
        for rule in role['rules']:
            rule['resourceNames'] = [name for name in rule.get('resourceNames', []) if name not in names]
        role['rules'] = [rule for rule in role['rules'] if rule.get('resourceNames')]
        control('argocd', 'replace', '-f', '-', '-o', 'json', document=role)
    observed = control('argocd', 'get', 'configmap', 'railshot-credentials', '-o', 'json')
    require(not any(row['target_id'] == ident for row in json.loads(observed['data']['policy.json'])['targets']), 'RUNTIME_RENEWAL_RESIDUAL')


def execute(config_path, request):
    ident, generation = request.get('target_id'), request.get('generation')
    answer = {'target_id': ident, 'generation': generation, 'status': 'blocked', 'stage': 'operator_configuration', 'blockers': [], 'cluster_verified': False}
    receipt_path = None
    started = False
    recovering = False
    try:
        common, template, generic_profile, route_profiles = load_operator_config(config_path)
        answer['stage'] = 'resource_verification'
        server, public_hash, hosts_hash = validate_request(request)
        claim_root = Path(template['state_dir']) / 'route-authorities'
        profile_id, route_profile = select_route_profile(route_profiles, request, claim_root)
        edge, native_binding = edge_inputs(route_profile, request)
        checked_worker_receipt(openstack_routes.verify_runtime(edge, environment_id=request['target_id'],
            generation=request['generation'], base=route_profile['worker_base'], runtime=native_binding), request)
        root = apps.private_directory(common['state_dir'])
        home = apps.private_directory(root / ident)
        receipt_path = home / 'registration.json'
        evidence = request['evidence']
        static = {key: value for key, value in request.items() if key not in ('action', 'evidence')}
        fingerprint = digest({'request': static, 'evidence': {**evidence, 'server': server}, 'public_key_sha256': public_hash,
                              'known_hosts_sha256': hosts_hash, 'template': template, 'profile': common['template_environment'],
                              'route_profile': profile_id, 'route_inputs': route_profile})
        ssh = {'user': evidence['ssh_user'], 'port': 2223, 'connect_host': request['address'],
               'identity_file': request['identity_file'], 'known_hosts_file': request['known_hosts_file']}
        selected = {key: evidence[key] for key in ('resource_id', 'management_network', 'placement', 'architecture', 'initialization')}
        selected.update(purpose='runtime', project_id=request['project_id'], ssh=ssh, server_file=str(home / 'server.json'),
                        management_endpoint='https://' + request['address'] + ':16443')
        shared = apps.private_directory(template['state_dir'])
        with os.fdopen(os.open(shared / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
            info = os.fstat(lock.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prior = runtime.read_private(receipt_path) if receipt_path.exists() else None
            require(not prior or prior['input_sha256'] == fingerprint, 'RUNTIME_BINDING_CONFLICT')
            recovering = bool(prior and prior['status'] in ('running', 'unknown'))
            removal = request['action'] in ('delete', 'inspect-delete', 'resume-delete')
            if recovering and request['action'] not in ('reconcile', 'inspect-delete', 'resume-delete'):
                return {**answer, 'status': 'unknown', 'stage': 'reconciliation', 'blockers': [{'code': 'RUNTIME_PREPARATION_UNVERIFIED'}]}
            deletion_path = home / 'deletion.json'
            deleted = runtime.read_private(deletion_path) if deletion_path.exists() else None
            if deleted:
                require(deleted.get('input_sha256') == fingerprint, 'RUNTIME_BINDING_CONFLICT')
                if removal and deleted.get('status') == 'succeeded':
                    return {**answer, 'status': 'succeeded', 'stage': 'revoked', 'revocation_verified': True, 'residuals': []}
                if request['action'] not in ('inspect-delete', 'resume-delete'):
                    return {**answer, 'status': 'unknown', 'stage': 'reconciliation', 'blockers': [{'code': 'RUNTIME_REMOVAL_RECONCILE_REQUIRED'}]}
                require(prior and deleted.get('binding') and (not prior.get('binding') or prior['binding'] == deleted['binding']),
                        'RUNTIME_REMOVAL_BINDING_UNAVAILABLE')
                prior = {**prior, 'binding': deleted['binding']}
            if request['action'] == 'reconcile' and not prior:
                # Registration intent is saved before any external mutation. Its
                # absence proves a fresh prepare can be explicitly requested.
                return {**answer, 'status': 'blocked', 'stage': 'registration', 'resumable': True,
                        'blockers': [{'code': 'RUNTIME_NOT_REGISTERED'}]}
            require(not (request['action'] == 'verify' or removal) or prior and (prior['status'] == 'succeeded' or deleted), 'RUNTIME_NOT_REGISTERED')
            for path, value in ((home / 'server.json', server),
                                (home / 'registry.json', {'version': 1, 'targets': {ident: selected}})):
                require(not prior or path.exists() and runtime.read_private(path) == value, 'RUNTIME_BINDING_CONFLICT')
                if not prior:
                    runtime.save(path, value)
            native, _ = runtime.registered_node(selected, ident, 'registration.' + ident)
            anchor = {**copy.deepcopy(generic_profile['target']), 'id': ident, 'namespace': ident, 'project': ident,
                      'argocd_namespace': 'argocd', 'cluster_server': selected['management_endpoint'],
                      'image_pull_secret': {'namespace': ident, 'name': 'ghcr-pull'}}
            registered = {'app': 'runtime-anchor', 'tenant': generic_profile['tenant'], 'target': anchor}
            cd = {**template['cd'], 'targets': {ident: registered}}
            physical = {'provider': 'openstack', 'resource_id': evidence['resource_id'], 'project_id': request['project_id']}
            authority = {**physical, 'cluster_server': selected['management_endpoint'], 'private_address': evidence['private_ipv4'], 'ssh': native['inventory']['control_plane'][0]['ssh']}
            claim = {'environment_id': ident, 'physical_sha256': digest(physical), 'authority_sha256': digest(authority)}
            route_claim = {'environment_id': ident, 'project_id': request['project_id'],
                           'resource_id': evidence['resource_id'], 'profile_id': profile_id,
                           'tunnel_id': route_profile['tunnel']['tunnel_id']}
            claims = [apps.private_directory(shared / 'environments') / (digest(physical) + '.json'),
                      apps.private_directory(shared / 'environment-ids') / (digest(ident) + '.json'),
                      apps.private_directory(shared / 'route-authorities') /
                      (digest(route_profile['tunnel']['tunnel_id']) + '.json')]
            claim_values = [claim, claim, route_claim]
            for index, (path, value) in enumerate(zip(claims, claim_values)):
                prior_claim = runtime.read_private(path) if path.exists() else None
                released = (index == 2 and isinstance(prior_claim, dict) and prior_claim.get('status') == 'released'
                            and prior_claim.get('profile_id') == profile_id
                            and prior_claim.get('tunnel_id') == route_profile['tunnel']['tunnel_id'])
                require(prior_claim is None or prior_claim == value or released, 'ENVIRONMENT_OWNERSHIP_CONFLICT')
            with runtime.runtime_kubectl(native) as kube:
                answer['stage'] = 'cluster_verification'
                health(kube, evidence['private_ipv4'])
                answer['cluster_verified'] = True
                if not removal and request['action'] not in ('verify', 'reconcile'):
                    if not prior:
                        answer['stage'] = 'registration'
                        runtime.preflight_renewal(cd, registered, ident)
                        # Existing unowned objects cannot be adopted, even before writes begin.
                        require(not kube('default', 'get', 'namespace', ident, '--ignore-not-found', '-o', 'json'), 'RUNTIME_NAMESPACE_ALREADY_EXISTS')
                        runtime.save(receipt_path, {'status': 'running', 'input_sha256': fingerprint})
                        started = True
                        for index, (path, value) in enumerate(zip(claims, claim_values)):
                            if index == 2 and path.exists() and runtime.read_private(path).get('status') == 'released':
                                runtime.save(path, value)
                            else:
                                apps._claim(path, value)
                        runtime.grant_control_objects(cd, registered, ident)
                        pull = runtime.read_private(generic_profile['pull_secret_file'])
                        for document in runtime.runtime_documents(anchor, ident, pull, None):
                            runtime.owned_apply(kube, document)
                        for document in tunnel_documents(route_profile, request):
                            runtime.owned_apply(kube, document)
                    answer['stage'] = 'application_configuration'
                    edge, tunnel, ingress, edge_path, tunnel_path = materialize_route(
                        route_profile, request, str(home / 'registry.json'), home, kube)
                    profile = copy.deepcopy(generic_profile)
                    profile['ingress'] = ingress
                    application_config = {**copy.deepcopy(template), 'state_dir': str(home / 'applications'),
                                          'registry_file': str(home / 'registry.json'), 'environments': {ident: profile}}
                    for path, value in ((home / 'applications.json', application_config),
                            (edge_path, edge), (tunnel_path, tunnel)):
                        require(not prior or path.exists() and runtime.read_private(path) == value, 'RUNTIME_BINDING_CONFLICT')
                        if not prior:
                            runtime.save(path, value)
                    route_binding(profile, request, application_config['registry_file'])
                elif request['action'] in ('verify', 'reconcile'):
                    # Readback must not call register_runtime/materialize_route.
                    # In particular, an unknown journal is not permission to
                    # repeat writes. Only a fully observed binding is recovered.
                    application_config = runtime.read_private(home / 'applications.json')
                    require(set(application_config['environments']) == {ident}, 'RUNTIME_APPLICATION_BINDING_INCOMPLETE')
                    route_binding(application_config['environments'][ident], request, str(home / 'registry.json'))
                if removal:
                    if deleted:
                        inspect_removal(kube, cd, registered, ident, prior['binding'], shared)
                    else:
                        require(readback(kube, cd, registered, ident, evidence['private_ipv4'], home / 'tunnel.json',
                                         require_empty_routes=True) == prior['binding'], 'RUNTIME_IDENTITY_REPLACED')
                    if request['action'] == 'inspect-delete':
                        return {**answer, 'status': 'blocked', 'stage': 'removal', 'resumable': True,
                                'blockers': [], 'residuals': [{'kind': 'RuntimeRegistration', 'name': ident}]}
                    runtime.save(deletion_path, {'status': 'unknown', 'input_sha256': fingerprint, 'binding': prior['binding']})
                    started = True
                    released = openstack_routes.unregister_runtime(runtime.read_private(home / 'edge.json'))
                    require(isinstance(released, dict) and released.get('status') == 'unregistered'
                            and released.get('https_verified') is False
                            and released.get('binding') == runtime.read_private(home / 'edge.json')['worker_binding'],
                            'RUNTIME_EDGE_WORKER_REVOCATION_UNVERIFIED')
                    remove_environment(kube, cd, registered, ident, prior['binding'], shared)
                    runtime.save(claims[2], {**route_claim, 'status': 'released',
                                            'released_at': datetime.now(timezone.utc).isoformat()})
                    runtime.save(deletion_path, {'status': 'succeeded', 'input_sha256': fingerprint})
                    return {**answer, 'status': 'succeeded', 'stage': 'revoked', 'revocation_verified': True, 'residuals': []}
                if not prior:
                    renewal, _ = runtime.register_argo(kube, cd, registered, ident, ident, None, tls_server_name=evidence['private_ipv4'])
                    runtime.install_renewal(cd, renewal)
                answer['stage'] = 'permission_verification'
                binding = readback(kube, cd, registered, ident, evidence['private_ipv4'], home / 'tunnel.json')
                require(not prior or 'binding' not in prior and request['action'] == 'reconcile'
                        or prior['binding'] == binding, 'RUNTIME_IDENTITY_REPLACED')
                receipt = {'status': 'succeeded', 'input_sha256': fingerprint, 'binding': binding}
                if not prior or request['action'] == 'reconcile':
                    runtime.save(receipt_path, receipt)
                return {**answer, 'status': 'succeeded', 'stage': 'complete', 'blockers': [],
                        'application_config_path': str(home / 'applications.json'), 'binding_sha256': digest(receipt),
                        'verified_at': datetime.now(timezone.utc).isoformat()}
    except Exception as error:
        if started and receipt_path is not None and request.get('action') not in ('delete', 'resume-delete'):
            runtime.save(receipt_path, {'status': 'unknown', 'input_sha256': fingerprint})
        code = str(error) if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', str(error)) else 'RUNTIME_PREPARATION_UNVERIFIED' if started else 'RUNTIME_PREREQUISITE_UNAVAILABLE'
        return {**answer, 'status': 'unknown' if started or recovering else 'blocked', 'blockers': [{'code': code}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    require(args.check != bool(args.request), 'RUNTIME_REQUEST_INVALID')
    print(json.dumps(check_config(args.config) if args.check else execute(args.config, runtime.read_private(args.request))))


if __name__ == '__main__':
    main()
