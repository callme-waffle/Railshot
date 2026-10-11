import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'deployment/scripts'))
import environment as environment
import personal_runtime as personal_runtime

spec = importlib.util.spec_from_file_location('tested_runtime_access', ROOT / 'apps/agent/runtime_access.py')
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)
TARGET = 'personal-12345678-1234-1234-1234-123456789abc'
APP = 'app-' + 'a' * 24


def command(namespace, *args):
    return shlex.join(access.PREFIX + [namespace, *args])


def documents(namespace=TARGET):
    target = {'namespace': namespace, 'image_pull_secret': {'name': 'ghcr-pull'}}
    return environment.runtime_documents(target, namespace if namespace == APP else TARGET,
                                         {'auths': {'ghcr.io': {'auth': 'synthetic'}}}, None)


def connector_documents(monkeypatch):
    profile = {'tunnel': {'name': 'railshot-tunnel', 'tunnel_id': '66666666-6666-4666-8666-666666666666',
        'credentials_file': '/private/credentials', 'ca_file': '/private/ca', 'origin_vip': '10.0.0.51'}}
    values = {'/private/credentials': json.dumps({'AccountTag': 'operator', 'TunnelSecret': 'synthetic',
              'TunnelID': profile['tunnel']['tunnel_id']}).encode(),
              '/private/ca': b'-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n'}
    monkeypatch.setattr(personal_runtime.runtime, 'read_private', lambda path, raw=False: values[path])
    return personal_runtime.tunnel_documents(profile, {'target_id': TARGET})


@pytest.mark.parametrize('namespace', [TARGET, APP])
def test_native_registration_documents_use_supported_fixed_commands(namespace):
    for doc in documents(namespace):
        ns = doc['metadata'].get('namespace', 'default')
        cmd = command(ns, 'apply', '--server-side', '--field-manager=railshot-registration', '-f', '-', '-o', 'json')
        parsed, _, action = access.parse(cmd, doc, TARGET)
        assert parsed == namespace and action == 'apply'
    access.parse(command(namespace, 'config', 'view', '--raw', '--minify', '-o', 'json'), None, TARGET)
    access.parse(command(namespace, 'create', '--raw', '/api/v1/namespaces/' + namespace + '/serviceaccounts/railshot-argocd/token', '-f', '-'),
                 {'apiVersion': 'authentication.k8s.io/v1', 'kind': 'TokenRequest', 'spec': {'expirationSeconds': 21600}}, TARGET)


def test_actual_connector_documents_use_only_exact_target_resources(monkeypatch):
    docs = connector_documents(monkeypatch)
    assert [(doc['kind'], doc['metadata']['name']) for doc in docs] == [
        ('Secret', 'railshot-tunnel-credentials'), ('ConfigMap', 'railshot-tunnel-ca'),
        ('ServiceAccount', 'railshot-tunnel'), ('ConfigMap', 'railshot-tunnel-config'),
        ('Deployment', 'railshot-tunnel')]
    for doc in docs:
        cmd = command(TARGET, 'apply', '--server-side', '--field-manager=railshot-registration', '-f', '-', '-o', 'json')
        assert access.parse(cmd, doc, TARGET)[2] == 'apply'
        assert access.parse(command(TARGET, 'get', doc['kind'], doc['metadata']['name'],
            '--ignore-not-found', '-o', 'json'), None, TARGET)[2] == 'read'


@pytest.mark.parametrize('change', ['namespace', 'name', 'image', 'secret_key', 'extra_label'])
def test_connector_allowlist_rejects_nearby_or_mutated_resources(monkeypatch, change):
    docs = connector_documents(monkeypatch)
    doc = copy.deepcopy(docs[-1] if change == 'image' else docs[0])
    if change == 'namespace':
        doc['metadata']['namespace'] = 'kube-system'
    elif change == 'name':
        doc['metadata']['name'] += '-other'
    elif change == 'image':
        doc['spec']['template']['spec']['containers'][0]['image'] = 'customer/image:latest'
    elif change == 'secret_key':
        doc['data']['other'] = base64.b64encode(b'private').decode()
    else:
        doc['metadata']['labels']['customer'] = 'other'
    with pytest.raises(ValueError):
        access.validate_document(doc, TARGET, TARGET)


