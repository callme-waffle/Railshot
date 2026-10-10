#!/usr/bin/env python3
"""Apply and observe reviewed GitOps Applications through an operator kubectl context."""
import argparse
import base64
import copy
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

from handoff import MIGRATION_ANNOTATIONS, database_binding, document_hash, http_path, require, secret_env

KINDS = [{'group': 'apps', 'kind': 'Deployment'}, {'group': '', 'kind': 'Service'},
         {'group': 'networking.k8s.io', 'kind': 'NetworkPolicy'}]
JOB_KIND = {'group': 'batch', 'kind': 'Job'}
PVC_KIND = {'group': '', 'kind': 'PersistentVolumeClaim'}
LABEL = r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?'
SHA = r'[a-f0-9]{40}'


def workload_kinds(work):
    return KINDS + [entry for entry in (JOB_KIND, PVC_KIND)
                    if any(item['kind'] == entry['kind'] for item in work['items'])]


def validate_workload(work, name, namespace, target_id):
    """Bound owned resources, including the single restricted migration Job."""
    kinds = workload_kinds(work)
    require(work.get('apiVersion') == 'v1' and work.get('kind') == 'List' and len(work['items']) == len(kinds) and
            {item['kind'] for item in work['items']} == {item['kind'] for item in kinds},
            'reviewed Deployment, Service, NetworkPolicy and optional migration Job required')
    deployment = next(item for item in work['items'] if item['kind'] == 'Deployment')
    pod = deployment['spec']['template']['spec']
    require(deployment['spec']['template']['metadata']['labels'].get('railshot.io/target') == target_id,
            'workload target differs')
    for item in work['items']:
        annotations = {}
        if item['kind'] == 'NetworkPolicy' and item['metadata'].get('annotations'):
            annotations = {'annotations': {'argocd.argoproj.io/sync-wave': '-2'}}
        item_name = name
        if item['kind'] == 'PersistentVolumeClaim':
            item_name = name + '-data'
            spec = item['spec']
            require(item['apiVersion'] == 'v1' and set(spec) == {'accessModes', 'storageClassName', 'resources'}
                    and spec['accessModes'] == ['ReadWriteOnce'] and spec['storageClassName'] == 'railshot-persistent'
                    and set(spec['resources']) == {'requests'} and set(spec['resources']['requests']) == {'storage'}
                    and re.fullmatch(r'(?:[1-9]|10)Gi', spec['resources']['requests']['storage'])
                    and deployment['spec']['replicas'] == 1 and deployment['spec'].get('strategy') == {'type': 'Recreate'}
                    and {'name': 'data', 'persistentVolumeClaim': {'claimName': item_name}} in pod['volumes']
                    and not any(row['kind'] == 'Job' for row in work['items']), 'restricted persistent claim required')
        if item['kind'] == 'Job':
            item_name = item['metadata']['name']
            require(re.fullmatch(re.escape(name) + r'-migrate-[a-f0-9]{12}', item_name), 'bound migration identity required')
            annotations = {'annotations': MIGRATION_ANNOTATIONS}
            spec = item['spec']; migration = spec['template']['spec']
            require(item['apiVersion'] == 'batch/v1' and set(spec) == {'backoffLimit', 'activeDeadlineSeconds', 'template'} and
                    spec['backoffLimit'] == 0 and spec['activeDeadlineSeconds'] == 300 and
                    set(migration) == set(pod) | {'restartPolicy'} and migration['restartPolicy'] == 'Never' and
                    all(migration[key] == value for key, value in pod.items() if key != 'containers') and
                    len(migration['containers']) == 1, 'restricted same-runtime migration Job required')
            container = migration['containers'][0]; runtime = pod['containers'][0]
            require(set(container) == (set(runtime) - {'ports', 'readinessProbe'}) | {'command'} and
                    all(container[key] == value for key, value in runtime.items()
                        if key not in {'ports', 'readinessProbe', 'command', 'env'}) and
                    isinstance(container['command'], list) and 0 < len(container['command']) <= 20 and
                    all(isinstance(value, str) for value in container['command']),
                    'migration must use the reviewed image and container restrictions')
            require(spec['template']['metadata'] == {'labels': {
                'app.kubernetes.io/name': name, 'railshot.io/target': target_id, 'railshot.io/role': 'migration'}},
                'migration workload labels differ')
        require(item['metadata'] == {'name': item_name, 'namespace': namespace, **annotations},
                'workload namespace/name differs')
    return kinds


