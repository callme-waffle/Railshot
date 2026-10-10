#!/usr/bin/env python3
"""Promote existing platform worker digests; never create or replace runner Jobs.

Operator-owned 0600 config/state files and the fixed control-host kubectl identity
are required. CI workflow promotion runs while controller replenishment is suspended.
Worker config: runner_url, build_node, object_uids (from discover). Apps config:
repository=Jasmin-Softbank/railshot-apps, branch. State files are private rollback
records. Worker verification executes the renewal/controller code, not a customer
build: existing runner Jobs are preserved and are never recreated by this helper.
"""
import argparse
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load(ROOT / 'deployment/scripts/bootstrap-platform.py', 'workers_bootstrap')
renderer = load(ROOT / 'deployment/scripts/render-platform.py', 'workers_renderer')
sys.path.insert(0, str(ROOT / 'gitops'))
credentials = load(ROOT / 'gitops/credentials.py', 'workers_credentials')
require = bootstrap.require
KEYS = {
    'build_config': ('ConfigMap', 'railshot-system', 'railshot-build-controller'),
    'build_cron': ('CronJob', 'railshot-system', 'railshot-build-controller'),
    'credentials_config': ('ConfigMap', 'argocd', 'railshot-credentials'),
    'credentials_cron': ('CronJob', 'argocd', 'railshot-credentials'),
}
WORKFLOW = '.github/workflows/railshot-deploy.yml'


def scoped_config(config, scope):
    require(scope in {'all', 'ci', 'credentials'}, 'WORKER_SCOPE_INVALID')
    keys = set(KEYS) if scope == 'all' else {key for key in KEYS if key.startswith('build_' if scope == 'ci' else 'credentials_')}
    require(set(config) in ({'runner_url', 'build_node', 'object_uids'}, {'runner_url', 'build_node', 'object_uids', 'runner_count'}) and
            type(config.get('runner_count', 1)) is int and 1 <= config.get('runner_count', 1) <= 64 and
            keys <= set(config['object_uids']) <= set(KEYS), 'WORKER_CONFIG_REQUIRED')
    return {**config, 'object_uids': {key: config['object_uids'][key] for key in sorted(keys)}}


def kubectl(action, key, document=None):
    kind, namespace, name = KEYS[key]
    if action in {'jobs', 'job', 'pods', 'logs', 'create-job'}:
        require(kind == 'CronJob', 'WORKER_JOB_SCOPE_REQUIRED')
        if action == 'jobs':
            return bootstrap.kube('get', 'jobs', '-n', namespace, '-o', 'json')['items']
        if action == 'job':
            return bootstrap.kube_get('job', document, namespace)
        if action == 'pods':
            return bootstrap.kube('get', 'pods', '-n', namespace, '-l', 'job-name=' + document, '-o', 'json')['items']
        if action == 'logs':
            return bootstrap.native(['/usr/local/bin/k3s', 'kubectl', '--request-timeout=20s', '-n', namespace,
                                     'logs', document, '--tail=50', '--limit-bytes=65536'], timeout=35).decode()
        return bootstrap.kube('create', '-f', '-', '-o', 'json', document=document)
    args = [action, kind, name, '-n', namespace, '-o', 'json']
    if action == 'patch':
        args += ['--type=json', '--patch-file=/dev/stdin']
    return bootstrap.kube(*args, document=document)


def github_token(value):
    """Normalize Secret-file padding and reject unsafe header bytes without echoing it."""
    require(isinstance(value, str), 'GITHUB_TOKEN_INVALID')
    token = value.strip()
    require(re.fullmatch(r'[A-Za-z0-9_]+', token) is not None, 'GITHUB_TOKEN_INVALID')
    return token


