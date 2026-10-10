#!/usr/bin/env python3
"""Register one operator-bound runtime and application after runtime_ready."""
import argparse
import base64
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'infrastructure/ansible'), str(ROOT / 'gitops'), str(ROOT / 'ci/scripts')]
import run as ansible
import argo
import bridge
import credentials
from handoff import application_name
from storage import durable_write
from publication import validate_target

SA = 'railshot-argocd'
MANAGER = 'railshot-registration'


def read_private(path, *, raw=False):
    path = Path(path)
    info = path.lstat()
    argo.require(path.is_absolute() and path.resolve() == path and stat.S_ISREG(info.st_mode)
                 and info.st_uid == os.geteuid() and not info.st_mode & 0o077
                 and 0 < info.st_size <= 1_000_000, 'private operator file required')
    data = path.read_bytes()
    return data if raw else json.loads(data, object_pairs_hook=ansible.unique_pairs)


def save(path, value):
    durable_write(path, bridge.encoded(value))


def label(value):
    return isinstance(value, str) and re.fullmatch(argo.LABEL, value)


def registered_node(selected, target_id, request_id, *, timeout_seconds=None):
    """Resolve the existing Ansible registry without claiming live cloud readiness."""
    timeout = selected.get('timeout_seconds', 1200) if timeout_seconds is None else timeout_seconds
    if 'server_file' in selected:
        fields = {'resource_id', 'project_id', 'management_network', 'placement',
                  'architecture', 'initialization', 'ssh'}
        argo.require(fields | {'server_file'} <= set(selected)
                     and not set(selected) - fields - {'server_file', 'purpose', 'timeout_seconds', 'management_endpoint'},
                     'OpenStack registry fields differ')
        server = read_private(selected['server_file'])
        request = ansible.from_openstack(server, request_id=request_id, operation='guest.check',
            target_id=target_id, **{key: selected[key] for key in fields}, timeout_seconds=timeout)
        node = request['inventory']['control_plane'][0]
        # This is a verified resource binding, not a fabricated Terraform descriptor.
        # Retain all source/connection inputs in the private registration identity.
        resource = {'target_id': target_id, 'provider_kind': 'openstack', 'resource_id': node['resource_id'],
                    'addresses': {'private': node['private_ipv4']}, 'openstack_server': server,
                    'registered_request': request}
        connect_host = node['ssh'].get('connect_host', node['private_ipv4'])
        if connect_host != node['private_ipv4']:
            resource['addresses']['metrics'] = connect_host
        if 'management_endpoint' in selected:
            endpoint = selected['management_endpoint']
            argo.require(isinstance(endpoint, str), 'registered HTTPS management endpoint required')
            parsed = urlsplit(endpoint)
            argo.require(parsed.scheme == 'https' and parsed.hostname == connect_host
                         and parsed.port is not None and 1 <= parsed.port <= 65535
                         and parsed.netloc == f'{connect_host}:{parsed.port}'
                         and not parsed.path and not parsed.query and not parsed.fragment,
                         'management endpoint must match the registered private connection host')
            resource['management_endpoint'] = endpoint
    else:
        resource = read_private(selected['descriptor_file'])
        argo.require(resource['target_id'] == target_id, 'descriptor identity mismatch')
        request = ansible.from_descriptor(resource, request_id=request_id, operation='guest.check',
            ssh=selected['ssh'], timeout_seconds=timeout)
        resource.pop('management_endpoint', None)  # Descriptor metadata cannot select a management route.
        resource['addresses'].pop('metrics', None)
        if 'management_endpoint' in selected:
            argo.require(request['target']['provider'] == 'gcp', 'management endpoint override requires GCP or OpenStack')
            public = ipaddress.IPv4Address(resource['addresses'].get('public', ''))
            argo.require(public.is_global and not public.is_multicast
                         and selected['management_endpoint'] == f'https://{public}:6443',
                         'GCP management endpoint must match the provisioned public IPv4 API')
            resource['management_endpoint'] = selected['management_endpoint']
            resource['addresses']['metrics'] = str(public)
    for key in ('identity_file', 'known_hosts_file'):
        ansible.private_file(selected['ssh'][key], identity=key == 'identity_file')
    return request, resource


def load(registry_file, target_id, config_file, binding_file=None):
    registry, config = read_private(registry_file), read_private(config_file)
    return load_configuration(registry, target_id, config, binding_file)


