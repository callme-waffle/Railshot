"""Actual shared environment registration; uses existing audited Argo/CI writers."""
import copy
import hashlib
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
for relative in ('deployment/scripts', 'infrastructure/ansible', 'gitops', 'ci/scripts'):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

from .contract import ProviderError
from .control import digest, private_dir, read_private, save


def bootstrap(application_config_path, context):
    import applications
    import environment
    import cluster_edge
    config = applications.load_config(application_config_path)
    identifier = context['environment_id']
    if set(config['environments']) != {identifier}:
        raise ProviderError('APPLICATION_BINDING_MISMATCH', 409)
    profile = config['environments'][identifier]
    registry = read_private(config['registry_file'])
    selected = registry['targets'][identifier]
    native, descriptor = environment.registered_node(selected, identifier, 'bootstrap.' + identifier)
    if native['target']['provider'] != context['spec']['provider'] or native['runtime']['profile'] != 'kubeadm-1c2w':
        raise ProviderError('APPLICATION_BINDING_MISMATCH', 409)
    edge = cluster_edge.validate(native)
    home = private_dir(Path(context['state_dir']) / 'bootstrap' / identifier)
    public = edge['fixed_routes'][0]
    app = 'environment-bootstrap'
    target = {**copy.deepcopy(profile['target']), 'id': identifier, 'namespace': identifier, 'project': identifier,
              'argocd_namespace': 'argocd', 'node_port': public['node_port'],
              'path': 'gitops/applications/' + app + '/' + identifier,
              'image_pull_secret': {'namespace': identifier, 'name': 'ghcr-pull'}}
    registration = {'version': 1, 'cd': {**config['cd'], 'targets': {identifier: {
        'app': app, 'tenant': profile['tenant'], 'target': target,
        'public_http': {'url': 'https://' + public['hostname'] + public['health_path'], 'expected_json': {}}}}},
        'registration': {'state_dir': str(private_dir(Path(context['state_dir']) / 'registration-claims')),
                         'source_repository': profile['source_repository'], 'pull_secret_file': profile['pull_secret_file']}}
    observer = profile['ingress'].get('observability_config_file')
    ci_credentials = profile['ingress'].get('ci_credentials_file')
    if not isinstance(ci_credentials, str) or not Path(ci_credentials).is_absolute():
        raise ProviderError('CI_CREDENTIAL_REFERENCE_REQUIRED', 409)
    registration['registration']['ci_credentials_file'] = ci_credentials
    if not isinstance(observer, str) or not Path(observer).is_absolute():
        raise ProviderError('OBSERVABILITY_NOT_CONFIGURED', 409)
    registration['registration']['observability_config_file'] = observer
    request_path = home / 'request.json'
    if request_path.exists() and read_private(request_path) != registration:
        raise ProviderError('APPLICATION_BOOTSTRAP_POLICY_CHANGED', 409)
    save(request_path, registration)
    result = environment.register(config['registry_file'], identifier, str(request_path), str(home))
    if result.get('status') != 'succeeded' or result.get('deployment_supported') is not True:
        raise ProviderError('APPLICATION_BOOTSTRAP_OUTCOME_UNKNOWN', 502, True)
    # Read actual Argo Secret/renewal policy, not merely a locally generated configuration.
    registered = read_private(home / 'cd.json')['targets'][identifier]
    environment.shared_cluster_snapshot(config['cd'], registered, identifier)
    receipt = {'version': 1, 'status': 'succeeded', 'environment_id': identifier,
               'account_id': context['account_id'], 'owner_id': context['owner_id'],
               'application_config_hash': digest(config), 'cluster_binding_hash': digest(native['target']),
               'registration_sha256': hashlib.sha256((home / 'registration.json').read_bytes()).hexdigest(),
               'observability_registered': result.get('observability', {}).get('registered') is True}
    if not receipt['observability_registered']:
        raise ProviderError('OBSERVABILITY_REGISTRATION_UNVERIFIED', 502, True)
    observer_config = read_private(observer)
    if 'api_registrar' not in observer_config:
        receipt['observer_product_file'] = str(Path(observer_config['state_dir']) / 'product.json')
    receipt_path = home / 'binding.json'; save(receipt_path, receipt)
    return {'status': 'succeeded', 'application_config_path': str(application_config_path),
            'application_binding_receipt_path': str(receipt_path)}