def github(path, method='GET', body=None):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    token = github_token(os.environ.get('GITHUB_TOKEN'))
    require(path.startswith('repos/Jasmin-Softbank/railshot-apps/'), 'APPS_GITHUB_BINDING_REQUIRED')
    request = Request('https://api.github.com/' + path, method=method,
                      data=bootstrap.encoded(body) if body is not None else None,
                      headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                               'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    require(len(raw) <= 2 * 1024 * 1024, 'APPS_GITHUB_RESPONSE_TOO_LARGE')
    return json.loads(raw or b'null')


def container(spec):
    values = spec['jobTemplate']['spec']['template']['spec']['containers']
    require(len(values) == 1, 'WORKER_CONTAINER_BINDING_DIFFERS')
    return values[0]


def discover_workers(*, kube=kubectl):
    objects = {key: kube('get', key) for key in KEYS}
    policy = json.loads(objects['build_config']['data']['policy.json'])
    return {'runner_url': 'https://github.com/' + policy['repository'], 'build_node': policy['node'],
            'object_uids': {key: obj['metadata']['uid'] for key, obj in objects.items()}}


def drain(kube):
    deadline = time.monotonic() + 65
    while True:
        cron = kube('get', 'build_cron')
        require(cron['spec'].get('suspend') is True, 'BUILD_CONTROLLER_NOT_SUSPENDED')
        if not cron.get('status', {}).get('active'):
            return
        require(time.monotonic() < deadline, 'BUILD_CONTROLLER_STILL_ACTIVE')
        time.sleep(2)


def patch_field(key, field, before, after, uid, kube):
    current = kube('get', key)
    require(current['metadata']['uid'] == uid and current[field] == before, 'WORKER_CONCURRENT_CHANGE')
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': uid},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': current['metadata']['resourceVersion']},
             {'op': 'test', 'path': '/' + field, 'value': before},
             {'op': 'replace', 'path': '/' + field, 'value': after}]
    # An uncertain response is observed once, never automatically resent.
    try:
        kube('patch', key, patch)
    except Exception:
        pass
    observed = kube('get', key)
    require(observed['metadata']['uid'] == uid and observed[field] == after, 'WORKER_PATCH_NOT_VERIFIED')


def save_operation(state, path, key, field, before, after, kube):
    if before == after:
        return
    operation = {'key': key, 'field': field, 'before': before, 'after': after,
                 'uid': state['config']['object_uids'][key], 'status': 'unknown'}
    state['operations'].append(operation)
    bootstrap.write(path, state)
    patch_field(key, field, before, after, operation['uid'], kube)
    operation['status'] = 'verified'
    bootstrap.write(path, state)


def owned_by(obj, kind, uid):
    return any(owner.get('kind') == kind and owner.get('uid') == uid and owner.get('controller') is True
               for owner in obj.get('metadata', {}).get('ownerReferences', []))


def job_matches(job, cron, image):
    if not owned_by(job, 'CronJob', cron['metadata']['uid']):
        return False
    containers = job.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
    desired = container(cron['spec'])
    return (len(containers) == 1 and containers[0].get('image') == image and
            all(containers[0].get(key) == desired.get(key) for key in ('name', 'command', 'args')))


def job_proof(key, job, cron, image, policy, kube):
    require(job_matches(job, cron, image), 'WORKER_JOB_BINDING_DIFFERS')
    status = job.get('status', {})
    require(not status.get('failed') and not any(c['type'] == 'Failed' and c['status'] == 'True'
                                               for c in status.get('conditions', [])), 'WORKER_JOB_FAILED')
    if not any(c['type'] == 'Complete' and c['status'] == 'True' for c in status.get('conditions', [])):
        return None
    pods = [pod for pod in kube('pods', key, job['metadata']['name']) if owned_by(pod, 'Job', job['metadata']['uid'])]
    require(len(pods) == 1, 'WORKER_JOB_POD_NOT_UNIQUE')
    pod = pods[0]
    containers = pod.get('spec', {}).get('containers', [])
    running = pod.get('status', {}).get('containerStatuses', [])
    require(pod.get('status', {}).get('phase') == 'Succeeded' and len(containers) == len(running) == 1 and
            containers[0].get('image') == image and running[0].get('name') == containers[0].get('name') and
            running[0].get('state', {}).get('terminated', {}).get('exitCode') == 0, 'WORKER_JOB_POD_NOT_SUCCEEDED')
    resolved = running[0].get('imageID', '').removeprefix('docker-pullable://').removeprefix('containerd://')
    require(resolved in {image, 'sha256:' + image.rsplit(':', 1)[1]}, 'WORKER_JOB_IMAGE_DIGEST_DIFFERS')
    proof = {'status': 'verified', 'job': job['metadata']['name'], 'job_uid': job['metadata']['uid'],
             'pod_uid': pod['metadata']['uid'], 'image': image, 'resolved_image': running[0]['imageID']}
    if key == 'credentials_cron':
        lines = kube('logs', key, pod['metadata']['name']).strip().splitlines()
        results = json.loads(lines[-1]).get('results', []) if lines else []
        names = {target['secret'] for target in policy['targets']}
        require(len(results) == len(names) and {row.get('secret') for row in results} == names and
                all(row.get('status') == 'renewed' for row in results), 'WORKER_RENEWAL_TARGETS_NOT_VERIFIED')
        proof['renewed'] = sorted(names)
    return proof