def test_connector_patches_are_exact_cas_operations(monkeypatch):
    config = next(doc for doc in connector_documents(monkeypatch) if doc['metadata']['name'] == 'railshot-tunnel-config')
    config_patch = [
        {'op': 'test', 'path': '/metadata/uid', 'value': 'uid-1'},
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '1'},
        {'op': 'replace', 'path': '/data/config.json', 'value': config['data']['config.json']},
        {'op': 'replace', 'path': '/metadata/annotations/railshot.io~1application-owners', 'value': '{}'},
    ]
    args = ['patch', 'ConfigMap', 'railshot-tunnel-config', '--type=json', '--patch-file=/dev/stdin', '-o', 'json']
    assert access.parse(command(TARGET, *args), config_patch, TARGET)[2] == 'tunnel-patch'
    config_patch.append({'op': 'replace', 'path': '/data/customer', 'value': 'unsafe'})
    with pytest.raises(ValueError):
        access.parse(command(TARGET, *args), config_patch, TARGET)


@pytest.mark.parametrize('cmd', [
    'sudo -n sh -c id',
    command('default', 'get', 'secrets', '-A', '-o', 'json'),
    command('kube-system', 'get', 'secret', 'admin', '-o', 'json'),
    command(TARGET, 'exec', 'pod', '--', 'sh'),
    command(TARGET, 'get', 'secret', 'x', '--kubeconfig=/etc/admin', '-o', 'json'),
    command(TARGET, 'get', 'secret', 'x', '-o', 'json') + '; id',
    command(TARGET, 'get', 'secret', '--as=admin', '-o', 'json'),
    command('personal-' + 'f' * 36, 'config', 'view', '--raw', '--minify', '-o', 'json'),
    command(TARGET, 'delete', 'namespace', TARGET),
])
def test_fixed_channel_rejects_shell_flags_and_other_namespaces(cmd):
    with pytest.raises(ValueError):
        access.parse(cmd, None, TARGET)


