"""App-scoped Kubernetes lifecycle. The caller fences Argo/CI and owns the app lock.

Snapshots are PRIVATE: only resource identities, hashes and replica/suspend controls
are retained. No manifests, Secret data, container environment or CSI handles leave
this module. A failed/uncertain write is never retried here.
"""
import copy
import hashlib
import json
import re
import time
from urllib.parse import quote

TIMEOUT = 120
SA = 'railshot-argocd'
FINALIZER = 'external-provisioner.volume.kubernetes.io/finalizer'
LABEL = 'railshot.io/registration'
TARGET = 'railshot.io/target'
SUPPORTED = {
    '': {'Pod', 'Service', 'Endpoints', 'ServiceAccount', 'Secret', 'ConfigMap', 'PersistentVolumeClaim', 'Event'},
    'apps': {'Deployment', 'StatefulSet', 'ReplicaSet', 'ControllerRevision'},
    'batch': {'Job', 'CronJob'}, 'networking.k8s.io': {'NetworkPolicy'},
    'rbac.authorization.k8s.io': {'Role', 'RoleBinding'},
    'discovery.k8s.io': {'EndpointSlice'}, 'events.k8s.io': {'Event'},
    'cilium.io': {'CiliumEndpoint'},
}
TRANSIENT = {'Pod', 'ReplicaSet', 'ControllerRevision', 'Endpoints', 'EndpointSlice', 'Event', 'CiliumEndpoint'}
PARENTS = {'Pod': {'ReplicaSet', 'StatefulSet', 'Job'}, 'ReplicaSet': {'Deployment'},
           'ControllerRevision': {'StatefulSet'}, 'Job': {'CronJob'}, 'EndpointSlice': {'Service'},
           'PersistentVolumeClaim': {'StatefulSet'}, 'CiliumEndpoint': {'Pod'}}
CONTROLS = {'Deployment': 'replicas', 'StatefulSet': 'replicas', 'CronJob': 'suspend', 'Job': 'suspend'}


class LifecycleRuntimeError(ValueError):
    def __init__(self, code='APPLICATION_RUNTIME_BLOCKED', *, unknown=False, residuals=None, steps=None):
        self.code, self.unknown = code, unknown
        self.residuals, self.steps = residuals or [], steps or []
        super().__init__(code)


def require(condition, code='APPLICATION_RUNTIME_SCOPE_MISMATCH'):
    if not condition:
        raise LifecycleRuntimeError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def name(value):
    return isinstance(value, str) and re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?', value) is not None


def call(kube, ns, *args, document=None, write=False):
    try:
        return kube(ns, *args, **({'document': document} if document is not None else {}))
    except Exception:
        raise LifecycleRuntimeError('APPLICATION_RUNTIME_WRITE_UNCERTAIN' if write else
                                    'APPLICATION_RUNTIME_READ_FAILED', unknown=write) from None


def get(kube, ns, resource, key):
    return call(kube, ns, 'get', resource, key, '--ignore-not-found', '-o', 'json')


def listed(kube, ns, path, kind, api_version):
    result, cursor = [], ''
    while True:
        response = call(kube, ns, 'get', '--raw', path + '?limit=500' +
                        ('&continue=' + quote(cursor, safe='') if cursor else ''))
        require(isinstance(response, dict) and isinstance(response.get('items'), list),
                'APPLICATION_RUNTIME_DISCOVERY_FAILED')
        # Native typed Lists omit TypeMeta on their items. The discovery entry
        # (or fixed built-in endpoint) is the authority, never a plural-name guess.
        require(response.get('kind', kind + 'List') == kind + 'List' and
                response.get('apiVersion', api_version) == api_version,
                'APPLICATION_RUNTIME_DISCOVERY_FAILED')
        for item in response['items']:
            require(isinstance(item, dict) and item.get('kind', kind) == kind and
                    item.get('apiVersion', api_version) == api_version,
                    'APPLICATION_RUNTIME_DISCOVERY_FAILED')
            result.append({**item, 'kind': kind, 'apiVersion': api_version})
        require(len(result) <= 10000, 'APPLICATION_RUNTIME_INVENTORY_TOO_LARGE')
        next_cursor = response.get('metadata', {}).get('continue', '')
        require(isinstance(next_cursor, str) and (not next_cursor or next_cursor != cursor),
                'APPLICATION_RUNTIME_DISCOVERY_FAILED')
        if not next_cursor:
            return result
        cursor = next_cursor