def load_configuration(registry, target_id, config, binding_file=None):
    """Validate already-read operator files without writing a temporary configuration."""
    argo.require(registry.get('version') == 1 and isinstance(registry.get('targets'), dict), 'registry v1 required')
    selected = registry['targets'][target_id]
    argo.require(selected['purpose'] == 'runtime', 'registered runtime required')
    request, descriptor = registered_node(selected, target_id, 'registration.' + target_id)
    argo.require(set(config) == {'version', 'cd', 'registration'} and config['version'] == 1, 'registration config v1 required')
    cd, settings = config['cd'], config['registration']
    argo.require(set(cd) == {'version', 'state_dir', 'repository', 'branch', 'context', 'targets'} and cd['version'] == 1
                 and isinstance(cd['targets'], dict) and target_id in cd['targets'], 'existing CD base config required')
    argo.require(set(settings) >= {'state_dir', 'source_repository', 'pull_secret_file'}
                 and not set(settings) - {'state_dir', 'source_repository', 'pull_secret_file', 'edge_config_file', 'target_security_group_id', 'expires_at', 'observability_config_file'},
                 'registration settings differ')
    argo.require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', settings['source_repository']), 'source repository required')
    if settings.get('edge_config_file'):
        argo.require(descriptor['provider_kind'] == 'aws',
                     'AWS edge is AWS-only; GCP requires a provider-local public URL and verified management route')
        expiry = settings.get('expires_at')
        argo.require(isinstance(expiry, str) and expiry.endswith('Z')
                     and datetime.fromisoformat(expiry.replace('Z', '+00:00')) > datetime.now(timezone.utc),
                     'future UTC edge expiry required before registration')
    registered = copy.deepcopy(cd['targets'][target_id]); target = registered['target']
    argo.require(set(registered) == {'target', 'app', 'tenant', 'public_http'} and target['id'] == target_id,
                 'one registered application required')
    argo.require(re.fullmatch(r'[a-z][a-z0-9-]{1,28}[a-z0-9]', registered['app'])
                 and re.fullmatch(r'[a-z0-9]{1,20}', registered['tenant']), 'application identity differs')
    for key in ('namespace', 'project', 'argocd_namespace'):
        argo.require(label(target[key]), 'explicit namespace/project required')
    argo.require(target['argocd_namespace'] == 'argocd' and target['project'] != 'default'
                 and target['namespace'] not in {'default', 'argocd', 'kube-system', 'kube-public', 'kube-node-lease'},
                 'dedicated application namespace required')
    expected_server = 'https://' + descriptor['addresses']['private'] + ':6443'
    if request['target']['provider'] in ('openstack', 'gcp'):
        expected_server = descriptor.get('management_endpoint', expected_server)
    argo.require(target.get('cluster_server', expected_server) == expected_server
                 and target['architecture'] == request['target']['architecture'] == 'amd64',
                 'cluster endpoint must match the registered private runtime')
    target['cluster_server'] = expected_server
    path = PurePosixPath(target['path'])
    argo.require(not path.is_absolute() and '..' not in path.parts and str(path) not in ('', '.')
                 and registered['app'] in path.parts and target_id in path.parts, 'dedicated app/target Git path required')
    argo.https_url(target['repo_url'])
    argo.require(type(target['node_port']) is int and 30000 <= target['node_port'] <= 32767, 'allocated NodePort required')
    argo.require(target['image_pull_secret'] == {'namespace': target['namespace'], 'name': target['image_pull_secret']['name']}
                 and label(target['image_pull_secret']['name']), 'namespaced pull Secret required')
    pull = read_private(settings['pull_secret_file'])
    argo.require(set(pull) == {'auths'} and set(pull['auths']) == {'ghcr.io'}
                 and set(pull['auths']['ghcr.io']) == {'auth'}
                 and ':' in base64.b64decode(pull['auths']['ghcr.io']['auth'], validate=True).decode(), 'dedicated GHCR pull config required')
    binding = read_private(binding_file) if binding_file else None
    argo.require(bool(binding) == bool(target.get('database')), 'database binding must match the registered target')
    if binding:
        db = target['database']
        db.setdefault('host', binding['host'])
        db.setdefault('port', binding['port'])
        argo.require(set(db) == {'host', 'port', 'runtime_secret', 'migration_secret', 'ca_secret'}
                     and str(ipaddress.IPv4Address(db['host'])) == db['host'] and db['port'] == 5432
                     and all(label(db[k]) for k in ('runtime_secret', 'migration_secret', 'ca_secret'))
                     and len({db[k] for k in ('runtime_secret', 'migration_secret', 'ca_secret')}) == 3,
                     'explicit database Secret references required')
        argo.require(binding['version'] == 1 and binding['host'] == db['host'] and binding['port'] == db['port']
                     and binding['sslmode'] == 'verify-full', 'database TLS binding differs')
        ca = read_private(binding['sslrootcert'], raw=True)
        argo.require(b'-----BEGIN CERTIFICATE-----' in ca, 'database CA required')
        binding = {**binding, 'ca': ca.decode('ascii')}
    public = registered['public_http']
    if settings.get('edge_config_file') and 'health_path' in public:
        argo.require(set(public) == {'health_path', 'expected_json'}, 'edge health expectation required')
        argo.http_path(public['health_path'])
    else:
        argo.require(set(public) == {'url', 'expected_json'}, 'HTTP expectation required')
        argo.http_path(urlsplit(argo.https_url(public['url'])).path)
    argo.require(isinstance(public['expected_json'], dict), 'HTTP JSON expectation required')
    cd = {**cd, 'targets': {target_id: registered}}
    identity = {'descriptor': descriptor, 'cd': cd, 'settings': settings,
                'ssh_sha256': {key: hashlib.sha256(Path(selected['ssh'][key]).read_bytes()).hexdigest() for key in ('identity_file', 'known_hosts_file')},
                'pull_sha256': argo.document_hash(pull), 'database_sha256': argo.document_hash(binding)}
    return request, cd, settings, pull, binding, identity


@contextmanager
def runtime_kubectl(request):
    node = request['inventory']['control_plane'][0]
    with ansible.forwarded_port(node['ssh'].get('transport_ref'), time.monotonic() + 540) as port:
        host = next(iter(ansible.build_inventory(request, port)['all']['children']['k3s_server']['hosts'].values()))
        prefix = ['ssh', *shlex.split(host['ansible_ssh_common_args']), '-i', host['ansible_ssh_private_key_file'],
                  '-p', str(host['ansible_port']), '-o', 'ConnectTimeout=15', host['ansible_user'] + '@' + host['ansible_host']]
        def kubectl(namespace, *args, document=None):
            remote = shlex.join(['sudo', '-n', 'k3s', 'kubectl', '--request-timeout=20s', '-n', namespace, *args])
            # Personal relay rechecks the complete app ownership/storage inventory
            # before lifecycle mutations. Keep ordinary commands on their short deadline.
            lifecycle = (node['ssh'].get('port') == 2223 and re.fullmatch(r'app-[a-f0-9]{24}', namespace)
                         and args and args[0] in ('delete', 'patch'))
            raw = argo.native([*prefix, remote], document=document, **({'timeout': 300} if lifecycle else {}))
            return json.loads(raw) if raw.strip() else None
        yield kubectl


