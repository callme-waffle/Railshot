#!/usr/bin/env python3
"""Replenish a reviewed pool of isolated ephemeral build runners; one bounded CronJob tick, stdlib only."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

NAMESPACE = 'railshot-build'
SECRET = 'railshot-build-runner-registration'
ANNOTATION = 'railshot.io/runner-controller'
PREFIX = 'railshot-runner-'
MAX_BYTES = 1024 * 1024


class Blocked(Exception):
    """Safe reason code, never the HTTP response or a credential-bearing exception."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def validate_template(job, policy):
    try:
        if policy['version'] != 1 or hashlib.sha256(canonical(job)).hexdigest() != policy['template_sha256']:
            raise ValueError()
        if not re.fullmatch(r'Jasmin-Softbank/[A-Za-z0-9_.-]+', policy['repository']):
            raise ValueError()
        if not re.fullmatch(r'ghcr\.io/jasmin-softbank/railshot-ci-runner@sha256:[a-f0-9]{64}', policy['image']):
            raise ValueError()
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', policy['node']):
            raise ValueError()
        if type(policy.get('runner_count', 1)) is not int or not 1 <= policy.get('runner_count', 1) <= 64:
            raise ValueError()
        pod = job['spec']['template']['spec']
        runner, = pod['containers']
        env = {item['name']: item for item in runner['env']}
        registration = next(v for v in pod['volumes'] if v['name'] == 'registration-token')
        checks = [job['apiVersion'] == 'batch/v1', job['kind'] == 'Job',
                  job['metadata'] == {'name': 'railshot-runner-template', 'namespace': NAMESPACE},
                  job['spec']['backoffLimit'] == 0, job['spec']['activeDeadlineSeconds'] == 7200,
                  pod['restartPolicy'] == 'Never', pod['serviceAccountName'] == 'railshot-build-runner',
                  pod['automountServiceAccountToken'] is False,
                  pod['hostPID'] is False, pod['hostIPC'] is False, pod['hostNetwork'] is True,
                  pod['nodeSelector'] == {'kubernetes.io/arch': 'amd64', 'kubernetes.io/hostname': policy['node'], 'railshot.io/node-role': 'build'},
                  runner['image'] == policy['image'], runner['name'] == 'runner',
                  runner['securityContext'] == {'runAsUser': 0, 'runAsGroup': 0, 'privileged': False, 'allowPrivilegeEscalation': False, 'capabilities': {'add': ['NET_ADMIN']}, 'seccompProfile': {'type': 'Localhost', 'localhostProfile': 'railshot-codex-bwrap.json'}, 'appArmorProfile': {'type': 'Localhost', 'localhostProfile': 'railshot-codex-bwrap'}},
                  env['RAILSHOT_RUNNER_URL'].get('value') == 'https://github.com/' + policy['repository'],
                  env['RAILSHOT_RUNNER_LABELS'].get('value') == 'railshot-ci',
                  set(env) == {'RAILSHOT_RUNNER_URL', 'RAILSHOT_RUNNER_NAME', 'RAILSHOT_RUNNER_LABELS', 'RAILSHOT_POD_NAMESPACE', 'RAILSHOT_POD_NAME', 'RAILSHOT_POD_UID'},
                  len(runner['env']) == len(env), not runner.get('envFrom'), not runner.get('command'), not runner.get('args'),
                  not any('secretKeyRef' in item.get('valueFrom', {}) for item in env.values()),
                  registration['secret']['secretName'] == SECRET,
                  not pod.get('initContainers'), not pod.get('ephemeralContainers')]
        if not all(checks):
            raise ValueError()
    except (KeyError, TypeError, ValueError, StopIteration):
        raise Blocked('UNREVIEWED_RUNNER_TEMPLATE') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, base, token, *, ca=None, timeout=5, deadline=None):
        self.base, self.token, self.timeout = base.rstrip('/'), token, timeout
        self.deadline = deadline if deadline is not None else time.monotonic() + 45
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context(cafile=ca)))

    def request(self, method, path, body=None, *, patch=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Blocked('HTTP_DEADLINE')
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'railshot-runner-controller'}
        if body is not None:
            headers['Content-Type'] = 'application/merge-patch+json' if patch else 'application/json'
        try:
            with self.opener.open(Request(self.base + path, method=method, headers=headers,
                                          data=canonical(body) if body is not None else None),
                                  timeout=min(self.timeout, remaining)) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise Blocked('HTTP_RESPONSE_TOO_LARGE')
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise Blocked('INVALID_HTTP_RESPONSE')
                return value
        except HTTPError as error:
            raise Blocked('HTTP_' + str(error.code)) from None
        except (OSError, URLError, ValueError):
            raise Blocked('HTTP_UNCERTAIN') from None


