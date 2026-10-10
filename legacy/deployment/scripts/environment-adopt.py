#!/usr/bin/env python3
"""Explicitly adopt an existing healthy runtime; never install or rotate credentials.

Plan records exact object UID/resourceVersion, content hashes and original labels.
Apply requires its operator-approved digest and changes only those objects' ownership
labels with Kubernetes JSON Patch preconditions. Any uncertain write needs operator
recovery; a partial attempt is never automatically replayed.
"""
import argparse
import base64
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

import environment as env
import runtime_upgrade as runtime

ROOT = Path(__file__).resolve().parents[2]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


update = module('adoption_runtime_update', 'deployment/scripts/runtime-update.py')
observer = module('adoption_observer', 'observability/register.py')


def canonical_path(value):
    path = Path(value)
    env.argo.require(path.is_absolute() and path.resolve() == path and str(path) != '/', 'absolute canonical operator path required')
    return path


def load(refs):
    env.argo.require(set(refs) == {'registry', 'target_id', 'config', 'registration_dir', 'policy_file', 'binding'}, 'adoption references differ')
    for key in ('registry', 'config', 'registration_dir', 'policy_file'):
        canonical_path(refs[key])
    if refs['binding']:
        canonical_path(refs['binding'])
    configuration = env.read_private(refs['config'])
    data = (update.load_node_configuration(env.read_private(refs['registry']), refs['target_id'], configuration, refs['binding'])
            if update.node_only(configuration) else env.load(refs['registry'], refs['target_id'], refs['config'], refs['binding']))
    request, cd, settings, pull, binding, identity = data
    env.argo.require(not settings.get('edge_config_file'), 'adoption requires the existing allocated public URL without edge allocation')
    env.argo.require(settings.get('observability_config_file'), 'existing observer configuration required')
    observation = observer.settings(env.read_private(settings['observability_config_file']))
    identity['environment_id'] = Path(refs['registration_dir']).name
    policy = env.read_private(refs['policy_file'])
    # Validate the approved baseline independently of the current release's desired policy.
    runtime.validate({'version': 1, 'source_sha': '0' * 40, 'from_policy': policy, 'to_policy': policy,
                      'from_policy_sha256': runtime.digest(policy), 'to_policy_sha256': runtime.digest(policy)})
    return data, observation, policy


def unclaimed(refs, settings, resource):
    home = canonical_path(refs['registration_dir'])
    for name in ('registration.json', 'cd.json', 'adoption-receipt.json', 'runtime-release.json'):
        env.argo.require(not (home / name).exists(), 'registration/adoption state already exists; operator recovery required')
    shared = canonical_path(settings['state_dir'])
    env.argo.require(not (shared / (refs['target_id'] + '.json')).exists(), 'target is already claimed')
    if shared.exists():
        for path in shared.glob('*.json'):
            env.argo.require(env.read_private(path).get('resource_id') != resource, 'runtime resource is already claimed')


def content_hash(obj):
    value = copy.deepcopy(obj); value.pop('status', None)
    value['metadata'] = {k: v for k, v in value['metadata'].items()
                         if k not in ('labels', 'resourceVersion', 'managedFields')}
    return runtime.digest(value)


def snapshot(kube, scope, kind, namespace, name, desired=None, allow_absent=False):
    obj = kube(namespace, 'get', kind, name, *(['--ignore-not-found'] if allow_absent else []), '-o', 'json')
    if obj is None:
        env.argo.require(allow_absent, 'existing object required')
        return {'scope': scope, 'kind': kind, 'namespace': namespace, 'name': name,
                'absent': True, 'desired_labels': None}, None
    meta = obj['metadata']; original = meta.get('labels', {})
    env.argo.require(meta['name'] == name and meta.get('namespace', 'default') == namespace
                     and isinstance(meta.get('uid'), str) and meta['uid']
                     and isinstance(meta.get('resourceVersion'), str) and meta['resourceVersion'], 'existing object identity required')
    if desired is not None:
        env.argo.require(not meta.get('ownerReferences') and not meta.get('deletionTimestamp'), 'owned or terminating object cannot be adopted')
        env.argo.require(all(key not in original or original[key] == value for key, value in desired.items()),
                         'existing manager or owner label differs')
    return {'scope': scope, 'kind': kind, 'namespace': namespace, 'name': name,
            'uid': meta['uid'], 'resource_version': meta['resourceVersion'], 'labels_present': 'labels' in meta,
            'original_labels': original, 'desired_labels': desired,
            'content_sha256': content_hash(obj)}, obj


