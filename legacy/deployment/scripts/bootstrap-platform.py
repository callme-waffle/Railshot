#!/usr/bin/env python3
"""Bootstrap the registered AWS platform with fixed native commands; no cloud calls on import.

One operator-owned input binds the existing state, instances, deadline and private
files. This is deliberately not a provider dispatcher or a user-programmable runner.
"""
import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen
import zipfile

ACCOUNT = '721622471953'
REGION = 'ap-northeast-2'
REPOSITORY = 'Jasmin-Softbank/Railshot'
CONTROL = 'i-033ae2db907fde68e'
MODULES = ('control', 'ci', 'platform-verification')
SHA = r'[a-f0-9]{64}'
REF = r'[a-f0-9]{40}'
LABELS = {'app.kubernetes.io/managed-by': 'railshot-bootstrap'}


class Blocked(Exception):
    """Only fixed codes cross the operator output boundary."""


def require(condition, code):
    if not condition:
        raise Blocked(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def private(path, *, raw=False):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and path.resolve() == path and stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'PRIVATE_FILE_REQUIRED')
    data = path.read_bytes()
    require(len(data) <= 32 * 1024 * 1024, 'PRIVATE_INPUT_TOO_LARGE')
    return data if raw else json.loads(data)


def write(path, value):
    """A small fsynced record, never a successful receipt before its readback."""
    path = Path(path)
    require(not path.is_symlink(), 'SYMLINK_STATE_REFUSED')
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        os.chmod(stream.name, 0o600)
        stream.write(value if isinstance(value, bytes) else encoded(value))
        stream.flush(); os.fsync(stream.fileno())
    os.replace(stream.name, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def native(command, *, body=None, cwd=None, env=None, timeout=120):
    """Bound the whole process group; never print credential-bearing native errors."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, _ = process.communicate(body, timeout=timeout)
        require(process.returncode == 0, 'NATIVE_COMMAND_FAILED')
        return out
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config(path):
    value = private(path)
    require(set(value) == {'version', 'source', 'state_dir', 'aws', 'terraform', 'control', 'build',
                           'github', 'secrets', 'registration', 'adopted_uids', 'import_package'}, 'BOOTSTRAP_CONFIG_V1_REQUIRED')
    require(value['version'] == 1 and value['aws'] == {'account_id': ACCOUNT, 'region': REGION}, 'REGISTERED_AWS_ONLY')
    require(set(value['source']) == {'ref', 'archive_sha256'} and re.fullmatch(REF, value['source']['ref'])
            and re.fullmatch(SHA, value['source']['archive_sha256']), 'PINNED_SOURCE_REQUIRED')
    require(set(value['terraform']) == set(MODULES), 'FIXED_TERRAFORM_MODULES_REQUIRED')
    for name, item in value['terraform'].items():
        require(set(item) == {'state', 'variables', 'lineage'}, 'EXACT_TERRAFORM_BINDING_REQUIRED')
        require(Path(item['state']).is_absolute() and Path(item['variables']).is_absolute(), 'ABSOLUTE_STATE_REQUIRED')
        require((name == 'platform-verification' and item['lineage'] is None)
                or (isinstance(item['lineage'], str) and bool(item['lineage'])), 'STATE_LINEAGE_REQUIRED')
        private(item['variables'])
    for role in ('control', 'build'):
        node = value[role]
        require(set(node) == {'instance_id', 'private_ip', 'root_volume_id', 'node_name', 'node_uid', 'stop_at', 'ssh'},
                'NODE_BINDING_REQUIRED')
        require(re.fullmatch(r'i-[a-f0-9]{17}', node['instance_id']) and
                re.fullmatch(r'vol-[a-f0-9]{17}', node['root_volume_id']), 'NODE_IDENTITY_REQUIRED')
        require(re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,62}', node['node_name']), 'NODE_NAME_REQUIRED')
        require(node['node_uid'] is None or re.fullmatch(r'[a-f0-9-]{36}', node['node_uid']), 'NODE_UID_REQUIRED')
        require(set(node['ssh']) == {'identity_file', 'known_hosts_file'} and
                all(Path(p).is_absolute() for p in node['ssh'].values()), 'SSH_REFERENCES_REQUIRED')
        private(node['ssh']['identity_file'], raw=True)
        host_keys = Path(node['ssh']['known_hosts_file'])
        require(host_keys.is_file() and not host_keys.is_symlink() and not host_keys.stat().st_mode & 0o022, 'PINNED_HOST_KEYS_REQUIRED')
    require(value['control']['instance_id'] == CONTROL and value['control']['node_uid'], 'VERIFIER_CONTROL_BINDING_REQUIRED')
    deadline = value['build']['stop_at']
    require(isinstance(deadline, str) and deadline.endswith('Z') and
            datetime.fromisoformat(deadline.replace('Z', '+00:00')) > datetime.now(timezone.utc), 'BUILD_DEADLINE_EXPIRED')
    require(set(value['github']) == {'ref', 'target_id', 'node_port', 'application'} and
            value['github']['ref'] in ('main', 'integration/team-assembly-20261002') and
            re.fullmatch(r'[a-z][a-z0-9-]{0,62}', value['github']['target_id']) and
            (value['github']['application'] is None or
             isinstance(value['github']['application'], str) and
             re.fullmatch(r'[a-z][a-z0-9-]{0,62}', value['github']['application'])) and
            type(value['github']['node_port']) is int and 30000 <= value['github']['node_port'] <= 32767,
            'REGISTERED_PLATFORM_REQUIRED')
    require(set(value['secrets']) == {'api_token', 'github_token', 'pull_config', 'executors', 'controller_github_token'},
            'FIXED_SECRET_REFERENCES_REQUIRED')
    for p in value['secrets'].values():
        private(p, raw=True)
    require(set(value['registration']) == {'documents', 'credentials_policy'}, 'REGISTRATION_INPUT_REQUIRED')
    private(value['registration']['documents']); private(value['registration']['credentials_policy'])
    require(isinstance(value['adopted_uids'], dict), 'ADOPTION_UIDS_REQUIRED')
    return value


def plan_changes(plan):
    """Never authorize destroy/replacement or bootstrap-byte/lifetime drift."""
    result = []
    for item in plan.get('resource_changes', []):
        if item.get('mode') == 'data':
            continue
        change = item['change']; actions = change['actions']
        require('delete' not in actions and actions in (['no-op'], ['create'], ['update']), 'DESTRUCTIVE_PLAN_REFUSED')
        if item['type'] == 'aws_instance' and actions != ['no-op']:
            require(actions == ['update'], 'EXISTING_INSTANCE_REQUIRED')
            before, after = change['before'], change['after']
            for field in ('ami', 'subnet_id', 'associate_public_ip_address', 'root_block_device',
                          'user_data', 'user_data_base64', 'instance_type', 'iam_instance_profile'):
                require(before.get(field) == after.get(field), 'INSTANCE_IDENTITY_OR_BOOTSTRAP_DRIFT')
        if actions != ['no-op']:
            result.append({'address': item['address'], 'actions': actions})
    return result


def matches_known_plan(actual, planned, unknown):
    if unknown is True:
        return True
    if isinstance(planned, dict):
        return isinstance(actual, dict) and all(matches_known_plan(actual.get(k), v, (unknown or {}).get(k)) for k, v in planned.items())
    if isinstance(planned, list):
        return isinstance(actual, list) and len(actual) == len(planned) and all(
            matches_known_plan(a, p, unknown[i] if isinstance(unknown, list) else None) for i, (a, p) in enumerate(zip(actual, planned)))
    return actual == planned


def object_key(document):
    meta = document['metadata']
    return '/'.join((document['kind'], meta.get('namespace', ''), meta['name']))


def contains(actual, wanted):
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(key in actual and contains(actual[key], val) for key, val in wanted.items())
    if isinstance(wanted, list):
        return isinstance(actual, list) and len(actual) == len(wanted) and all(contains(a, b) for a, b in zip(actual, wanted))
    return actual == wanted


def observer_product_file(files):
    """Bind the API to the registrar output in the verified private import."""
    entries = {item['path']: item for item in files}
    def document(path):
        prefix = '/var/lib/railshot/'
        require(isinstance(path, str) and path.startswith(prefix), 'OBSERVER_CONFIG_NOT_IMPORTED')
        item = entries.get(path[len(prefix):])
        require(item is not None and item['kind'] == 'source', 'OBSERVER_CONFIG_NOT_IMPORTED')
        return json.loads(base64.b64decode(item['data']))
    products = set()
    for profile in document('/var/lib/railshot/config/app-db/profiles.json')['profiles']:
        if not profile.get('deployment_file'):
            continue
        registration = document(profile['deployment_file'])['registration']
        if not registration.get('observability_config_file'):
            continue
        state = document(registration['observability_config_file'])['state_dir']
        require(isinstance(state, str) and state.startswith('/var/lib/railshot/state/') and
                PurePosixPath(state).as_posix() == state and '..' not in PurePosixPath(state).parts,
                'OBSERVER_STATE_PATH_INVALID')
        products.add(state + '/product.json')
    require(len(products) <= 1, 'MULTIPLE_OBSERVER_PRODUCT_FILES')
    return next(iter(products), None)


def kube(*args, document=None):
    return json.loads(native(['/usr/local/bin/k3s', 'kubectl', '--request-timeout=20s', *args],
                             body=encoded(document) if document is not None else None, timeout=35) or b'null')


def kube_get(kind, name, namespace=None):
    return kube('get', kind, name, *(['-n', namespace] if namespace else []), '--ignore-not-found', '-o', 'json')


def renewal_preserved(actual, expected):
    """Accept only the shared environment scope extension, without replacing it."""
    if actual == expected:
        return True
    fields = {'secret', 'target_id', 'server', 'project', 'namespaces', 'service_account', 'ca_sha256', 'audiences'}
    if not (isinstance(actual, dict) and isinstance(expected, dict)
            and fields <= set(expected) <= fields | {'tls_server_name'}
            and isinstance(expected['target_id'], str) and not expected['target_id'].startswith('app-')
            and expected['secret'] == 'railshot-' + expected['target_id'] and actual.get('project') == ''
            and {k: v for k, v in actual.items() if k not in ('project', 'namespaces')} ==
                {k: v for k, v in expected.items() if k not in ('project', 'namespaces')}):
        return False
    old, new = expected['namespaces'], actual.get('namespaces')
    return (isinstance(old, list) and isinstance(new, list) and bool(old)
            and all(isinstance(namespace, str) for namespace in old + new)
            and len(set(new)) == len(new) and set(old) <= set(new)
            and all(re.fullmatch(r'app-[a-f0-9]{24}', namespace) for namespace in set(new) - set(old)))


def ensure_object(document, uids, preserve_existing=False, claim=None):
    key = object_key(document); meta = document['metadata']; ns = meta.get('namespace')
    registrations = (preserve_existing and key == 'Role/argocd/railshot-product-registrations')
    if registrations:
        require(document.get('apiVersion') == 'rbac.authorization.k8s.io/v1' and document.get('rules') == [],
                'REGISTRATION_INITIAL_ROLE_MUST_BE_EMPTY')
    old = kube_get(document['kind'], meta['name'], ns)
    if old:
        recovered = (key not in uids and claim and old['metadata'].get('annotations', {}).get('railshot.io/bootstrap') == claim
                     and not old['metadata'].get('ownerReferences'))
        require(uids.get(key) == old['metadata']['uid'] or recovered, 'EXISTING_OBJECT_UID_NOT_ADOPTED')
        require(not old['metadata'].get('deletionTimestamp'), 'OBJECT_DELETING')
    else:
        require(key not in uids, 'ADOPTED_OBJECT_DISAPPEARED')
    if old and registrations:
        # Exact contract from environment.grant_control_objects; never reset its appended names.
        rules = old.get('rules') or []; seen = set()
        require(isinstance(rules, list), 'REGISTRATION_ROLE_DIFFERS')
        for rule in rules:
            require(isinstance(rule, dict) and set(rule) == {'apiGroups', 'resources', 'verbs', 'resourceNames'} and
                    isinstance(rule['apiGroups'], list) and isinstance(rule['resources'], list) and
                    len(rule['apiGroups']) == len(rule['resources']) == 1 and rule['verbs'] in (['get', 'patch'], ['delete']) and
                    isinstance(rule['resourceNames'], list) and rule['resourceNames'] and all(
                        isinstance(value, str) and re.fullmatch(r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', value)
                        for value in rule['resourceNames']), 'REGISTRATION_ROLE_DIFFERS')
            kind = (rule['apiGroups'][0], rule['resources'][0])
            grant = (*kind, tuple(rule['verbs']))
            require(kind in {('argoproj.io', 'appprojects'), ('argoproj.io', 'applications'), ('', 'secrets')}
                    and grant not in seen, 'REGISTRATION_ROLE_DIFFERS')
            seen.add(grant)
            if rule['verbs'] == ['delete']:
                pattern = {'applications': r'app-[a-f0-9]{24}-[a-z0-9-]+', 'appprojects': r'app-[a-f0-9]{24}',
                           'secrets': r'railshot-app-[a-f0-9]{24}'}[kind[1]]
                require(all(re.fullmatch(pattern, value) for value in rule['resourceNames']), 'REGISTRATION_ROLE_DIFFERS')
        current = kube_get(document['kind'], meta['name'], ns)
        require(current and current['metadata']['uid'] == old['metadata']['uid'] and not current['metadata'].get('deletionTimestamp')
                and (current.get('rules') or []) == rules, 'REGISTRATION_ROLE_READBACK_DIFFERS')
        return current['metadata']['uid']
    # Existing secrets belong to their rotation owner. Bootstrap never rolls them back.
    if old and document['kind'] == 'Secret':
        require(old.get('type', 'Opaque') == document.get('type', 'Opaque') and
                set(old.get('data', {})) == set(document.get('data', {})), 'SECRET_SHAPE_CHANGED')
        if meta['name'] == 'railshot-executors':
            require(old['data'] == document['data'], 'EXECUTOR_BINDING_CHANGED')
        return old['metadata']['uid']
    if old and meta['name'] == 'railshot-credentials' and document['kind'] in ('Role', 'ConfigMap'):
        if document['kind'] == 'Role':
            require(len(old['rules']) == len(document['rules']) == 1, 'RENEWAL_ROLE_DIFFERS')
            actual, expected = old['rules'][0], document['rules'][0]
            require(all(actual.get(k) == v for k, v in expected.items() if k != 'resourceNames') and
                    set(expected['resourceNames']) <= set(actual['resourceNames']), 'RENEWAL_ROLE_DIFFERS')
        else:
            actual = json.loads(old['data']['policy.json']); expected = json.loads(document['data']['policy.json'])
            require(actual['version'] == expected['version'] == 1 and all(
                        any(renewal_preserved(row, target) for row in actual['targets']) for target in expected['targets'])
                    and old['data']['kubeconfig'] == document['data']['kubeconfig'], 'RENEWAL_POLICY_DIFFERS')
        return old['metadata']['uid']  # Do not remove later registrations.
    if old and preserve_existing:
        if key == 'ConfigMap/railshot-system/railshot-environments':
            # Add the verified observer path once; never retarget existing bindings or drop other keys.
            wanted = document['data']; actual = old.get('data', {})
            require(set(wanted) <= {'profiles_file', 'observer_file'} and
                    all(k not in actual or actual[k] == v for k, v in wanted.items()), 'ENVIRONMENT_BINDING_CHANGED')
            if not set(wanted) <= set(actual):
                data = {**actual, **wanted}
                kube('patch', 'configmap', meta['name'], '-n', ns, '--type=merge', '-p',
                     json.dumps({'metadata': {'resourceVersion': old['metadata']['resourceVersion']}, 'data': data}))
                current = kube_get(document['kind'], meta['name'], ns)
                require(current and current['metadata']['uid'] == old['metadata']['uid'] and not current['metadata'].get('deletionTimestamp') and
                        current.get('data') == data, 'ENVIRONMENT_BINDING_READBACK_DIFFERS')
                return current['metadata']['uid']
        require(all(contains(old.get(field), document[field]) for field in
                    ('spec', 'data', 'rules', 'subjects', 'roleRef', 'automountServiceAccountToken') if field in document), 'REGISTERED_OBJECT_DRIFT')
        return old['metadata']['uid']
    desired = json.loads(json.dumps(document))
    if claim:
        desired['metadata'].setdefault('annotations', {})['railshot.io/bootstrap'] = claim
    for key, value in LABELS.items():
        desired['metadata'].setdefault('labels', {}).setdefault(key, value)
    command = ['create'] if registrations else ['apply', '--server-side', '--field-manager=railshot-bootstrap']
    kube(*command, '-f', '-', '-o', 'json', document=desired)
    current = kube_get(document['kind'], meta['name'], ns)
    if old:
        require(current['metadata']['uid'] == old['metadata']['uid'], 'OBJECT_REPLACED')
    for field in ('spec', 'data', 'rules', 'subjects', 'roleRef', 'automountServiceAccountToken'):
        if field in document:
            actual = (current.get(field) or []) if registrations and field == 'rules' else current.get(field)
            require(contains(actual, document[field]), 'OBJECT_READBACK_DIFFERS')
    return current['metadata']['uid']


def guest_identity(node):
    require(os.geteuid() == 0, 'GUEST_ROOT_REQUIRED')
    token = urlopen(Request('http://169.254.169.254/latest/api/token', method='PUT',
                           headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'}), timeout=5).read().decode()
    actual = urlopen(Request('http://169.254.169.254/latest/meta-data/instance-id',
                            headers={'X-aws-ec2-metadata-token': token}), timeout=5).read().decode()
    require(actual == node['instance_id'], 'GUEST_IDENTITY_DIFFERS')
    if node['stop_at']:
        deadline = datetime.fromisoformat(node['stop_at'].replace('Z', '+00:00'))
        require(deadline > datetime.now(timezone.utc), 'GUEST_DEADLINE_EXPIRED')
        timer = Path('/etc/systemd/system/railshot-ci-stop.timer').read_text()
        require('OnCalendar=' + deadline.strftime('%Y-%m-%d %H:%M:%S UTC') + '\n' in timer, 'GUEST_DEADLINE_DIFFERS')


def guest_source(source):
    root = Path('/var/lib/railshot-bootstrap') / source['ref']
    marker = root / '.bootstrap-source.json'
    if root.exists():
        require(root.is_dir() and not root.is_symlink() and marker.is_file(), 'UNOWNED_SOURCE_DIRECTORY')
        record = json.loads(marker.read_text())
        require(record['archive_sha256'] == source['archive_sha256'], 'SOURCE_ARCHIVE_CHANGED')
        require(all(digest((root / name).read_bytes()) == value for name, value in record['files'].items()), 'INSTALLED_SOURCE_CHANGED')
        return root
    url = f'https://codeload.github.com/{REPOSITORY}/tar.gz/' + source['ref']
    with urlopen(url, timeout=120) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    require(digest(data) == source['archive_sha256'] and len(data) <= 64 * 1024 * 1024, 'SOURCE_ARCHIVE_HASH_DIFFERS')
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root.parent) as tmp:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                require(not path.is_absolute() and '..' not in path.parts and not member.issym()
                        and not member.islnk() and (member.isfile() or member.isdir()), 'UNSAFE_SOURCE_ARCHIVE')
            archive.extractall(tmp, filter='data')
        entries = list(Path(tmp).iterdir()); require(len(entries) == 1 and entries[0].is_dir(), 'SOURCE_ARCHIVE_ROOT_DIFFERS')
        files = {str(p.relative_to(entries[0])): digest(p.read_bytes()) for p in entries[0].rglob('*') if p.is_file()}
        write(entries[0] / '.bootstrap-source.json', {'archive_sha256': source['archive_sha256'], 'files': files})
        entries[0].rename(root)
    return root


def node_ready(node):
    actual = kube_get('node', node['node_name'])
    require(actual is not None and (not node['node_uid'] or actual['metadata']['uid'] == node['node_uid']), 'NODE_UID_DIFFERS')
    require(actual['status']['nodeInfo']['kubeletVersion'] == 'v1.34.11+k3s1' and
            any(c['type'] == 'Ready' and c['status'] == 'True' for c in actual['status']['conditions']), 'NODE_NOT_READY')
    return actual


def argo_reconciliation(workloads):
    """Bound Git discovery latency; restart only the two consumers of these settings."""
    settings = {'timeout.reconciliation': '30s', 'timeout.reconciliation.jitter': '5s'}
    consumers = [w for w in workloads if w['metadata']['name'] in
                 {'argocd-repo-server', 'argocd-application-controller'}]
    require(len(consumers) == 2, 'ARGO_RECONCILIATION_CONSUMERS_MISSING')
    config = kube_get('configmap', 'argocd-cm', 'argocd')
    require(config is not None, 'ARGO_CONFIGURATION_MISSING')
    if any(config.get('data', {}).get(key) != value for key, value in settings.items()):
        kube('patch', 'configmap', 'argocd-cm', '-n', 'argocd', '--type=merge', '--patch-file=/dev/stdin', '-o', 'json',
             document={'data': settings})
    current = kube_get('configmap', 'argocd-cm', 'argocd')
    require(current['metadata']['uid'] == config['metadata']['uid'] and
            all(current.get('data', {}).get(key) == value for key, value in settings.items()), 'ARGO_RECONCILIATION_DIFFERS')
    binding = current['metadata']['uid'] + ':' + current['metadata']['resourceVersion']
    annotation = 'railshot.io/argocd-reconciliation-config'
    for workload in consumers:
        name = workload['metadata']['name']
        current = kube_get(workload['kind'], name, 'argocd')
        if current['spec']['template'].get('metadata', {}).get('annotations', {}).get(annotation) != binding:
            kube('patch', workload['kind'], name, '-n', 'argocd', '--type=merge', '--patch-file=/dev/stdin', '-o', 'json',
                 document={'spec': {'template': {'metadata': {'annotations': {annotation: binding}}}}})
        native(['k3s', 'kubectl', '-n', 'argocd', 'rollout', 'status', workload['kind'] + '/' + name, '--timeout=180s'], timeout=190)
        current = kube_get(workload['kind'], name, 'argocd')
        require(current['metadata']['uid'] == workload['metadata']['uid'] and
                current['spec']['template']['spec']['containers'] == workload['spec']['template']['spec']['containers'] and
                current['spec']['template']['metadata']['annotations'].get(annotation) == binding, 'ARGO_IDENTITY_CHANGED')


def argo_health_persistence(workloads):
    controllers = [w for w in workloads if w['metadata']['name'] == 'argocd-application-controller']
    require(len(controllers) == 1, 'ARGO_CONTROLLER_MISSING')
    controller = controllers[0]
    config = kube_get('configmap', 'argocd-cmd-params-cm', 'argocd')
    require(config is not None, 'ARGO_PARAMETERS_MISSING')
    changed = config.get('data', {}).get('controller.resource.health.persist') != 'true'
    if changed:
        kube('patch', 'configmap', 'argocd-cmd-params-cm', '-n', 'argocd', '--type=merge', '--patch-file=/dev/stdin', '-o', 'json',
             document={'data': {'controller.resource.health.persist': 'true'}})
    params = kube_get('configmap', 'argocd-cmd-params-cm', 'argocd')
    require(params['metadata']['uid'] == config['metadata']['uid'] and
            params.get('data', {}).get('controller.resource.health.persist') == 'true', 'ARGO_HEALTH_CONFIGURATION_DIFFERS')
    binding = params['metadata']['uid'] + ':' + params['metadata']['resourceVersion']
    annotation = 'railshot.io/argocd-health-config'
    current = kube_get(controller['kind'], 'argocd-application-controller', 'argocd')
    if current['spec']['template'].get('metadata', {}).get('annotations', {}).get(annotation) != binding:
        kube('patch', controller['kind'], 'argocd-application-controller', '-n', 'argocd', '--type=merge', '--patch-file=/dev/stdin', '-o', 'json',
             document={'spec': {'template': {'metadata': {'annotations': {annotation: binding}}}}})
    native(['k3s', 'kubectl', '-n', 'argocd', 'rollout', 'status', controller['kind'] + '/argocd-application-controller', '--timeout=180s'], timeout=190)
    current = kube_get(controller['kind'], 'argocd-application-controller', 'argocd')
    params = kube_get('configmap', 'argocd-cmd-params-cm', 'argocd')
    require(params['metadata']['uid'] == config['metadata']['uid'] and
            params.get('data', {}).get('controller.resource.health.persist') == 'true', 'ARGO_HEALTH_CONFIGURATION_DIFFERS')
    require(current['metadata']['uid'] == controller['metadata']['uid'] and
            current['spec']['template']['spec']['containers'] == controller['spec']['template']['spec']['containers'] and
            current['spec']['template']['metadata']['annotations'].get(annotation) == binding, 'ARGO_IDENTITY_CHANGED')


METADATA_PROBE = '''import http.client,json
c=http.client.HTTPConnection('169.254.169.254',80,timeout=3)
try:
 c.request('PUT','/latest/api/token',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'})
 c.getresponse().read(); responded=True
except (OSError,TimeoutError): responded=False
finally: c.close()
print(json.dumps({'responded':responded}))
'''

API_IDENTITY = '''import json,os,pathlib,subprocess,sys,urllib.request
try:
 expected=json.load(sys.stdin)
 forbidden=('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_PROFILE','AWS_DEFAULT_PROFILE',
  'AWS_ROLE_ARN','AWS_WEB_IDENTITY_TOKEN_FILE','AWS_CONTAINER_CREDENTIALS_RELATIVE_URI','AWS_CONTAINER_CREDENTIALS_FULL_URI',
  'AWS_EC2_METADATA_SERVICE_ENDPOINT')
 assert not any(os.environ.get(k) for k in forbidden)
 for key,default in [('AWS_SHARED_CREDENTIALS_FILE','~/.aws/credentials'),('AWS_CONFIG_FILE','~/.aws/config')]:
  assert not pathlib.Path(os.environ.get(key,default)).expanduser().exists()
 base='http://169.254.169.254/latest/'
 request=urllib.request.Request(base+'api/token',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'})
 token=urllib.request.urlopen(request,timeout=5).read().decode()
 def metadata(path):
  return urllib.request.urlopen(urllib.request.Request(base+'meta-data/'+path,headers={'X-aws-ec2-metadata-token':token}),timeout=5).read().decode().strip()
 instance=metadata('instance-id'); role=metadata('iam/security-credentials/')
 assert instance==expected['instance_id'] and role=='railshot-control-poc'
 result=subprocess.run(['aws','sts','get-caller-identity','--region','ap-northeast-2','--output','json'],capture_output=True,timeout=30)
 assert result.returncode==0
 identity=json.loads(result.stdout)
 assert identity['Account']=='721622471953' and identity['Arn']=='arn:aws:sts::721622471953:assumed-role/railshot-control-poc/'+instance
 print(json.dumps({'verified':True,'instance_id':instance,'account_id':identity['Account'],'role_arn':identity['Arn']}))
except Exception:
 print(json.dumps({'verified':False})); sys.exit(1)
'''


def metadata_drop(event, endpoint, pod):
    summary = event.get('summary') or {}
    return (event.get('type') == 'drop' and event.get('reason') in ('Policy denied', 'Policy denied by denylist') and
            event.get('source') == endpoint['id'] and event.get('srcLabel') == endpoint['status']['identity']['id'] and
            summary.get('l3', {}).get('src') == pod['status']['podIP'] and summary.get('l3', {}).get('dst') == '169.254.169.254' and
            str(summary.get('l4', {}).get('dst')) == '80')


def denied_metadata(cilium, endpoint, pod):
    sandboxes = json.loads(native(['k3s', 'crictl', 'pods', '-o', 'json']))['items']
    matches = [s for s in sandboxes if s.get('state') == 'SANDBOX_READY' and s.get('metadata', {}).get('uid') == pod['metadata']['uid']]
    require(len(matches) == 1, 'METADATA_SANDBOX_NOT_READY')
    sandbox = json.loads(native(['k3s', 'crictl', 'inspectp', '-o', 'json', matches[0]['id']]))
    pid = sandbox.get('info', {}).get('pid')
    require(type(pid) is int and pid > 1 and sandbox['status']['metadata']['uid'] == pod['metadata']['uid'] and
            sandbox['status'].get('state') == 'SANDBOX_READY' and sandbox['status']['network']['ip'] == pod['status']['podIP'], 'METADATA_SANDBOX_DIFFERS')
    command = ['k3s', 'kubectl', 'exec', '-n', 'kube-system', cilium['metadata']['name'], '-c', 'cilium-agent', '--',
               'timeout', '15', 'cilium-dbg', 'monitor', '--type', 'drop', '--json', '--from', str(endpoint['id'])]
    with tempfile.TemporaryFile() as events, tempfile.TemporaryFile() as errors:
        monitor = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=events, stderr=errors, start_new_session=True)
        try:
            time.sleep(0.4)
            require(monitor.poll() is None, 'METADATA_MONITOR_NOT_RUNNING')
            for _ in range(2):
                result = json.loads(native(['nsenter', '--net=/proc/' + str(pid) + '/ns/net', '/usr/bin/python3', '-'], body=METADATA_PROBE.encode(), timeout=6))
                require(result.get('responded') is False, 'METADATA_DENY_PROBE_REACHED_IMDS')
            time.sleep(0.2); events.seek(0)
            verified = []
            for line in events.read().splitlines():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if metadata_drop(event, endpoint, pod):
                    verified.append(event['reason'])
            require(verified, 'METADATA_POLICY_DROP_NOT_OBSERVED')
        finally:
            try:
                os.killpg(monitor.pid, signal.SIGTERM); monitor.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            finally:
                try:
                    os.killpg(monitor.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                monitor.wait()
    current = kube_get('pod', pod['metadata']['name'], pod['metadata']['namespace'])
    require(current and current['metadata']['uid'] == pod['metadata']['uid'] and current['status']['podIP'] == pod['status']['podIP'], 'METADATA_PROBE_POD_CHANGED')
    return {'pod_uid': pod['metadata']['uid'], 'namespace': pod['metadata']['namespace'], 'endpoint_id': endpoint['id'],
            'identity_id': endpoint['status']['identity']['id'], 'pod_ip': pod['status']['podIP'], 'reason': verified[0]}


def metadata_document(source):
    document = json.loads(encoded(source))
    specs = document.get('specs', [])
    # In pinned 1.20.2 each egressDeny entry becomes one internal PolicyEntry.
    require(len(specs) == 5 and all(len(s.get('egressDeny', [])) == 1 and not any(
        s.get(key) for key in ('ingress', 'ingressDeny', 'egress', 'labels')) for s in specs), 'METADATA_RULE_SHAPE_DIFFERS')
    binding = digest(encoded(specs))
    for index, rule in enumerate(specs):
        rule['labels'] = [{'key': 'railshot.io/metadata-spec-sha256', 'value': binding, 'source': 'unspec'},
                          {'key': 'railshot.io/metadata-rule', 'value': str(index), 'source': 'unspec'}]
    return document, binding


def metadata_policy(node, data, apply=False, identity=False):
    document, binding = metadata_document(data['document']); policy_name = 'railshot-platform-metadata'
    require(document.get('kind') == 'CiliumClusterwideNetworkPolicy' and document['metadata'] == {'name': policy_name}, 'FIXED_METADATA_POLICY_REQUIRED')
    node_ready(node)
    pods = kube('get', 'pods', '-A', '-o', 'json')['items']
    cilium = [p for p in pods if p['metadata']['namespace'] == 'kube-system' and p['metadata'].get('labels', {}).get('k8s-app') == 'cilium'
              and p['spec'].get('nodeName') == node['node_name'] and not p['metadata'].get('deletionTimestamp')]
    require(len(cilium) == 1, 'CONTROL_CILIUM_POD_REQUIRED'); cilium = cilium[0]
    command = ['k3s', 'kubectl', 'exec', '-n', 'kube-system', cilium['metadata']['name'], '-c', 'cilium-agent', '--', 'cilium-dbg']
    require(re.search(r'\b1\.20\.2\b', native(command + ['version']).decode()), 'CILIUM_VERSION_DIFFERS')
    uid = data.get('uid')
    if apply:
        uid = ensure_object(document, {object_key(document): uid} if uid else {}, claim=data['claim'])
    policy = kube_get('ciliumclusterwidenetworkpolicy', policy_name)
    require(policy and policy['metadata']['uid'] == uid and policy.get('specs') == document['specs'], 'METADATA_POLICY_READBACK_DIFFERS')
    deadline = time.monotonic() + 60
    while True:
        repository = json.loads(native(command + ['policy', 'get', '-o', 'json']))
        policies = json.loads(repository['policy'])
        imported = []
        for rule in policies:
            # Actual 1.20.2 policy get emits internal PolicyEntry.Labels, not API Rule.labels.
            labels = {(v['source'], v['key']): v['value'] for v in rule.get('Labels', [])}
            if labels.get(('k8s', 'io.cilium.k8s.policy.uid')) == uid:
                imported.append(labels)
        if (len(imported) == len(document['specs']) and
                all(v.get(('unspec', 'railshot.io/metadata-spec-sha256')) == binding for v in imported) and
                {v.get(('unspec', 'railshot.io/metadata-rule')) for v in imported} == {str(i) for i in range(len(document['specs']))}): break
        require(time.monotonic() < deadline, 'CILIUM_METADATA_POLICY_NOT_IMPORTED'); time.sleep(1)
    revision = repository['revision']
    native(command + ['policy', 'wait', str(revision), '--max-wait-time', '60', '--fail-wait-time', '30'], timeout=70)
    endpoints = json.loads(native(command + ['endpoint', 'list', '-o', 'json']))
    groups = {'argocd': [], 'railshot-system': [], 'dns': [], 'local-path': []}
    for pod in pods:
        ns = pod['metadata']['namespace']; labels = pod['metadata'].get('labels', {})
        if pod['spec'].get('nodeName') != node['node_name'] or pod.get('status', {}).get('phase') != 'Running': continue
        group = (ns if ns == 'argocd' or (ns == 'railshot-system' and not (labels.get('app') == 'railshot-api' and pod['spec'].get('serviceAccountName') == 'railshot-product')) else
                 'dns' if ns == 'kube-system' and labels.get('k8s-app') == 'kube-dns' else
                 'local-path' if ns == 'kube-system' and labels.get('app') == 'local-path-provisioner' else None)
        if group is None: continue
        require(not pod['spec'].get('hostNetwork'), 'METADATA_HOST_NETWORK_BYPASS')
        matched = [e for e in endpoints if any(a.get('ipv4') == pod['status'].get('podIP') for a in e.get('status', {}).get('networking', {}).get('addressing', []))]
        require(len(matched) == 1 and matched[0]['status'].get('state') == 'ready' and
                matched[0]['status']['policy']['realized']['policy-revision'] >= revision, 'METADATA_ENDPOINT_NOT_REALIZED')
        groups[group].append((pod, matched[0]))
    require(all(groups.values()), 'METADATA_REPRESENTATIVE_PODS_MISSING')
    proof = {'policy_uid': uid, 'policy_revision': revision, 'spec_sha256': binding,
             'denied': [denied_metadata(cilium, group[0][1], group[0][0]) for group in groups.values()]}
    if identity:
        deployment = kube_get('deployment', 'railshot-api', 'railshot-system')
        require(deployment and deployment['metadata']['uid'] == data.get('api_uid'), 'METADATA_API_NOT_ADOPTED')
        replicasets = kube('get', 'replicasets', '-n', 'railshot-system', '-o', 'json')['items']
        owned = {r['metadata']['uid'] for r in replicasets if any(o.get('controller') and o.get('uid') == data['api_uid']
                 and o.get('kind') == 'Deployment' for o in r['metadata'].get('ownerReferences', []))}
        api = [p for p in pods if p['metadata']['namespace'] == 'railshot-system' and p['metadata'].get('labels', {}).get('app') == 'railshot-api'
               and p['spec'].get('serviceAccountName') == 'railshot-product' and p['spec'].get('nodeName') == node['node_name'] and
               not p['spec'].get('hostNetwork') and not p['metadata'].get('deletionTimestamp') and
               any(o.get('controller') and o.get('kind') == 'ReplicaSet' and o.get('uid') in owned for o in p['metadata'].get('ownerReferences', [])) and
               any(c['type'] == 'Ready' and c['status'] == 'True' for c in p.get('status', {}).get('conditions', []))]
        require(len(api) == 1, 'ONE_READY_API_POD_REQUIRED')
        result = json.loads(native(['k3s', 'kubectl', 'exec', '-i', '-n', 'railshot-system', api[0]['metadata']['name'], '-c', 'api', '--', 'python3', '-c', API_IDENTITY],
                                  body=encoded({'instance_id': node['instance_id']}), timeout=45))
        require(result.get('verified') is True, 'API_INSTANCE_IDENTITY_NOT_VERIFIED')
        current = kube_get('pod', api[0]['metadata']['name'], 'railshot-system')
        require(current and current['metadata']['uid'] == api[0]['metadata']['uid'], 'METADATA_API_POD_CHANGED')
        proof['api'] = {**result, 'pod_uid': current['metadata']['uid']}
    return proof


def guest(request):
    """Executed from this reviewed source over pinned SSM/SSH, all secrets on stdin."""
    os.umask(0o077)
    try:
        guest_identity(request['node'])
        action = request['action']; data = request.get('data', {})
        result = {}
        if action == 'control':
            root = guest_source(request['source'])
            if not Path('/usr/local/bin/k3s').exists():
                native(['bash', str(root / 'infrastructure/ansible/control.sh')], timeout=1200)
            else:
                require(Path('/etc/railshot/node-role').read_text().strip() == 'control', 'UNOWNED_CONTROL_NODE')
                script = (root / 'infrastructure/ansible/control.sh').read_text()
                expected_config = script.split("cat > \"$tmp/config.yaml\" <<'YAML'\n", 1)[1].split('\nYAML', 1)[0] + '\n'
                require(Path('/etc/rancher/k3s/config.yaml').read_text() == expected_config, 'CONTROL_CONFIGURATION_DIFFERS')
            actual = node_ready(request['node'])
            native(['k3s', 'kubectl', 'label', 'node', actual['metadata']['name'], 'railshot.io/node-role=platform', '--overwrite'])
            workloads = kube('get', 'deployment,statefulset', '-n', 'argocd', '-o', 'json')['items']
            require(len(workloads) == 7, 'ARGO_WORKLOAD_SET_DIFFERS')
            for old in workloads:
                spec = old['spec']['template']['spec']; selector = spec.get('nodeSelector', {})
                require(selector.get('railshot.io/node-role', 'platform') == 'platform', 'ARGO_PLACEMENT_CONFLICT')
                kube('patch', old['kind'], old['metadata']['name'], '-n', 'argocd', '--type=merge', '--patch-file=/dev/stdin', '-o', 'json',
                     document={'spec': {'template': {'spec': {'nodeSelector': {**selector, 'railshot.io/node-role': 'platform'}}}}})
                native(['k3s', 'kubectl', '-n', 'argocd', 'rollout', 'status', old['kind'] + '/' + old['metadata']['name'], '--timeout=180s'], timeout=190)
                current = kube_get(old['kind'], old['metadata']['name'], 'argocd')
                require(current['metadata']['uid'] == old['metadata']['uid'] and current['spec']['template']['spec']['containers'] == spec['containers'], 'ARGO_IDENTITY_CHANGED')
            kube('patch', 'configmap', 'argocd-cm', '-n', 'argocd', '--type=merge', '--patch-file=/dev/stdin', '-o', 'json',
                 document={'data': {'resource.respectRBAC': 'strict'}})
            argo_health_persistence(workloads)
            argo_reconciliation(workloads)
            result = {'node_uid': actual['metadata']['uid'], 'argo_workloads': len(workloads)}
        elif action == 'agent-token':
            node_ready(request['node'])
            result = {'token': native(['k3s', 'token', 'create', '--description', 'railshot bootstrap build agent', '--ttl', '1h']).decode().strip()}
        elif action == 'build-observe':
            path = Path('/etc/rancher/k3s/config.yaml')
            require(not Path('/var/lib/rancher/k3s/server').exists(), 'BUILD_IS_CONTROL_PLANE')
            result = {'installed': Path('/usr/local/bin/k3s').exists()}
            if result['installed']:
                import yaml
                conf = yaml.safe_load(path.read_text())
                require(not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o600 and
                        conf.get('server') == data['server'] and conf.get('node-name') == request['node']['node_name'] and
                        conf.get('lb-server-port') == 6443 and conf.get('docker', False) is False and
                        conf.get('node-label') == ['railshot.io/node-role=build'] and
                        conf.get('node-taint') == ['railshot.io/dedicated=build:NoSchedule'], 'BUILD_AGENT_BINDING_DIFFERS')
                native(['systemctl', 'is-active', '--quiet', 'k3s-agent'])
        elif action == 'build-join':
            require(not Path('/usr/local/bin/k3s').exists() and not Path('/etc/rancher/k3s').exists(), 'BUILD_JOIN_ALREADY_ATTEMPTED')
            require(data['token'].startswith('K10') and '\n' not in data['token'], 'INVALID_AGENT_TOKEN')
            directory = Path('/etc/rancher/k3s'); directory.mkdir(mode=0o700, parents=True)
            write(directory / 'agent-token', data['token'].encode())
            write(directory / 'config.yaml', ('server: ' + data['server'] + '\ntoken-file: /etc/rancher/k3s/agent-token\nnode-name: '
                  + request['node']['node_name'] + '\nlb-server-port: 6443\nnode-label:\n  - railshot.io/node-role=build\n'
                  'node-taint:\n  - railshot.io/dedicated=build:NoSchedule\n').encode())
            with urlopen('https://get.k3s.io', timeout=120) as response:
                installer = response.read(1024 * 1024)
            require(digest(installer) == 'e5cc3b3d9dfc1662c2d9be6da5abc9a4cd317d6abc3a5ffc02e3dd3248207fee', 'K3S_INSTALLER_HASH_DIFFERS')
            native(['sh', '-s'], body=installer, env={**os.environ, 'INSTALL_K3S_VERSION': 'v1.34.11+k3s1', 'INSTALL_K3S_EXEC': 'agent'}, timeout=360)
            result = {'joined': True}
        elif action == 'build-health':
            actual = node_ready(data['build'])
            require(actual['metadata']['labels'].get('railshot.io/node-role') == 'build' and
                    {'key': 'railshot.io/dedicated', 'value': 'build', 'effect': 'NoSchedule'} in actual['spec'].get('taints', []), 'BUILD_PLACEMENT_DIFFERS')
            for name in ('cilium', 'cilium-envoy'):
                native(['k3s', 'kubectl', '-n', 'kube-system', 'rollout', 'status', 'daemonset/' + name, '--timeout=180s'], timeout=190)
            result = {'node_uid': actual['metadata']['uid']}
        elif action == 'build-quiesce':
            cron = kube_get('cronjob', 'railshot-build-controller', 'railshot-system')
            if cron:
                require(cron['metadata']['uid'] == data['uid'], 'BUILD_CONTROLLER_NOT_ADOPTED')
                kube('patch', 'cronjob', 'railshot-build-controller', '-n', 'railshot-system', '--type=merge',
                     '--patch-file=/dev/stdin', '-o', 'json', document={'spec': {'suspend': True}})
                deadline = time.monotonic() + 75
                while kube_get('cronjob', 'railshot-build-controller', 'railshot-system').get('status', {}).get('active'):
                    require(time.monotonic() < deadline, 'CONTROLLER_STILL_ACTIVE'); time.sleep(3)
            if kube_get('namespace', 'railshot-build'):
                jobs = kube('get', 'jobs', '-n', 'railshot-build', '-o', 'json')['items']
                require(all(any(c['type'] in ('Complete', 'Failed') and c['status'] == 'True'
                                for c in job.get('status', {}).get('conditions', [])) for job in jobs), 'BUILD_JOBS_ACTIVE')
            result = {'quiescent': True}
        elif action == 'ci':
            root = guest_source(request['source'])
            native(['ansible-playbook', '-i', 'localhost,', '-c', 'local', str(root / 'infrastructure/ansible/ci.yml'),
                    '-e', 'railshot_ci_k3s_build_worker=true'], timeout=1200)
            native(['bash', str(root / 'infrastructure/ansible/test-ci-network.sh')], timeout=600)
            native(['bash', str(root / 'ci/scripts/runner/prepare-host.sh'), '--k3s-build-worker'])
            native(['/usr/local/sbin/railshot-ci-network', '--check'])
            result = {'network': 'verified', 'marker': Path('/etc/railshot/ci-runner-host').read_text().strip()}
        elif action == 'objects':
            result = {'uids': {}}
            uids = dict(data['uids'])
            for document in data['documents']:
                key = object_key(document)
                uid = ensure_object(document, uids, data.get('preserve_existing', False), data.get('claim'))
                uids[key] = uid; result['uids'][key] = uid
        elif action == 'get':
            # Exact metadata only; never return Secret bytes to public receipts.
            current = kube_get(data['kind'], data['name'], data.get('namespace'))
            result = {'exists': current is not None}
            if current:
                result.update(uid=current['metadata']['uid'], status=current.get('status', {}))
        elif action == 'cron-test':
            result = cron_test(data)
        elif action == 'import':
            result = import_state(data)
        elif action in ('metadata-before', 'metadata-after', 'metadata-final'):
            result = metadata_policy(request['node'], data, apply=action == 'metadata-before', identity=action == 'metadata-final')
        else:
            raise Blocked('UNKNOWN_BOOTSTRAP_ACTION')
        print(json.dumps({'status': 'ok', 'result': result}))
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'code': str(error) if isinstance(error, Blocked) else 'GUEST_STEP_FAILED'}))
        raise SystemExit(1)


def cron_test(data):
    name, ns = data['name'], data['namespace']
    cron = kube_get('cronjob', name, ns)
    require(cron['metadata']['uid'] == data['uid'] and cron['spec']['concurrencyPolicy'] == 'Forbid', 'CRON_BINDING_DIFFERS')
    job_name = name + '-bootstrap-' + data['run'][:12]
    job = kube_get('job', job_name, ns)
    if job is None:
        spec = json.loads(json.dumps(cron['spec']['jobTemplate']['spec']))
        spec.pop('ttlSecondsAfterFinished', None)
        job = kube('create', '-f', '-', '-o', 'json', document={'apiVersion': 'batch/v1', 'kind': 'Job',
                   'metadata': {'name': job_name, 'namespace': ns, 'labels': LABELS,
                                'annotations': {'railshot.io/bootstrap-cron-uid': data['uid']}}, 'spec': spec})
    require(job['metadata'].get('annotations', {}).get('railshot.io/bootstrap-cron-uid') == data['uid'], 'BOOTSTRAP_JOB_CONFLICT')
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        job = kube_get('job', job_name, ns)
        require(not job.get('status', {}).get('failed'), 'BOOTSTRAP_JOB_FAILED')
        if any(c['type'] == 'Complete' and c['status'] == 'True' for c in job.get('status', {}).get('conditions', [])):
            break
        time.sleep(3)
    else:
        raise Blocked('BOOTSTRAP_JOB_TIMEOUT')
    sa = cron['spec']['jobTemplate']['spec']['template']['spec']['serviceAccountName']
    permission_ns = 'railshot-build' if name == 'railshot-build-controller' else ns
    checks = ([('get', 'secrets/' + secret, True) for secret in data['secrets']]
              + [('patch', 'secrets/' + secret, True) for secret in data['secrets']]
              + [('get', 'secrets/railshot-bootstrap-forbidden', False), ('list', 'secrets', False)])
    for verb, resource, allowed in checks:
        command = ['k3s', 'kubectl', 'auth', 'can-i', verb, resource, '-n', permission_ns, '--as=system:serviceaccount:' + ns + ':' + sa]
        process = subprocess.run(command, capture_output=True, timeout=30)
        require(process.stdout.strip() == (b'yes' if allowed else b'no'), 'CRON_RBAC_DIFFERS')
    kube('patch', 'cronjob', name, '-n', ns, '--type=merge', '--patch-file=/dev/stdin', '-o', 'json', document={'spec': {'suspend': False}})
    require(kube_get('cronjob', name, ns)['spec'].get('suspend') is False, 'CRON_ENABLE_NOT_OBSERVED')
    return {'uid': data['uid'], 'job_uid': job['metadata']['uid'], 'job': job_name, 'verified': True}


# The importer is a fixed stdin program in a temporary Pod, not an uploaded command.
IMPORTER = '''import base64,hashlib,json,os,pathlib,sqlite3,sys
os.umask(0o077)
p=json.load(sys.stdin); root=pathlib.Path('/var/lib/railshot'); marker=root/'.bootstrap-import.json'
def check(ok):
 if not ok: raise RuntimeError('IMPORT_BINDING_DIFFERS')
def semantic(item,path):
 check(not any(part.is_symlink() for part in [path,*path.parents]))
 check(path.is_file() and path.stat().st_uid==os.geteuid() and not path.stat().st_mode & 0o077)
 raw=path.read_bytes()
 if item['kind']=='terraform': check(json.loads(raw)['lineage']==item['lineage'])
 if item['kind']=='api':
  v=json.loads(raw); check(v['version']==1 and all(k in v for k in ('operations','keys','bindings','plans')))
 if item['kind']=='budget':
  db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True); check(db.execute('PRAGMA integrity_check').fetchone()==('ok',))
  check(not db.execute('PRAGMA foreign_key_check').fetchall())
  check({'snapshots','reservations'} <= {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
  scopes={r[0]:{'provider':r[1],'source_scope':r[2]} for r in db.execute('SELECT scope,provider,source_scope FROM snapshots')}
  check(item.get('scopes') and all(scopes.get(k)==v for k,v in item['scopes'].items())); db.close()
 if item['kind']=='source': check(hashlib.sha256(raw).hexdigest()==item['sha256'])
 if item['kind'] in ('cd','edge','registration'):
  v=json.loads(raw); check(isinstance(v,dict) and item.get('binding') and all(v.get(k)==val for k,val in item['binding'].items()))
if marker.exists():
 old=json.loads(marker.read_text()); check(old['package_id']==p['package_id'] and old['manifest_sha256']==p['manifest_sha256'] and old['owner']==p['destination_owner'])
 for item in p['files']: semantic(item,root/item['path'])
 print(json.dumps({'imported':False,'verified':True,'package_id':p['package_id']})); sys.exit(0)
check(not p.get('verify_only'))
for item in p['files']:
 path=root/item['path']; raw=base64.b64decode(item['data']); check(hashlib.sha256(raw).hexdigest()==item['sha256'])
 check(not any(part.is_symlink() for part in [path,*path.parents]))
 path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 if path.exists(): check(path.read_bytes()==raw)
 else:
  with path.open('xb') as f: os.chmod(path,0o600); f.write(raw); f.flush(); os.fsync(f.fileno())
 semantic(item,path)
 check(hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256'])
 fd=os.open(path.parent,os.O_RDONLY); os.fsync(fd); os.close(fd)
with marker.open('x') as f:
 os.chmod(marker,0o600); json.dump({'package_id':p['package_id'],'manifest_sha256':p['manifest_sha256'],'owner':p['destination_owner']},f); f.flush(); os.fsync(f.fileno())
fd=os.open(root,os.O_RDONLY); os.fsync(fd); os.close(fd)
print(json.dumps({'imported':True,'verified':True,'package_id':p['package_id']}))
'''


def frozen_destination(data, import_pod=None):
    """Read current controllers and all PVC users, including terminating Pods."""
    freeze = data['destination_freeze']
    app = kube_get('application', 'railshot-platform', 'argocd')
    require((app or {}).get('metadata', {}).get('uid') == freeze['application_uid'], 'IMPORT_APPLICATION_IDENTITY_DIFFERS')
    if app:
        automated = app.get('spec', {}).get('syncPolicy', {}).get('automated')
        require((automated is None or automated.get('enabled') is False) and not app.get('operation') and
                app.get('status', {}).get('operationState', {}).get('phase') not in ('Running', 'Terminating'),
                'IMPORT_ARGO_NOT_FROZEN')
    deployment = kube_get('deployment', 'railshot-api', 'railshot-system')
    require((deployment or {}).get('metadata', {}).get('uid') == freeze['deployment_uid'], 'IMPORT_DEPLOYMENT_IDENTITY_DIFFERS')
    require(deployment is None or deployment['spec'].get('replicas') == 0, 'IMPORT_REQUIRES_FROZEN_API')
    selector = (deployment or {}).get('spec', {}).get('selector', {}).get('matchLabels', {})
    pods = kube('get', 'pods', '-n', 'railshot-system', '-o', 'json')['items']
    for pod in pods:
        labels = pod['metadata'].get('labels', {})
        uses_pvc = any(v.get('persistentVolumeClaim', {}).get('claimName') == 'railshot-api' for v in pod['spec'].get('volumes', []))
        selected = bool(selector) and all(labels.get(k) == v for k, v in selector.items())
        if import_pod and pod['metadata']['uid'] == import_pod['metadata']['uid']:
            continue
        require(not uses_pvc and not selected, 'IMPORT_DESTINATION_POD_STILL_PRESENT')
    autoscalers = kube('get', 'horizontalpodautoscalers', '-n', 'railshot-system', '-o', 'json')['items']
    require(not any(h['spec']['scaleTargetRef'].get('kind') == 'Deployment' and
                    h['spec']['scaleTargetRef'].get('name') == 'railshot-api' for h in autoscalers), 'IMPORT_API_AUTOSCALER_PRESENT')


def import_state(data):
    # An existing live product writer must be stopped by its owner before export.
    deployments = kube_get('deployment', 'railshot-api', 'railshot-system')
    pvc = kube_get('persistentvolumeclaim', 'railshot-api', 'railshot-system')
    require(pvc and pvc['metadata']['uid'] == data['pvc_uid'], 'IMPORT_PVC_IDENTITY_DIFFERS')
    require((deployments or {}).get('metadata', {}).get('uid') == data['destination_freeze']['deployment_uid'], 'IMPORT_DEPLOYMENT_IDENTITY_DIFFERS')
    if data.get('verify_only') and deployments and deployments['spec'].get('replicas') == 1:
        return json.loads(native(['k3s', 'kubectl', 'exec', '-i', '-n', 'railshot-system', 'deployment/railshot-api', '--', 'python3', '-c', IMPORTER],
                                 body=encoded({**data['package'], 'verify_only': True}), timeout=90))
    if data.get('verify_only'):
        data['package']['verify_only'] = True
    name = 'railshot-bootstrap-import-' + data['package']['package_id'][:12]
    pod = kube_get('pod', name, 'railshot-system')
    if pod:
        require(pod['metadata'].get('annotations', {}).get('railshot.io/import-manifest') == data['package']['manifest_sha256'] and
                pod['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'railshot-bootstrap' and
                not pod['metadata'].get('ownerReferences') and pod['spec'].get('automountServiceAccountToken') is False and
                pod['spec'].get('serviceAccountName', 'default') == 'default' and
                pod['spec']['volumes'] == [{'name': 'state', 'persistentVolumeClaim': {'claimName': 'railshot-api'}}] and
                len(pod['spec']['containers']) == 1 and pod['spec']['containers'][0]['image'] == data['image'] and
                pod['spec']['containers'][0]['volumeMounts'] == [{'name': 'state', 'mountPath': '/var/lib/railshot'}], 'IMPORT_POD_CONFLICT')
        if pod.get('status', {}).get('phase') in ('Succeeded', 'Failed'):
            frozen_destination(data, pod)
            delete_import_pod(pod)
            pod = None
    frozen_destination(data, pod)
    if pod is None:
        pod = kube('create', '-f', '-', '-o', 'json', document={'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': name, 'namespace': 'railshot-system', 'labels': LABELS,
                         'annotations': {'railshot.io/import-manifest': data['package']['manifest_sha256']}},
            'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                     'nodeSelector': {'railshot.io/node-role': 'platform'}, 'imagePullSecrets': [{'name': 'ghcr-pull'}],
                     'securityContext': {'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000, 'fsGroupChangePolicy': 'OnRootMismatch'},
                     'containers': [{'name': 'import', 'image': data['image'], 'command': ['sleep', '600'],
                        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                        'volumeMounts': [{'name': 'state', 'mountPath': '/var/lib/railshot'}]}],
                     'volumes': [{'name': 'state', 'persistentVolumeClaim': {'claimName': 'railshot-api'}}]}})
    require(pod['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'railshot-bootstrap' and
            pod['spec']['containers'][0]['image'] == data['image'], 'IMPORT_POD_CONFLICT')
    native(['k3s', 'kubectl', 'wait', '-n', 'railshot-system', 'pod/' + name, '--for=condition=Ready', '--timeout=120s'], timeout=130)
    frozen_destination(data, pod)
    result = json.loads(native(['k3s', 'kubectl', 'exec', '-i', '-n', 'railshot-system', name, '--', 'python3', '-c', IMPORTER],
                               body=encoded(data['package']), timeout=90))
    require(result.get('verified') is True, 'IMPORT_NOT_VERIFIED')
    delete_import_pod(pod)
    return result


def delete_import_pod(pod):
    name = pod['metadata']['name']
    kube('delete', '--raw=/api/v1/namespaces/railshot-system/pods/' + name, '-f', '-',
         document={'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': pod['metadata']['uid']}})
    native(['k3s', 'kubectl', 'wait', '-n', 'railshot-system', 'pod/' + name, '--for=delete', '--timeout=60s'], timeout=70)
    require(kube_get('pod', name, 'railshot-system') is None, 'IMPORT_POD_DELETION_NOT_OBSERVED')


class Bootstrap:
    def __init__(self, root, settings):
        self.root, self.config = Path(root), settings
        self.home = Path(settings['state_dir'])
        require(self.home.is_absolute() and self.root not in self.home.parents and not self.home.is_symlink(), 'PRIVATE_STATE_OUTSIDE_CHECKOUT_REQUIRED')
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.home.stat()
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'PRIVATE_STATE_DIRECTORY_REQUIRED')
        self.path = self.home / 'bootstrap.json'
        self.binding = digest(encoded(settings))
        self.record = private(self.path) if self.path.exists() else {'version': 1, 'binding': self.binding, 'attempts': {}, 'uids': dict(settings['adopted_uids']), 'workflow': None}
        require(self.record['binding'] == self.binding, 'BOOTSTRAP_INPUT_CHANGED')

    def save(self):
        write(self.path, self.record)

    def aws(self, *args):
        return json.loads(native(['aws', '--region', REGION, '--output', 'json', *args],
                                 env={**os.environ, 'AWS_PAGER': '', 'AWS_MAX_ATTEMPTS': '1'}) or b'null')

    def gh(self, path, method='GET', body=None):
        command = ['gh', 'api', path, '--method', method]
        if body is not None:
            command += ['--input', '-']
        return json.loads(native(command, body=encoded(body) if body is not None else None) or b'null')

    def remote(self, role, action, data=None, timeout=1500):
        sys.path.insert(0, str(self.root / 'infrastructure/ansible'))
        from transport import forwarded_port
        node = self.config[role]
        source = Path(__file__).read_text().split('# LOCAL ENTRYPOINT\n')[0]
        request = {'action': action, 'node': node, 'source': self.config['source'], 'data': data or {}}
        program = source.encode() + b'\nguest(json.loads(' + repr(encoded(request)).encode() + b'))\n'
        with forwarded_port('ssm:' + REGION + ':' + node['instance_id'], time.monotonic() + timeout) as port:
            output = native(['ssh', '-F', '/dev/null', '-i', node['ssh']['identity_file'], '-p', str(port),
                    '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                    '-o', 'UserKnownHostsFile=' + node['ssh']['known_hosts_file'], '-o', 'HostKeyAlias=' + node['private_ip'],
                    '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3', 'railshot-operator@127.0.0.1',
                    'sudo -n systemd-run --quiet --wait --pipe --collect --unit=railshot-bootstrap-' + role + '-' + action + '-' + self.binding[:12]
                    + ' --property=RuntimeMaxSec=' + str(timeout - 30) + ' --property=KillMode=control-group /usr/bin/python3 -'],
                    body=program, timeout=timeout)
        value = json.loads(output)
        require(value['status'] == 'ok', 'GUEST_RESULT_NOT_VERIFIED')
        return value['result']

    def preflight(self):
        require(self.aws('sts', 'get-caller-identity')['Account'] == ACCOUNT, 'AWS_ACCOUNT_DIFFERS')
        require(native(['git', 'rev-parse', 'HEAD'], cwd=self.root).decode().strip() == self.config['source']['ref'], 'CHECKOUT_REF_DIFFERS')
        require(not native(['git', 'status', '--porcelain'], cwd=self.root).strip(), 'CLEAN_PUBLISHED_CHECKOUT_REQUIRED')
        head = self.gh('repos/' + REPOSITORY + '/commits/' + self.config['github']['ref'])['sha']
        require(head == self.config['source']['ref'], 'TRUSTED_BRANCH_MOVED')
        for role in ('control', 'build'):
            expected = self.config[role]
            reservations = self.aws('ec2', 'describe-instances', '--instance-ids', expected['instance_id'])['Reservations']
            require(len(reservations) == 1 and len(reservations[0]['Instances']) == 1, 'EC2_IDENTITY_MISSING')
            node = reservations[0]['Instances'][0]
            volumes = [x['Ebs']['VolumeId'] for x in node['BlockDeviceMappings'] if x['DeviceName'] == node['RootDeviceName']]
            require(node['PrivateIpAddress'] == expected['private_ip'] and volumes == [expected['root_volume_id']]
                    and node['State']['Name'] == 'running', 'EC2_IDENTITY_OR_STATE_DIFFERS')
            ssm = self.aws('ssm', 'describe-instance-information', '--filters', json.dumps([{'Key': 'InstanceIds', 'Values': [expected['instance_id']]}]))
            require(len(ssm['InstanceInformationList']) == 1 and ssm['InstanceInformationList'][0]['PingStatus'] == 'Online', 'SSM_NOT_ONLINE')

    def terraform(self, name):
        item = self.config['terraform'][name]; state = Path(item['state'])
        require(state.resolve() == state and not state.is_symlink(), 'EXACT_STATE_PATH_REQUIRED')
        prior_lineage = self.record.get('lineages', {}).get(name, item['lineage'])
        previous = self.record['attempts'].get(name)
        work = self.home / name; work.mkdir(mode=0o700, exist_ok=True)
        saved_plan = work / 'reviewed.tfplan'
        recover_lineage = state.exists() and prior_lineage is None
        if recover_lineage:
            require(name == 'platform-verification' and previous and previous['status'] == 'unknown' and saved_plan.is_file() and
                    digest(saved_plan.read_bytes()) == previous['plan_sha256'], 'UNBOUND_VERIFIER_STATE_REFUSED')
        if state.exists():
            current_state = private(state)
            require(recover_lineage or current_state['lineage'] == prior_lineage, 'TERRAFORM_LINEAGE_DIFFERS')
        else:
            require(name == 'platform-verification' and prior_lineage is None, 'EXISTING_TERRAFORM_STATE_REQUIRED')
        env = {**os.environ, 'TF_IN_AUTOMATION': '1', 'TF_INPUT': '0', 'TF_DATA_DIR': str(work / '.terraform')}
        prefix = ['terraform', '-chdir=' + str(self.root / 'infrastructure/terraform' / name)]
        init = ['init', '-input=false', '-lockfile=readonly']
        if name != 'ci':
            init += ['-reconfigure', '-backend-config=path=' + str(state)]
        native(prefix + init, env=env, timeout=180)
        original = json.loads(native(prefix + ['show', '-json', str(saved_plan)], env=env)) if recover_lineage else None
        plan = work / 'observed.tfplan' if recover_lineage else saved_plan
        arguments = ['plan', '-input=false', '-lock-timeout=30s', '-var-file=' + item['variables'], '-out=' + str(plan)]
        if name == 'ci':
            arguments += ['-state=' + str(state)]
        native(prefix + arguments, env=env, timeout=300)
        observed = json.loads(native(prefix + ['show', '-json', str(plan)], env=env))
        changes = plan_changes(observed)
        if recover_lineage:
            planned = {r['address']: r for r in original['resource_changes'] if r.get('mode') != 'data'}
            actual = {r['address']: r for r in observed['resource_changes'] if r.get('mode') != 'data'}
            require(planned and set(planned) == set(actual) and not changes, 'VERIFIER_RECOVERY_RESOURCES_DIFFER')
            for address, expected in planned.items():
                current = actual[address]
                require(expected['type'] == current['type'] and expected.get('provider_name') == current.get('provider_name') and
                        expected['change']['actions'] == ['create'] and current['change']['actions'] == ['no-op'] and
                        matches_known_plan(current['change']['after'], expected['change']['after'], expected['change'].get('after_unknown')),
                        'VERIFIER_RECOVERY_IDENTITY_DIFFERS')
        if previous and previous['status'] == 'unknown':
            require(not changes, 'UNCERTAIN_TERRAFORM_APPLY_REQUIRES_RECONCILIATION')
        if changes:
            self.record['attempts'][name] = {'status': 'unknown', 'plan_sha256': digest(plan.read_bytes()), 'changes': changes}; self.save()
            command = ['apply', '-input=false', '-lock-timeout=30s']
            if name == 'ci':
                command += ['-state=' + str(state)]
            native(prefix + command + [str(plan)], env=env, timeout=600)
        require(state.is_file(), 'TERRAFORM_STATE_NOT_OBSERVED')
        actual = private(state)
        if recover_lineage:
            require(actual['lineage'] == current_state['lineage'], 'TERRAFORM_STATE_REPLACED')
        if prior_lineage:
            require(actual['lineage'] == prior_lineage, 'TERRAFORM_STATE_REPLACED')
        self.record.setdefault('lineages', {})[name] = actual['lineage']
        if changes:
            self.record['attempts'][name]['status'] = 'completed'
        elif previous and previous['status'] == 'unknown':
            previous['status'] = 'observed_completed'
        self.save()  # The confirmed lineage and final attempt status are one atomic record.
        command = ['output', '-json']
        if name == 'ci':
            command += ['-state=' + str(state)]
        return {key: value['value'] for key, value in json.loads(native(prefix + command, env=env)).items()}

    def objects(self, documents, preserve_existing=False):
        # Save one UID after each object, so an uncertain later write does not replay earlier objects.
        for document in documents:
            result = self.remote('control', 'objects', {'documents': [document], 'uids': self.record['uids'], 'preserve_existing': preserve_existing, 'claim': self.binding})
            self.record['uids'].update(result['uids']); self.save()

    def bootstrap_secrets(self):
        import yaml
        docs = list(yaml.safe_load_all((self.root / 'deployment/manifests/product-access.yaml').read_text()
                    .replace('TARGET_REQUIRED', self.config['github']['target_id'])))
        application = self.config['github']['application']
        for document in docs:
            if document['kind'] == 'Role':
                for rule in document['rules'][:]:
                    if rule.get('resourceNames') == ['APPLICATION_REQUIRED']:
                        if application is None:
                            document['rules'].remove(rule)
                        else:
                            rule['resourceNames'] = [application]
        namespaces = [{'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name, 'labels': labels}}
                      for name, labels in [('railshot-system', {}), ('railshot-build', {'pod-security.kubernetes.io/enforce': 'privileged'})]]
        self.objects(namespaces + docs)
        values = self.config['secrets']
        pull = private(values['pull_config'])
        require(set(pull.get('auths', {})) == {'ghcr.io'}, 'SINGLE_GHCR_PULL_CREDENTIAL_REQUIRED')
        executors = private(values['executors'])
        require('cd.json' in executors and 'kubeconfig' in executors and
                set(executors) <= {'cd.json', 'kubeconfig', 'observer.json', 'profiles.json', 'registration.json', 'edge.json'}, 'EXECUTOR_SECRET_KEYS_DIFFER')
        def secret(ns, name, data, kind='Opaque'):
            return {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'namespace': ns, 'name': name}, 'type': kind,
                    'data': {key: base64.b64encode(val if isinstance(val, bytes) else val.encode()).decode() for key, val in data.items()}}
        secrets = [secret('railshot-system', 'railshot-api', {'token': private(values['api_token'], raw=True).strip()}),
                   secret('railshot-system', 'railshot-github', {'token': private(values['github_token'], raw=True).strip()}),
                   secret('railshot-system', 'railshot-executors', executors),
                   secret('railshot-system', 'railshot-runner-controller-github', {'token': private(values['controller_github_token'], raw=True).strip()}),
                   secret('railshot-build', 'railshot-build-runner-registration', {'token': ''})]
        secrets += [secret(ns, 'ghcr-pull', {'.dockerconfigjson': json.dumps(pull)}, 'kubernetes.io/dockerconfigjson')
                    for ns in ('railshot-system', 'railshot-build', 'argocd')]
        self.objects(secrets)
        registration = private(self.config['registration']['documents'])
        require(registration.get('kind') == 'List', 'REGISTRATION_LIST_REQUIRED')
        for document in registration['items']:
            require(document['kind'] in {'ServiceAccount', 'Role', 'RoleBinding', 'Secret', 'ConfigMap', 'AppProject', 'Application'} and
                    document['metadata'].get('namespace') in {'argocd', 'railshot-system'}, 'REGISTRATION_SCOPE_DIFFERS')
        self.objects(registration['items'], preserve_existing=True)

    def publication(self):
        endpoint = 'repos/' + REPOSITORY + '/actions/workflows/platform-publish.yml/runs'
        attempt = self.record['workflow']
        if attempt is None:
            before = self.gh(endpoint + '?event=workflow_dispatch&per_page=100')['workflow_runs']
            attempt = {'before': [r['id'] for r in before], 'actor': self.gh('user')['id'], 'run_id': None,
                       'requested_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                       'source_sha': self.config['source']['ref'], 'status': 'unknown'}
            self.record['workflow'] = attempt; self.save()
            self.gh('repos/' + REPOSITORY + '/actions/workflows/platform-publish.yml/dispatches', 'POST',
                    {'ref': self.config['github']['ref'], 'inputs': {'components': '["dashboard","api","ci-runner"]', 'publish': True, 'deploy': False}})
        # Observe even after an uncertain dispatch. Never dispatch a second build on resume.
        deadline = time.monotonic() + 2400
        while not attempt['run_id'] and time.monotonic() < deadline:
            runs = self.gh(endpoint + '?event=workflow_dispatch&per_page=100')['workflow_runs']
            selected = [r for r in runs if r['id'] not in attempt['before'] and r['head_sha'] == attempt['source_sha']
                        and r['actor']['id'] == attempt['actor'] and r['created_at'] >= attempt['requested_at']]
            require(len(selected) <= 1, 'WORKFLOW_DISPATCH_AMBIGUOUS')
            if selected:
                attempt['run_id'] = selected[0]['id']; self.save(); break
            time.sleep(3)
        require(attempt['run_id'], 'WORKFLOW_DISPATCH_NOT_OBSERVED')
        while time.monotonic() < deadline:
            run = self.gh('repos/' + REPOSITORY + '/actions/runs/' + str(attempt['run_id']))
            require(run['head_sha'] == attempt['source_sha'] and run['event'] == 'workflow_dispatch', 'WORKFLOW_IDENTITY_DIFFERS')
            if run['status'] == 'completed':
                require(run['conclusion'] == 'success', 'PLATFORM_PUBLICATION_FAILED'); break
            time.sleep(10)
        else:
            raise Blocked('PLATFORM_PUBLICATION_TIMEOUT')
        artifacts = self.gh('repos/' + REPOSITORY + '/actions/runs/' + str(attempt['run_id']) + '/artifacts?per_page=100')['artifacts']
        images = {}; directory = self.home / 'images'; directory.mkdir(mode=0o700, exist_ok=True)
        for component in ('dashboard', 'api', 'ci-runner'):
            matches = [a for a in artifacts if a['name'] == 'platform-image-' + component + '-' + attempt['source_sha'] and not a['expired']]
            require(len(matches) == 1, 'PUBLISHED_ARTIFACT_MISSING_OR_DUPLICATED')
            raw = native(['gh', 'api', 'repos/' + REPOSITORY + '/actions/artifacts/' + str(matches[0]['id']) + '/zip'])
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                require(archive.namelist() == [component + '.json'], 'PUBLICATION_ARCHIVE_DIFFERS')
                value = json.loads(archive.read(component + '.json'))
            require(set(value) == {component} and re.fullmatch('ghcr.io/jasmin-softbank/railshot-' + component + '@sha256:' + SHA, value[component]), 'PUBLISHED_DIGEST_INVALID')
            images.update(value); write(directory / (component + '.json'), value)
        attempt['status'] = 'completed'; attempt['images'] = images; self.save()
        return images

    def handoff(self, image):
        reference = self.config['import_package']
        if reference is None:
            return
        package = private(reference)
        require(set(package) == {'version', 'package_id', 'source_owner', 'destination_owner', 'freeze_receipt', 'freeze_sha256',
                                'destination_freeze_receipt', 'destination_freeze_sha256', 'archive', 'files'}
                and package['version'] == 1 and re.fullmatch(r'[a-f0-9]{32}', package['package_id']), 'IMPORT_PACKAGE_REQUIRED')
        freeze = private(package['freeze_receipt'], raw=True)
        require(digest(freeze) == package['freeze_sha256'], 'FREEZE_RECEIPT_DIFFERS')
        receipt = json.loads(freeze)
        require(receipt['source_owner'] == package['source_owner'] and
                package['source_owner'] != package['destination_owner'] and
                receipt['writers'] == {'api': 'stopped', 'terraform': 'frozen', 'edge': 'frozen', 'budget': 'frozen'}, 'SOURCE_WRITERS_NOT_FROZEN')
        frozen = private(package['destination_freeze_receipt'], raw=True)
        require(digest(frozen) == package['destination_freeze_sha256'], 'DESTINATION_FREEZE_RECEIPT_DIFFERS')
        destination = json.loads(frozen)
        require(set(destination) == {'version', 'destination_owner', 'application_uid', 'deployment_uid', 'pvc_uid', 'writers'} and
                destination['version'] == 1 and destination['destination_owner'] == package['destination_owner'] and
                destination['writers'] == {'api': 'stopped', 'argocd': 'frozen'}, 'DESTINATION_WRITERS_NOT_FROZEN')
        for kind, key in [('Application/argocd/railshot-platform', 'application_uid'),
                          ('Deployment/railshot-system/railshot-api', 'deployment_uid'),
                          ('PersistentVolumeClaim/railshot-system/railshot-api', 'pvc_uid')]:
            require(destination[key] == self.record['uids'].get(kind), 'DESTINATION_ADOPTION_DIFFERS')
        files = []; names = set()
        with tarfile.open(fileobj=io.BytesIO(private(package['archive'], raw=True)), mode='r:*') as archive:
            members = {m.name: m for m in archive.getmembers()}
            require(len(members) == len(archive.getmembers()) and set(members) == {f['path'] for f in package['files']}, 'IMPORT_ARCHIVE_DIFFERS')
            require(sum(m.size for m in members.values()) <= 32 * 1024 * 1024, 'IMPORT_CONTENT_TOO_LARGE')
            for item in package['files']:
                path = PurePosixPath(item['path'])
                require(not path.is_absolute() and path.as_posix() == item['path'] and '..' not in path.parts and
                        path.parts[0] in {'state', 'cd', 'infra', 'edge', 'budget', 'registration', 'terraform', 'billing', 'config', 'environments', 'control-terraform'}
                        and '.terraform' not in path.parts and path.name not in {'owner.json', '.terraform.tfstate.lock.info'}
                        and not path.name.endswith(('.lock', '-wal', '-shm'))
                        and item['path'] not in names and members[item['path']].isfile(), 'IMPORT_PATH_REFUSED')
                if path.parts[0] == 'config':
                    require((len(path.parts) == 3 and path.parts[1] == 'app-db') or item['path'] == 'config/edge.json', 'IMPORT_CONFIG_PATH_REFUSED')
                    if item['path'] == 'config/edge.json':
                        require(item['kind'] == 'edge' and item.get('binding') == {'version': 1,
                                'state_dir': '/var/lib/railshot/edge/allocations', 'terraform_dir': '/var/lib/railshot/edge/module',
                                'variables_file': '/var/lib/railshot/edge/inputs.tfvars.json'}, 'IMPORT_EDGE_BINDING_DIFFERS')
                    else:
                        require(item['kind'] == 'source', 'IMPORT_CONFIG_MUST_BE_IMMUTABLE')
                require(item['kind'] in {'terraform', 'budget', 'api', 'source', 'cd', 'edge', 'registration'}, 'IMPORT_KIND_REFUSED')
                if item['kind'] in {'cd', 'edge', 'registration'}:
                    require(isinstance(item.get('binding'), dict) and item['binding'], 'IMPORT_OWNER_BINDING_REQUIRED')
                if item['kind'] == 'budget':
                    require(isinstance(item.get('scopes'), dict) and item['scopes'], 'IMPORT_BUDGET_SCOPE_REQUIRED')
                raw = archive.extractfile(members[item['path']]).read(); names.add(item['path'])
                require(digest(raw) == item['sha256'], 'IMPORT_CHECKSUM_DIFFERS')
                files.append({**item, 'data': base64.b64encode(raw).decode()})
        require('config/app-db/profiles.json' in names, 'IMPORT_PROFILES_REQUIRED')
        observer = observer_product_file(files)
        executors = private(self.config['secrets']['executors'])
        for item in files:
            path = PurePosixPath(item['path'])
            if len(path.parts) == 2 and path.parts[0] == 'config' and path.name in executors:
                require(item['kind'] != 'edge', 'IMPORT_MUTABLE_CONFIG_PROJECTED')
                require(digest(executors[path.name].encode()) == item['sha256'], 'IMPORT_PROJECTED_CONFIG_CONFLICT')
        payload = {**package, 'files': files, 'manifest_sha256': digest(encoded(package))}
        pvc = self.record['uids'].get('PersistentVolumeClaim/railshot-system/railshot-api')
        result = self.remote('control', 'import', {'image': image, 'pvc_uid': pvc, 'package': payload,
                            'destination_freeze': destination, 'verify_only': bool(self.record.get('import'))})
        require(result['verified'] and result['package_id'] == package['package_id'], 'IMPORT_RECEIPT_DIFFERS')
        self.record['import'] = {'package_id': package['package_id'], 'manifest_sha256': payload['manifest_sha256'], 'owner': package['destination_owner']}; self.save()
        bindings = {'profiles_file': '/var/lib/railshot/config/app-db/profiles.json'}
        if observer:
            bindings['observer_file'] = observer
        self.objects([{'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'railshot-environments', 'namespace': 'railshot-system'},
                       'data': bindings}], preserve_existing=True)

    def release(self, images, verification):
        # The existing publisher requires a clean ephemeral checkout and non-force push.
        workspace = self.home / 'release-checkout'
        if not workspace.exists():
            native(['git', 'clone', '--no-checkout', 'https://github.com/' + REPOSITORY + '.git', str(workspace)], timeout=180)
        native(['git', '-C', str(workspace), 'fetch', '--no-tags', 'origin', self.config['source']['ref']], timeout=180)
        native(['git', '-C', str(workspace), 'switch', '--detach', self.config['source']['ref']])
        native(['git', '-C', str(workspace), 'config', 'credential.helper', '!gh auth git-credential'])
        result = json.loads(native([sys.executable, str(workspace / 'deployment/scripts/publish-platform.py'), '--artifacts', str(self.home / 'images'),
                    '--source-sha', self.config['source']['ref'], '--target-id', self.config['github']['target_id'],
                    '--dashboard-node-port', str(self.config['github']['node_port'])], timeout=180))
        self.record['release'] = result; self.save()
        verifier = load_module(self.root / 'deployment/scripts/verify-platform.py', 'bootstrap_verifier')
        variables = verification['workflow_variables']
        proof = verifier.remote(result['revision'], {k: images[k] for k in ('dashboard', 'api')},
                                variables['RAILSHOT_PLATFORM_VERIFY_DOCUMENT_VERSION'], variables['RAILSHOT_PLATFORM_VERIFY_DOCUMENT_SHA256'])
        require(proof['status'] == 'verified', 'PLATFORM_ROLLOUT_NOT_VERIFIED')
        self.record['verification'] = proof; self.save()
        return proof

    def workflow_variables(self, verification):
        values = {**verification['workflow_variables'], 'RAILSHOT_PLATFORM_TARGET_ID': self.config['github']['target_id'],
                  'RAILSHOT_PLATFORM_NODE_PORT': str(self.config['github']['node_port'])}
        endpoint = 'repos/' + REPOSITORY + '/actions/variables'
        current = self.gh(endpoint + '?per_page=100')
        require(current['total_count'] <= 100, 'WORKFLOW_VARIABLE_INVENTORY_TOO_LARGE')
        existing = {v['name']: v['value'] for v in current['variables']}
        for name, value in values.items():
            require(isinstance(value, str), 'WORKFLOW_VARIABLE_VALUE_INVALID')
            if name not in existing:
                self.gh(endpoint, 'POST', {'name': name, 'value': value})
            elif existing[name] != value:
                require(name in {'RAILSHOT_PLATFORM_VERIFY_DOCUMENT_VERSION', 'RAILSHOT_PLATFORM_VERIFY_DOCUMENT_SHA256'}, 'WORKFLOW_BINDING_DIFFERS')
                self.gh(endpoint + '/' + name, 'PATCH', {'name': name, 'value': value})
            require(self.gh(endpoint + '/' + name)['value'] == value, 'WORKFLOW_VARIABLE_READBACK_DIFFERS')

    def metadata(self, stage):
        import yaml
        document = yaml.safe_load((self.root / 'deployment/manifests/product-metadata.yaml').read_text())
        proof = self.remote('control', 'metadata-' + stage, {'document': document, 'claim': self.binding,
                            'uid': self.record['uids'].get(object_key(document)),
                            'api_uid': self.record['uids'].get('Deployment/railshot-system/railshot-api')})
        self.record['uids'][object_key(document)] = proof['policy_uid']
        self.record['metadata_' + stage] = proof; self.save()

    def run(self):
        self.preflight()
        executor = private(self.config['terraform']['control']['variables']).get('enable_product_executor', False)
        require(type(executor) is bool, 'PRODUCT_EXECUTOR_OPT_IN_INVALID')
        if executor: self.metadata('before')
        control = self.terraform('control')
        if executor: self.metadata('after')
        build = self.terraform('ci'); verification = self.terraform('platform-verification')
        require(control['instance_id'] == self.config['control']['instance_id'] and
                build['node_descriptor']['instance_id'] == self.config['build']['instance_id'] and
                build['node_descriptor']['lifecycle']['stop_at'] == self.config['build']['stop_at'], 'TERRAFORM_OUTPUT_BINDING_DIFFERS')
        self.workflow_variables(verification)
        self.preflight()  # Recheck instance and retained volume identity after apply.
        self.remote('control', 'control')
        server = 'https://' + self.config['control']['private_ip'] + ':6443'
        observed = self.remote('build', 'build-observe', {'server': server})
        if not observed['installed']:
            require(not self.record['attempts'].get('join'), 'UNCERTAIN_AGENT_JOIN_REQUIRES_RECONCILIATION')
            token = self.remote('control', 'agent-token')['token']
            self.record['attempts']['join'] = {'status': 'unknown'}; self.save()
            self.remote('build', 'build-join', {'server': server, 'token': token})
        observed = self.remote('control', 'build-health', {'build': self.config['build']})
        known_uid = self.record.get('build_node_uid', self.config['build']['node_uid'])
        require(not known_uid or known_uid == observed['node_uid'], 'BUILD_NODE_REPLACED')
        self.record['build_node_uid'] = observed['node_uid']; self.record['attempts']['join'] = {'status': 'completed'}; self.save()
        self.remote('control', 'build-quiesce', {'uid': self.record['uids'].get('CronJob/railshot-system/railshot-build-controller')})
        self.remote('build', 'ci')
        self.bootstrap_secrets()
        images = self.publication()
        renderer = load_module(self.root / 'deployment/scripts/render-platform.py', 'bootstrap_renderer')
        import yaml
        platform = renderer.render(images, self.config['github']['target_id'], self.config['github']['node_port'])
        self.objects([d for d in platform['items'] if d['kind'] == 'PersistentVolumeClaim'])
        self.handoff(images['api'])
        controller = renderer.render_build_controller(images, 'https://github.com/Jasmin-Softbank/railshot-apps', self.config['build']['node_name'])
        sys.path.insert(0, str(self.root / 'gitops'))
        import credentials
        policy = private(self.config['registration']['credentials_policy'])
        renewal = credentials.render(policy, images['api'])
        for manifest in (controller, renewal):
            for document in manifest['items']:
                if document['kind'] == 'CronJob':
                    document['spec']['suspend'] = True
            self.objects(manifest['items'])
        self.remote('control', 'cron-test', {'namespace': 'argocd', 'name': 'railshot-credentials', 'run': self.binding,
                    'uid': self.record['uids']['CronJob/argocd/railshot-credentials'], 'secrets': [t['secret'] for t in policy['targets']]})
        # Controller RBAC is scoped to the build namespace, not its platform namespace.
        self.remote('control', 'cron-test', {'namespace': 'railshot-system', 'name': 'railshot-build-controller', 'run': self.binding,
                    'uid': self.record['uids']['CronJob/railshot-system/railshot-build-controller'], 'secrets': ['railshot-build-runner-registration']})
        self.objects(list(yaml.safe_load_all((self.root / 'gitops/applications/railshot-platform.yaml').read_text())))
        proof = self.release(images, verification)
        if executor: self.metadata('final')
        return proof


# LOCAL ENTRYPOINT
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--validate-only', action='store_true', help='private input validation only; no Terraform/cloud/SSH/GitHub calls')
    args = parser.parse_args()
    try:
        settings = config(args.config)
        if args.validate_only:
            print(json.dumps({'status': 'validated', 'runtime_verified': False})); return 0
        root = Path(__file__).resolve().parents[2]
        bootstrap = Bootstrap(root, settings)
        with (bootstrap.home / 'bootstrap.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = bootstrap.run()
        print(json.dumps(result)); return 0
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'code': str(error) if isinstance(error, Blocked) else 'BOOTSTRAP_REQUIRES_READBACK',
                          'retryable': False})); return 2


if __name__ == '__main__':
    sys.exit(main())
