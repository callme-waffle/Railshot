#!/usr/bin/env python3
"""Fixed customer-side Kubernetes relay; never accepts an arbitrary SSH command."""
import base64
import fcntl
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import shlex
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import time

CONFIG = Path('/etc/railshot-personal')
PAYLOAD = Path('/opt/railshot/personal')
HOME = Path('/var/lib/railshot-personal-kube')
USER = 'railshot-runtime'
WRAPPER = Path('/usr/local/sbin/railshot-runtime-access')
SUDOERS = Path('/etc/sudoers.d/railshot-runtime-access')
SSH_UNIT = Path('/etc/systemd/system/railshot-personal-kube.service')
TLS_UNIT = Path('/etc/systemd/system/railshot-personal-kube-proxy.service')
MARKER = '# Managed by RailShot personal client v1\n'
MAX_BYTES = 1048576
LABEL = re.compile(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?')
PREFIX = ['sudo', '-n', 'k3s', 'kubectl', '--request-timeout=20s', '-n']
APP_CONTROLS = {'deployments.apps': ('Deployment', 'replicas'), 'statefulsets.apps': ('StatefulSet', 'replicas'),
                'cronjobs.batch': ('CronJob', 'suspend'), 'jobs.batch': ('Job', 'suspend')}
TUNNEL_NAME = 'railshot-tunnel'
TUNNEL_RESOURCES = {
    ('serviceaccount', TUNNEL_NAME), ('secret', TUNNEL_NAME + '-credentials'),
    ('configmap', TUNNEL_NAME + '-ca'), ('configmap', TUNNEL_NAME + '-config'),
    ('deployment', TUNNEL_NAME),
}
TUNNEL_OWNERS = 'railshot.io/application-owners'
TUNNEL_HASH = 'railshot.io/config-sha256'


def require(value, code='RUNTIME_ACCESS_REJECTED'):
    if not value:
        raise ValueError(code)


def safe(path, *, directory=False, private=True, owner=0):
    path = Path(path)
    require(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)))
    info = path.stat()
    require(info.st_uid == owner and not info.st_mode & (0o077 if private else 0o022))
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
    return path


def read(path):
    path = safe(path)
    require(path.stat().st_size <= MAX_BYTES)
    return json.loads(path.read_text())


def write(path, value, *, mode=0o600, public=False):
    path = Path(path)
    safe(path.parent, directory=True, private=False)
    if path.exists() or path.is_symlink():
        safe(path, private=not public)
    fd, temporary = tempfile.mkstemp(prefix='.runtime-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value if isinstance(value, str) else json.dumps(value, sort_keys=True))
            stream.flush(); os.fchmod(stream.fileno(), mode); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(args, *, input=None, timeout=45):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout, check=False)
    require(result.returncode == 0, 'RUNTIME_ACCESS_COMMAND_FAILED')
    require(len(result.stdout.encode()) <= MAX_BYTES, 'RUNTIME_ACCESS_RESPONSE_TOO_LARGE')
    return result.stdout.strip()


def namespace_allowed(namespace, target):
    return namespace == target or bool(re.fullmatch(r'app-[a-f0-9]{24}', namespace))


def app_namespace(namespace):
    return bool(re.fullmatch(r'app-[a-f0-9]{24}', namespace))


def tunnel_resource(namespace, kind, name, target):
    return namespace == target and (kind.lower(), name) in TUNNEL_RESOURCES