def capture(refs, loaded):
    (request, cd, settings, pull, binding, identity), observation, policy = loaded
    if update.node_only(identity):
        with update.connection(request) as prefix:
            observed = update.node_call(prefix, {'action': 'inspect', 'include_ca': True})
        env.argo.require(runtime.matches(observed, policy), 'live runtime differs from adoption baseline')
        management = {**identity['management'], 'ca_data': observed['management_ca_data'],
                      'ca_sha256': hashlib.sha256(base64.b64decode(observed['management_ca_data'], validate=True)).hexdigest()}
        bound = update.node_identity(identity, observed, management)
        health = update.node_management_health({'management': management,
            **{key: observed[key] for key in ('node_uid', 'node_ip', 'architecture')}}, observed)
        # No application/observer object is claimed or created by node-only adoption.
        # The common release separately reconciles observer ownership and real collection.
        return {'version': 2, 'scope': 'node-only', 'input_sha256': env.argo.document_hash(bound),
                'target_id': refs['target_id'], 'provider': request['target']['provider'],
                'resource_id': identity['descriptor']['resource_id'], 'environment_id': identity['environment_id'],
                'policy_sha256': runtime.digest(policy), 'observer_config_sha256': runtime.digest(observation),
                'management': management, 'runtime': update.public_runtime(observed), 'objects': []}, {
                'management': health, 'observability': {'registered': False, 'collection_state': 'pending', 'reconciliation_required': True},
                'application': {'status': 'not_applicable', 'reason': 'node-only'},
                'public_http': {'status': 'not_applicable', 'reason': 'node-only'}}
    target_id = refs['target_id']; registered = cd['targets'][target_id]; target = registered['target']
    input_sha = env.argo.document_hash(identity); owner = input_sha[:32]
    labels = {'app.kubernetes.io/managed-by': 'railshot', 'railshot.io/registration': owner}
    objects = []
    control = lambda namespace, *args, **kwargs: env.argo.kubectl(cd['context'], namespace, *args, **kwargs)
    def take(kube, scope, kind, namespace, name, desired=None, allow_absent=False):
        row, obj = snapshot(kube, scope, kind, namespace, name, desired, allow_absent)
        objects.append(row)
        return obj
    with update.connection(request) as prefix:
        observed = update.node_call(prefix, {'action': 'inspect'})
    env.argo.require(observed['node_ip'] == identity['descriptor']['addresses']['private']
                     and observed['architecture'] == 'amd64' and runtime.matches(observed, policy), 'live runtime differs from adoption baseline')
    cm = take(control, 'control', 'ConfigMap', 'argocd', 'railshot-credentials')
    renewals = env.credentials.validate_policy(json.loads(cm['data']['policy.json']))
    rows = [r for r in renewals['targets'] if r['target_id'] == target_id]
    env.argo.require(len(rows) == 1, 'one existing renewal registration required')
    renewal = rows[0]
    env.argo.require('previous_scope' not in renewal, 'environment cluster transition requires reconciliation')
    exact_scope = renewal['project'] == target['project'] and renewal['namespaces'] == [target['namespace']]
    shared_scope = (not target_id.startswith('app-') and renewal['project'] == ''
                    and target['namespace'] in renewal['namespaces'])
    env.argo.require(renewal['server'] == target['cluster_server'] and (exact_scope or shared_scope),
                     'existing management scope differs')
    expected_name = identity['descriptor']['addresses']['private'] if identity['descriptor'].get('management_endpoint') else None
    env.argo.require(renewal.get('tls_server_name') == expected_name, 'registered management TLS name differs')
    secret = take(control, 'control', 'Secret', 'argocd', renewal['secret'], env.credentials.LABELS)
    checked_secret = copy.deepcopy(secret)
    checked_secret['metadata'].setdefault('labels', {}).update(env.credentials.LABELS)
    credential, ca = env.credentials.registration(checked_secret, renewal, time.time())
    cron = take(control, 'control', 'CronJob', 'argocd', 'railshot-credentials')
    pod = cron['spec']['jobTemplate']['spec']['template']['spec']
    env.argo.require(not cron['spec'].get('suspend') and pod['serviceAccountName'] == 'railshot-credentials'
                     and pod['containers'][0]['command'][:2] == ['python3', '/app/gitops/credentials.py'], 'active existing renewal worker required')
    role = take(control, 'control', 'Role', 'argocd', 'railshot-credentials')
    env.argo.require(len(role['rules']) == 1 and role['rules'][0]['apiGroups'] == ['']
                     and role['rules'][0]['resources'] == ['secrets'] and role['rules'][0]['verbs'] == ['get', 'patch']
                     and renewal['secret'] in role['rules'][0]['resourceNames'], 'existing renewal permissions differ')
    project = take(control, 'control', 'AppProject', 'argocd', target['project'], labels)
    env.argo.require(project['spec']['destinations'] == [{'server': target['cluster_server'], 'namespace': target['namespace']}]
                     and project['spec']['sourceRepos'] == [target['repo_url']]
                     and not project['spec'].get('clusterResourceWhitelist'), 'dedicated existing Argo project required')
    app_name = env.application_name(target_id, target['namespace'], registered['app'])
    app = take(control, 'control', 'Application', 'argocd', app_name)
    env.argo.require(app['spec']['project'] == target['project']
                     and app['spec']['destination'] == {'server': target['cluster_server'], 'namespace': target['namespace']}
                     and app['spec']['source']['repoURL'] == target['repo_url'] and app['spec']['source']['path'] == target['path']
                     and app['status']['health']['status'] == 'Healthy' and app['status']['sync']['status'] == 'Synced',
                     'existing Argo application binding or health differs')
    observed_files = {}
    observation_request = {'version': 1, 'target_id': target_id, 'environment_id': identity['environment_id'],
        'app': registered['app'], 'namespace': target['namespace'], 'node_ip': observed['node_ip'],
        'probe_url': registered['public_http']['url'], 'registry_file': refs['registry'], 'context': cd['context']}
    row, rendering = observer.registration_row(observation, observation_request, identity['descriptor'])
    for name in ('desired.json', 'product.json'):
        path = Path(observation['state_dir']) / name
        if path.exists():
            document = env.read_private(path)
            observer.merge_rows(document['targets'], row)
            observed_files[str(path)] = runtime.digest(document)
    with env.runtime_kubectl(request) as kube:
        runtime_objects = {}
        for document in env.runtime_documents(target, owner, pull, binding):
            meta = document['metadata']; namespace = meta.get('namespace', 'default')
            runtime_objects[document['kind'], meta['name']] = take(kube, 'runtime', document['kind'], namespace, meta['name'], labels)
        sa = runtime_objects['ServiceAccount', env.SA]
        env.argo.require(renewal['service_account'] == {'name': env.SA, 'namespace': target['namespace'], 'uid': sa['metadata']['uid']},
                         'runtime ServiceAccount UID differs from the existing credential')
        local_ca = kube(target['namespace'], 'config', 'view', '--raw', '--minify', '-o', 'json')
        env.argo.require(base64.b64decode(local_ca['clusters'][0]['cluster']['certificate-authority-data'], validate=True) == ca,
                         'management CA differs from the runtime CA')
        service = take(kube, 'runtime', 'Service', target['namespace'], registered['app'])
        env.argo.require(service['spec']['selector'].get('railshot.io/target') == target_id
                         and any(p.get('nodePort') == target['node_port'] for p in service['spec']['ports']), 'application service binding differs')
        take(kube, 'runtime', 'Deployment', target['namespace'], registered['app'])
        application = update.app_health(kube, registered, target_id)
        observer_labels = {'app.kubernetes.io/managed-by': 'railshot-observer', 'railshot.io/observer': observation['owner']}
        missing = []
        for document in observer.cluster(rendering)['items']:
            meta = document['metadata']
            obj = take(kube, 'runtime', document['kind'], meta.get('namespace', 'default'), meta['name'], observer_labels, allow_absent=True)
            if obj is None:
                missing.append({'kind': document['kind'], 'namespace': meta.get('namespace', 'default'), 'name': meta['name']})
        observer_health = ({'exporters_ready': False, 'missing': missing} if missing else update.observer_health(kube))
        observer_health.update(collection_state='pending', reconciliation_required=True)
    pods = env.credentials.customer(renewal['server'], ca, credential['bearerToken'],
        '/api/v1/namespaces/' + target['namespace'] + '/pods?limit=1',
        **({'server_name': renewal['tls_server_name']} if 'tls_server_name' in renewal else {}))
    env.argo.require(pods.get('kind') == 'PodList' and isinstance(pods.get('items'), list), 'management TLS read failed')
    public = registered['public_http']
    probe = env.bridge.public_probe(public, urlsplit(public['url']).path)
    env.argo.require(probe['state'] == 'succeeded', 'existing public HTTPS health unverified')
    return {'input_sha256': input_sha, 'target_id': target_id, 'provider': request['target']['provider'],
            'resource_id': identity['descriptor']['resource_id'], 'environment_id': identity['environment_id'],
            'policy_sha256': runtime.digest(policy), 'observer_config_sha256': runtime.digest(observation),
            'observer_files': observed_files, 'runtime': observed, 'objects': objects}, {
            'application': application, 'observability': observer_health,
            'management': {'tls_verified': True, 'read_verified': True, 'ca_sha256': hashlib.sha256(ca).hexdigest()},
            'public_http': probe}


