#!/usr/bin/env python3
"""Read a bounded log tail for one backend-owned, currently deployed review."""
import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import sys
from urllib import request
from urllib.parse import urlencode, urlsplit

import argo
import bridge
import credentials

LIMIT = 32768
NAME = r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?'


def result(state, reason=None, entries=None):
    return {'state': state, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'entries': entries or [], 'reason': reason}


def redact(text):
    # Defense in depth only: callers must first enforce session ownership.
    text = re.sub(r'-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?(?:-----END [^-\r\n]*PRIVATE KEY-----|$)',
                  '[REDACTED PRIVATE KEY]', text, flags=re.S)
    text = re.sub(r'(?m)^[A-Za-z0-9+/=]{48,}\r?$', '[REDACTED KEY DATA]', text)
    text = re.sub(r'(?i)\b(?:bearer|basic)\s+[A-Za-z0-9_./+=-]+', '[REDACTED AUTH]', text)
    text = re.sub(r'(?i)(\b[a-z0-9_-]*(?:password|passwd|secret|token|api[_-]?key|authorization|cookie)[a-z0-9_-]*\b["\x27]?\s*[:=]\s*)(?:"[^"\r\n]*"|\x27[^\x27\r\n]*\x27|[^\s,;}]+)',
                  r'\1[REDACTED]', text)
    text = re.sub(r'([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text, flags=re.I)
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b', '[REDACTED]', text)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)


def private_directory(path):
    path = Path(path)
    info = path.lstat()
    argo.require(path.is_absolute() and not path.is_symlink() and path.is_dir() and
                 info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'private owned review required')
    return path


def load_bound_review(config, value):
    argo.require(isinstance(value, dict) and set(value) == {
        'deployment_id', 'app', 'target_id', 'source_commit', 'run_id', 'revision'}, 'bound log request required')
    argo.require(all(isinstance(value[k], str) and re.fullmatch(bridge.ID, value[k])
                     for k in ('deployment_id', 'app', 'target_id')) and
                 all(isinstance(value[k], str) and re.fullmatch(argo.SHA, value[k])
                     for k in ('source_commit', 'revision')) and
                 isinstance(value['run_id'], str) and re.fullmatch(r'[1-9][0-9]{0,19}', value['run_id']),
                 'bounded deployment identity required')
    directory = private_directory(private_directory(config['state_dir']) / value['deployment_id'])
    private_directory(directory / 'review')
    review = argo.load_review(directory / 'review')
    receipt, app = review['receipt'], review['application']
    registered = config['targets'][value['target_id']]
    target = registered['target']
    argo.require(all(receipt[k] == value[k] for k in ('app', 'target_id', 'source_commit')) and
                 str(receipt['run_id']) == value['run_id'] and
                 receipt['app'] == registered['app'] and receipt['tenant'] == registered['tenant'] and
                 target['id'] == value['target_id'] and app['spec']['source']['targetRevision'] == value['revision'] and
                 app['metadata']['namespace'] == target['argocd_namespace'] and
                 app['spec']['project'] == target['project'] and
                 app['spec']['source']['repoURL'] == target['repo_url'] and app['spec']['source']['path'] == target['path'] and
                 app['spec']['destination'] == {'server': target['cluster_server'], 'namespace': target['namespace']},
                 'registered review identity differs')
    return review


def customer_auth(config, review):
    app = review['application']; spec = app['spec']; target = review['receipt']['target_id']
    secret = argo.kubectl(config['context'], app['metadata']['namespace'], 'get', 'secret', 'railshot-' + target, '-o', 'json')
    meta = secret['metadata']
    argo.require(secret['kind'] == 'Secret' and meta['name'] == 'railshot-' + target and
                 meta['namespace'] == app['metadata']['namespace'], 'registered credential required')
    data = {key: base64.b64decode(value, validate=True).decode() for key, value in secret['data'].items()}
    argo.require(credentials.credential_labels_match(meta.get('labels'), target, data['project'], data['namespaces'].split(',')),
                 'registered credential owner or application scope differs')
    shared_cluster = (not re.fullmatch(credentials.APPLICATION_ID, target) and data['project'] == '' and
                      meta['labels']['argocd.argoproj.io/secret-type'] == 'cluster')
    argo.require(data['name'] == target and data['server'] == spec['destination']['server'] and
                 (data['project'] == spec['project'] or shared_cluster) and data['clusterResources'] == 'false' and
                 spec['destination']['namespace'] in data['namespaces'].split(','), 'registered credential scope differs')
    server = argo.https_url(data['server']); url = urlsplit(server)
    argo.require(not url.path, 'explicit API origin required')
    auth = json.loads(data['config']); tls = auth['tlsClientConfig']
    argo.require(set(auth) == {'bearerToken', 'tlsClientConfig'} and {'caData', 'insecure'} <= set(tls) <= {'caData', 'insecure', 'serverName'} and
                 tls['insecure'] is False and isinstance(auth['bearerToken'], str) and
                 0 < len(auth['bearerToken']) < 32768 and not re.search(r'\s', auth['bearerToken']), 'CA-verified bearer required')
    options = {'server_name': credentials.tls_name(tls['serverName'])} if 'serverName' in tls else {}
    return (server, base64.b64decode(tls['caData'], validate=True), auth['bearerToken']), options


def tail(auth, path, *, server_name=None):
    server, ca, token = auth
    context = ssl.create_default_context(cadata=ca.decode('ascii'))
    if server_name is not None:
        url = urlsplit(server)
        connection = credentials.RegisteredHTTPSConnection(url.hostname, url.port, server_name=server_name,
                                                           context=context, timeout=10)
        try:
            connection.request('GET', path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'text/plain'})
            response = connection.getresponse()
            argo.require(response.status == 200, 'log read failed')
            raw = response.read(LIMIT + 1)
        finally:
            connection.close()
    else:
        opener = request.build_opener(request.ProxyHandler({}), request.HTTPSHandler(context=context), credentials.NoRedirect())
        req = request.Request(server + path, headers={'Authorization': 'Bearer ' + token, 'Accept': 'text/plain'})
        with opener.open(req, timeout=10) as response:
            argo.require(response.status == 200, 'log read failed')
            raw = response.read(LIMIT + 1)
    argo.require(len(raw) <= LIMIT, 'bounded log response required')
    return redact(raw.decode('utf-8', errors='replace')).encode()[:LIMIT // 3].decode('utf-8', errors='ignore')


def owns(resource, kind, uid):
    return any(owner.get('kind') == kind and owner.get('uid') == uid and owner.get('controller') is True
               for owner in resource['metadata'].get('ownerReferences', []))


def template_matches(template, expected):
    labels = template['metadata'].get('labels', {})
    return all(labels.get(k) == v for k, v in expected['metadata']['labels'].items()) and {
        c['name']: c['image'] for c in template['spec']['containers']} == {
        c['name']: c['image'] for c in expected['spec']['containers']}


def execute(config, value):
    review = load_bound_review(config, value)
    app = review['application']
    def current():
        live = argo.kubectl(config['context'], app['metadata']['namespace'], 'get', 'application', app['metadata']['name'], '-o', 'json')
        return argo.observe(review, live)['deployed']
    if not current():
        return result('unavailable', 'deployment_not_current')
    auth, tls_options = customer_auth(config, review)
    namespace = app['spec']['destination']['namespace']
    expected = next(item for item in review['workload']['items'] if item['kind'] == 'Deployment')
    template = expected['spec']['template']
    image = template['spec']['containers'][0]['image']
    argo.require(len(template['spec']['containers']) == 1 and re.search(r'@sha256:[0-9a-f]{64}$', image), 'one pinned runtime image required')
    deployment_path = '/apis/apps/v1/namespaces/' + namespace + '/deployments/' + expected['metadata']['name']
    live = credentials.customer(*auth, deployment_path, **tls_options)
    argo.require(live['kind'] == 'Deployment' and live['metadata']['namespace'] == namespace and
                 live['metadata']['name'] == expected['metadata']['name'] and not live['metadata'].get('deletionTimestamp') and
                 live['spec']['selector'] == expected['spec']['selector'] and template_matches(live['spec']['template'], template),
                 'current Deployment binding differs')
    selector = urlencode({'labelSelector': ','.join(k + '=' + v for k, v in template['metadata']['labels'].items()), 'limit': 20})
    replicas = credentials.customer(*auth, '/apis/apps/v1/namespaces/' + namespace + '/replicasets?' + selector, **tls_options)
    argo.require(replicas['kind'] == 'ReplicaSetList' and not replicas.get('metadata', {}).get('continue') and
                 len(replicas['items']) <= 20, 'bounded ReplicaSets required')
    owners = {row['metadata']['uid'] for row in replicas['items'] if row['metadata']['namespace'] == namespace and
              not row['metadata'].get('deletionTimestamp') and owns(row, 'Deployment', live['metadata']['uid']) and
              template_matches(row['spec']['template'], template)}
    pod_path = '/api/v1/namespaces/' + namespace + '/pods'
    pods = credentials.customer(*auth, pod_path + '?' + selector, **tls_options)
    argo.require(pods['kind'] == 'PodList' and not pods.get('metadata', {}).get('continue') and len(pods['items']) <= 20,
                 'bounded Pods required')
    selected = [pod for pod in pods['items'] if not pod['metadata'].get('deletionTimestamp') and
                any(owns(pod, 'ReplicaSet', uid) for uid in owners)]
    if not selected:
        return result('unavailable', 'runtime_logs_not_ready')
    entries = []
    for pod in sorted(selected, key=lambda row: row['metadata']['name'])[:3]:
        name = pod['metadata']['name']; container = template['spec']['containers'][0]['name']
        argo.require(pod['metadata']['namespace'] == namespace and re.fullmatch(NAME, name) and re.fullmatch(NAME, container) and
                     template_matches(pod, template), 'runtime Pod binding differs')
        statuses = pod.get('status', {}).get('containerStatuses', [])
        argo.require(len(statuses) == 1 and statuses[0]['name'] == container and
                     statuses[0].get('imageID', '').endswith(image[image.index('@') + 1:]), 'running image digest differs')
        text = tail(auth, pod_path + '/' + name + '/log?' + urlencode({
            'container': container, 'tailLines': 100, 'limitBytes': LIMIT // 3, 'timestamps': 'true', 'follow': 'false'}), **tls_options)
        # Reject a replacement Pod or Argo update that occurred during the read.
        after = credentials.customer(*auth, pod_path + '/' + name, **tls_options)
        argo.require(after['metadata']['uid'] == pod['metadata']['uid'] and template_matches(after, template), 'runtime Pod replaced')
        entries.append({'pod': name, 'container': container, 'text': text})
    after = credentials.customer(*auth, deployment_path, **tls_options)
    if after['metadata']['uid'] != live['metadata']['uid'] or after['metadata']['generation'] != live['metadata']['generation'] or not current():
        return result('unavailable', 'deployment_not_current')
    return result('ready', entries=entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(4097)
        argo.require(len(raw) <= 4096, 'bounded log request required')
        output = execute(bridge.read_config(args.config), json.loads(raw))
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        output = result('unavailable', 'runtime_logs_unavailable')
    print(json.dumps(output, allow_nan=False))


if __name__ == '__main__':
    main()