def check_execution(state, path, key, cron, policy, kube, *, create):
    if state['initial'][key]['value'].get('suspend') is True:
        return {'status': 'suspended', 'scope': 'declarations', 'executed': False}
    image = state['images']['api']
    executions = state.setdefault('executions', {})
    intent = executions.get(key)
    deadline = time.monotonic() + (180 if key == 'build_cron' else 360)
    while True:
        if intent:
            job = kube('job', key, intent['name'])
            if job is None and intent['mode'] == 'manual':
                require(False, 'WORKER_SAMPLE_CREATE_UNKNOWN_NO_REPLAY')
            if job is not None:
                require(intent.get('uid') in (None, job['metadata']['uid']), 'WORKER_SAMPLE_REPLACED')
                if intent['mode'] == 'manual':
                    require(job['metadata'].get('annotations', {}).get('railshot.io/worker-release') == state['source_sha'],
                            'WORKER_SAMPLE_RELEASE_DIFFERS')
        else:
            job = None
        if job is None:
            candidates = [candidate for candidate in kube('jobs', key)
                          if candidate['metadata']['uid'] not in state['prior_jobs'][key] and job_matches(candidate, cron, image)]
            if candidates:
                job = sorted(candidates, key=lambda item: item['metadata'].get('creationTimestamp', ''))[-1]
                intent = {'mode': 'scheduled', 'name': job['metadata']['name'], 'uid': job['metadata']['uid']}
            elif key == 'credentials_cron':
                require(create and key not in executions, 'WORKER_EXECUTION_NOT_OBSERVED')
                # Never overlap a scheduled renewal: its token CAS is not a substitute for serialized execution.
                require(not cron.get('status', {}).get('active'), 'WORKER_RENEWAL_STILL_ACTIVE')
                name = 'railshot-credentials-release-' + state['source_sha'][:20]
                require(kube('job', key, name) is None, 'WORKER_SAMPLE_ALREADY_EXISTS')
                spec = copy.deepcopy(cron['spec']['jobTemplate']['spec'])
                spec.update(backoffLimit=0, activeDeadlineSeconds=300)
                spec.pop('ttlSecondsAfterFinished', None)
                document = {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name,
                    'namespace': cron['metadata']['namespace'], 'annotations': {'railshot.io/worker-release': state['source_sha']},
                    'ownerReferences': [{'apiVersion': 'batch/v1', 'kind': 'CronJob', 'name': cron['metadata']['name'],
                                         'uid': cron['metadata']['uid'], 'controller': True}]}, 'spec': spec}
                intent = {'mode': 'manual', 'name': name, 'uid': None, 'status': 'create_unknown'}
                executions[key] = intent
                bootstrap.write(path, state)  # Intent survives a lost create response; never replay creation.
                try:
                    kube('create-job', key, document)
                except Exception:
                    pass
                job = kube('job', key, name)
                require(job is not None, 'WORKER_SAMPLE_CREATE_UNKNOWN_NO_REPLAY')
                require(job['metadata'].get('annotations', {}).get('railshot.io/worker-release') == state['source_sha'],
                        'WORKER_SAMPLE_RELEASE_DIFFERS')
        if job is not None:
            require(job_matches(job, cron, image), 'WORKER_JOB_BINDING_DIFFERS')
            intent['uid'] = job['metadata']['uid']
            executions[key] = intent
            bootstrap.write(path, state)
            proof = job_proof(key, job, cron, image, policy, kube)
            if proof:
                intent['status'] = 'verified'
                intent['proof'] = proof
                bootstrap.write(path, state)
                return proof
        require(time.monotonic() < deadline, 'WORKER_EXECUTION_TIMEOUT')
        time.sleep(3)