def plan(refs, output):
    loaded = load(refs)
    unclaimed(refs, loaded[0][2], loaded[0][5]['descriptor']['resource_id'])
    snapshot_value, health = capture(refs, loaded)
    document = {'version': 1, 'created_at': time.time(), 'refs': refs, 'snapshot': snapshot_value, 'health': health}
    output = canonical_path(output)
    env.argo.require(not output.exists(), 'adoption plans are immutable; select a new private output file')
    env.bridge.private_directory(output.parent)
    # O_EXCL prevents replacing an approved plan even if another planner won the race.
    with os.fdopen(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'w') as stream:
        json.dump(document, stream); stream.flush(); os.fsync(stream.fileno())
    return {'status': 'planned', 'plan_sha256': runtime.digest(document), 'target_id': refs['target_id'],
            'provider': snapshot_value['provider'], 'object_count': len(snapshot_value['objects']), 'expires_in_seconds': 900}


def label_patch(row):
    if row.get('absent'):
        return []
    merged = {**row['original_labels'], **(row['desired_labels'] or {})}
    if merged == row['original_labels']:
        return []
    result = [{'op': 'test', 'path': '/metadata/uid', 'value': row['uid']},
              {'op': 'test', 'path': '/metadata/resourceVersion', 'value': row['resource_version']}]
    if row['labels_present']:
        result.append({'op': 'test', 'path': '/metadata/labels', 'value': row['original_labels']})
    result.append({'op': 'add', 'path': '/metadata/labels', 'value': merged})
    return result