def https_url(value):
    parsed = urlsplit(value)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and
            not parsed.query and not parsed.fragment and '*' not in value, 'explicit credential-free HTTPS URL required')
    return value


def load_review(directory):
    directory = Path(directory)
    data = {}
    for name in ('application', 'workload', 'receipt'):
        path = directory / (name + '.json')
        require(path.is_file() and not path.is_symlink() and path.stat().st_size < 2_000_000,
                'small regular reviewed JSON files required')
        data[name] = json.loads(path.read_bytes())
    app, work, receipt = data['application'], data['workload'], data['receipt']
    require(receipt['status'] == 'rendered_for_review' and receipt['deployed'] is False,
            'reviewed handoff receipt required')
    require(receipt['documents'] == {name: document_hash(data[name]) for name in ('application', 'workload')},
            'reviewed document hashes differ')
    require(app['apiVersion'] == 'argoproj.io/v1alpha1' and app['kind'] == 'Application' and
            set(app) == {'apiVersion', 'kind', 'metadata', 'spec'}, 'plain Argo Application required')
    meta, spec = app['metadata'], app['spec']
    require(set(meta) == {'name', 'namespace', 'labels'} and
            all(re.fullmatch(LABEL, meta[k]) for k in ('name', 'namespace')), 'explicit Application identity required')
    require(meta['labels'] == {'app.kubernetes.io/managed-by': 'railshot', 'railshot.io/target': receipt['target_id']},
            'Application owner/target differs')
    require(set(spec) == {'project', 'source', 'destination'} and
            re.fullmatch(LABEL, spec['project']) and spec['project'] != 'default', 'restricted project required')
    source, dest = spec['source'], spec['destination']
    require(set(source) == {'repoURL', 'targetRevision', 'path', 'directory'} and
            source['directory'] == {'recurse': False} and re.fullmatch(SHA, source['targetRevision']),
            'one pinned directory source required')
    https_url(source['repoURL']); https_url(dest['server'])
    path = PurePosixPath(source['path'])
    require(re.fullmatch(r'[A-Za-z0-9_./-]+', source['path']) and not path.is_absolute() and
            str(path) not in ('', '.') and '..' not in path.parts, 'safe relative Git path required')
    require(set(dest) == {'server', 'namespace'} and re.fullmatch(LABEL, dest['namespace']) and
            dest['namespace'] not in {'default', 'kube-system', 'kube-public', 'kube-node-lease', meta['namespace']},
            'dedicated runtime namespace required')
    validate_workload(work, receipt['app'], dest['namespace'], receipt['target_id'])
    job = next((item for item in work['items'] if item['kind'] == 'Job'), None)
    if 'database' in receipt:
        database = database_binding(receipt['database'])
        deployment = next(item for item in work['items'] if item['kind'] == 'Deployment')
        runtime_env = deployment['spec']['template']['spec']['containers'][0]['env']
        require(secret_env('DATABASE_URL', database['runtime_secret'], 'DATABASE_URL') in runtime_env,
                'runtime database Secret differs')
        if job:
            container = job['spec']['template']['spec']['containers'][0]
            require(receipt['migration'] == {'name': job['metadata']['name'], 'image': container['image']} and
                    [entry for entry in container['env'] if entry['name'] in ('DATABASE_URL', 'MIGRATION_DATABASE_URL')] == [
                        secret_env(key, database['migration_secret'], 'MIGRATION_DATABASE_URL')
                        for key in ('DATABASE_URL', 'MIGRATION_DATABASE_URL')], 'migration database Secret differs')
    require(not job or receipt.get('migration'), 'migration receipt required')
    require(receipt['http']['path_mode'] == 'preserve', 'HTTP path rewriting is unsupported')
    http_path(receipt['http']['route']); http_path(receipt['http']['health_path'])
    return data