def owned_apply(kube, document):
    meta = document['metadata']; namespace = meta.get('namespace', 'default')
    existing = kube(namespace, 'get', document['kind'], meta['name'], '--ignore-not-found', '-o', 'json')
    if existing:
        argo.require(not existing['metadata'].get('ownerReferences') and all(
            existing['metadata'].get('labels', {}).get(k) == v for k, v in meta['labels'].items()), 'existing object belongs to another registration')
    kube(namespace, 'apply', '--server-side', '--field-manager=' + MANAGER, '-f', '-', '-o', 'json', document=document)
    observed = kube(namespace, 'get', document['kind'], meta['name'], '-o', 'json')
    for key in ('rules', 'subjects', 'roleRef', 'data', 'automountServiceAccountToken'):
        if key in document:
            argo.require(observed.get(key) == document[key], 'registered object readback differs')
    return observed


def runtime_documents(target, owner, pull, binding):
    namespace = target['namespace']; labels = {'app.kubernetes.io/managed-by': 'railshot', 'railshot.io/registration': owner}
    def doc(kind, name, **fields):
        return {'apiVersion': 'rbac.authorization.k8s.io/v1' if kind in ('Role', 'RoleBinding') else 'v1',
                'kind': kind, 'metadata': {'name': name, **({'namespace': namespace} if kind != 'Namespace' else {}), 'labels': labels}, **fields}
    verbs = ['get', 'list', 'watch', 'create', 'update', 'patch', 'delete']
    rules = [{'apiGroups': [group], 'resources': resources, 'verbs': verbs} for group, resources in (
        ('apps', ['deployments']), ('', ['services', 'persistentvolumeclaims']), ('networking.k8s.io', ['networkpolicies']))]
    rules.extend([{'apiGroups': [''], 'resources': ['pods', 'events'], 'verbs': ['get', 'list', 'watch']},
                  {'apiGroups': [''], 'resources': ['pods/log'], 'verbs': ['get']},
                  {'apiGroups': ['apps'], 'resources': ['replicasets'], 'verbs': ['get', 'list', 'watch']},
                  {'apiGroups': [''], 'resources': ['serviceaccounts/token'], 'resourceNames': [SA], 'verbs': ['create']}])
    if binding:
        rules.append({'apiGroups': ['batch'], 'resources': ['jobs'], 'verbs': verbs})
    result = [doc('Namespace', namespace), doc('ServiceAccount', SA, automountServiceAccountToken=False),
              doc('Role', SA, rules=rules), doc('RoleBinding', SA,
                  roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': SA},
                  subjects=[{'kind': 'ServiceAccount', 'name': SA, 'namespace': namespace}])]
    def secret(name, values, kind='Opaque'):
        return doc('Secret', name, type=kind, data={k: base64.b64encode(v.encode()).decode() for k, v in values.items()})
    result.append(secret(target['image_pull_secret']['name'], {'.dockerconfigjson': json.dumps(pull)}, 'kubernetes.io/dockerconfigjson'))
    if binding:
        db = target['database']
        def url(role):
            account = binding[role]
            return ('postgresql://' + quote(account['username'], safe='') + ':' + quote(account['password'], safe='')
                    + '@' + db['host'] + ':5432/' + quote(binding['database'], safe='') + '?'
                    + urlencode({'sslmode': 'verify-full', 'sslrootcert': '/etc/railshot/db/ca.crt'}))
        result.extend([secret(db['runtime_secret'], {'DATABASE_URL': url('runtime')}),
                       secret(db['migration_secret'], {'MIGRATION_DATABASE_URL': url('migration')}),
                       secret(db['ca_secret'], {'ca.crt': binding['ca']})])
    return result


def application_project(registered, owner, binding=None):
    target = registered['target']
    project = {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'AppProject',
               'metadata': {'name': target['project'], 'namespace': 'argocd'},
               'spec': {'sourceRepos': [target['repo_url']], 'destinations': [{'server': target['cluster_server'], 'namespace': target['namespace']}],
                        'clusterResourceWhitelist': [], 'namespaceResourceWhitelist': copy.deepcopy(argo.KINDS + [argo.PVC_KIND])}}
    if binding:
        project['spec']['namespaceResourceWhitelist'].append({'group': 'batch', 'kind': 'Job'})
    project['metadata']['labels'] = {'app.kubernetes.io/managed-by': 'railshot', 'railshot.io/registration': owner}
    return project