def discover(kube, ns):
    core = call(kube, ns, 'get', '--raw', '/api')
    groups = call(kube, ns, 'get', '--raw', '/apis')
    require(isinstance(core, dict) and isinstance(core.get('versions'), list) and
            isinstance(groups, dict) and isinstance(groups.get('groups'), list),
            'APPLICATION_RUNTIME_DISCOVERY_FAILED')
    paths = ['/api/' + version for version in core['versions'] if version == 'v1']
    require(paths, 'APPLICATION_RUNTIME_DISCOVERY_FAILED')
    for group in groups['groups']:
        versions = group.get('versions', [])
        preferred = group.get('preferredVersion', {}).get('groupVersion')
        require(versions and preferred in [v.get('groupVersion') for v in versions],
                'APPLICATION_RUNTIME_DISCOVERY_FAILED')
        for version in sorted(versions, key=lambda v: v['groupVersion'] != preferred):
            gv = version['groupVersion']
            require(re.fullmatch(r'[a-z0-9.-]+/v[0-9]+(?:(?:alpha|beta)[0-9]+)?', gv),
                    'APPLICATION_RUNTIME_DISCOVERY_FAILED')
            paths.append('/apis/' + gv)
    seen, found = set(), []
    for path in paths:
        discovery = call(kube, ns, 'get', '--raw', path)
        require(isinstance(discovery, dict) and isinstance(discovery.get('resources'), list),
                'APPLICATION_RUNTIME_DISCOVERY_FAILED')
        gv = path.removeprefix('/apis/').removeprefix('/api/')
        group = gv.rsplit('/', 1)[0] if '/' in gv else ''
        for resource in discovery['resources']:
            key = resource.get('name', '')
            if not resource.get('namespaced') or '/' in key or 'list' not in resource.get('verbs', []):
                continue
            kind = resource.get('kind')
            require(name(key) and isinstance(kind, str) and re.fullmatch(r'[A-Z][A-Za-z0-9]*', kind),
                    'APPLICATION_RUNTIME_DISCOVERY_FAILED')
            if (group, key) in seen:
                continue
            seen.add((group, key))
            for item in listed(kube, ns, path + '/namespaces/' + ns + '/' + key, kind, gv):
                require(item['kind'] in SUPPORTED.get(group, set()), 'APPLICATION_RUNTIME_UNSUPPORTED_RESOURCE')
                if group == 'cilium.io':
                    require(gv == 'cilium.io/v2', 'APPLICATION_RUNTIME_UNSUPPORTED_RESOURCE')
                if item['kind'] == 'Event':
                    continue  # The core/events APIs expose the same inert history with identical UIDs.
                found.append((key + ('.' + group if group else ''), item))
    return found


def identity(item, ns=None):
    meta = item.get('metadata', {})
    require(name(meta.get('name')) and isinstance(meta.get('uid'), str) and
            re.fullmatch(r'[A-Za-z0-9-]{1,128}', meta['uid']) and
            (ns is None or meta.get('namespace') == ns))
    return {'kind': item['kind'], 'name': meta['name'], **({'namespace': ns} if ns else {})}


def labelled(item, app):
    labels = item['metadata'].get('labels', {})
    return labels.get('app.kubernetes.io/managed-by') == 'railshot' and labels.get(LABEL) == app