def apply_workers(config, images, source_sha, state_path, *, kube=kubectl, before_resume=None, scope='all'):
    require(re.fullmatch(r'[a-f0-9]{40}', source_sha or ''), 'SOURCE_SHA_REQUIRED')
    config = scoped_config(config, scope)
    require(before_resume is None or scope == 'ci', 'CI_PROMOTION_SCOPE_REQUIRED')
    require(not Path(state_path).exists(), 'WORKER_STATE_EXISTS_OBSERVE_OR_ROLLBACK')
    for name in ('api', 'ci-runner'):
        renderer.image_ref(images, name)
    objects = {key: kube('get', key) for key in config['object_uids']}
    require(all(obj['metadata']['uid'] == config['object_uids'][key] for key, obj in objects.items()), 'WORKER_UID_DIFFERS')
    if before_resume:
        require(objects['build_cron']['spec'].get('suspend') is not True,
                'CI_WORKER_SUSPENDED_BY_OPERATOR')
    desired_crons = {}
    if 'build_cron' in objects:
        desired = renderer.render_build_controller(images, config['runner_url'], config['build_node'], config.get('runner_count', 1))['items']
        desired_config = next(obj['data'] for obj in desired if obj['kind'] == 'ConfigMap')
        old_policy = json.loads(objects['build_config']['data']['policy.json'])
        require(old_policy['repository'] == config['runner_url'].removeprefix('https://github.com/') and
                old_policy['node'] == config['build_node'], 'RUNNER_BINDING_DIFFERS')
        desired_crons['build_cron'] = next(obj for obj in desired if obj['kind'] == 'CronJob')
    if 'credentials_cron' in objects:
        policy = credentials.validate_policy(json.loads(objects['credentials_config']['data']['policy.json']))
        desired_crons['credentials_cron'] = next(obj for obj in credentials.render(policy, images['api'])['items'] if obj['kind'] == 'CronJob')
    for key, desired_cron in desired_crons.items():
        actual, expected = container(objects[key]['spec']), container(desired_cron['spec'])
        require(actual['name'] == expected['name'] and actual['command'] == expected['command'], 'WORKER_COMMAND_DIFFERS')
    state = {'version': 1, 'kind': 'workers', 'source_sha': source_sha, 'images': images,
             'config': config, 'status': 'applying', 'operations': [],
             'prior_jobs': {key: [job['metadata']['uid'] for job in kube('jobs', key)] for key in desired_crons},
             'executions': {},
             'initial': {key: {'uid': obj['metadata']['uid'], 'field': 'data' if obj['kind'] == 'ConfigMap' else 'spec',
                              'value': obj['data'] if obj['kind'] == 'ConfigMap' else obj['spec']}
                         for key, obj in objects.items()}}
    bootstrap.write(state_path, state)
    try:
        if 'build_cron' in objects:
            original = objects['build_cron']['spec']
            suspended = {**copy.deepcopy(original), 'suspend': True}
            save_operation(state, state_path, 'build_cron', 'spec', original, suspended, kube)
            drain(kube)
            save_operation(state, state_path, 'build_config', 'data', objects['build_config']['data'], desired_config, kube)
        for key in desired_crons:
            old = suspended if key == 'build_cron' else objects[key]['spec']
            new = copy.deepcopy(old)
            container(new)['image'] = images['api']
            save_operation(state, state_path, key, 'spec', old, new, kube)
        if before_resume:
            verify_declarations(state, kube)
            # While replenishment is suspended, pin the workflow before a new runner can start.
            # Existing runner Jobs keep their immutable templates and already-selected workflow.
            before_resume({'status': 'declarations_verified', 'source_sha': source_sha, 'images': images,
                           'controller_suspended': True, 'executable_verification': False, 'runner_jobs': 'preserved'})
        if 'build_cron' in objects:
            final = copy.deepcopy(original)
            container(final)['image'] = images['api']
            current = copy.deepcopy(suspended)
            container(current)['image'] = images['api']
            save_operation(state, state_path, 'build_cron', 'spec', current, final, kube)
        state['status'] = 'applied'
        state['declarations_applied'] = True
        bootstrap.write(state_path, state)
        return verify_workers(state_path, kube=kube, create_sample=True)
    except Exception:
        state = bootstrap.private(state_path)  # Preserve a durable sample-creation intent from verification.
        state['status'] = 'incomplete'
        bootstrap.write(state_path, state)
        raise