def terminal(job):
    conditions = job.get('status', {}).get('conditions', [])
    for kind in ('Failed', 'Complete'):
        if any(item.get('type') == kind and item.get('status') == 'True' for item in conditions):
            return kind
    return None


class Controller:
    def __init__(self, kube, github, template, policy):
        validate_template(template, policy)
        self.kube, self.github, self.template, self.policy = kube, github, template, policy
        self.slots = ['slot-' + str(i) for i in range(policy.get('runner_count', 1))]
        self.jobs_path = '/apis/batch/v1/namespaces/' + NAMESPACE + '/jobs'
        self.secret_path = '/api/v1/namespaces/' + NAMESPACE + '/secrets/' + SECRET

    def jobs(self):
        items, cursor = [], ''
        for _ in range(10):
            page = self.kube.request('GET', self.jobs_path + '?' + urlencode({'limit': 100, 'continue': cursor}))
            items.extend(page['items'])
            cursor = page.get('metadata', {}).get('continue', '')
            if not cursor:
                return [j for j in items if j.get('spec', {}).get('template', {}).get('metadata', {}).get('labels', {}).get('app') == 'railshot-build-runner']
        raise Blocked('JOB_LIST_LIMIT')

    def demand(self):
        root = '/repos/' + self.policy['repository'] + '/actions'
        # Include running jobs: one active node must not prevent scaling for the
        # next queued job. Stop scanning as soon as the reviewed pool is full.
        demand, seen_runs = set(), set()
        for status in ('queued', 'in_progress'):
            for page in range(1, 5):
                runs = self.github.request('GET', root + '/runs?' + urlencode(
                    {'status': status, 'per_page': 20, 'page': page}))['workflow_runs']
                for run in runs:
                    if not isinstance(run.get('id'), int) or run['id'] <= 0:
                        raise Blocked('INVALID_GITHUB_RUN')
                    if run['id'] in seen_runs:
                        continue
                    seen_runs.add(run['id'])
                    jobs = self.github.request('GET', root + '/runs/' + str(run['id']) + '/jobs?filter=latest&per_page=100')['jobs']
                    for job in jobs:
                        labels = {label.lower() for label in job.get('labels', [])}
                        if job.get('status') in ('queued', 'in_progress') and 'railshot-ci' in labels:
                            if not labels <= {'self-hosted', 'linux', 'x64', 'railshot-ci'}:
                                raise Blocked('UNSUPPORTED_RUNNER_LABELS')
                            if type(job.get('id')) is not int or job['id'] <= 0:
                                raise Blocked('INVALID_GITHUB_JOB')
                            demand.add(job['id'])
                            if len(demand) >= len(self.slots):
                                return len(demand)
                if len(runs) < 20:
                    break
        return len(demand)

    def save(self, secret, state, token=None):
        patch = {'metadata': {'resourceVersion': secret['metadata']['resourceVersion'],
                              'annotations': {ANNOTATION: canonical(state).decode()}}}
        if token is not None:
            patch['data'] = {'token': base64.b64encode(token.encode()).decode()}
        return self.kube.request('PATCH', self.secret_path, patch, patch=True)

    def tick(self):
        secret = self.kube.request('GET', self.secret_path)
        if secret.get('metadata', {}).get('name') != SECRET or secret['metadata'].get('namespace') != NAMESPACE:
            raise Blocked('INVALID_REGISTRATION_SECRET')
        raw = secret['metadata'].get('annotations', {}).get(ANNOTATION)
        try:
            state = json.loads(raw) if raw else {'version': 2, 'slots': {}}
            if state.get('version') == 1 and set(state) == {'version', 'failures', 'pending'}:
                state = {'version': 2, 'slots': {'slot-0': {k: state[k] for k in ('failures', 'pending')}}}
            if set(state) != {'version', 'slots'} or state['version'] != 2 or not isinstance(state['slots'], dict):
                raise ValueError()
            # Changing a pool cannot silently abandon durable create intents.
            if set(state['slots']) - set(self.slots):
                raise ValueError()
            for node in self.slots:
                slot = state['slots'].setdefault(node, {'failures': 0, 'pending': None})
                if (set(slot) != {'failures', 'pending'} or type(slot['failures']) is not int
                        or not 0 <= slot['failures'] <= 3 or slot['pending'] is not None
                        and not re.fullmatch(PREFIX + r'[a-f0-9]{16}', slot['pending'])):
                    raise ValueError()
        except (ValueError, TypeError):
            raise Blocked('INVALID_CONTROLLER_STATE') from None
        jobs = self.jobs()
        live = [job for job in jobs if not terminal(job)]
        # A legacy runner uses the old shared checkout; let it finish before
        # admitting isolated parallel runners during an upgrade.
        if any('railshot.io/runner-slot' not in job.get('metadata', {}).get('annotations', {}) for job in live):
            return {'state': 'active'}
        occupied = {job['metadata']['annotations']['railshot.io/runner-slot'] for job in live}
        if not occupied <= set(self.slots) or len(occupied) != len(live):
            raise Blocked('RUNNER_SLOT_BINDING_MISMATCH')
        if len(live) >= len(self.slots):
            return {'state': 'active'}
        unknown, changed = [], False
        for slot_id, slot in state['slots'].items():
            if not slot['pending']:
                continue
            previous = next((job for job in jobs if job['metadata']['name'] == slot['pending']), None)
            if previous is None:
                unknown.append(slot_id); occupied.add(slot_id)
                continue
            actual = previous['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname']
            if actual != self.policy['node'] or previous.get('metadata', {}).get('annotations', {}).get('railshot.io/runner-slot', 'slot-0') != slot_id:
                raise Blocked('RUNNER_NODE_BINDING_MISMATCH')
            if terminal(previous):
                slot['failures'] = min(3, slot['failures'] + 1) if terminal(previous) == 'Failed' else 0
                slot['pending'] = None
                changed = True
        if changed:
            secret = self.save(secret, state)
        available = [slot_id for slot_id in self.slots if slot_id not in occupied and state['slots'][slot_id]['failures'] < 3]
        if not available:
            return {'state': 'unknown', 'code': 'PENDING_JOB_NOT_OBSERVED'} if unknown else {'state': 'blocked', 'code': 'CONSECUTIVE_RUNNER_FAILURES'}
        count = min(len(available), max(0, self.demand() - len(occupied)))
        if not count:
            return {'state': 'unknown', 'code': 'PENDING_JOB_NOT_OBSERVED'} if unknown else {'state': 'active' if live else 'idle'}
        registration = self.github.request('POST', '/repos/' + self.policy['repository'] + '/actions/runners/registration-token', {})
        token = registration.get('token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{1,4096}', token):
            raise Blocked('INVALID_REGISTRATION_TOKEN')
        created = []
        for slot_id in available[:count]:
            name = PREFIX + uuid.uuid4().hex[:16]
            job = copy.deepcopy(self.template)
            job['metadata']['name'] = name
            pod = job['spec']['template']['spec']
            job['metadata']['annotations'] = {'railshot.io/runner-slot': slot_id}
            for item in pod['containers'][0]['env']:
                if item['name'] == 'RAILSHOT_RUNNER_NAME':
                    item['value'] = name
            state['slots'][slot_id]['pending'] = name
            secret = self.save(secret, state, token)  # CAS intent before each create.
            try:
                self.kube.request('POST', self.jobs_path, job)
            except Blocked:
                return {'state': 'unknown', 'code': 'JOB_CREATE_UNCERTAIN'}
            created.append(name)
        return {'state': 'created', 'job': created[0], **({'jobs': created, 'capacity': len(self.slots)} if len(self.slots) > 1 else {})}


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_BYTES:
        raise Blocked('CONFIG_TOO_LARGE')
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', default='/etc/railshot-controller/job.json')
    parser.add_argument('--policy', default='/etc/railshot-controller/policy.json')
    args = parser.parse_args()
    try:
        deadline = time.monotonic() + 45
        kube = Client('https://kubernetes.default.svc', Path('/run/railshot-kubernetes/token').read_text().strip(),
                      ca='/run/railshot-kubernetes/ca.crt', deadline=deadline)
        github = Client('https://api.github.com', Path('/run/secrets/github/token').read_text().strip(), deadline=deadline)
        result = Controller(kube, github, read_json(args.template), read_json(args.policy)).tick()
    except Blocked as error:
        result = {'state': 'blocked', 'code': str(error)}
    except (OSError, ValueError, KeyError, TypeError):
        result = {'state': 'blocked', 'code': 'INVALID_CONTROLLER_INPUT'}
    print(json.dumps(result))
    return 1 if result['state'] in ('unknown', 'blocked') else 0


if __name__ == '__main__':
    raise SystemExit(main())