def pod_template(item):
    spec = item.get('spec', {})
    if item['kind'] == 'Pod':
        return item
    if item['kind'] == 'CronJob':
        return spec.get('jobTemplate', {}).get('spec', {}).get('template', {})
    return spec.get('template', {})


def ownership(objects, binding, *, shared_renewal=None):
    app = binding['application_id']
    by_uid = {item['metadata']['uid']: item for _, item in objects}
    require(len(by_uid) == len(objects))
    for _, item in objects:
        kind, meta, spec = item['kind'], item['metadata'], item.get('spec', {})
        labels = meta.get('labels', {})
        require(labels.get(LABEL, app) == app and labels.get(TARGET, app) == app,
                'APPLICATION_RUNTIME_FOREIGN_OWNER')
        owners = meta.get('ownerReferences', [])
        if owners:
            for owner in owners:
                parent = by_uid.get(owner.get('uid'))
                require(parent is not None and parent['kind'] in PARENTS.get(kind, set()) and
                        owner.get('kind') == parent['kind'] and owner.get('name') == parent['metadata']['name'] and
                        owner.get('apiVersion') == parent['apiVersion'], 'APPLICATION_RUNTIME_FOREIGN_OWNER')
        elif kind in ('Deployment', 'StatefulSet', 'CronJob', 'Job'):
            require(pod_template(item).get('metadata', {}).get('labels', {}).get(TARGET) == app,
                    'APPLICATION_RUNTIME_FOREIGN_OWNER')
        elif kind == 'Service':
            require(spec.get('selector', {}).get(TARGET) == app and spec.get('type') in ('ClusterIP', 'NodePort') and
                    not spec.get('externalIPs') and not spec.get('externalName'),
                    'APPLICATION_RUNTIME_UNSUPPORTED_SERVICE')
        elif kind == 'NetworkPolicy':
            require(spec.get('podSelector', {}).get('matchLabels', {}).get(TARGET) == app,
                    'APPLICATION_RUNTIME_FOREIGN_OWNER')
        elif kind == 'Event':
            pass  # Inert namespace-scoped history, including events about already-deleted Pods.
        elif kind == 'Endpoints':
            service = next((s for _, s in objects if s['kind'] == 'Service' and s['metadata']['name'] == meta['name']), None)
            require(service is not None, 'APPLICATION_RUNTIME_FOREIGN_OWNER')
            for subset in item.get('subsets', []):
                for address in subset.get('addresses', []) + subset.get('notReadyAddresses', []):
                    ref = address.get('targetRef', {})
                    require(ref.get('uid') in by_uid and by_uid[ref['uid']]['kind'] == 'Pod',
                            'APPLICATION_RUNTIME_FOREIGN_OWNER')
        elif kind == 'ServiceAccount' and meta['name'] == 'default':
            require(not item.get('secrets') and not item.get('imagePullSecrets'), 'APPLICATION_RUNTIME_FOREIGN_OWNER')
        elif kind == 'ConfigMap' and meta['name'] == 'kube-root-ca.crt':
            require(set(item.get('data', {})) == {'ca.crt'}, 'APPLICATION_RUNTIME_FOREIGN_OWNER')
        else:
            require(kind not in TRANSIENT and labelled(item, app), 'APPLICATION_RUNTIME_FOREIGN_OWNER')
        if kind == 'RoleBinding':
            ref = item.get('roleRef', {})
            require(ref.get('kind') == 'Role' and ref.get('apiGroup') == 'rbac.authorization.k8s.io' and
                    any(p['kind'] == 'Role' and p['metadata']['name'] == ref.get('name') for _, p in objects),
                    'APPLICATION_RUNTIME_FOREIGN_OWNER')
            if meta['name'] == 'railshot-environment-argocd':
                shared = (shared_renewal or {}).get('service_account', {})
                require(labelled(item, app) and ref == {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': SA}
                        and shared.get('name') == SA and
                        name(shared.get('namespace')) and shared['namespace'] != app and shared.get('uid') and
                        item.get('subjects') == [{'kind': 'ServiceAccount', 'name': shared['name'],
                                                  'namespace': shared['namespace']}],
                        'APPLICATION_RUNTIME_FOREIGN_OWNER')
            else:
                require(item.get('subjects') and all(s.get('kind') == 'ServiceAccount' and s.get('namespace') == app and
                        any(p['kind'] == 'ServiceAccount' and p['metadata']['name'] == s.get('name') for _, p in objects)
                        for s in item['subjects']), 'APPLICATION_RUNTIME_FOREIGN_OWNER')
        pod = pod_template(item).get('spec', {})
        require(not pod.get('hostNetwork') and not pod.get('hostPID') and not pod.get('hostIPC'),
                'APPLICATION_RUNTIME_EXTERNAL_STORAGE')
        for volume in pod.get('volumes', []):
            require(set(volume) <= {'name', 'emptyDir', 'secret', 'configMap', 'projected', 'downwardAPI', 'persistentVolumeClaim'},
                    'APPLICATION_RUNTIME_EXTERNAL_STORAGE')
            if 'persistentVolumeClaim' in volume:
                claim = volume['persistentVolumeClaim'].get('claimName')
                require(any(p['kind'] == 'PersistentVolumeClaim' and p['metadata']['name'] == claim
                            for _, p in objects), 'APPLICATION_RUNTIME_EXTERNAL_STORAGE')
        if kind == 'StatefulSet':
            require(all(template.get('metadata', {}).get('labels', {}).get(LABEL) == app
                        for template in spec.get('volumeClaimTemplates', [])), 'APPLICATION_RUNTIME_EXTERNAL_STORAGE')


def record(resource, item, ns):
    meta, kind = item['metadata'], item['kind']
    body = {key: value for key, value in item.items() if key not in ('apiVersion', 'kind', 'metadata', 'status')}
    body = copy.deepcopy(body)
    field = CONTROLS.get(kind)
    control = None
    if field:
        control = body['spec'].pop(field, 1 if field == 'replicas' else False)
        require((type(control) is int and control >= 0) if field == 'replicas' else type(control) is bool)
    # Controller bookkeeping changes independently of the approved declaration.
    annotations = {k: v for k, v in meta.get('annotations', {}).items()
                   if k not in ('deployment.kubernetes.io/revision', 'kubectl.kubernetes.io/last-applied-configuration')}
    body['metadata'] = {'labels': meta.get('labels', {}), 'annotations': annotations,
                        'ownerReferences': meta.get('ownerReferences', []), 'finalizers': meta.get('finalizers', [])}
    return {**identity(item, ns), 'resource': resource, 'apiVersion': item['apiVersion'], 'uid': meta['uid'],
            'spec_sha256': digest(body), **({'control': {field: control}} if field else {})}


def storage(kube, ns, objects):
    claims = [item for _, item in objects if item['kind'] == 'PersistentVolumeClaim']
    if not claims:
        return []
    volumes = listed(kube, ns, '/api/v1/persistentvolumes', 'PersistentVolume', 'v1')
    attachments = listed(kube, ns, '/apis/storage.k8s.io/v1/volumeattachments',
                         'VolumeAttachment', 'storage.k8s.io/v1')
    result = []
    for claim in claims:
        spec, cm = claim['spec'], claim['metadata']
        matches = [pv for pv in volumes if pv['metadata']['name'] == spec.get('volumeName')]
        require(len(matches) == 1 and claim.get('status', {}).get('phase') == 'Bound',
                'APPLICATION_RUNTIME_UNSUPPORTED_STORAGE')
        pv = matches[0]; vs = pv['spec']; csi = vs.get('csi', {})
        driver, handle = csi.get('driver'), csi.get('volumeHandle')
        provisioner = pv['metadata'].get('annotations', {}).get('pv.kubernetes.io/provisioned-by')
        require(vs.get('persistentVolumeReclaimPolicy') == 'Delete' and
                vs.get('claimRef', {}).get('uid') == cm['uid'] and vs['claimRef'].get('namespace') == ns and
                vs['claimRef'].get('name') == cm['name'] and not spec.get('selector') and
                set(vs.get('accessModes', [])) in ({'ReadWriteOnce'}, {'ReadWriteOncePod'}),
                'APPLICATION_RUNTIME_UNSUPPORTED_STORAGE')
        sc_name = vs.get('storageClassName')
        require(name(sc_name) and sc_name == spec.get('storageClassName'), 'APPLICATION_RUNTIME_UNSUPPORTED_STORAGE')
        sc = get(kube, ns, 'storageclass', sc_name)
        local = vs.get('local', {}).get('path')
        if local:
            driver, handle = 'rancher.io/local-path', local
            expected_path = '/var/lib/rancher/railshot-volumes/' + pv['metadata']['name'] + '_' + ns + '_' + cm['name']
            terms = vs.get('nodeAffinity', {}).get('required', {}).get('nodeSelectorTerms', [])
            require(sc_name == 'railshot-persistent' and provisioner == driver and local == expected_path
                    and re.fullmatch(r'pvc-[a-f0-9-]{36}', pv['metadata']['name'])
                    and pv['metadata']['name'] == 'pvc-' + cm['uid']
                    and sc and sc['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'railshot'
                    and sc.get('metadata', {}).get('annotations', {}).get('defaultVolumeType') == 'local'
                    and sc.get('volumeBindingMode') == 'WaitForFirstConsumer' and not csi and not vs.get('hostPath')
                    and len(terms) == 1 and len(terms[0].get('matchExpressions', [])) == 1
                    and terms[0]['matchExpressions'][0].get('key') == 'kubernetes.io/hostname'
                    and terms[0]['matchExpressions'][0].get('operator') == 'In'
                    and len(terms[0]['matchExpressions'][0].get('values', [])) == 1,
                    'APPLICATION_RUNTIME_UNSUPPORTED_STORAGE')
            require(sum(p.get('spec', {}).get('local', {}).get('path') == local for p in volumes) == 1,
                    'APPLICATION_RUNTIME_SHARED_STORAGE')
        else:
            require(driver and handle and provisioner == driver and FINALIZER in pv['metadata'].get('finalizers', [])
                    and bool(csi.get('volumeAttributes', {}).get('storage.kubernetes.io/csiProvisionerIdentity')),
                    'APPLICATION_RUNTIME_UNSUPPORTED_STORAGE')
            require(sum(p.get('spec', {}).get('csi', {}).get('driver') == driver and
                        p.get('spec', {}).get('csi', {}).get('volumeHandle') == handle for p in volumes) == 1,
                    'APPLICATION_RUNTIME_SHARED_STORAGE')
        require(sc and sc.get('provisioner') == driver and sc.get('reclaimPolicy', 'Delete') == 'Delete',
                'APPLICATION_RUNTIME_UNSUPPORTED_STORAGE')
        result.append({'claim': identity(claim, ns), 'claim_uid': cm['uid'], 'volume': identity(pv),
                       'volume_uid': pv['metadata']['uid'], 'spec_sha256': digest(vs),
                       'driver': driver, 'handle_sha256': digest(handle),
                       **({'volume_source': 'local'} if local else {}),
                       'storage_class_uid': sc['metadata']['uid'],
                       'storage_class_sha256': digest({k: v for k, v in sc.items() if k != 'metadata'}),
                       'attachments': [identity(a) for a in attachments
                                       if a.get('spec', {}).get('source', {}).get('persistentVolumeName') == pv['metadata']['name']]})
    return result


def inventory(kube, binding, renewal, action, *, shared_renewal=None):
    """Read a complete owned namespace and return a secret-free PRIVATE snapshot."""
    try:
        return _inventory(kube, binding, renewal, action, shared_renewal=shared_renewal)
    except LifecycleRuntimeError:
        raise
    except Exception:
        raise LifecycleRuntimeError('APPLICATION_RUNTIME_INVALID_INVENTORY') from None


def _inventory(kube, binding, renewal, action, *, shared_renewal=None):
    require(action in ('stop', 'start', 'delete'))
    app = binding.get('application_id', '')
    require(binding.get('version') == 1 and re.fullmatch(r'app-[a-f0-9]{24}', app))
    target = binding['registered']['target']
    require(target.get('id') == target.get('namespace') == app)
    anchor = renewal.get('service_account', {})
    require(anchor.get('name') == SA and anchor.get('namespace') == app and anchor.get('uid'))
    if action == 'delete':
        require(not target.get('database'), 'APPLICATION_RUNTIME_EXTERNAL_DATABASE')
    namespace = get(kube, app, 'namespace', app)
    require(namespace and labelled(namespace, app) and not namespace['metadata'].get('ownerReferences') and
            not namespace['metadata'].get('deletionTimestamp'), 'APPLICATION_RUNTIME_NAMESPACE_MISMATCH')
    namespace_id = identity(namespace)
    objects = discover(kube, app)
    for _, item in objects:
        identity(item, app)
        require(not item['metadata'].get('deletionTimestamp') or item['kind'] in TRANSIENT,
                'APPLICATION_RUNTIME_RESOURCE_TERMINATING')
    sas = [item for _, item in objects if item['kind'] == 'ServiceAccount' and item['metadata']['name'] == SA]
    require(len(sas) == 1 and sas[0]['metadata']['uid'] == anchor['uid'] and labelled(sas[0], app),
            'APPLICATION_RUNTIME_SERVICE_ACCOUNT_MISMATCH')
    ownership(objects, binding, shared_renewal=shared_renewal)
    shared = None
    if shared_renewal is not None:
        shared = shared_renewal.get('service_account', {})
        require(set(shared) == {'name', 'namespace', 'uid'} and shared['name'] == SA and
                name(shared['namespace']) and shared['namespace'] != app and isinstance(shared['uid'], str) and
                re.fullmatch(r'[A-Za-z0-9-]{1,128}', shared['uid']),
                'APPLICATION_RUNTIME_SHARED_SERVICE_ACCOUNT_MISMATCH')
        # The canonical credential was checked by the caller. Read its exact SA
        # here as well; it is never included in this app's mutation inventory.
        live = get(kube, shared['namespace'], 'serviceaccount', shared['name'])
        require(live and live.get('kind') == 'ServiceAccount' and live.get('apiVersion') == 'v1' and
                live.get('metadata', {}).get('name') == shared['name'] and
                live['metadata'].get('namespace') == shared['namespace'] and
                live['metadata'].get('uid') == shared['uid'] and
                not live['metadata'].get('deletionTimestamp') and not live['metadata'].get('ownerReferences'),
                'APPLICATION_RUNTIME_SHARED_SERVICE_ACCOUNT_MISMATCH')
    records = sorted((record(resource, item, app) for resource, item in objects if item['kind'] not in TRANSIENT),
                     key=lambda r: (r['kind'], r['name']))
    volumes = storage(kube, app, objects)
    public = [identity(item, app) for _, item in objects if item['kind'] != 'Event']
    public += [r['volume'] for r in volumes]
    retained = [{'kind': 'Node', 'name': binding['environment_id']}, {'kind': 'SharedLoadBalancer', 'name': binding['environment_id']}]
    require(all(name(r['name']) for r in retained))
    if action != 'delete':
        retained.extend(r for r in public if r['kind'] not in ('Pod', 'ReplicaSet', 'ControllerRevision', 'EndpointSlice', 'Endpoints'))
    resources = [namespace_id, *public] if action == 'delete' else [r for r in public if r['kind'] in CONTROLS or r['kind'] == 'Pod']
    return {'version': 1, 'application_id': app, 'namespace': {**namespace_id, 'uid': namespace['metadata']['uid']},
            'service_account': {key: anchor[key] for key in ('name', 'namespace', 'uid')}, 'records': records, 'storage': volumes,
            **({'shared_service_account': copy.deepcopy(shared)} if shared is not None else {}),
            'resources': sorted(resources, key=lambda r: (r['kind'], r['name'])), 'retained': retained}


def comparable(snapshot, *, controls=True):
    records = copy.deepcopy(snapshot['records'])
    if not controls:
        for row in records:
            row.pop('control', None)
    # Attachments follow scheduling and are checked independently during deletion.
    volumes = [{k: v for k, v in row.items() if k != 'attachments'} for row in snapshot['storage']]
    return {k: snapshot[k] for k in ('version', 'application_id', 'namespace', 'service_account')} | {
        **({'shared_service_account': snapshot['shared_service_account']} if 'shared_service_account' in snapshot else {}),
        'records': records, 'storage': volumes}


def running_pods(kube, ns):
    return [p for p in listed(kube, ns, '/api/v1/namespaces/' + ns + '/pods', 'Pod', 'v1')
            if p.get('metadata', {}).get('deletionTimestamp') or p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')]


def ready(kube, ns, records):
    for row in records:
        if row['kind'] not in ('Deployment', 'StatefulSet') or row['control']['replicas'] == 0:
            continue
        live = get(kube, ns, row['resource'], row['name'])
        require(live and record(row['resource'], live, ns) == row, 'APPLICATION_RUNTIME_WRITE_READBACK_MISMATCH')
        generation = live['metadata'].get('generation')
        status = live.get('status', {})
        if not (type(generation) is int and generation > 0 and
                status.get('observedGeneration', 0) >= generation and
                status.get('readyReplicas', 0) >= row['control']['replicas']):
            return False
    return True


def wait(check):
    deadline = time.monotonic() + TIMEOUT
    while True:
        if check():
            return
        if time.monotonic() >= deadline:
            raise LifecycleRuntimeError('APPLICATION_RUNTIME_TIMEOUT', unknown=True)
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def deleted(kube, ns, snapshot):
    namespace = get(kube, ns, 'namespace', ns)
    if namespace:
        require(namespace['metadata']['uid'] == snapshot['namespace']['uid'], 'APPLICATION_RUNTIME_NAMESPACE_REPLACED')
        return False
    if not snapshot['storage']:
        return True
    volumes = listed(kube, ns, '/api/v1/persistentvolumes', 'PersistentVolume', 'v1')
    attachments = listed(kube, ns, '/apis/storage.k8s.io/v1/volumeattachments',
                         'VolumeAttachment', 'storage.k8s.io/v1')
    for stored in snapshot['storage']:
        if any(p['metadata']['name'] == stored['volume']['name'] or
               (stored.get('volume_source') == 'local' and
                digest(p.get('spec', {}).get('local', {}).get('path')) == stored['handle_sha256']) or
               (p.get('spec', {}).get('csi', {}).get('driver') == stored['driver'] and
                digest(p['spec']['csi'].get('volumeHandle')) == stored['handle_sha256']) for p in volumes):
            return False
        if any(a.get('spec', {}).get('source', {}).get('persistentVolumeName') == stored['volume']['name'] for a in attachments):
            return False
    return True


def execute(kube, binding, action, expected_inventory, stopped_inventory=None):
    """CAS approved declarations, send each mutation once, then verify readback.

    start requires the exact original stop snapshot, not an inferred replica count.
    On any error after the first write the caller must persist unknown and reconcile;
    this function provides no automatic retry or rollback of a partial operation.
    """
    steps, wrote = [], False
    try:
        shared = ({'service_account': expected_inventory['shared_service_account']}
                  if 'shared_service_account' in expected_inventory else None)
        current = inventory(kube, binding, {'service_account': expected_inventory['service_account']}, action,
                            shared_renewal=shared)
        require(comparable(current) == comparable(expected_inventory), 'APPLICATION_RUNTIME_PLAN_STALE')
        ns = binding['application_id']
        if action == 'start':
            require(stopped_inventory is not None and comparable(current, controls=False) ==
                    comparable(stopped_inventory, controls=False), 'APPLICATION_RUNTIME_STOP_SNAPSHOT_MISMATCH')
        if action == 'delete':
            namespace = get(kube, ns, 'namespace', ns)
            require(namespace and namespace['metadata']['uid'] == current['namespace']['uid'])
            document = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'propagationPolicy': 'Foreground',
                        'preconditions': {'uid': current['namespace']['uid'], 'resourceVersion': namespace['metadata']['resourceVersion']}}
            wrote = True
            call(kube, ns, 'delete', '--raw', '/api/v1/namespaces/' + ns, '-f', '-', document=document, write=True)
            steps.append({'name': 'namespace-delete-requested', 'status': 'succeeded'})
            wait(lambda: deleted(kube, ns, current))
            steps.append({'name': 'namespace-and-storage-absent', 'status': 'succeeded'})
        else:
            restore = {(r['kind'], r['name']): r for r in (stopped_inventory or current)['records']}
            for row in sorted(current['records'], key=lambda r: (r['kind'] != 'CronJob', r['kind'], r['name'])):
                if not row.get('control'):
                    continue
                field = CONTROLS[row['kind']]
                desired = (0 if field == 'replicas' else True) if action == 'stop' else restore[row['kind'], row['name']]['control'][field]
                live = get(kube, ns, row['resource'], row['name'])
                require(live and record(row['resource'], live, ns) == row, 'APPLICATION_RUNTIME_PLAN_STALE')
                if row['control'][field] == desired:
                    continue
                patch = [{'op': 'test', 'path': '/metadata/uid', 'value': row['uid']},
                         {'op': 'test', 'path': '/metadata/resourceVersion', 'value': live['metadata']['resourceVersion']},
                         {'op': 'add', 'path': '/spec/' + field, 'value': desired}]
                wrote = True
                call(kube, ns, 'patch', row['resource'], row['name'], '--type=json', '--patch-file=/dev/stdin',
                     '-o', 'json', document=patch, write=True)
                observed = get(kube, ns, row['resource'], row['name'])
                desired_row = {**row, 'control': {field: desired}}
                require(observed and record(row['resource'], observed, ns) == desired_row,
                        'APPLICATION_RUNTIME_WRITE_READBACK_MISMATCH')
                steps.append({'name': action + '-' + row['kind'] + '-' + row['name'], 'status': 'succeeded'})
            if action == 'stop':
                wait(lambda: not running_pods(kube, ns))
                steps.append({'name': 'no-running-pods', 'status': 'succeeded'})
            after = inventory(kube, binding, {'service_account': current['service_account']}, action,
                              shared_renewal=shared)
            expected = copy.deepcopy(current)
            for row in expected['records']:
                if row.get('control'):
                    field = CONTROLS[row['kind']]
                    row['control'][field] = (0 if field == 'replicas' else True) if action == 'stop' else restore[row['kind'], row['name']]['control'][field]
            require(comparable(after) == comparable(expected), 'APPLICATION_RUNTIME_WRITE_READBACK_MISMATCH')
            if action == 'start':
                wait(lambda: ready(kube, ns, expected['records']))
                steps.append({'name': 'workloads-ready', 'status': 'succeeded'})
        return {'status': 'succeeded', 'resources': current['resources'], 'steps': steps, 'residuals': []}
    except Exception as error:
        code = error.code if isinstance(error, LifecycleRuntimeError) else 'APPLICATION_RUNTIME_INVALID_INVENTORY'
        unknown = wrote or (isinstance(error, LifecycleRuntimeError) and error.unknown)
        raise LifecycleRuntimeError(code, unknown=unknown, steps=steps,
                                    residuals=expected_inventory.get('resources', []) if unknown else []) from None