@pytest.mark.parametrize('change', ['clusterrole', 'wildcard', 'secret_read', 'foreign_subject', 'token_secret', 'extra_metadata'])
def test_document_rbac_cannot_escalate(change):
    docs = documents()
    doc = copy.deepcopy(docs[2])
    if change == 'clusterrole':
        doc['kind'] = 'ClusterRole'
    elif change == 'wildcard':
        doc['rules'][0]['resources'] = ['*']
    elif change == 'secret_read':
        doc['rules'] = [{'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['get']}]
    elif change == 'foreign_subject':
        doc = copy.deepcopy(docs[3]); doc['subjects'][0]['namespace'] = 'kube-system'
    elif change == 'token_secret':
        doc = copy.deepcopy(docs[4]); doc['type'] = 'kubernetes.io/service-account-token'
    else:
        doc['metadata']['ownerReferences'] = [{'kind': 'Namespace', 'name': 'customer'}]
    with pytest.raises(ValueError):
        access.validate_document(doc, TARGET, TARGET)


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setattr(access, 'CONFIG', tmp_path)
    monkeypatch.setattr(access, 'safe', lambda path, **kwargs: Path(path))
    config = {'target_id': TARGET, 'generation': 1, 'project_id': 'project-1'}
    runtime = {'resource_id': 'vm-1', 'ssh': {'host': '10.26.1.5', 'port': 22, 'user': 'ubuntu',
                      'identity_file': '/private/key', 'known_hosts_file': '/private/hosts'}}
    objects, commands = {}, []
    def remote(argv, input=None):
        commands.append(argv)
        assert '-F' in argv and 'StrictHostKeyChecking=yes' in argv and 'IdentityAgent=none' in argv
        args = shlex.split(argv[-1]); ns, args = args[6], args[7:]
        if args[:2] == ['config', 'view']:
            assert args[-1] == 'jsonpath={.clusters[0].cluster.certificate-authority-data}'
            return base64.b64encode(b'-----BEGIN CERTIFICATE-----\nsynthetic').decode()
        if args[0] in ('create', 'apply'):
            doc = json.loads(input)
            doc['metadata'].update(uid='namespace-uid', resourceVersion='1')
            key = ('default' if doc['kind'] == 'Namespace' else ns, doc['kind'].lower(), doc['metadata']['name'])
            if args[0] == 'create' and key in objects:
                raise ValueError('already exists')
            objects[key] = doc
            return json.dumps(doc)
        if args[0] == 'get':
            return json.dumps(objects.get(('default' if args[1].lower() == 'namespace' else ns, args[1].lower(), args[2])))
        if args[0] == 'delete':
            return json.dumps({'status': 'Success'})
        raise AssertionError(args)
    def invoke(namespace, args, document=None):
        return access.execute(config, runtime, {'command': command(namespace, *args), 'document': document}, runner=remote)
    return invoke, objects, commands


def test_real_environment_kubectl_command_reaches_relay_parser(relay, monkeypatch):
    invoke, objects, commands = relay
    native = {'inventory': {'control_plane': [{'ssh': {}}]}}
    monkeypatch.setattr(environment.ansible, 'build_inventory', lambda *a: {'all': {'children': {'k3s_server': {'hosts': {'vm': {
        'ansible_ssh_common_args': '-o StrictHostKeyChecking=yes', 'ansible_ssh_private_key_file': '/server/key',
        'ansible_port': 2223, 'ansible_user': 'railshot-runtime', 'ansible_host': '10.253.240.2'}}}}}})
    def transport(args, document=None):
        parsed = shlex.split(args[-1])
        result = invoke(parsed[6], parsed[7:], document)
        return json.dumps(result) if result is not None else ''
    monkeypatch.setattr(environment.argo, 'native', transport)
    with environment.runtime_kubectl(native) as kube:
        for doc in documents():
            environment.owned_apply(kube, doc)
        ca = kube(TARGET, 'config', 'view', '--raw', '--minify', '-o', 'json')
    assert set(ca) == {'clusters'}
    assert len(objects) == 5
    assert any(shlex.split(args[-1])[7] == 'create' for args in commands)


def test_unrecorded_existing_namespace_is_never_adopted(relay):
    invoke, objects, commands = relay
    doc = documents()[0]
    objects['default', 'namespace', TARGET] = {**doc, 'metadata': {**doc['metadata'], 'uid': 'foreign-uid'}}
    with pytest.raises(ValueError, match='RUNTIME_NAMESPACE_NOT_OWNED'):
        invoke('default', ['apply', '--server-side', '--field-manager=railshot-registration', '-f', '-', '-o', 'json'], doc)
    assert len(commands) == 1


def test_replaced_namespace_rejects_token_and_secret_reads(relay):
    invoke, objects, commands = relay
    doc = documents()[0]
    invoke('default', ['apply', '--server-side', '--field-manager=railshot-registration', '-f', '-', '-o', 'json'], doc)
    objects['default', 'namespace', TARGET]['metadata']['uid'] = 'replacement'
    with pytest.raises(ValueError):
        invoke(TARGET, ['get', 'secret', 'ghcr-pull', '-o', 'json'])


def test_delete_requires_exact_uid_and_never_deletes_namespace(relay):
    invoke, objects, commands = relay
    for doc in documents():
        invoke(doc['metadata'].get('namespace', 'default'), ['apply', '--server-side', '--field-manager=railshot-registration', '-f', '-', '-o', 'json'], doc)
    options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': 'wrong', 'resourceVersion': '1'}}
    args = ['delete', '--raw', '/api/v1/namespaces/' + TARGET + '/secrets/ghcr-pull', '-f', '-']
    with pytest.raises(ValueError):
        invoke(TARGET, args, options)
    options['preconditions']['uid'] = 'namespace-uid'
    assert invoke(TARGET, args, options) == {'status': 'Success'}
    args[2] = '/api/v1/namespaces/' + TARGET
    with pytest.raises(ValueError):
        invoke(TARGET, args, options)


def test_tls_proxy_accepts_only_registered_server_source():
    fake = SimpleNamespace(allowed_source='10.253.240.1')
    assert access.Relay.verify_request(fake, None, ('10.253.240.1', 50000))
    for source in ('127.0.0.1', '10.253.240.3', '10.26.2.180'):
        assert not access.Relay.verify_request(fake, None, (source, 50000))


def test_safe_file_rejects_links_and_shared_writes(tmp_path):
    path = tmp_path / 'file'; path.write_text('x'); path.chmod(0o600)
    assert access.safe(path, owner=os.geteuid()) == path
    path.chmod(0o666)
    with pytest.raises(ValueError):
        access.safe(path, owner=os.geteuid())
    path.chmod(0o600)
    linked = tmp_path / 'link'; linked.symlink_to(path)
    with pytest.raises(ValueError):
        access.safe(linked, owner=os.geteuid())


def test_install_creates_only_fixed_accounts_and_wg_bound_services(tmp_path, monkeypatch):
    config_dir = tmp_path / 'config'; config_dir.mkdir()
    home = tmp_path / 'home'
    for name, path in {'CONFIG': config_dir, 'HOME': home, 'PAYLOAD': tmp_path / 'payload',
                       'WRAPPER': tmp_path / 'wrapper', 'SUDOERS': tmp_path / 'sudoers',
                       'SSH_UNIT': tmp_path / 'ssh.service', 'TLS_UNIT': tmp_path / 'proxy.service'}.items():
        monkeypatch.setattr(access, name, path)
    monkeypatch.setattr(access.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(access, 'safe', lambda path, **kwargs: Path(path))
    account = SimpleNamespace(pw_uid=993, pw_gid=993, pw_dir=str(home), pw_shell='/bin/sh')
    monkeypatch.setattr(access.pwd, 'getpwnam', lambda user: account)
    (config_dir / 'runtime-access-account.json').write_text(json.dumps({'user': access.USER, 'uid': 993, 'gid': 993}))
    key = 'ssh-ed25519 ' + 'A' * 68
    config = {'target_id': TARGET, 'generation': 1, 'project_id': 'project-1',
              'runtime_access': {'public_key': key}, 'tunnel': {'address': '10.253.240.2/32', 'allowed_ips': '10.253.240.1/32'}}
    runtime = {**{k: config[k] for k in ('target_id', 'generation', 'project_id')}, 'version': 1, 'architecture': 'amd64',
               'private_ipv4': '10.26.1.5', 'ssh': {'host': '10.26.1.5', 'user': 'ubuntu', 'port': 22,
                                                   'identity_file': '/private/customerkey', 'known_hosts_file': '/private/customerhosts'}}
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        if args[0] == 'ssh-keygen':
            Path(args[-1]).write_text('private-key')
            Path(args[-1] + '.pub').write_text(key)
        return 'active' if args[:2] == ['systemctl', 'is-active'] else ''
    result = access.install(config, runtime, runner=runner)
    assert result == {'ssh_user': access.USER, 'ssh_port': 2223, 'ssh_host_key': key}
    ssh_config = (config_dir / 'runtime_access_sshd_config').read_text()
    assert 'ListenAddress 10.253.240.2\n' in ssh_config and 'Port 2223\n' in ssh_config
    assert 'PermitRootLogin no\n' in ssh_config and 'AllowTcpForwarding no\n' in ssh_config
    assert 'ForceCommand /usr/bin/python3 -I ' in ssh_config and 'runtime_access.py forward\n' in ssh_config
    assert 'from="10.253.240.1/32"' in (home / 'authorized_keys').read_text()
    assert 'NOPASSWD: ' + str(access.WRAPPER) + ' ""' in access.SUDOERS.read_text()
    assert access.WRAPPER.read_text().endswith('runtime_access.py serve\n')
    assert not any(arg in ('k3s', 'ansible-playbook', 'openstack') for call in calls for arg in call)
    assert 'private-key' not in json.dumps(result)


@pytest.fixture
def lifecycle_relay(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('runtime_lifecycle_fixtures', ROOT / 'deployment/scripts/tests/test_lifecycle_runtime.py')
    fixtures = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixtures)
    kube = fixtures.Kube()
    account = fixtures.obj('ServiceAccount', 'railshot-argocd')
    account['metadata'].update(namespace=TARGET, uid='anchor-uid')
    kube.shared_accounts[TARGET] = account
    kube.objects.append(fixtures.obj('RoleBinding', 'railshot-environment-argocd',
        roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': 'railshot-argocd'},
        subjects=[{'kind': 'ServiceAccount', 'name': 'railshot-argocd', 'namespace': TARGET}]))
    binding = {'version': 1, 'application_id': APP, 'environment_id': TARGET,
               'registered': {'target': {'id': APP, 'namespace': APP}}}
    renewal = {'service_account': {'name': 'railshot-argocd', 'namespace': APP,
                                  'uid': kube.find('ServiceAccount', 'railshot-argocd')['metadata']['uid']}}
    shared = {'service_account': {'name': 'railshot-argocd', 'namespace': TARGET, 'uid': 'anchor-uid'}}
    config = {'target_id': TARGET, 'generation': 1, 'project_id': 'project-1'}
    native = {'resource_id': 'vm-1', 'ssh': {'host': '10.26.1.5', 'port': 22, 'user': 'ubuntu',
              'identity_file': '/private/key', 'known_hosts_file': '/private/hosts'}}
    monkeypatch.setattr(access, 'CONFIG', tmp_path)
    monkeypatch.setattr(access, 'safe', lambda path, **kwargs: Path(path))
    monkeypatch.setattr(access, 'lifecycle_module', lambda: fixtures.runtime)
    anchor_ns = {'metadata': {'uid': 'anchor-ns', 'labels': {'railshot.io/registration': TARGET}}}
    ledger = {APP: {'uid': kube.find('Namespace', APP)['metadata']['uid'], 'owner': APP},
              TARGET: {'uid': 'anchor-ns', 'owner': TARGET}}
    for value in ledger.values():
        value.update(target_id=TARGET, generation=1, resource_id='vm-1')
    (tmp_path / 'runtime-access-objects.json').write_text(json.dumps(ledger))
    def runner(argv, input=None):
        parts = shlex.split(argv[-1]); ns, args = parts[6], parts[7:]
        doc = json.loads(input) if input else None
        if ns == 'default' and args[:2] == ['get', 'namespace']:
            if args[2] == TARGET:
                return json.dumps(anchor_ns)
            ns = APP
        if args[0] == 'get' and '--raw' not in args and '--ignore-not-found' not in args:
            args.insert(-2, '--ignore-not-found')
        return json.dumps(kube(ns, *args, document=doc))
    def through(ns, *args, document=None):
        return access.execute(config, native, {'command': command(ns, *args), 'document': document}, runner=runner)
    return SimpleNamespace(fixtures=fixtures, kube=kube, through=through, binding=binding, renewal=renewal,
                           shared=shared, config=config, native=native, ledger=ledger, path=tmp_path / 'runtime-access-objects.json')


def test_existing_lifecycle_stop_start_delete_runs_through_fixed_relay(lifecycle_relay):
    f = lifecycle_relay; lifecycle = f.fixtures.runtime
    anchor_before = copy.deepcopy(f.kube.shared_accounts)
    original = lifecycle.inventory(f.through, f.binding, f.renewal, 'stop', shared_renewal=f.shared)
    assert lifecycle.execute(f.through, f.binding, 'stop', original)['status'] == 'succeeded'
    stopped = lifecycle.inventory(f.through, f.binding, f.renewal, 'start', shared_renewal=f.shared)
    assert lifecycle.execute(f.through, f.binding, 'start', stopped, stopped_inventory=original)['status'] == 'succeeded'
    delete = lifecycle.inventory(f.through, f.binding, f.renewal, 'delete', shared_renewal=f.shared)
    assert lifecycle.execute(f.through, f.binding, 'delete', delete)['status'] == 'succeeded'
    assert f.kube.find('Namespace', APP) is None
    assert f.kube.shared_accounts == anchor_before
    saved = json.loads(f.path.read_text())
    assert saved[APP]['status'] == 'deleting' and saved[TARGET] == f.ledger[TARGET]


def test_relay_independently_rejects_customer_resource_before_app_namespace_delete(lifecycle_relay):
    f = lifecycle_relay
    f.kube.objects.append(f.fixtures.obj('Secret', 'customer-secret', labels={'customer': 'keep'}, data={'key': 'private'}))
    ns = f.kube.find('Namespace', APP)
    options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Foreground',
               'preconditions': {k: ns['metadata'][k] for k in ('uid', 'resourceVersion')}}
    with pytest.raises(ValueError):
        f.through(APP, 'delete', '--raw', '/api/v1/namespaces/' + APP, '-f', '-', document=options)
    assert f.kube.writes == []
    assert f.kube.find('Secret', 'customer-secret') is not None


def test_relay_rejects_app_namespace_from_another_target_or_vm(lifecycle_relay):
    f = lifecycle_relay
    for field, value in [('target_id', 'personal-other'), ('generation', 2), ('resource_id', 'vm-other')]:
        bad = copy.deepcopy(f.ledger); bad[APP][field] = value
        f.path.write_text(json.dumps(bad))
        with pytest.raises(ValueError, match='RUNTIME_NAMESPACE_BINDING_CHANGED'):
            f.through(APP, 'get', '--raw', '/api')
    assert f.kube.writes == []


@pytest.mark.parametrize('path', ['/api/v1/namespaces/kube-system/secrets?limit=500',
                                  '/api/v1/namespaces/' + APP + '/pods/pod/exec?limit=500',
                                  '/api/v1/namespaces/' + APP + '/../secrets?limit=500',
                                  '/api/v1/pods?limit=500', '/api/v1/secrets?limit=500'])
def test_raw_inventory_never_escapes_owned_app_namespace(path):
    with pytest.raises(ValueError):
        access.parse(command(APP, 'get', '--raw', path), None, TARGET)


@pytest.mark.parametrize('shared_storage', [False, True])
def test_owned_app_storage_is_verified_locally_and_after_namespace_delete(lifecycle_relay, shared_storage):
    f = lifecycle_relay; lifecycle = f.fixtures.runtime
    case = f.fixtures.LifecycleRuntimeTest(methodName='runTest'); case.kube = f.kube
    _, volume = case.storage()
    if shared_storage:
        volume['spec']['accessModes'] = ['ReadWriteMany']
        ns = f.kube.find('Namespace', APP)
        options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Foreground',
                   'preconditions': {key: ns['metadata'][key] for key in ('uid', 'resourceVersion')}}
        with pytest.raises(ValueError):
            f.through(APP, 'delete', '--raw', '/api/v1/namespaces/' + APP, '-f', '-', document=options)
        assert f.kube.writes == [] and f.kube.find('PersistentVolume', 'pv-data') is not None
    else:
        inventory = lifecycle.inventory(f.through, f.binding, f.renewal, 'delete', shared_renewal=f.shared)
        assert lifecycle.execute(f.through, f.binding, 'delete', inventory)['status'] == 'succeeded'
        assert f.kube.find('Namespace', APP) is None and f.kube.find('PersistentVolume', 'pv-data') is None


def test_only_personal_app_lifecycle_ssh_extends_native_deadline(monkeypatch):
    calls = []
    node = {'ssh': {'port': 2223}}
    native = {'inventory': {'control_plane': [node]}}
    monkeypatch.setattr(environment.ansible, 'build_inventory', lambda *a: {'all': {'children': {'k3s_server': {'hosts': {'vm': {
        'ansible_ssh_common_args': '-o StrictHostKeyChecking=yes', 'ansible_ssh_private_key_file': '/server/key',
        'ansible_port': node['ssh']['port'], 'ansible_user': 'railshot-runtime', 'ansible_host': '10.253.240.2'}}}}}})
    def execute(*args, **kwargs):
        calls.append(kwargs.get('timeout', 30)); return '{}'
    monkeypatch.setattr(environment.argo, 'native', execute)
    with environment.runtime_kubectl(native) as kube:
        kube(APP, 'get', 'namespace', APP, '-o', 'json')
        kube(APP, 'delete', '--raw', '/api/v1/namespaces/' + APP, '-f', '-', document={})
        kube(TARGET, 'delete', '--raw', '/api/v1/namespaces/' + TARGET + '/secrets/ghcr-pull', '-f', '-', document={})
    node['ssh']['port'] = 22
    with environment.runtime_kubectl(native) as kube:
        kube(APP, 'delete', '--raw', '/api/v1/namespaces/' + APP, '-f', '-', document={})
    assert calls == [30, 300, 30, 30]