def native(args, *, document=None, timeout=30):
    """Capture native output. Never echo commands, stdin or tool diagnostics."""
    try:
        result = subprocess.run(args, input=None if document is None else json.dumps(document), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('native operation did not complete; observe before retrying') from exc
    require(result.returncode == 0, 'native operation failed; no completion is claimed')
    return result.stdout


def kubectl(context, namespace, *args, document=None):
    require(isinstance(context, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,200}', context),
            'explicit operator kubectl context required')
    raw = native(['kubectl', '--context', context, '--request-timeout=20s', '-n', namespace, *args], document=document)
    return json.loads(raw) if raw.strip() else None


def verify_git(review, repository):
    source = review['application']['spec']['source']
    prefix = ['git', '-C', str(repository)]
    remote = native([*prefix, 'remote', 'get-url', 'origin']).strip()
    https_url(remote)
    require(remote.rstrip('/').removesuffix('.git') == source['repoURL'].rstrip('/').removesuffix('.git'),
            'Git origin differs from reviewed source')
    tree = source['targetRevision'] + ':' + source['path']
    require(native([*prefix, 'ls-tree', '--name-only', tree]).splitlines() == ['workload.json'],
            'reviewed Git directory must contain only workload.json')
    stored = json.loads(native([*prefix, 'show', tree + '/workload.json']))
    require(document_hash(stored) == review['receipt']['documents']['workload'], 'pinned Git workload differs from review')


def projects(reviews):
    result = {}
    for review in reviews:
        app = review['application']; spec = app['spec']
        key = (app['metadata']['namespace'], spec['project'])
        project = result.setdefault(key, {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'AppProject',
            'metadata': {'name': key[1], 'namespace': key[0]},
            'spec': {'sourceRepos': [], 'destinations': [], 'clusterResourceWhitelist': [],
                     'namespaceResourceWhitelist': copy.deepcopy(KINDS)}})
        if spec['source']['repoURL'] not in project['spec']['sourceRepos']:
            project['spec']['sourceRepos'].append(spec['source']['repoURL'])
        if spec['destination'] not in project['spec']['destinations']:
            project['spec']['destinations'].append(copy.deepcopy(spec['destination']))
        for kind in workload_kinds(review['workload']):
            if kind not in project['spec']['namespaceResourceWhitelist']:
                project['spec']['namespaceResourceWhitelist'].append(copy.deepcopy(kind))
    return {'apiVersion': 'v1', 'kind': 'List', 'items': list(result.values())}


def validate_project(project, app, workload=None):
    spec, desired = project['spec'], app['spec']
    require(project['metadata']['name'] == desired['project'] and
            project['metadata']['namespace'] == app['metadata']['namespace'], 'live project identity differs')
    permitted = sorted(spec.get('namespaceResourceWhitelist', []), key=lambda x: x['kind'])
    expected = workload_kinds(workload) if workload else KINDS
    require(spec.get('clusterResourceWhitelist', []) == [] and
            permitted in [sorted(KINDS + extra, key=lambda x: x['kind'])
                          for extra in ([], [JOB_KIND], [PVC_KIND], [JOB_KIND, PVC_KIND])] and
            all(kind in permitted for kind in expected) and
            not spec.get('namespaceResourceBlacklist'), 'project must restrict resources to the reviewed workload kinds')
    require(isinstance(spec.get('sourceRepos'), list) and desired['source']['repoURL'] in spec['sourceRepos'],
            'project does not allow this repository')
    for repo in spec['sourceRepos']:
        https_url(repo)
    require(isinstance(spec.get('destinations'), list) and desired['destination'] in spec['destinations'],
            'project does not allow this runtime server and namespace')
    for dest in spec['destinations']:
        require(set(dest) == {'server', 'namespace'} and re.fullmatch(LABEL, dest['namespace']) and
                dest['namespace'] not in {'default', 'kube-system', 'kube-public', 'kube-node-lease', app['metadata']['namespace']},
                'project has a broad or privileged destination')
        https_url(dest['server'])


def validate_live(live, expected, *, revision=True):
    meta, spec = live['metadata'], live['spec']
    require(meta['name'] == expected['metadata']['name'] and meta['namespace'] == expected['metadata']['namespace'] and
            not meta.get('ownerReferences') and all(meta.get('labels', {}).get(k) == v
                                                   for k, v in expected['metadata']['labels'].items()),
            'live Application is not owned by this target')
    require(spec['project'] == expected['spec']['project'] and spec['destination'] == expected['spec']['destination'] and
            set(spec) <= {'project', 'source', 'destination', 'syncPolicy'} and not spec.get('syncPolicy'),
            'live Application destination/policy differs')
    desired = expected['spec']['source']; source = spec['source']
    keys = ('repoURL', 'path', 'targetRevision') if revision else ('repoURL', 'path')
    require(all(source.get(k) == desired[k] for k in keys) and
            set(source) <= {'repoURL', 'path', 'targetRevision', 'directory'} and
            set(source.get('directory', {})) <= {'recurse'} and not source.get('directory', {}).get('recurse'),
            'live Application source differs')


def observe(review, live):
    app, receipt = review['application'], review['receipt']
    validate_live(live, app)
    status = live.get('status', {}); sync = status.get('sync', {}); operation = status.get('operationState', {})
    revision = app['spec']['source']['targetRevision']; compared = sync.get('comparedTo', {})
    compared_source = compared.get('source', {})
    binding = (compared.get('destination') == app['spec']['destination'] and all(
        compared_source.get(k) == app['spec']['source'][k] for k in ('repoURL', 'path', 'targetRevision')))
    expected_resources = {(item['apiVersion'].split('/')[0] if '/' in item['apiVersion'] else '', item['kind'],
                           item['metadata']['namespace'], item['metadata']['name']) for item in review['workload']['items']}
    resources = status.get('resources', [])
    def resource_key(row):
        return (row.get('group', ''), row.get('kind'), row.get('namespace'), row.get('name'))
    current = [row for row in resources if resource_key(row) in expected_resources]
    retained = [row for row in resources if resource_key(row) not in expected_resources]
    # Argo sets requiresPruning only for live objects tracked by this Application.
    # IgnoreExtraneous leaves these old Jobs OutOfSync individually, but preserves their health.
    retained_ok = all(resource_key(row)[:3] == ('batch', 'Job', app['spec']['destination']['namespace']) and
                      re.fullmatch(re.escape(receipt['app']) + r'-migrate-[a-f0-9]{12}', row.get('name', '')) and
                      row.get('requiresPruning') is True and not row.get('hook') and
                      row.get('status') == 'OutOfSync' and row.get('health', {}).get('status') == 'Healthy'
                      for row in retained)
    # Argo 3 defaults to aggregate Application health; per-resource health may be absent.
    resources_ok = (retained_ok and {resource_key(row) for row in current} == expected_resources and
                    len({resource_key(row) for row in resources}) == len(resources) and
                    all(row.get('status') == 'Synced' and not row.get('requiresPruning') for row in current) and
                    all(not row.get('health') or row['health'].get('status') == 'Healthy' for row in resources))
    deployment = next(item for item in review['workload']['items'] if item['kind'] == 'Deployment')
    images = {c['image'] for c in deployment['spec']['template']['spec']['containers']}
    sync_result = operation.get('syncResult', {})
    job = next((item for item in review['workload']['items'] if item['kind'] == 'Job'), None)
    # A successful ordered sync must contain this exact Job, not a previous migration.
    migration_results = [row for row in sync_result.get('resources', []) if job and
        resource_key(row) == ('batch', 'Job', job['metadata']['namespace'], job['metadata']['name'])]
    migration_ok = not job or len(migration_results) == 1 and migration_results[0].get('status') == 'Synced'
    summary = status.get('summary', {})
    observed_images = summary.get('images', [])
    # A summary can include retained migration images. Bind the current Deployment instead.
    # ResourceResult.images is part of Argo's syncResult API; absent evidence stays unverified.
    if retained or 'images' not in summary:
        observed_images = []
        matches = [row for row in sync_result.get('resources', [])
                   if resource_key(row) == ('apps', 'Deployment', deployment['metadata']['namespace'], deployment['metadata']['name'])]
        if sync_result.get('revision') == revision and len(matches) == 1 and matches[0].get('status') == 'Synced':
            observed_images = matches[0].get('images', [])
    errors = any(c.get('type', '').endswith('Error') for c in status.get('conditions', []))
    complete = (not live.get('operation') and not errors and binding and resources_ok and migration_ok and
                sync.get('revision') == revision and sync.get('status') == 'Synced' and
                status.get('health', {}).get('status') == 'Healthy' and operation.get('phase') == 'Succeeded' and
                sync_result.get('revision') == revision and images == set(observed_images))
    failed = errors or (not live.get('operation') and operation.get('phase') in {'Failed', 'Error'} and
                        operation.get('syncResult', {}).get('revision') == revision)
    return {'status': 'deployed' if complete else 'failed' if failed else 'progressing', 'deployed': bool(complete),
            'application': app['metadata']['name'], 'target_id': receipt['target_id'],
            'namespace': app['spec']['destination']['namespace'], 'git_revision': revision,
            'observed_revision': sync.get('revision'), 'sync': sync.get('status'),
            'health': status.get('health', {}).get('status'), 'source_commit': receipt['source_commit'],
            'run_id': receipt['run_id'], 'producer_attempt': receipt['producer_attempt'],
            'bundle_artifact_id': receipt['bundle_artifact_id'],
            **({'migration': {'name': job['metadata']['name'], 'state': 'succeeded' if complete else 'unverified'}} if job else {}),
            'http': receipt['http'], 'public_verified': False, 'url': None}


def deploy(review, context, *, sync, timeout):
    app = review['application']; namespace = app['metadata']['namespace']; name = app['metadata']['name']
    live = kubectl(context, namespace, 'get', 'application', name, '--ignore-not-found', '-o', 'json')
    if live:
        validate_live(live, app, revision=not sync)
    if sync:
        # A repeated request for an already verified revision is read-only.
        # This also avoids re-running migration Jobs for a completed deployment.
        if live and live['spec']['source']['targetRevision'] == app['spec']['source']['targetRevision']:
            observed = observe(review, live)
            if observed['deployed']:
                return observed
        if live and live.get('operation'):
            validate_live(live, app)
        if not live or not live.get('operation'):
            kubectl(context, namespace, 'apply', '--server-side', '--field-manager=railshot-argocd', '-f', '-', '-o', 'json', document=app)
            # Argo owns workload application. The native operation requests one exact revision, without prune/force.
            patch = {'operation': {'initiatedBy': {'username': 'railshot'}, 'sync': {
                'revision': app['spec']['source']['targetRevision'], 'prune': False, 'syncStrategy': {'apply': {}}}}}
            kubectl(context, namespace, 'patch', 'application', name, '--type=merge', '--patch-file=/dev/stdin', '-o', 'json', document=patch)
    deadline = time.monotonic() + timeout
    while True:
        live = kubectl(context, namespace, 'get', 'application', name, '-o', 'json')
        result = observe(review, live)
        if result['status'] != 'progressing' or time.monotonic() >= deadline:
            return result
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def register_cluster(reviews, context, config, *, application_credential=False):
    app = reviews[0]['application']; target = reviews[0]['receipt']['target_id']
    namespace, project, server = app['metadata']['namespace'], app['spec']['project'], app['spec']['destination']['server']
    require(all(r['receipt']['target_id'] == target and r['application']['metadata']['namespace'] == namespace and
                r['application']['spec']['project'] == project and r['application']['spec']['destination']['server'] == server
                for r in reviews), 'register one target/project at a time')
    require(set(config) == {'bearerToken', 'tlsClientConfig'} and isinstance(config['bearerToken'], str) and
            0 < len(config['bearerToken']) < 32768 and not re.search(r'\s', config['bearerToken']), 'scoped bearer credential required on stdin')
    tls = config['tlsClientConfig']
    require(set(tls) <= {'caData', 'insecure', 'serverName'} and tls.get('insecure') is False and
            b'-----BEGIN CERTIFICATE-----' in base64.b64decode(tls['caData'], validate=True), 'verified cluster CA required')
    namespaces = sorted({r['application']['spec']['destination']['namespace'] for r in reviews})
    require(type(application_credential) is bool, 'explicit application credential mode required')
    if application_credential:
        require(re.fullmatch(r'app-[a-f0-9]{24}', target) and project == target and namespaces == [target],
                'application credential must retain its exact project and namespace')
    strings = {'name': target, 'server': server, 'namespaces': ','.join(namespaces), 'clusterResources': 'false',
               'project': project, 'config': json.dumps(config, separators=(',', ':'))}
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque', 'metadata': {
        'name': 'railshot-' + target, 'namespace': namespace, 'labels': {
            'argocd.argoproj.io/secret-type': 'railshot-application' if application_credential else 'cluster',
            'app.kubernetes.io/managed-by': 'railshot'}},
        'data': {key: base64.b64encode(value.encode()).decode() for key, value in strings.items()}}
    existing = kubectl(context, namespace, 'get', 'secret', secret['metadata']['name'], '--ignore-not-found', '-o', 'json')
    if existing:
        labels = existing['metadata'].get('labels', {})
        allowed_type = ('cluster', 'railshot-application') if application_credential else ('cluster',)
        require(labels.get('app.kubernetes.io/managed-by') == 'railshot'
                and labels.get('argocd.argoproj.io/secret-type') in allowed_type and
                all(existing['data'].get(k) == secret['data'][k] for k in ('server', 'project', 'name')),
                'existing registration belongs to another owner or target')
        require(set(base64.b64decode(existing['data']['namespaces']).decode().split(',')) <= set(namespaces),
                'include all existing registered namespaces; do not detach active apps')
        if application_credential:
            require(existing['kind'] == 'Secret' and existing['metadata']['name'] == secret['metadata']['name']
                    and existing['metadata']['namespace'] == namespace and not existing['metadata'].get('ownerReferences')
                    and existing['data']['namespaces'] == secret['data']['namespaces']
                    and existing['data']['clusterResources'] == secret['data']['clusterResources'],
                    'existing application credential scope differs')
            previous = json.loads(base64.b64decode(existing['data']['config'], validate=True))
            require(set(previous) == {'bearerToken', 'tlsClientConfig'} and previous['tlsClientConfig'] == tls
                    and isinstance(previous['bearerToken'], str) and 0 < len(previous['bearerToken']) < 32768
                    and not re.search(r'\s', previous['bearerToken']), 'existing application credential TLS or token differs')
            # Relabeling leaves the existing SA credential and renewal binding intact.
            secret['data'] = copy.deepcopy(existing['data'])
    if application_credential and existing:
        if labels['argocd.argoproj.io/secret-type'] == 'cluster':
            patch = [{'op': 'test', 'path': '/metadata/uid', 'value': existing['metadata']['uid']},
                     {'op': 'test', 'path': '/metadata/resourceVersion', 'value': existing['metadata']['resourceVersion']},
                     {'op': 'replace', 'path': '/metadata/labels/argocd.argoproj.io~1secret-type', 'value': 'railshot-application'}]
            kubectl(context, namespace, 'patch', 'secret', secret['metadata']['name'], '--type=json',
                    '--patch-file=/dev/stdin', '-o', 'json', document=patch)
    else:
        kubectl(context, namespace, 'apply', '--server-side', '--field-manager=railshot-argocd', '-f', '-', '-o', 'json', document=secret)
    observed = kubectl(context, namespace, 'get', 'secret', secret['metadata']['name'], '-o', 'json')
    require(observed.get('data') == secret['data'] and all(
        observed.get('metadata', {}).get('labels', {}).get(k) == v for k, v in secret['metadata']['labels'].items()),
        'cluster registration readback differs')
    if application_credential and existing:
        require(observed['metadata'].get('uid') == existing['metadata'].get('uid'), 'application credential was replaced')
    return {'status': 'application_credential_registered' if application_credential else 'cluster_registered',
            'target_id': target, 'namespaces': namespaces, 'deployed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('project', 'register-cluster', 'sync', 'verify'))
    parser.add_argument('reviews', nargs='+', type=Path)
    parser.add_argument('--context', help='operator context; credentials stay in private kubeconfig')
    parser.add_argument('--repo', type=Path, help='config Git checkout containing the exact reviewed commit')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    try:
        require(0 <= args.timeout <= 1800, 'timeout must be between 0 and 1800 seconds')
        reviews = [load_review(path) for path in args.reviews]
        identities = [(r['application']['metadata']['namespace'], r['application']['metadata']['name']) for r in reviews]
        require(len(set(identities)) == len(identities), 'duplicate Application inputs')
        ports = [(r['application']['spec']['destination']['server'], r['receipt']['http']['node_port']) for r in reviews]
        require(len(set(ports)) == len(ports), 'allocate different NodePorts to apps sharing one cluster')
        if args.command == 'project':
            result = projects(reviews)
        else:
            require(args.context, 'operator context required')
            for review in reviews:
                app = review['application']
                live_project = kubectl(args.context, app['metadata']['namespace'], 'get', 'appproject', app['spec']['project'], '-o', 'json')
                validate_project(live_project, app, review['workload'])
            if args.command == 'register-cluster':
                raw = sys.stdin.read(131073)
                require(len(raw) <= 131072, 'cluster credential input is too large')
                result = register_cluster(reviews, args.context, json.loads(raw))
            else:
                require(args.repo, 'reviewed config Git checkout required')
                for review in reviews:
                    verify_git(review, args.repo)
                results = []
                for review in reviews:
                    try:
                        results.append(deploy(review, args.context, sync=args.command == 'sync', timeout=args.timeout))
                    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                        results.append({'status': 'blocked', 'deployed': False,
                                        'application': review['application']['metadata']['name'], 'reason': str(exc)})
                result = {'status': 'deployed' if all(r['deployed'] for r in results) else 'incomplete',
                          'deployed': all(r['deployed'] for r in results), 'applications': results}
        print(json.dumps(result, indent=2))
        return 0 if result.get('status') != 'incomplete' else 1
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        print(json.dumps({'status': 'blocked', 'deployed': False, 'reason': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
