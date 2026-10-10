"""Read bounded workload facts with the existing application-scoped credential.

No Pod output, exception messages, environment values or mutation is exported.
Observation failure does not change the bridge's deterministic result.
"""
from datetime import datetime, timezone
import re
from urllib.parse import urlencode

import argo
import credentials
import logs

REASONS = {'CrashLoopBackOff', 'ImagePullBackOff', 'ErrImagePull', 'CreateContainerConfigError',
           'CreateContainerError', 'RunContainerError', 'ContainerCreating', 'PodInitializing',
           'OOMKilled', 'Error', 'Completed'}


def workload(config, review):
    checked = datetime.now(timezone.utc).isoformat()
    missing = {'state': 'unavailable', 'checked_at': checked, 'pods': [], 'code': 'WORKLOAD_OBSERVATION_UNAVAILABLE'}
    try:
        auth, tls = logs.customer_auth(config, review)
        namespace = review['application']['spec']['destination']['namespace']
        expected = next(item for item in review['workload']['items'] if item['kind'] == 'Deployment')
        template = expected['spec']['template']
        path = '/apis/apps/v1/namespaces/' + namespace + '/deployments/' + expected['metadata']['name']
        live = credentials.customer(*auth, path, **tls)
        argo.require(live['kind'] == 'Deployment' and live['metadata']['namespace'] == namespace
                     and live['metadata']['name'] == expected['metadata']['name'] and not live['metadata'].get('deletionTimestamp')
                     and live['spec']['selector'] == expected['spec']['selector']
                     and logs.template_matches(live['spec']['template'], template), 'deployment binding differs')
        selector = urlencode({'labelSelector': ','.join(k + '=' + v for k, v in template['metadata']['labels'].items()), 'limit': 20})
        replicas = credentials.customer(*auth, '/apis/apps/v1/namespaces/' + namespace + '/replicasets?' + selector, **tls)
        argo.require(replicas['kind'] == 'ReplicaSetList' and not replicas.get('metadata', {}).get('continue')
                     and len(replicas['items']) <= 20, 'bounded replicas required')
        owners = {row['metadata']['uid'] for row in replicas['items'] if row['metadata']['namespace'] == namespace
                  and logs.owns(row, 'Deployment', live['metadata']['uid']) and logs.template_matches(row['spec']['template'], template)}
        listing = credentials.customer(*auth, '/api/v1/namespaces/' + namespace + '/pods?' + selector, **tls)
        argo.require(listing['kind'] == 'PodList' and not listing.get('metadata', {}).get('continue')
                     and len(listing['items']) <= 20, 'bounded pods required')
        selected = [p for p in listing['items'] if not p['metadata'].get('deletionTimestamp')
                    and any(logs.owns(p, 'ReplicaSet', uid) for uid in owners)]
        rows = []
        expected_images = {c['name']: c['image'] for c in template['spec']['containers']}
        for pod in selected:
            meta = pod['metadata']
            argo.require(meta['namespace'] == namespace and re.fullmatch(logs.NAME, meta['name'])
                         and logs.template_matches(pod, template), 'pod binding differs')
            statuses = pod.get('status', {}).get('containerStatuses', [])
            argo.require(len(statuses) <= len(expected_images), 'bounded containers required')
            containers = []
            for c in statuses:
                argo.require(c['name'] in expected_images, 'container binding differs')
                state = c.get('state', {})
                status = next((key for key in ('running', 'waiting', 'terminated') if key in state), 'unknown')
                detail = state.get(status, {})
                image_id = c.get('imageID', '')
                image_hash = re.search(r'sha256:[0-9a-f]{64}$', image_id)
                containers.append({'name': c['name'], 'state': status, 'ready': c.get('ready') is True,
                    'restart_count': c.get('restartCount') if type(c.get('restartCount')) is int else None,
                    'image_id': image_hash[0] if image_hash else None,
                    'image_matches': bool(image_hash and expected_images[c['name']].endswith('@' + image_hash[0])),
                    'reason': detail.get('reason') if detail.get('reason') in REASONS else None,
                    'exit_code': detail.get('exitCode') if type(detail.get('exitCode')) is int else None})
            ready = (len(containers) == len(expected_images) and all(c['ready'] and c['image_matches'] for c in containers)
                     and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in pod.get('status', {}).get('conditions', [])))
            rows.append({'name': meta['name'], 'ready': ready, 'containers': containers})
        after = credentials.customer(*auth, path, **tls)
        argo.require(after['metadata']['uid'] == live['metadata']['uid']
                     and after['metadata']['generation'] == live['metadata']['generation'], 'deployment changed during observation')
        desired = live['spec'].get('replicas', 1)
        status = live.get('status', {})
        ready = (type(desired) is int and desired > 0 and len(rows) >= desired and all(p['ready'] for p in rows)
                 and status.get('observedGeneration', 0) >= live['metadata']['generation']
                 and status.get('availableReplicas', 0) >= desired)
        return {'state': 'ready' if ready else 'progressing', 'checked_at': checked, 'pods': rows,
                'code': None, 'observed_generation': status.get('observedGeneration'), 'desired_replicas': desired}
    except Exception:
        return missing