def readback(kube, row):
    current, obj = snapshot(kube, row['scope'], row['kind'], row['namespace'], row['name'], allow_absent=row.get('absent', False))
    if row.get('absent'):
        env.argo.require(obj is None, 'previously absent observer object appeared; adoption did not create it')
        return None
    expected = {**row['original_labels'], **(row['desired_labels'] or {})}
    env.argo.require(current['uid'] == row['uid'] and current['content_sha256'] == row['content_sha256']
                     and current['original_labels'] == expected, 'adopted object readback differs')
    return current['resource_version']


def apply(plan_file, expected_digest):
    document = env.read_private(plan_file)
    env.argo.require(re.fullmatch('[a-f0-9]{64}', expected_digest or '')
                     and runtime.digest(document) == expected_digest, 'approved adoption plan digest differs')
    env.argo.require(set(document) == {'version', 'created_at', 'refs', 'snapshot', 'health'}
                     and document['version'] == 1 and 0 <= time.time() - document['created_at'] <= 900, 'adoption plan expired or invalid')
    refs = document['refs']; loaded = load(refs)
    request, cd, settings, pull, binding, identity = loaded[0]
    home = env.bridge.private_directory(refs['registration_dir'])
    shared = env.bridge.private_directory(settings['state_dir'])
    with os.fdopen(os.open(shared / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        unclaimed(refs, settings, identity['descriptor']['resource_id'])
        fresh, health = capture(refs, loaded)
        env.argo.require(fresh == document['snapshot'], 'adoption inputs or live UID/resourceVersion/content changed; make a new plan')
        record_path = home / 'registration.json'; receipt_path = home / 'adoption-receipt.json'
        record = {'status': 'unknown', 'input_sha256': fresh['input_sha256'], 'target_id': refs['target_id'],
                  'environment_id': fresh['environment_id'], 'provider_kind': fresh['provider'], 'deployment_supported': False,
                  'steps': [], 'adoption_plan_sha256': expected_digest}
        receipt = {'status': 'running', 'target_id': refs['target_id'], 'provider': fresh['provider'],
                   'plan_sha256': expected_digest, 'automatic_rollback': False, 'patched': [], 'health': health}
        # Reserve ownership only after current health and every pinned object were verified.
        env.save(shared / (refs['target_id'] + '.json'), {'input_sha256': fresh['input_sha256'],
            'resource_id': fresh['resource_id'], 'environment_id': fresh['environment_id']})
        env.save(record_path, record); env.save(receipt_path, receipt)
        if update.node_only(fresh):
            try:
                # Local claims only: repeat identity/TLS readback after reservation.
                after, checked_health = capture(refs, loaded)
                env.argo.require(after == fresh, 'node identity or management CA changed during adoption')
                record.pop('deployment_supported')
                record.update(version=2, scope='node-only', status='succeeded', resource_id=fresh['resource_id'],
                    **{key: fresh['runtime'][key] for key in ('node_uid', 'node_ip', 'architecture')},
                    management=fresh['management'], baseline_policy_sha256=fresh['policy_sha256'],
                    observability=checked_health['observability'], steps=['existing_node_identity_verified', 'local_ownership_claimed', 'management_tls_verified'])
                env.save(record_path, record)
                receipt.update(status='verified', scope='node-only', health=checked_health, verified_at=datetime.now(timezone.utc).isoformat())
                env.save(receipt_path, receipt)
            except Exception as error:
                receipt.update(status='recovery_required', error_type=type(error).__name__)
                env.save(receipt_path, receipt)
            return {key: receipt[key] for key in ('status', 'target_id', 'provider', 'plan_sha256')}
        control = lambda namespace, *args, **kwargs: env.argo.kubectl(cd['context'], namespace, *args, **kwargs)
        try:
            with env.runtime_kubectl(request) as kube:
                for row in fresh['objects']:
                    client = kube if row['scope'] == 'runtime' else control
                    patch = label_patch(row)
                    if patch:
                        receipt['pending'] = {key: row[key] for key in ('scope', 'kind', 'namespace', 'name', 'uid')}
                        env.save(receipt_path, receipt)
                        client(row['namespace'], 'patch', row['kind'], row['name'], '--type=json',
                               '--patch-file=/dev/stdin', '-o', 'json', document=patch)
                    version = readback(client, row)
                    if patch:
                        receipt['patched'].append({**receipt.pop('pending'), 'resource_version': version})
                        env.save(receipt_path, receipt)
                registered = cd['targets'][refs['target_id']]
                receipt['health']['application'] = update.app_health(kube, registered, refs['target_id'])
                if not receipt['health']['observability'].get('missing'):
                    receipt['health']['observability'].update(update.observer_health(kube))
            receipt['health']['management'] = update.management_health(cd, refs['target_id'])
            public = registered['public_http']
            receipt['health']['public_http'] = env.bridge.public_probe(public, urlsplit(public['url']).path)
            env.argo.require(receipt['health']['public_http']['state'] == 'succeeded', 'post-adoption HTTPS health unverified')
            with update.connection(request) as prefix:
                after = update.node_call(prefix, {'action': 'inspect'})
            env.argo.require(after == fresh['runtime'], 'runtime changed during label adoption')
            env.save(home / 'cd.json', cd)
            record.update(status='succeeded', deployment_supported=True, app=registered['app'], namespace=registered['target']['namespace'],
                          cluster_server=registered['target']['cluster_server'], node_port=registered['target']['node_port'],
                          cd_sha256=hashlib.sha256(env.read_private(home / 'cd.json', raw=True)).hexdigest(),
                          observability={'registered': False, 'collection_state': 'pending', 'reconciliation_required': True},
                          steps=['existing_identity_verified', 'ownership_labels_adopted', 'existing_health_verified'])
            env.save(record_path, record)
            receipt.update(status='verified', verified_at=datetime.now(timezone.utc).isoformat())
            env.save(receipt_path, receipt)
        except Exception as error:
            receipt.update(status='recovery_required', error_type=type(error).__name__)
            env.save(receipt_path, receipt)
        return {key: receipt[key] for key in ('status', 'target_id', 'provider', 'plan_sha256')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    planner = commands.add_parser('plan')
    for name in ('registry', 'target-id', 'config', 'registration-dir', 'policy-file', 'out'):
        planner.add_argument('--' + name, required=True)
    planner.add_argument('--binding')
    applier = commands.add_parser('apply')
    applier.add_argument('--plan', required=True); applier.add_argument('--expected-plan-sha256', required=True)
    args = parser.parse_args(); os.umask(0o077)
    try:
        if args.command == 'plan':
            refs = {key: getattr(args, key) for key in ('registry', 'target_id', 'config', 'registration_dir', 'policy_file', 'binding')}
            result = plan(refs, args.out)
        else:
            result = apply(args.plan, args.expected_plan_sha256)
    except Exception as error:
        result = {'status': 'blocked', 'error_type': type(error).__name__}
    print(json.dumps(result))
    return 0 if result['status'] in ('planned', 'verified') else 1


if __name__ == '__main__':
    sys.exit(main())