def teardown(application_config_path, context):
    """Revoke app admission and cluster credentials before provider VM teardown."""
    import applications
    import application_lifecycle as lifecycle
    import cluster_applications
    import environment
    import lifecycle_runtime as workloads
    config = applications.load_config(application_config_path)
    identifier = context['environment_id']
    if set(config['environments']) != {identifier}:
        raise ProviderError('APPLICATION_BINDING_MISMATCH', 409)
    home = private_dir(Path(context['state_dir']) / 'bootstrap' / identifier)
    receipt_path = home / 'teardown.json'
    journal = read_private(receipt_path) if receipt_path.exists() else None
    bound = read_private(home / 'binding.json')
    if any(bound.get(key) != context[key] for key in ('environment_id', 'owner_id', 'account_id')) or bound.get('application_config_hash') != digest(config):
        raise ProviderError('APPLICATION_BINDING_CHANGED', 409)
    if journal and journal.get('status') == 'succeeded':
        return journal
    if journal and journal.get('binding_hash') != digest(bound):
        raise ProviderError('ENVIRONMENT_TEARDOWN_BINDING_CHANGED', 409, True)
    app_root = Path(config['state_dir'])
    for path in app_root.glob('app-*/registration.json'):
        row = read_private(path)
        if row.get('environment_id') == identifier:
            life = path.parent / 'lifecycle.json'
            if not life.exists() or read_private(life).get('status') != 'deleted':
                raise ProviderError('ENVIRONMENT_APPLICATIONS_REMAIN', 409)
    profile = config['environments'][identifier]
    registered = read_private(home / 'cd.json')['targets'][identifier]
    pseudo = {'version': 1, 'application_id': identifier, 'environment_id': identifier,
              'registered': registered, 'cd': config['cd']}
    control = lifecycle.control_for(pseudo)
    registry = read_private(config['registry_file'])
    native, _ = environment.registered_node(registry['targets'][identifier], identifier, 'teardown.' + identifier)
    with environment.runtime_kubectl(native) as kube:
        if journal is None:
            _, policy, renewal, secret, _, _ = environment.shared_cluster_snapshot(config['cd'], registered, identifier)
            if renewal['namespaces'] != [identifier] or any(row['target_id'] != identifier and row['server'] == renewal['server'] for row in policy['targets']):
                raise ProviderError('ENVIRONMENT_SHARED_REGISTRATIONS_REMAIN', 409)
            project = control('get', 'appproject', identifier, '-o', 'json')
            environment.argo.require(project['metadata'].get('labels', {}).get('railshot.io/registration') == identifier
                and project['spec'] == environment.application_project(registered, identifier)['spec'], 'environment project ownership differs')
            app_name = environment.application_name(identifier, identifier, registered['app'])
            environment.argo.require(not control('get', 'application', app_name, '--ignore-not-found', '-o', 'json'),
                                     'bootstrap environment application must be absent')
            expected = {'renewal': renewal, 'shared': None, 'application_name': app_name}
            lifecycle.ci_binding(profile, pseudo)
            lifecycle.remove_renewal(pseudo, expected, inspect=True)
            namespace = kube(identifier, 'get', 'namespace', identifier, '-o', 'json')
            environment.argo.require(namespace['metadata'].get('labels', {}).get('railshot.io/registration') == identifier
                and not namespace['metadata'].get('ownerReferences'), 'bootstrap namespace ownership differs')
            objects = workloads.discover(kube, identifier)
            workloads.ownership(objects, pseudo)
            environment.argo.require(all(item['kind'] in ('ServiceAccount', 'Role', 'RoleBinding', 'Secret', 'ConfigMap') for _, item in objects),
                                     'bootstrap namespace has workloads or storage')
            volumes = kube('default', 'get', 'pv', '-o', 'json')['items']
            environment.argo.require(all(not item.get('spec', {}).get('local') and not item.get('spec', {}).get('hostPath')
                and item.get('spec', {}).get('persistentVolumeReclaimPolicy') == 'Retain' for item in volumes),
                'persistent volumes require verified external Retain policy before environment deletion')
            save(home / 'retained-volumes.json', {'version': 1, 'volumes': volumes})
            journal = {'status': 'unknown', 'environment_id': identifier, 'binding_hash': digest(bound),
                       'expected': expected, 'secret': lifecycle.identity(secret),
                       'project': lifecycle.identity(project, spec=True), 'namespace_uid': namespace['metadata']['uid'], 'completed': []}
            save(receipt_path, journal)
        expected = journal['expected']
        volumes = read_private(home / 'retained-volumes.json')['volumes']
        def step(name, execute):
            if name not in journal['completed']:
                execute()  # Each operation verifies saved identity or already absent state.
                journal['completed'].append(name)
                save(receipt_path, journal)
        step('ci', lambda: lifecycle.unbind_ci(profile, pseudo))
        step('renewal', lambda: lifecycle.remove_renewal(pseudo, expected))
        step('grants', lambda: lifecycle.grant_cleanup(pseudo, expected))
        step('secret', lambda: lifecycle.delete_control(pseudo, 'secret', expected['renewal']['secret'], journal['secret']))
        step('project', lambda: lifecycle.delete_control(pseudo, 'appproject', identifier, journal['project']))
        step('revoke', lambda: lifecycle.revoke_control_grants(pseudo, expected))
        namespace = kube(identifier, 'get', 'namespace', identifier, '--ignore-not-found', '-o', 'json')
        if namespace:
            environment.argo.require(namespace['metadata']['uid'] == journal['namespace_uid']
                and namespace['metadata'].get('labels', {}).get('railshot.io/registration') == identifier,
                'bootstrap namespace identity changed during teardown')
            if not namespace['metadata'].get('deletionTimestamp'):
                # Admission remains fenced; re-check no late workloads before deletion.
                objects = workloads.discover(kube, identifier)
                workloads.ownership(objects, pseudo)
                environment.argo.require(all(item['kind'] in ('ServiceAccount', 'Role', 'RoleBinding', 'Secret', 'ConfigMap') for _, item in objects),
                                         'bootstrap namespace has new workloads or storage')
                kube(identifier, 'delete', '--raw', '/api/v1/namespaces/' + identifier, '-f', '-', document={
                    'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Foreground',
                    'preconditions': {key: namespace['metadata'][key] for key in ('uid', 'resourceVersion')}})
            deadline = time.monotonic() + 120
            while kube(identifier, 'get', 'namespace', identifier, '--ignore-not-found', '-o', 'json'):
                if time.monotonic() >= deadline:
                    raise ProviderError('ENVIRONMENT_NAMESPACE_DELETION_UNVERIFIED', 502, True)
                time.sleep(1)
    if profile['provider'] == 'ncp':
        import ncp_routes
        edge = ncp_routes.teardown(config, identifier)
    else:
        edge = cluster_applications.teardown(config, identifier)
    result = {'status': 'succeeded', 'environment_id': identifier, 'data_policy': 'retain',
              'retained_resources': edge['retained_resources'] + [
                  {'kind': 'persistent_volume', 'id': item['metadata']['name'], 'status': 'retained'} for item in volumes]}
    save(receipt_path, result)
    return result