def verify_declarations(state, kube):
    expected = copy.deepcopy(state['initial'])
    for op in state['operations']:
        expected[op['key']]['value'] = op['after']
    for key, item in expected.items():
        actual = kube('get', key)
        require(actual['metadata']['uid'] == item['uid'] and actual[item['field']] == item['value'], 'WORKER_READBACK_DIFFERS')
    return expected


def verify_workers(state_path, *, kube=kubectl, create_sample=False):
    state = bootstrap.private(state_path)
    require(state['kind'] == 'workers' and state.get('declarations_applied') is True and state['status'] != 'rolled_back',
            'WORKER_APPLY_INCOMPLETE')
    expected = verify_declarations(state, kube)
    policy = (credentials.validate_policy(json.loads(expected['credentials_config']['value']['policy.json']))
              if 'credentials_config' in expected else None)
    executions = {}
    for key in (key for key in ('build_cron', 'credentials_cron') if key in expected):
        cron = kube('get', key)
        require(cron['metadata']['uid'] == expected[key]['uid'] and cron['spec'] == expected[key]['value'], 'WORKER_READBACK_DIFFERS')
        executions[key] = check_execution(state, state_path, key, cron, policy, kube, create=create_sample)
    executable = all(proof['status'] == 'verified' for proof in executions.values())
    state['status'] = 'verified' if executable else 'declarations_verified'
    bootstrap.write(state_path, state)
    return {'status': state['status'], 'source_sha': state['source_sha'], 'images': state['images'],
            'scope': 'worker_execution' if executable else 'declarations/suspended',
            'executable_verification': executable, 'executions': executions, 'runner_jobs': 'preserved'}


def rollback_workers(state_path, *, kube=kubectl):
    state = bootstrap.private(state_path)
    require(state['kind'] == 'workers' and state['status'] != 'rolled_back', 'WORKER_ROLLBACK_STATE_INVALID')
    for key, intent in state.get('executions', {}).items():
        if intent['mode'] == 'manual':
            job = kube('job', key, intent['name'])
            require(job is not None and job['metadata']['uid'] == intent['uid'] and
                    any(c['type'] in {'Complete', 'Failed'} and c['status'] == 'True'
                        for c in job.get('status', {}).get('conditions', [])), 'WORKER_SAMPLE_RECONCILIATION_REQUIRED')
    for op in reversed(state['operations']):
        if op['status'] == 'rolled_back':
            continue
        require(op['key'] in KEYS and op['field'] == ('data' if KEYS[op['key']][0] == 'ConfigMap' else 'spec'), 'WORKER_STATE_INVALID')
        current = kube('get', op['key'])
        require(current['metadata']['uid'] == op['uid'], 'WORKER_UID_DIFFERS')
        if current[op['field']] != op['before']:
            require(current[op['field']] == op['after'], 'WORKER_ROLLBACK_CONCURRENT_CHANGE')
            if op['key'] == 'build_config':
                drain(kube)
            patch_field(op['key'], op['field'], op['after'], op['before'], op['uid'], kube)
        op['status'] = 'rolled_back'
        bootstrap.write(state_path, state)
    for key, initial in state['initial'].items():
        current = kube('get', key)
        require(current['metadata']['uid'] == initial['uid'] and current[initial['field']] == initial['value'],
                'WORKER_ROLLBACK_READBACK_DIFFERS')
    state['status'] = 'rolled_back'
    bootstrap.write(state_path, state)
    return {'status': 'rolled_back', 'source_sha': state['source_sha']}