def register_argo(kube, cd, registered, target_id, owner, binding, *, tls_server_name=None):
    target = registered['target']; namespace = target['namespace']
    control = lambda ns, *args, **kwargs: argo.kubectl(cd['context'], ns, *args, **kwargs)
    app = {'metadata': {'namespace': 'argocd', 'name': target_id}, 'spec': {'project': target['project'],
           'source': {'repoURL': target['repo_url']}, 'destination': {'server': target['cluster_server'], 'namespace': namespace}}}
    review = {'application': app, 'receipt': {'target_id': target_id}}
    project = application_project(registered, owner, binding)
    # Each new runtime gets a dedicated project. Refuse to adopt an existing shared project.
    owned_apply(control, project)
    live_project = control('argocd', 'get', 'appproject', target['project'], '-o', 'json')
    argo.require(live_project['spec'] == project['spec'], 'project scope readback differs')
    ca_config = kube(namespace, 'config', 'view', '--raw', '--minify', '-o', 'json')
    ca_data = ca_config['clusters'][0]['cluster']['certificate-authority-data']
    ca = base64.b64decode(ca_data, validate=True)
    argo.require(b'-----BEGIN CERTIFICATE-----' in ca, 'runtime CA required')
    sa = kube(namespace, 'get', 'serviceaccount', SA, '-o', 'json')
    token_response = kube(namespace, 'create', '--raw', '/api/v1/namespaces/' + namespace + '/serviceaccounts/' + SA + '/token',
        '-f', '-', document={'apiVersion': 'authentication.k8s.io/v1', 'kind': 'TokenRequest', 'spec': {'expirationSeconds': credentials.LIFETIME}})
    token = token_response['status']['token']
    # TokenRequest selected the API server audience; verify this credential through that same TLS API.
    payload = token.split('.')[1]
    audiences = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))['aud']
    renewal = {'secret': 'railshot-' + target_id, 'target_id': target_id, 'server': target['cluster_server'],
               'project': target['project'], 'namespaces': [namespace], 'service_account': {'name': SA, 'namespace': namespace, 'uid': sa['metadata']['uid']},
               'ca_sha256': hashlib.sha256(ca).hexdigest(), 'audiences': audiences}
    if tls_server_name is not None:
        renewal['tls_server_name'] = tls_server_name
    credentials.validate_policy({'version': 1, 'targets': [renewal]})
    credentials.claims(token, renewal, time.time())
    for ns, resource, group, verb, allowed in [(namespace, 'deployments', 'apps', 'create', True),
            (namespace, 'secrets', '', 'get', False), ('kube-system', 'deployments', 'apps', 'create', False),
            ('', 'clusterroles', 'rbac.authorization.k8s.io', 'create', False)]:
        access = credentials.customer(target['cluster_server'], ca, token, '/apis/authorization.k8s.io/v1/selfsubjectaccessreviews',
            {'apiVersion': 'authorization.k8s.io/v1', 'kind': 'SelfSubjectAccessReview', 'spec': {
                'resourceAttributes': {'namespace': ns, 'resource': resource, 'group': group, 'verb': verb}}},
            **({'server_name': tls_server_name} if tls_server_name is not None else {}))
        argo.require(access['status']['allowed'] is allowed, 'runtime credential scope differs')
    tls_config = {'caData': ca_data, 'insecure': False}
    if tls_server_name is not None:
        tls_config['serverName'] = tls_server_name
    argo.register_cluster([review], cd['context'], {'bearerToken': token, 'tlsClientConfig': tls_config},
                          application_credential=bool(re.fullmatch(r'app-[a-f0-9]{24}', target_id)))
    stored = argo.kubectl(cd['context'], 'argocd', 'get', 'secret', renewal['secret'], '-o', 'json')
    stored_config, _ = credentials.registration(stored, renewal, time.time())
    expiration = credentials.claims(stored_config['bearerToken'], renewal, time.time())['exp']
    return renewal, datetime.fromtimestamp(expiration, timezone.utc).isoformat().replace('+00:00', 'Z')


def shared_cluster_policy(cd, registered, environment_id):
    """The operator's existing renewal row owns the one real cluster connection."""
    argo.require(label(environment_id) and not environment_id.startswith('app-'), 'environment cluster identity required')
    cm = argo.kubectl(cd['context'], 'argocd', 'get', 'configmap', 'railshot-credentials', '-o', 'json')
    policy = credentials.validate_policy(json.loads(cm['data']['policy.json']))
    selected = next((row for row in policy['targets'] if row['target_id'] == environment_id), None)
    argo.require(selected is not None and selected['server'] == registered['target']['cluster_server']
                 and selected['secret'] == 'railshot-' + environment_id, 'existing environment cluster registration required')
    argo.require('previous_scope' not in selected, 'environment cluster transition requires reconciliation')
    return cm, policy, selected


def shared_cluster_snapshot(cd, registered, environment_id):
    cm, policy, selected = shared_cluster_policy(cd, registered, environment_id)
    secret = argo.kubectl(cd['context'], 'argocd', 'get', 'secret', selected['secret'], '-o', 'json')
    config, ca = credentials.registration(secret, selected, time.time())
    argo.require(not secret['metadata'].get('ownerReferences') and not secret['metadata'].get('deletionTimestamp'),
                 'environment cluster is not available')
    return cm, policy, selected, secret, config, ca