def tunnel_renderer():
    path = Path(__file__).resolve().parents[2] / 'deployment/cloudflared/render.py'
    safe(path, private=False, owner=os.geteuid())
    spec = importlib.util.spec_from_file_location('railshot_runtime_tunnel_renderer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value)
        value[key] = item
    return value


def tunnel_config(value, *, initial=False):
    require(isinstance(value, str) and len(value.encode()) <= MAX_BYTES)
    try:
        config = json.loads(value, object_pairs_hook=unique_object)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError('RUNTIME_ACCESS_REJECTED') from None
    require(isinstance(config, dict) and set(config) == {'tunnel', 'credentials-file', 'ingress'}
            and isinstance(config['tunnel'], str)
            and re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', config['tunnel'])
            and config['credentials-file'] == '/etc/cloudflared/credentials/credentials.json'
            and isinstance(config['ingress'], list) and 1 <= len(config['ingress']) <= 51
            and config['ingress'][-1] == {'service': 'http_status:404'})
    hostnames, origin = [], '10.0.0.1'
    for row in config['ingress'][:-1]:
        require(isinstance(row, dict) and set(row) == {'hostname', 'service', 'originRequest'})
        hostname = row['hostname']
        require(isinstance(hostname, str) and len(hostname) <= 253 and '.' in hostname
                and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in hostname.split('.')))
        match = re.fullmatch(r'https://([0-9.]+):443', row['service']) if isinstance(row['service'], str) else None
        require(match is not None)
        address = str(ipaddress.IPv4Address(match.group(1)))
        require(any(ipaddress.ip_address(address) in ipaddress.ip_network(c)
                    for c in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
        require(row['originRequest'] == {'originServerName': hostname, 'httpHostHeader': hostname,
                'noTLSVerify': False, 'caPool': '/etc/cloudflared/ca/ca.pem'})
        require(origin == '10.0.0.1' or origin == address)
        origin = address
        hostnames.append(hostname)
    require(not initial or not hostnames)
    require(len(set(hostnames)) == len(hostnames) and hostnames == sorted(hostnames))
    renderer = tunnel_renderer()
    rendered = renderer.render(namespace='personal-' + '0' * 36, name=TUNNEL_NAME,
        tunnel_id=config['tunnel'], credentials_secret=TUNNEL_NAME + '-credentials', hostnames=hostnames,
        origin_vip=origin, ca_configmap=TUNNEL_NAME + '-ca')['items'][1]['data']['config.json']
    require(value == rendered)
    return hostnames


def tunnel_labels(target, rendered=False):
    labels = {'app.kubernetes.io/managed-by': 'railshot', 'railshot.io/registration': target}
    if rendered:
        labels.update({'app.kubernetes.io/name': 'cloudflared', 'app.kubernetes.io/instance': TUNNEL_NAME})
    return labels


def validate_tunnel_document(doc, namespace, target):
    kind, meta = doc['kind'], doc['metadata']
    key = (kind.lower(), meta.get('name'))
    require(tunnel_resource(namespace, *key, target) and meta.get('namespace') == target)
    renderer = tunnel_renderer()
    sample = renderer.render(namespace=target, name=TUNNEL_NAME,
        tunnel_id='11111111-1111-4111-8111-111111111111', credentials_secret=TUNNEL_NAME + '-credentials',
        hostnames=[], origin_vip='10.0.0.1', ca_configmap=TUNNEL_NAME + '-ca')['items']
    if key == ('secret', TUNNEL_NAME + '-credentials'):
        require(set(doc) == {'apiVersion', 'kind', 'metadata', 'type', 'data'} and doc['apiVersion'] == 'v1'
                and doc['type'] == 'Opaque' and set(meta) == {'name', 'namespace', 'labels'}
                and meta['labels'] == tunnel_labels(target) and isinstance(doc['data'], dict)
                and set(doc['data']) == {'credentials.json'} and isinstance(doc['data']['credentials.json'], str))
        try:
            credential = json.loads(base64.b64decode(doc['data']['credentials.json'], validate=True), object_pairs_hook=unique_object)
        except (TypeError, ValueError, UnicodeError):
            raise ValueError('RUNTIME_ACCESS_REJECTED') from None
        require(isinstance(credential, dict) and isinstance(credential.get('TunnelSecret'), str)
                and bool(credential['TunnelSecret']) and isinstance(credential.get('TunnelID'), str) and re.fullmatch(
                    r'[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}', credential.get('TunnelID', '')))
        return
    if key == ('configmap', TUNNEL_NAME + '-ca'):
        require(set(doc) == {'apiVersion', 'kind', 'metadata', 'data'} and doc['apiVersion'] == 'v1'
                and set(meta) == {'name', 'namespace', 'labels'} and meta['labels'] == tunnel_labels(target)
                and isinstance(doc['data'], dict) and set(doc['data']) == {'ca.pem'}
                and isinstance(doc['data']['ca.pem'], str) and doc['data']['ca.pem'].startswith('-----BEGIN CERTIFICATE-----')
                and doc['data']['ca.pem'].rstrip().endswith('-----END CERTIFICATE-----'))
        return
    rendered = next(value for value in sample if value['kind'] == kind)
    rendered['metadata']['labels'].update(tunnel_labels(target))
    if kind == 'ConfigMap':
        try:
            config = json.loads(doc.get('data', {}).get('config.json', '{}'))
        except (TypeError, ValueError, UnicodeError):
            raise ValueError('RUNTIME_ACCESS_REJECTED') from None
        require(isinstance(config, dict))
        tunnel_id = config.get('tunnel')
        rendered = renderer.render(namespace=target, name=TUNNEL_NAME, tunnel_id=tunnel_id,
            credentials_secret=TUNNEL_NAME + '-credentials', hostnames=[], origin_vip='10.0.0.1',
            ca_configmap=TUNNEL_NAME + '-ca')['items'][1]
        rendered['metadata']['labels'].update(tunnel_labels(target))
        rendered['metadata']['annotations'] = {TUNNEL_OWNERS: '{}'}
        tunnel_config(doc.get('data', {}).get('config.json'), initial=True)
    elif kind == 'Deployment':
        rendered['spec']['template']['metadata']['labels'].update(tunnel_labels(target))
        digest = doc.get('spec', {}).get('template', {}).get('metadata', {}).get('annotations', {}).get(TUNNEL_HASH, '')
        require(re.fullmatch(r'[a-f0-9]{64}', digest))
        rendered['spec']['template']['metadata']['annotations'][TUNNEL_HASH] = digest
    require(doc == rendered)


def validate_tunnel_patch(kind, document):
    require(isinstance(document, list) and len(document) == (4 if kind == 'configmap' else 3)
            and all(isinstance(row, dict) and set(row) == {'op', 'path', 'value'} for row in document))
    require([(row['op'], row['path']) for row in document[:2]] == [
        ('test', '/metadata/uid'), ('test', '/metadata/resourceVersion')])
    preconditions({'uid': document[0]['value'], 'resourceVersion': document[1]['value']})
    if kind == 'deployment':
        require((document[2]['op'], document[2]['path']) ==
                ('replace', '/spec/template/metadata/annotations/railshot.io~1config-sha256')
                and isinstance(document[2]['value'], str) and re.fullmatch(r'[a-f0-9]{64}', document[2]['value']))
        return
    require([(row['op'], row['path']) for row in document[2:]] == [
        ('replace', '/data/config.json'),
        ('replace', '/metadata/annotations/railshot.io~1application-owners')])
    hostnames = tunnel_config(document[2]['value'])
    try:
        owners = json.loads(document[3]['value'], object_pairs_hook=unique_object)
    except (TypeError, ValueError, UnicodeError):
        raise ValueError('RUNTIME_ACCESS_REJECTED') from None
    require(isinstance(owners, dict) and set(owners) == set(hostnames)
            and all(isinstance(value, str) and re.fullmatch(r'app-[a-f0-9]{24}', value) for value in owners.values()))


def raw_read(path, namespace):
    # Discovery metadata and app-only collections; never arbitrary raw API paths.
    if path in ('/api', '/apis', '/api/v1') or re.fullmatch(r'/apis/[a-z0-9.-]+/v[0-9]+(?:(?:alpha|beta)[0-9]+)?', path):
        return True
    collection, separator, query = path.partition('?')
    if not separator or not re.fullmatch(r'limit=500(?:&continue=[A-Za-z0-9%_.~-]{1,4096})?', query):
        return False
    if collection in ('/api/v1/persistentvolumes', '/apis/storage.k8s.io/v1/volumeattachments'):
        return True  # Existing lifecycle safety checks must detect shared backing storage.
    return bool(re.fullmatch(r'/(?:api/v1|apis/[a-z0-9.-]+/v[0-9]+(?:(?:alpha|beta)[0-9]+)?)/namespaces/'
                             + re.escape(namespace) + r'/[a-z][a-z0-9-]*', collection))


def validate_runtime(config, runtime):
    require(re.fullmatch(r'personal-[a-f0-9-]{36}', config['target_id']))
    require(all(runtime[k] == config[k] for k in ('target_id', 'generation', 'project_id')))
    require(runtime['version'] == 1 and runtime['architecture'] == 'amd64')
    host = str(ipaddress.IPv4Address(runtime['private_ipv4']))
    require(any(ipaddress.ip_address(host) in ipaddress.ip_network(c) for c in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
    ssh = runtime['ssh']
    require(set(ssh) == {'host', 'user', 'port', 'identity_file', 'known_hosts_file'})
    require(ssh['host'] == host and ssh['port'] == 22 and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', ssh['user']) and ssh['user'] != 'root')
    safe(ssh['identity_file']); safe(ssh['known_hosts_file'])
    return runtime


def parse(command, document, target):
    require(isinstance(command, str) and len(command) <= 4096 and not any(ord(c) < 32 for c in command))
    parts = shlex.split(command)
    require(parts[:6] == PREFIX and len(parts) >= 8)
    namespace, args = parts[6], parts[7:]
    if namespace == 'default' and isinstance(document, dict) and document.get('kind') == 'Namespace':
        namespace = document.get('metadata', {}).get('name', '')
    scoped = namespace_allowed(namespace, target)
    reads = [(['get', 'nodes', '-o', 'json'], 'default'),
             (['get', 'namespace', 'kube-system', '-o', 'json'], 'default'),
             (['get', 'services', '-A', '-o', 'json'], 'default'),
             (['get', 'deployment', 'coredns', '-o', 'json'], 'kube-system')]
    if (args, namespace) in reads:
        require(document is None)
        return namespace, args, 'read'
    if args == ['config', 'view', '--raw', '--minify', '-o', 'json']:
        require(scoped and document is None)
        return namespace, args, 'ca'
    if app_namespace(namespace) and args[:2] == ['get', '--raw'] and len(args) == 3:
        require(document is None and raw_read(args[2], namespace))
        return namespace, args, 'raw-read'
    if args[0] == 'get':
        require(document is None and len(args) in (5, 6) and args[-2:] == ['-o', 'json'])
        require(len(args) == 5 or args[3] == '--ignore-not-found')
        kind, name = args[1].lower(), args[2]
        require(LABEL.fullmatch(name))
        if kind == 'namespace':
            require(namespace in ('default', name) and namespace_allowed(name, target))
        else:
            if app_namespace(namespace) and kind in (*APP_CONTROLS, 'storageclass'):
                pass
            elif tunnel_resource(namespace, kind, name, target):
                pass
            else:
                require(scoped and kind in ('serviceaccount', 'role', 'rolebinding', 'secret'))
                require(name == 'ghcr-pull' if kind == 'secret' else name in ('railshot-argocd', 'railshot-environment-argocd'))
        return namespace, args, 'read'
    require(scoped)
    if args == ['apply', '--server-side', '--field-manager=railshot-registration', '-f', '-', '-o', 'json']:
        validate_document(document, namespace, target)
        return namespace, args, 'apply'
    expected = ['create', '--raw', '/api/v1/namespaces/' + namespace + '/serviceaccounts/railshot-argocd/token', '-f', '-']
    if args == expected:
        require(isinstance(document, dict) and set(document) == {'apiVersion', 'kind', 'spec'}
                and document['apiVersion'] == 'authentication.k8s.io/v1' and document['kind'] == 'TokenRequest'
                and set(document['spec']) == {'expirationSeconds'}
                and type(document['spec']['expirationSeconds']) is int and 600 <= document['spec']['expirationSeconds'] <= 86400)
        return namespace, args, 'token'
    if app_namespace(namespace) and args == ['delete', '--raw', '/api/v1/namespaces/' + namespace, '-f', '-']:
        require(isinstance(document, dict) and set(document) == {'apiVersion', 'kind', 'preconditions', 'propagationPolicy'}
                and document['apiVersion'] == 'v1' and document['kind'] == 'DeleteOptions'
                and document['propagationPolicy'] == 'Foreground')
        preconditions(document['preconditions'])
        return namespace, args, 'app-delete'
    if app_namespace(namespace) and len(args) == 7 and args[0] == 'patch' and args[1] in APP_CONTROLS:
        require(LABEL.fullmatch(args[2]) and args[3:] == ['--type=json', '--patch-file=/dev/stdin', '-o', 'json'])
        field = APP_CONTROLS[args[1]][1]
        require(isinstance(document, list) and len(document) == 3
                and all(isinstance(row, dict) and set(row) == {'op', 'path', 'value'} for row in document)
                and [(row['op'], row['path']) for row in document] == [
                    ('test', '/metadata/uid'), ('test', '/metadata/resourceVersion'), ('add', '/spec/' + field)])
        preconditions({'uid': document[0]['value'], 'resourceVersion': document[1]['value']})
        require((type(document[2]['value']) is int and 0 <= document[2]['value'] <= 1000) if field == 'replicas'
                else type(document[2]['value']) is bool)
        return namespace, args, 'app-patch'
    if namespace == target and len(args) == 7 and args[0] == 'patch' and (
            (args[1], args[2]) in (('ConfigMap', TUNNEL_NAME + '-config'), ('configmap', TUNNEL_NAME + '-config'),
                                   ('Deployment', TUNNEL_NAME), ('deployment', TUNNEL_NAME))):
        require(args[3:] == ['--type=json', '--patch-file=/dev/stdin', '-o', 'json'])
        validate_tunnel_patch(args[1].lower(), document)
        return namespace, args, 'tunnel-patch'
    if namespace == target and len(args) == 5 and args[:2] == ['delete', '--raw'] and args[3:] == ['-f', '-']:
        paths = {
            '/api/v1/namespaces/' + target + '/serviceaccounts/railshot-argocd',
            '/api/v1/namespaces/' + target + '/secrets/ghcr-pull',
            '/apis/rbac.authorization.k8s.io/v1/namespaces/' + target + '/roles/railshot-argocd',
            '/apis/rbac.authorization.k8s.io/v1/namespaces/' + target + '/rolebindings/railshot-argocd',
            '/api/v1/namespaces/' + target + '/serviceaccounts/' + TUNNEL_NAME,
            '/api/v1/namespaces/' + target + '/secrets/' + TUNNEL_NAME + '-credentials',
            '/api/v1/namespaces/' + target + '/configmaps/' + TUNNEL_NAME + '-ca',
            '/api/v1/namespaces/' + target + '/configmaps/' + TUNNEL_NAME + '-config',
            '/apis/apps/v1/namespaces/' + target + '/deployments/' + TUNNEL_NAME,
        }
        require(args[2] in paths and isinstance(document, dict) and set(document) == {'apiVersion', 'kind', 'preconditions'}
                and document['apiVersion'] == 'v1' and document['kind'] == 'DeleteOptions'
                and isinstance(document['preconditions'], dict) and set(document['preconditions']) == {'uid', 'resourceVersion'}
                and all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9-]{1,128}', v) for v in document['preconditions'].values()))
        return namespace, args, 'delete'
    raise ValueError('RUNTIME_ACCESS_COMMAND_REJECTED')


def preconditions(value):
    require(isinstance(value, dict) and set(value) == {'uid', 'resourceVersion'}
            and all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9-]{1,128}', v) for v in value.values()))


def lifecycle_module():
    # Reuse the existing audited ownership/storage checks, not a weaker deletion policy.
    path = safe(PAYLOAD / 'deployment/scripts/lifecycle_runtime.py', private=False)
    spec = importlib.util.spec_from_file_location('railshot_runtime_lifecycle', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_app_inventory(remote, namespace, target):
    lifecycle = lifecycle_module()
    sa = remote(namespace, ['get', 'serviceaccount', 'railshot-argocd', '-o', 'json'])
    shared = remote(target, ['get', 'serviceaccount', 'railshot-argocd', '--ignore-not-found', '-o', 'json'])
    shared_renewal = {'service_account': {'name': 'railshot-argocd', 'namespace': target,
                                        'uid': shared['metadata']['uid']}} if shared else None
    binding = {'version': 1, 'application_id': namespace, 'environment_id': target,
               'registered': {'target': {'id': namespace, 'namespace': namespace}}}
    renewal = {'service_account': {'name': 'railshot-argocd', 'namespace': namespace, 'uid': sa['metadata']['uid']}}
    return lifecycle.inventory(lambda ns, *args, **kwargs: remote(ns, list(args), kwargs.get('document')),
                               binding, renewal, 'delete', shared_renewal=shared_renewal)


def validate_document(doc, namespace, target):
    require(isinstance(doc, dict) and isinstance(doc.get('metadata'), dict))
    kind, meta = doc.get('kind'), doc['metadata']
    if isinstance(kind, str) and tunnel_resource(namespace, kind, meta.get('name'), target):
        validate_tunnel_document(doc, namespace, target)
        return
    require(kind in ('Namespace', 'ServiceAccount', 'Role', 'RoleBinding', 'Secret'))
    require(doc.get('apiVersion') == ('rbac.authorization.k8s.io/v1' if kind in ('Role', 'RoleBinding') else 'v1'))
    require(set(meta) == ({'name', 'labels'} if kind == 'Namespace' else {'name', 'namespace', 'labels'}))
    require(LABEL.fullmatch(meta['name']) and meta.get('namespace', namespace) == namespace)
    labels = meta['labels']
    require(isinstance(labels, dict) and set(labels) == {'app.kubernetes.io/managed-by', 'railshot.io/registration'}
            and labels['app.kubernetes.io/managed-by'] == 'railshot')
    owner = labels['railshot.io/registration']
    require(owner == (namespace if app_namespace(namespace) else target))
    common = {'apiVersion', 'kind', 'metadata'}
    if kind == 'Namespace':
        require(set(doc) == common and meta['name'] == namespace)
    elif kind == 'ServiceAccount':
        require(set(doc) == common | {'automountServiceAccountToken'} and meta['name'] == 'railshot-argocd'
                and doc['automountServiceAccountToken'] is False)
    elif kind == 'Secret':
        require(set(doc) == common | {'type', 'data'} and doc['type'] == 'kubernetes.io/dockerconfigjson'
                and meta['name'] == 'ghcr-pull' and set(doc.get('data', {})) == {'.dockerconfigjson'})
        require(isinstance(doc['data'], dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in doc['data'].items()))
        for value in doc['data'].values():
            base64.b64decode(value, validate=True)
    elif kind == 'RoleBinding':
        require(set(doc) == common | {'roleRef', 'subjects'})
        require(doc['roleRef'] == {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'railshot-argocd'})
        subject_ns = target if meta['name'] == 'railshot-environment-argocd' else namespace
        require(meta['name'] in ('railshot-environment-argocd', 'railshot-argocd'))
        require(doc['subjects'] == [{'kind': 'ServiceAccount', 'name': 'railshot-argocd', 'namespace': subject_ns}])
    else:
        require(set(doc) == common | {'rules'} and meta['name'] == 'railshot-argocd' and isinstance(doc['rules'], list))
        writable = {('apps', 'deployments'), ('', 'services'), ('', 'persistentvolumeclaims'),
                    ('networking.k8s.io', 'networkpolicies'), ('batch', 'jobs')}
        readable = {('', 'pods'), ('', 'events'), ('', 'pods/log'), ('apps', 'replicasets')}
        for rule in doc['rules']:
            require(isinstance(rule, dict) and set(rule) <= {'apiGroups', 'resources', 'verbs', 'resourceNames'}
                    and set(rule) >= {'apiGroups', 'resources', 'verbs'} and len(rule['apiGroups']) == 1
                    and isinstance(rule['resources'], list) and bool(rule['resources']) and isinstance(rule['verbs'], list) and bool(rule['verbs']))
            group = rule['apiGroups'][0]
            for resource in rule['resources']:
                if (group, resource) in writable:
                    require(set(rule['verbs']) <= {'get', 'list', 'watch', 'create', 'update', 'patch', 'delete'})
                elif (group, resource) in readable:
                    require(set(rule['verbs']) <= ({'get'} if resource == 'pods/log' else {'get', 'list', 'watch'}))
                else:
                    require((group, resource) == ('', 'serviceaccounts/token') and rule['verbs'] == ['create']
                            and rule.get('resourceNames') == ['railshot-argocd'])


def ssh_command(runtime, namespace, args):
    ssh = runtime['ssh']
    return ['ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'IdentityAgent=none', '-o', 'StrictHostKeyChecking=yes', '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'ProxyCommand=none', '-o', 'PermitLocalCommand=no', '-o', 'ConnectTimeout=10',
            '-o', 'UserKnownHostsFile=' + ssh['known_hosts_file'], '-i', ssh['identity_file'],
            '-p', '22', ssh['user'] + '@' + ssh['host'], shlex.join(PREFIX + [namespace, *args])]


def execute(config, runtime, envelope, runner=run):
    require(isinstance(envelope, dict) and set(envelope) == {'command', 'document'})
    namespace, args, action = parse(envelope['command'], envelope['document'], config['target_id'])
    def remote(ns, argv, document=None):
        raw = runner(ssh_command(runtime, ns, argv), input=None if document is None else json.dumps(document))
        return json.loads(raw) if raw else None
    if action == 'read' and (namespace in ('default', 'kube-system') or args[1].lower() == 'namespace'):
        return remote(namespace, args)
    ledger_path = CONFIG / 'runtime-access-objects.json'
    ledger = read(ledger_path) if ledger_path.exists() else {}
    observed = remote('default', ['get', 'namespace', namespace, '--ignore-not-found', '-o', 'json'])
    owned = ledger.get(namespace)
    if owned:
        require(all(owned.get(key) == value for key, value in {
            'target_id': config['target_id'], 'generation': config['generation'], 'resource_id': runtime['resource_id']}.items()),
            'RUNTIME_NAMESPACE_BINDING_CHANGED')
        if action == 'raw-read' and owned.get('status') == 'deleting' and observed is None:
            require(args[2].split('?')[0] in ('/api/v1/persistentvolumes', '/apis/storage.k8s.io/v1/volumeattachments'))
            return remote(namespace, args)
        require(observed and observed['metadata'].get('uid') == owned['uid']
                and observed['metadata'].get('labels', {}).get('railshot.io/registration') == owned['owner'])
        require(owned.get('status') != 'deleting' or action in ('read', 'raw-read'), 'RUNTIME_NAMESPACE_REMOVAL_UNVERIFIED')
    else:
        require(action == 'apply' and envelope['document']['kind'] == 'Namespace' and observed is None,
                'RUNTIME_NAMESPACE_NOT_OWNED')
    if action == 'ca':
        raw = runner(ssh_command(runtime, namespace, ['config', 'view', '--raw', '--minify',
                     '-o', 'jsonpath={.clusters[0].cluster.certificate-authority-data}']))
        require(b'-----BEGIN CERTIFICATE-----' in base64.b64decode(raw, validate=True))
        return {'clusters': [{'cluster': {'certificate-authority-data': raw}}]}
    if action in ('read', 'raw-read'):
        return remote(namespace, args)
    # A local root-owned ledger prevents claiming a preexisting customer namespace,
    # even if it happens to use a RailShot-shaped name or forged labels.
    if action == 'apply':
        document = envelope['document']
        require(not owned or document['metadata']['labels']['railshot.io/registration'] == owned['owner'])
        if not owned:
            # create is atomic; an existing object appearing after the read is never adopted.
            result = remote(namespace, ['create', '-f', '-', '-o', 'json'], document)
            require(result['metadata'].get('uid'))
            ledger[namespace] = {'uid': result['metadata']['uid'], 'owner': document['metadata']['labels']['railshot.io/registration'],
                                 'target_id': config['target_id'], 'generation': config['generation'], 'resource_id': runtime['resource_id']}
            write(ledger_path, ledger)
            return result
        existing = remote(namespace, ['get', document['kind'], document['metadata']['name'], '--ignore-not-found', '-o', 'json'])
        if existing:
            require(not existing['metadata'].get('ownerReferences') and all(
                existing['metadata'].get('labels', {}).get(k) == v for k, v in document['metadata']['labels'].items()))
    if action == 'delete':
        kind, name = args[2].split('/')[-2:]
        singular = {'serviceaccounts': 'serviceaccount', 'secrets': 'secret', 'roles': 'role', 'rolebindings': 'rolebinding',
                    'configmaps': 'configmap', 'deployments': 'deployment'}[kind]
        observed = remote(namespace, ['get', singular, name, '-o', 'json'])
        meta = observed['metadata']
        require(not meta.get('ownerReferences') and meta.get('labels', {}).get('app.kubernetes.io/managed-by') == 'railshot'
                and meta.get('labels', {}).get('railshot.io/registration') == owned['owner']
                and all(meta.get(key) == value for key, value in envelope['document']['preconditions'].items()))
    if action == 'tunnel-patch':
        live = remote(namespace, ['get', args[1], args[2], '--ignore-not-found', '-o', 'json'])
        meta = live['metadata'] if live else {}
        require(live and not meta.get('ownerReferences')
                and meta.get('labels', {}).get('app.kubernetes.io/managed-by') == 'railshot'
                and meta.get('labels', {}).get('railshot.io/registration') == owned['owner']
                and meta.get('uid') == envelope['document'][0]['value']
                and meta.get('resourceVersion') == envelope['document'][1]['value'])
    if action in ('app-delete', 'app-patch'):
        verify_app_inventory(remote, namespace, config['target_id'])
        if action == 'app-delete':
            require(all(observed['metadata'].get(key) == value for key, value in envelope['document']['preconditions'].items()))
            ledger[namespace]['status'] = 'deleting'
            write(ledger_path, ledger)  # Intent is retained if the delete reply is lost.
        else:
            live = remote(namespace, ['get', args[1], args[2], '--ignore-not-found', '-o', 'json'])
            doc = envelope['document']
            require(live and live['metadata'].get('uid') == doc[0]['value']
                    and live['metadata'].get('resourceVersion') == doc[1]['value'])
    return remote(namespace, args, envelope['document'])


def install(config, runtime, runner=run):
    require(os.geteuid() == 0)
    validate_runtime(config, runtime)
    address = str(ipaddress.ip_interface(config['tunnel']['address']).ip)
    allowed = config['tunnel']['allowed_ips']
    if isinstance(allowed, list):
        require(len(allowed) == 1); allowed = allowed[0]
    source = ipaddress.ip_network(allowed)
    require(source.version == 4 and source.prefixlen == 32 and str(source.network_address) != address)
    public_key = config['runtime_access']['public_key']
    require(re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]{68}={0,2}(?: [A-Za-z0-9:._-]+)?', public_key))
    public_key = ' '.join(public_key.split()[:2])
    marker_path = CONFIG / 'runtime-access-account.json'
    try:
        account = pwd.getpwnam(USER)
    except KeyError:
        runner(['useradd', '--system', '--user-group', '--no-create-home', '--home-dir', str(HOME), '--shell', '/bin/sh', USER], timeout=180)
        runner(['usermod', '--password', '*', USER], timeout=180)
        account = pwd.getpwnam(USER)
        write(marker_path, {'user': USER, 'uid': account.pw_uid, 'gid': account.pw_gid})
    require(read(marker_path) == {'user': USER, 'uid': account.pw_uid, 'gid': account.pw_gid}
            and account.pw_uid != 0 and account.pw_dir == str(HOME) and account.pw_shell == '/bin/sh')
    HOME.mkdir(mode=0o755, exist_ok=True)
    safe(HOME, directory=True, private=False)  # Root owns all forced-command files.
    module = PAYLOAD / 'apps/agent/runtime_access.py'
    command = '/usr/bin/python3 -I ' + str(module) + ' forward'
    write(HOME / 'authorized_keys', 'restrict,from="' + str(source) + '",command="' + command + '" ' + public_key + '\n', mode=0o644, public=True)
    # The shebang must be first; the ownership marker remains in a fixed second line.
    wrapper = '#!/bin/sh\n' + MARKER + 'exec /usr/bin/python3 -I ' + str(module) + ' serve\n'
    sudoers = MARKER + USER + ' ALL=(root) NOPASSWD: ' + str(WRAPPER) + ' ""\n'
    for path, content, mode in ((WRAPPER, wrapper, 0o755), (SUDOERS, sudoers, 0o440)):
        if path.exists():
            safe(path, private=False); require(MARKER in path.read_text().splitlines(keepends=True)[:2])
        write(path, content, mode=mode, public=True)
    runner(['visudo', '-cf', str(SUDOERS)])
    host_key = CONFIG / 'runtime_access_host_key'
    if not host_key.exists():
        runner(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(host_key)])
    safe(host_key)
    conf = MARKER + ('Port 2223\nListenAddress ' + address + '\nHostKey ' + str(host_key)
        + '\nPidFile /run/railshot-personal-kube.pid\nAuthorizedKeysFile ' + str(HOME / 'authorized_keys')
        + '\nPermitRootLogin no\nAllowUsers ' + USER + '\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n'
        'PubkeyAuthentication yes\nAuthenticationMethods publickey\nUsePAM no\nPermitTTY no\nForceCommand ' + command
        + '\nAllowTcpForwarding no\nAllowAgentForwarding no\nX11Forwarding no\nPermitTunnel no\nPermitUserEnvironment no\nPermitUserRC no\n')
    write(CONFIG / 'runtime_access_sshd_config', conf)
    runner(['/usr/sbin/sshd', '-t', '-f', str(CONFIG / 'runtime_access_sshd_config')])
    service = MARKER + '[Unit]\nAfter=wg-quick@railshot0.service\n[Service]\nType=simple\nRestart=on-failure\n'
    for path, executable in ((SSH_UNIT, '/usr/sbin/sshd -D -e -f ' + str(CONFIG / 'runtime_access_sshd_config')),
                             (TLS_UNIT, '/usr/bin/python3 -I ' + str(module) + ' proxy')):
        if path.exists():
            safe(path, private=False); require(path.read_text().startswith(MARKER))
        write(path, service + 'ExecStart=' + executable + '\n[Install]\nWantedBy=multi-user.target\n', mode=0o644, public=True)
    runner(['systemctl', 'daemon-reload'])
    for unit in (SSH_UNIT.stem, TLS_UNIT.stem):
        runner(['systemctl', 'enable', '--now', unit])
        require(runner(['systemctl', 'is-active', unit]) == 'active')
    return {'ssh_user': USER, 'ssh_port': 2223, 'ssh_host_key': ' '.join(host_key.with_suffix('.pub').read_text().split()[:2])}


def serve():
    require(os.geteuid() == 0 and os.environ.get('SUDO_USER') == USER)
    account = read(CONFIG / 'runtime-access-account.json')
    require(os.environ.get('SUDO_UID') == str(account['uid']))
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES)
    config, runtime = read(CONFIG / 'client.json'), read(CONFIG / 'runtime.json')
    validate_runtime(config, runtime)
    lock_path = CONFIG / 'runtime-access.lock'
    if lock_path.exists() or lock_path.is_symlink():
        safe(lock_path)
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = execute(config, runtime, json.loads(raw))
    if result is not None:
        print(json.dumps(result))


def forward():
    require(os.geteuid() == pwd.getpwnam(USER).pw_uid)
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    require(isinstance(command, str) and len(command) <= 4096)
    parts = shlex.split(command)
    raw = sys.stdin.buffer.read(MAX_BYTES + 1) if '-f' in parts or '--patch-file=/dev/stdin' in parts else b''
    require(len(raw) <= MAX_BYTES)
    envelope = {'command': command, 'document': json.loads(raw) if raw else None}
    # Match the server's extended deadline only for app lifecycle requests whose
    # root-side safety check inventories all resources before changing anything.
    lifecycle = len(parts) > 7 and app_namespace(parts[6]) and parts[7] in ('delete', 'patch')
    result = run(['sudo', '-n', str(WRAPPER)], input=json.dumps(envelope), timeout=270 if lifecycle else 60)
    if result:
        print(result)


class Relay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 16
    def verify_request(self, request, client_address):
        return client_address[0] == self.allowed_source


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        # Fixed selected VM: no HTTP CONNECT/SOCKS destination or TLS termination.
        try:
            with socket.create_connection(self.server.destination, timeout=10) as upstream, selectors.DefaultSelector() as selected:
                for source, destination in ((self.request, upstream), (upstream, self.request)):
                    source.settimeout(20)
                    selected.register(source, selectors.EVENT_READ, destination)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    events = selected.select(timeout=10)
                    if not events:
                        continue
                    for key, _ in events:
                        chunk = key.fileobj.recv(65536)
                        if not chunk:
                            return
                        key.data.sendall(chunk)
        except (OSError, TimeoutError):
            return


def proxy():
    require(os.geteuid() == 0)
    config, runtime = read(CONFIG / 'client.json'), read(CONFIG / 'runtime.json')
    validate_runtime(config, runtime)
    allowed = config['tunnel']['allowed_ips']
    if isinstance(allowed, list):
        require(len(allowed) == 1); allowed = allowed[0]
    source = ipaddress.ip_network(allowed)
    require(source.prefixlen == 32)
    with Relay((str(ipaddress.ip_interface(config['tunnel']['address']).ip), 16443), RelayHandler) as server:
        server.allowed_source = str(source.network_address)
        server.destination = (runtime['private_ipv4'], 6443)
        server.serve_forever()


if __name__ == '__main__':
    try:
        require(len(sys.argv) == 2 and sys.argv[1] in ('forward', 'serve', 'proxy'))
        {'forward': forward, 'serve': serve, 'proxy': proxy}[sys.argv[1]]()
    except Exception:
        print('RailShot runtime access failed; no completion is claimed.', file=sys.stderr)
        raise SystemExit(1)