def approved_receipt(receipt, source_sha):
    if receipt.get('scope') == 'ci-runtime':
        worker = receipt.get('workers', {})
        require(receipt.get('status') == 'prepared' and receipt.get('source_sha') == source_sha
                and worker.get('status') == 'declarations_verified' and worker.get('source_sha') == source_sha
                and worker.get('controller_suspended') is True and worker.get('runner_jobs') == 'preserved',
                'CI_WORKERS_NOT_PREPARED')
        return
    require(receipt.get('status') == 'verified' and receipt.get('source_sha') == source_sha, 'RELEASE_NOT_VERIFIED')
    targets = receipt.get('targets', [])
    require(len(targets) == 3 and {row.get('provider') for row in targets} == {'aws', 'gcp', 'openstack'} and
            len({row.get('target_id') for row in targets}) == 3 and all(row.get('target_id') and row.get('status') == 'verified' and
            row.get('release_sha') == source_sha for row in targets), 'THREE_TARGET_RELEASE_NOT_VERIFIED')


def apps_paths(config):
    require(set(config) == {'repository', 'branch'} and config['repository'] == 'Jasmin-Softbank/railshot-apps' and
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_/-]*', config['branch']) and '..' not in config['branch'], 'APPS_CONFIG_INVALID')
    base = 'repos/' + config['repository']
    return base + '/contents/' + WORKFLOW, base + '/actions/variables/PLATFORM_REF'


def promote_apps(config, source_sha, verified_receipt, state_path, *, gh=github):
    require(re.fullmatch(r'[a-f0-9]{40}', source_sha or ''), 'SOURCE_SHA_REQUIRED')
    approved_receipt(verified_receipt, source_sha)
    require(bootstrap.native(['git', 'rev-parse', 'HEAD'], cwd=ROOT).decode().strip() == source_sha, 'SOURCE_CHECKOUT_DIFFERS')
    require(not Path(state_path).exists(), 'APPS_STATE_EXISTS_OBSERVE_OR_ROLLBACK')
    template = bootstrap.native(['git', 'show', source_sha + ':ci/workflows/railshot-deploy.yml'], cwd=ROOT).decode()
    require(template.count('${{ vars.PLATFORM_REF }}') == 5, 'WORKFLOW_PLATFORM_PINS_CHANGED')
    content = base64.b64encode(template.replace('${{ vars.PLATFORM_REF }}', source_sha).encode()).decode()
    workflow_path, variable_path = apps_paths(config)
    old_workflow = gh(workflow_path + '?ref=' + config['branch'])
    old_variable = gh(variable_path)
    require(re.fullmatch(r'[a-f0-9]{40}', old_variable.get('value', '')), 'EXISTING_PLATFORM_REF_INVALID')
    state = {'version': 1, 'kind': 'apps', 'source_sha': source_sha, 'config': config, 'status': 'promoting',
             'old_workflow': {'sha': old_workflow['sha'], 'content': old_workflow['content']},
             'old_variable': old_variable, 'content': content,
             'workflow_sha': hashlib.sha1(b'blob ' + str(len(base64.b64decode(content))).encode() + b'\0' + base64.b64decode(content)).hexdigest(),
             'variable': None}
    bootstrap.write(state_path, state)
    try:
        if base64.b64decode(old_workflow['content']) != base64.b64decode(content):
            try:
                gh(workflow_path, 'PUT', {'message': 'Promote tested platform ' + source_sha, 'content': content,
                                        'sha': old_workflow['sha'], 'branch': config['branch']})
            except Exception:
                pass
        observed = gh(workflow_path + '?ref=' + config['branch'])
        require(base64.b64decode(observed['content']) == base64.b64decode(content), 'APPS_WORKFLOW_NOT_VERIFIED')
        state['workflow_sha'] = observed['sha']
        bootstrap.write(state_path, state)
        require(gh(variable_path) == old_variable, 'PLATFORM_REF_CONCURRENT_CHANGE')
        # GitHub variables have no CAS API. Pin all workflow reads first and never
        # replay an uncertain PATCH; retain both states for explicit recovery.
        if old_variable['value'] != source_sha:
            try:
                gh(variable_path, 'PATCH', {'name': 'PLATFORM_REF', 'value': source_sha})
            except Exception:
                pass
        current = gh(variable_path)
        require(current['value'] == source_sha, 'PLATFORM_REF_NOT_VERIFIED')
        state['variable'] = current
        require(gh(workflow_path + '?ref=' + config['branch'])['sha'] == state['workflow_sha'], 'APPS_WORKFLOW_CONCURRENT_CHANGE')
        state['status'] = 'verified'
        bootstrap.write(state_path, state)
        return {'status': 'verified', 'source_sha': source_sha, 'workflow_sha': state['workflow_sha'], 'platform_ref': source_sha}
    except Exception:
        state['status'] = 'incomplete'
        bootstrap.write(state_path, state)
        raise