def share_application_cluster(kube, cd, registered, environment_id):
    """Keep one Argo server cache and append only this app's exact namespace."""
    target = registered['target']; app_id = target['id']; namespace = target['namespace']
    argo.require(re.fullmatch(r'app-[a-f0-9]{24}', app_id) and namespace == target['project'] == app_id,
                 'application cluster scope differs')
    cm, policy, selected, secret, config, ca = shared_cluster_snapshot(cd, registered, environment_id)
    app_row = next((row for row in policy['targets'] if row['target_id'] == app_id), None)
    argo.require(app_row is not None and app_row['server'] == selected['server']
                 and app_row['ca_sha256'] == selected['ca_sha256']
                 and app_row.get('tls_server_name') == selected.get('tls_server_name')
                 and app_row['project'] == app_id and app_row['namespaces'] == [namespace], 'app credential binding differs')
    app_secret = argo.kubectl(cd['context'], 'argocd', 'get', 'secret', app_row['secret'], '-o', 'json')
    credentials.registration(app_secret, app_row, time.time())
    labels = {'app.kubernetes.io/managed-by': 'railshot', 'railshot.io/registration': app_id}
    live_namespace = kube('default', 'get', 'namespace', namespace, '-o', 'json')
    argo.require(not live_namespace['metadata'].get('ownerReferences') and all(
        live_namespace['metadata'].get('labels', {}).get(k) == v for k, v in labels.items()), 'app namespace owner differs')
    anchor = selected['service_account']
    sa = kube(anchor['namespace'], 'get', 'serviceaccount', anchor['name'], '-o', 'json')
    argo.require(sa['metadata']['uid'] == anchor['uid'], 'environment service account was replaced')
    binding = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
        'metadata': {'name': 'railshot-environment-argocd', 'namespace': namespace, 'labels': labels},
        'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': SA},
        'subjects': [{'kind': 'ServiceAccount', 'name': anchor['name'], 'namespace': anchor['namespace']}]}
    existing = kube(namespace, 'get', 'rolebinding', binding['metadata']['name'], '--ignore-not-found', '-o', 'json')
    if existing:
        argo.require(not existing['metadata'].get('ownerReferences') and all(
            existing['metadata'].get('labels', {}).get(k) == v for k, v in labels.items())
            and all(existing.get(k) == binding[k] for k in ('roleRef', 'subjects')), 'environment app RoleBinding differs')
    else:
        owned_apply(kube, binding)
    tls = {'server_name': selected['tls_server_name']} if 'tls_server_name' in selected else {}
    for ns, resource, group, verb, allowed in [(namespace, 'deployments', 'apps', 'create', True),
            (namespace, 'secrets', '', 'get', False), ('kube-system', 'deployments', 'apps', 'create', False)]:
        access = credentials.customer(selected['server'], ca, config['bearerToken'],
            '/apis/authorization.k8s.io/v1/selfsubjectaccessreviews', {'apiVersion': 'authorization.k8s.io/v1',
            'kind': 'SelfSubjectAccessReview', 'spec': {'resourceAttributes': {
                'namespace': ns, 'resource': resource, 'group': group, 'verb': verb}}}, **tls)
        argo.require(access['status']['allowed'] is allowed, 'shared environment credential scope differs')
    updated = {**selected, 'project': '', 'namespaces': sorted(set(selected['namespaces']) | {namespace})}
    replacement = {**policy, 'targets': [updated if row['target_id'] == environment_id else row for row in policy['targets']]}
    credentials.validate_policy(replacement)
    wanted_data = {**secret['data'], 'project': base64.b64encode(b'').decode(),
                   'namespaces': base64.b64encode(','.join(updated['namespaces']).encode()).decode()}
    if updated != selected:
        # Keep renewal valid through a crash between the two Kubernetes objects.
        # Only the exact old/new scopes are authorized during this transition.
        transition = {**updated, 'previous_scope': {key: selected[key] for key in ('project', 'namespaces')}}
        transitional_policy = {**policy, 'targets': [transition if row['target_id'] == environment_id else row for row in policy['targets']]}
        credentials.validate_policy(transitional_policy)
        cm['data']['policy.json'] = json.dumps(transitional_policy)
        cm = argo.kubectl(cd['context'], 'argocd', 'replace', '-f', '-', '-o', 'json', document=cm)
        argo.require(json.loads(cm['data']['policy.json']) == transitional_policy, 'cluster transition readback differs')
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': secret['metadata']['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': secret['metadata']['resourceVersion']},
                 *[{'op': 'replace', 'path': '/data/' + key, 'value': wanted_data[key]} for key in ('project', 'namespaces')]]
        argo.kubectl(cd['context'], 'argocd', 'patch', 'secret', selected['secret'], '--type=json',
                     '--patch-file=/dev/stdin', '-o', 'json', document=patch)
        cm['data']['policy.json'] = json.dumps(replacement)
        argo.kubectl(cd['context'], 'argocd', 'replace', '-f', '-', '-o', 'json', document=cm)
    # Old completed registrations retain their private credential, but stop announcing
    # a second Argo cluster for the same server. Token renewal patches data only.
    if app_secret['metadata']['labels'].get('argocd.argoproj.io/secret-type') == 'cluster':
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': app_secret['metadata']['uid']},
                 {'op': 'test', 'path': '/metadata/resourceVersion', 'value': app_secret['metadata']['resourceVersion']},
                 {'op': 'replace', 'path': '/metadata/labels/argocd.argoproj.io~1secret-type', 'value': 'railshot-application'}]
        argo.kubectl(cd['context'], 'argocd', 'patch', 'secret', app_row['secret'], '--type=json',
                     '--patch-file=/dev/stdin', '-o', 'json', document=patch)
    _, _, observed, live, _, _ = shared_cluster_snapshot(cd, registered, environment_id)
    argo.require(observed == updated and live['metadata']['uid'] == secret['metadata']['uid']
                 and {k: v for k, v in live['data'].items() if k != 'config'} == {
                     k: v for k, v in wanted_data.items() if k != 'config'}, 'shared cluster readback differs')
    private_secret = argo.kubectl(cd['context'], 'argocd', 'get', 'secret', app_row['secret'], '-o', 'json')
    credentials.registration(private_secret, app_row, time.time())
    argo.require(private_secret['metadata']['uid'] == app_secret['metadata']['uid']
                 and {k: v for k, v in private_secret['data'].items() if k != 'config'} == {
                     k: v for k, v in app_secret['data'].items() if k != 'config'}
                 and private_secret['metadata']['labels'].get('argocd.argoproj.io/secret-type') == 'railshot-application',
                 'private app credential readback differs')
    return {'secret': selected['secret'], 'uid': secret['metadata']['uid'], 'environment_id': environment_id,
            'service_account': anchor, 'namespace': namespace}


def preflight_renewal(cd, registered, target_id, *, allow_existing=False):
    """Validate the policy and existing identity before creating registration objects."""
    cm = argo.kubectl(cd['context'], 'argocd', 'get', 'configmap', 'railshot-credentials', '-o', 'json')
    policy = credentials.validate_policy(json.loads(cm['data']['policy.json']))
    target = registered['target']
    expected = {'secret': 'railshot-' + target_id, 'target_id': target_id, 'server': target['cluster_server'],
                'project': target['project'], 'namespaces': [target['namespace']]}
    previous = next((item for item in policy['targets'] if item['target_id'] == target_id), None)
    if previous is not None:
        argo.require(allow_existing and all(previous[key] == value for key, value in expected.items())
                     and previous['service_account']['name'] == SA
                     and previous['service_account']['namespace'] == target['namespace'], 'renewal target binding conflict')
    # Failed deployments may still own resources and need token renewal. Their
    # registration count is not an application quota. install_renewal validates
    # the actual candidate document size again before writing policy or RBAC.


def install_renewal(cd, renewal):
    def control(*args, **kwargs):
        return argo.kubectl(cd['context'], 'argocd', *args, **kwargs)
    cron = control('get', 'cronjob', 'railshot-credentials', '-o', 'json')
    pod = cron['spec']['jobTemplate']['spec']['template']['spec']
    argo.require(not cron['spec'].get('suspend') and pod['serviceAccountName'] == 'railshot-credentials'
                 and pod['containers'][0]['command'][:2] == ['python3', '/app/gitops/credentials.py'], 'existing token renewal worker required')
    cm = control('get', 'configmap', 'railshot-credentials', '-o', 'json')
    policy = credentials.validate_policy(json.loads(cm['data']['policy.json']))
    previous = next((t for t in policy['targets'] if t['target_id'] == renewal['target_id']), None)
    argo.require(previous is None or previous == renewal, 'renewal target binding conflict')
    # Recheck the complete candidate immediately before writes; preflight is not a reservation.
    if previous is None:
        policy['targets'].append(renewal)
    credentials.validate_policy(policy)
    role = control('get', 'role', 'railshot-credentials', '-o', 'json')
    empty_role = not (role.get('rules') or [])
    if empty_role:
        argo.require(not policy['targets'] or policy['targets'] == [renewal], 'renewal role differs')
        role['rules'] = [{'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['get', 'patch'], 'resourceNames': []}]
    argo.require(len(role['rules']) == 1 and role['rules'][0]['apiGroups'] == ['']
                 and role['rules'][0]['resources'] == ['secrets'] and role['rules'][0]['verbs'] == ['get', 'patch'], 'renewal role differs')
    names = role['rules'][0]['resourceNames']
    if renewal['secret'] not in names:
        names.append(renewal['secret'])
        control('replace', '-f', '-', '-o', 'json', document=role)  # resourceVersion prevents lost updates.
    if previous is None:
        cm['data']['policy.json'] = json.dumps(policy)
        control('replace', '-f', '-', '-o', 'json', document=cm)
    observed = control('get', 'configmap', 'railshot-credentials', '-o', 'json')
    argo.require(renewal in json.loads(observed['data']['policy.json'])['targets'], 'renewal policy readback differs')
    argo.require(renewal['secret'] in control('get', 'role', 'railshot-credentials', '-o', 'json')['rules'][0]['resourceNames'], 'renewal role readback differs')


def grant_control_objects(cd, registered, target_id, *, environment_id=None):
    """Append snapshot-derived names to one bootstrap-owned Role, under the registration lock."""
    namespace, name = 'argocd', 'railshot-product-registrations'
    expected = {('argoproj.io', 'appprojects'): registered['target']['project'],
                ('argoproj.io', 'applications'): application_name(target_id, registered['target']['namespace'], registered['app']),
                ('', 'secrets'): 'railshot-' + target_id}
    role = argo.kubectl(cd['context'], namespace, 'get', 'role', name, '-o', 'json')
    rules = role.get('rules') or []
    seen = set()
    for rule in rules:
        argo.require(set(rule) == {'apiGroups', 'resources', 'verbs', 'resourceNames'}
                     and len(rule['apiGroups']) == len(rule['resources']) == 1
                     and rule['verbs'] in (['get', 'patch'], ['delete']) and rule['resourceNames']
                     and all(label(value) for value in rule['resourceNames']), 'registration Role must contain exact names only')
        key = (rule['apiGroups'][0], rule['resources'][0])
        grant = (*key, tuple(rule['verbs']))
        argo.require(key in expected and grant not in seen, 'registration Role kind differs')
        seen.add(grant)
        if rule['verbs'] == ['delete']:
            pattern = {'applications': r'app-[a-f0-9]{24}-[a-z0-9-]+', 'appprojects': r'app-[a-f0-9]{24}',
                       'secrets': r'railshot-app-[a-f0-9]{24}'}[key[1]]
            argo.require(all(re.fullmatch(pattern, value) for value in rule['resourceNames']), 'app-only deletion grants required')
    original = copy.deepcopy(rules)
    for (group, resource), resource_name in expected.items():
        rule = next((r for r in rules if r['apiGroups'] == [group] and r['resources'] == [resource] and r['verbs'] == ['get', 'patch']), None)
        if rule is None:
            rule = {'apiGroups': [group], 'resources': [resource], 'verbs': ['get', 'patch'], 'resourceNames': []}
            rules.append(rule)
        if resource_name not in rule['resourceNames']:
            rule['resourceNames'].append(resource_name)
        if resource == 'secrets' and environment_id is not None:
            argo.require(label(environment_id) and not environment_id.startswith('app-'), 'environment cluster identity required')
            if 'railshot-' + environment_id not in rule['resourceNames']:
                rule['resourceNames'].append('railshot-' + environment_id)
    if rules != original:
        role['rules'] = rules
        argo.kubectl(cd['context'], namespace, 'replace', '-f', '-', '-o', 'json', document=role)
    observed = argo.kubectl(cd['context'], namespace, 'get', 'role', name, '-o', 'json')
    argo.require(observed.get('rules') == rules, 'registration Role readback differs')


def github_variable(repository, name, document=None, *, create=False):
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    argo.require(token, 'operator GitHub token required')
    path = '/repos/' + repository + '/actions/variables' + ('' if create else '/' + name)
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'}
    request = Request('https://api.github.com' + path, headers=headers,
                      data=bridge.encoded(document) if document is not None else None,
                      method='POST' if create else 'PATCH' if document is not None else 'GET')
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(131073)
            argo.require(len(raw) <= 131072, 'GitHub variable response too large')
            return json.loads(raw) if raw else None
    except HTTPError as error:
        if error.code == 404 and document is None:
            return None
        raise RuntimeError('GitHub variable operation unverified') from None


def bind_ci(settings, registered, target_id):
    repository = settings['source_repository']; name = 'RAILSHOT_TARGET_BINDINGS'
    old = github_variable(repository, name)
    bindings = json.loads(old['value']) if old else {}
    selected = {'app': registered['app'], 'tenant': registered['tenant'], 'image_pull_secret': registered['target']['image_pull_secret']}
    argo.require(isinstance(bindings, dict) and (target_id not in bindings or bindings[target_id] == selected), 'CI target binding conflict')
    updated = {**bindings, target_id: selected}
    validate_target({'CONFIGURED_BINDINGS': json.dumps(updated), 'TARGET_ID': target_id,
                     'APP': registered['app'], 'TENANT': registered['tenant']})
    if bindings != updated:
        github_variable(repository, name, {'name': name, 'value': json.dumps(updated)}, create=old is None)
    observed = github_variable(repository, name)
    argo.require(observed and json.loads(observed['value']) == updated, 'CI target binding readback differs')


def aws_security_group(descriptor, configured=None):
    configured = configured or descriptor.get('security_group_id')
    _, region, instance = ansible.transport_parts(descriptor['transport_ref'])
    result = json.loads(argo.native(['aws', 'ec2', 'describe-instances', '--region', region,
        '--instance-ids', instance, '--output', 'json', '--no-cli-pager']))
    instances = [item for reservation in result['Reservations'] for item in reservation['Instances']]
    argo.require(len(instances) == 1 and instances[0]['InstanceId'] == instance
                 and instances[0]['PrivateIpAddress'] == descriptor['addresses']['private'], 'cloud instance binding differs')
    groups = [group['GroupId'] for group in instances[0]['SecurityGroups']]
    argo.require((configured in groups) if configured else len(groups) == 1, 'select one attached runtime security group')
    selected = configured or groups[0]
    argo.require(re.fullmatch(r'sg-[a-f0-9]{8,17}', selected), 'AWS security group identity required')
    return selected


def register(registry_file, target_id, config_file, state_dir, binding_file=None):
    request, cd, settings, pull, binding, identity = load(registry_file, target_id, config_file, binding_file)
    home = bridge.private_directory(state_dir); shared = bridge.private_directory(settings['state_dir'])
    identity['environment_id'] = home.name
    digest = argo.document_hash(identity); owner = digest[:32]
    record_path = home / 'registration.json'; claim_path = shared / (target_id + '.json')
    record = {'status': 'running', 'input_sha256': digest, 'target_id': target_id, 'environment_id': home.name,
              'provider_kind': request['target']['provider'], 'steps': [], 'deployment_supported': False}
    # ponytail: one writer for GitHub variables and shared renewal policy; shard by control plane if throughput matters.
    with os.fdopen(os.open(shared / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if claim_path.exists():
            argo.require(read_private(claim_path)['input_sha256'] == digest, 'TARGET_REGISTRATION_CONFLICT')
        else:
            for path in shared.glob('*.json'):
                claim = read_private(path)
                argo.require(claim.get('resource_id') != identity['descriptor']['resource_id'], 'runtime already registered')
            save(claim_path, {'input_sha256': digest, 'resource_id': identity['descriptor']['resource_id'], 'environment_id': home.name})
        if record_path.exists():
            record = read_private(record_path)
            argo.require(record['input_sha256'] == digest, 'REGISTRATION_INPUT_CONFLICT')
            if record['status'] == 'succeeded':
                argo.require(record['cd_sha256'] == hashlib.sha256(read_private(home / 'cd.json', raw=True)).hexdigest(), 'CD_REGISTRATION_CHANGED')
                return record
        registered = cd['targets'][target_id]; target = registered['target']
        preflight_renewal(cd, registered, target_id, allow_existing=record_path.exists())
        record.update(status='running', app=registered['app'], namespace=target['namespace'], cluster_server=target['cluster_server'])
        def checkpoint(stage):
            record['stage'] = stage; save(record_path, record)
        def complete(stage):
            if stage not in record['steps']:
                record['steps'].append(stage)
            save(record_path, record)
        try:
            checkpoint('permissions')
            grant_control_objects(cd, registered, target_id)
            complete('permissions')
            checkpoint('edge')
            if settings.get('edge_config_file'):
                edge_request = {'target_id': target_id, 'tenant': registered['tenant'], 'app': registered['app'],
                    'environment_id': home.name, 'provider_kind': request['target']['provider'], 'target_private_ip': identity['descriptor']['addresses']['private'],
                    'namespace': target['namespace'], 'health_path': registered['public_http'].get('health_path') or urlsplit(registered['public_http']['url']).path,
                    'expected_json': registered['public_http']['expected_json'], 'expires_at': settings['expires_at']}
                if request['target']['provider'] == 'aws':
                    edge_request['target_security_group_id'] = aws_security_group(identity['descriptor'], settings.get('target_security_group_id'))
                save(home / 'edge-request.json', edge_request)
                argo.native([sys.executable, str(ROOT / 'gitops/edge.py'), 'prepare', '--config', settings['edge_config_file'],
                    '--request', str(home / 'edge-request.json'), '--out', str(home / 'edge.json')])
                allocation = read_private(home / 'edge.json')
                target['node_port'] = allocation['node_port']; registered['public_http'] = allocation['public_http']
                registered['edge'] = allocation['reference']
            complete('edge')
            with runtime_kubectl(request) as kube:
                checkpoint('namespace')
                services = kube('default', 'get', 'services', '-A', '-o', 'json')
                for service in services['items']:
                    if any(port.get('nodePort') == target['node_port'] for port in service.get('spec', {}).get('ports', [])):
                        argo.require(service['metadata']['namespace'] == target['namespace']
                                     and service['metadata']['name'] == registered['app']
                                     and service.get('spec', {}).get('selector', {}).get('railshot.io/target') == target_id,
                                     'NodePort is already assigned to another application')
                for document in runtime_documents(target, owner, pull, binding):
                    owned_apply(kube, document)
                complete('namespace')
                checkpoint('argo')
                tls_options = {}
                if (request['target']['provider'] == 'openstack' and request['inventory']['control_plane'][0]['ssh'].get('connect_host')
                        or request['target']['provider'] == 'gcp' and identity['descriptor'].get('management_endpoint')):
                    tls_options['tls_server_name'] = identity['descriptor']['addresses']['private']
                renewal, expiration = register_argo(kube, cd, registered, target_id, owner, binding, **tls_options)
                save(home / 'renewal.json', renewal); complete('argo')
            checkpoint('credentials')
            install_renewal(cd, renewal)
            record['credentials'] = {'renewal': 'configured', 'expires_at': expiration}; complete('credentials')
            if settings.get('observability_config_file'):
                checkpoint('observability')
                observation = {'version': 1, 'target_id': target_id, 'environment_id': home.name,
                    'app': registered['app'], 'namespace': target['namespace'],
                    'node_ip': identity['descriptor']['addresses']['private'],
                    'probe_url': registered['public_http']['url'], 'registry_file': str(registry_file), 'context': cd['context']}
                save(home / 'observability-request.json', observation)
                rc = ansible.execute([sys.executable, str(ROOT / 'observability/register.py'),
                    '--config', settings['observability_config_file'], '--request', str(home / 'observability-request.json'),
                    '--out', str(home / 'observability.json')], 900, dict(os.environ))
                argo.require(rc == 0, 'observability registration did not complete')
                observed = read_private(home / 'observability.json')
                argo.require(observed.get('status') == 'succeeded' and observed.get('target_id') == target_id
                             and observed.get('app') == registered['app'] and observed.get('registered') is True,
                             'observability registration binding differs')
                record['observability'] = {'registered': True, 'collection_state': 'pending'}
                complete('observability')
            checkpoint('ci')
            bind_ci(settings, registered, target_id); complete('ci')
            save(home / 'cd.json', cd)
            record.pop('error', None)
            record.update(status='succeeded', deployment_supported=True, node_port=target['node_port'],
                          cd_sha256=hashlib.sha256(read_private(home / 'cd.json', raw=True)).hexdigest())
            checkpoint('registered')
        except Exception:
            record.update(status='unknown', deployment_supported=False,
                          error={'code': 'REGISTRATION_RECONCILE_REQUIRED', 'outcome_unknown': True, 'retryable': False})
            save(record_path, record)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['register'])
    for arg in ('registry', 'target-id', 'config', 'state-dir'):
        parser.add_argument('--' + arg, required=True)
    parser.add_argument('--binding')
    args = parser.parse_args(); os.umask(0o077)
    try:
        result = register(args.registry, args.target_id, args.config, args.state_dir, args.binding)
    except Exception:
        result = {'status': 'blocked', 'target_id': args.target_id, 'deployment_supported': False,
                  'error': {'code': 'REGISTRATION_INPUT_INVALID', 'outcome_unknown': False, 'retryable': False}}
    print(json.dumps(result))
    return 0 if result['status'] == 'succeeded' else 3


if __name__ == '__main__':
    raise SystemExit(main())