def rollback_apps(state_path, *, gh=github):
    state = bootstrap.private(state_path)
    require(state['kind'] == 'apps' and state['status'] != 'rolled_back', 'APPS_ROLLBACK_STATE_INVALID')
    workflow_path, variable_path = apps_paths(state['config'])
    current = gh(workflow_path + '?ref=' + state['config']['branch'])
    variable = gh(variable_path)
    allowed = state['variable'] or state['old_variable']
    require(variable == allowed, 'PLATFORM_REF_CONCURRENT_CHANGE')
    require(current['sha'] in {state['old_workflow']['sha'], state['workflow_sha']}, 'APPS_WORKFLOW_CONCURRENT_CHANGE')
    if variable['value'] != state['old_variable']['value']:
        gh(variable_path, 'PATCH', {'name': 'PLATFORM_REF', 'value': state['old_variable']['value']})
        state['variable'] = gh(variable_path)
        require(state['variable']['value'] == state['old_variable']['value'], 'PLATFORM_REF_ROLLBACK_NOT_VERIFIED')
        bootstrap.write(state_path, state)
    if current['sha'] != state['old_workflow']['sha']:
        gh(workflow_path, 'PUT', {'message': 'Rollback platform promotion ' + state['source_sha'],
                                'content': state['old_workflow']['content'], 'sha': current['sha'], 'branch': state['config']['branch']})
    observed = gh(workflow_path + '?ref=' + state['config']['branch'])
    require(observed['sha'] == state['old_workflow']['sha'], 'APPS_ROLLBACK_NOT_VERIFIED')
    state['status'] = 'rolled_back'
    bootstrap.write(state_path, state)
    return {'status': 'rolled_back', 'source_sha': state['source_sha']}


def verify_apps(state_path, *, gh=github):
    """Read back a promotion, including recovery after an uncertain acknowledgement."""
    state = bootstrap.private(state_path)
    require(state['kind'] == 'apps' and state['status'] != 'rolled_back', 'APPS_VERIFY_STATE_INVALID')
    workflow_path, variable_path = apps_paths(state['config'])
    current = gh(workflow_path + '?ref=' + state['config']['branch'])
    variable = gh(variable_path)
    require(current['sha'] == state['workflow_sha'] and
            base64.b64decode(current['content']) == base64.b64decode(state['content']) and
            variable['value'] == state['source_sha'], 'APPS_PROMOTION_READBACK_DIFFERS')
    state['variable'] = variable
    state['status'] = 'verified'
    bootstrap.write(state_path, state)
    return {'status': 'verified', 'source_sha': state['source_sha'], 'workflow_sha': current['sha'], 'platform_ref': variable['value']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('discover', 'apply', 'verify', 'rollback', 'promote', 'rollback-apps'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--images', type=Path)
    parser.add_argument('--source-sha')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    try:
        require(args.mode == 'discover' or args.state and args.state.is_absolute(), 'ABSOLUTE_STATE_REQUIRED')
        if args.mode == 'discover':
            result = discover_workers()
        elif args.mode == 'apply':
            result = apply_workers(bootstrap.private(args.config), bootstrap.private(args.images), args.source_sha, args.state)
        elif args.mode == 'promote':
            result = promote_apps(bootstrap.private(args.config), args.source_sha, bootstrap.private(args.receipt), args.state)
        elif args.mode == 'verify':
            result = (verify_apps if bootstrap.private(args.state)['kind'] == 'apps' else verify_workers)(args.state)
        else:
            result = {'rollback': rollback_workers, 'rollback-apps': rollback_apps}[args.mode](args.state)
        print(json.dumps(result))
        return 0
    except bootstrap.Blocked as error:
        print(json.dumps({'status': 'blocked', 'code': str(error)}))
        return 1
    except Exception:
        print(json.dumps({'status': 'blocked', 'code': 'PLATFORM_WORKERS_NOT_VERIFIED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
