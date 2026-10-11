"""Offline HTTP tests exercise the real controller clients, durable intent and reviewed renderer."""
import base64
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import socket
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import replenish

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('controller_platform_render', ROOT / 'deployment/scripts/render-platform.py')
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)


def rendered(count=1):
    images = {name: f'ghcr.io/jasmin-softbank/railshot-{name}@sha256:' + 'a' * 64 for name in ('api', 'ci-runner')}
    return render.render_build_controller(images, 'https://github.com/Jasmin-Softbank/railshot-apps', 'build-worker', count)['items']


class FakeAPI:
    def __init__(self):
        self.jobs = []
        self.secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {
            'name': replenish.SECRET, 'namespace': replenish.NAMESPACE, 'resourceVersion': '1', 'annotations': {'other': 'keep'}},
            'data': {'token': base64.b64encode(b'initial-token').decode(), 'unrelated': 'a2VlcA=='}}
        self.calls, self.issued, self.created = [], 0, []
        self.queued_id = 1
        self.demand = 1
        self.labels = ['self-hosted', 'Linux', 'X64', 'railshot-ci']
        self.lose_create = self.drop_create = self.conflict_patch = False
        self.deny_jobs = False
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def handle_request(self):
                path = urlsplit(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or 'null')
                state.calls.append((self.command, path.path, body, dict(self.headers)))
                github = path.path.startswith('/repos/')
                if self.headers.get('Authorization') != ('Bearer long-github-token' if github else 'Bearer kube-token'):
                    return self.reply(403, {'message': 'unauthorized'})
                if path.path.endswith('/secrets/' + replenish.SECRET):
                    if self.command == 'GET':
                        return self.reply(200, state.secret)
                    if self.command == 'PATCH':
                        if state.conflict_patch or body['metadata']['resourceVersion'] != state.secret['metadata']['resourceVersion']:
                            return self.reply(409, {'message': 'conflict'})
                        assert self.headers.get('Content-Type') == 'application/merge-patch+json'
                        state.secret['metadata']['resourceVersion'] = str(int(state.secret['metadata']['resourceVersion']) + 1)
                        state.secret['metadata']['annotations'].update(body['metadata']['annotations'])
                        state.secret['data'].update(body.get('data', {}))
                        return self.reply(200, state.secret)
                if path.path == '/apis/batch/v1/namespaces/railshot-build/jobs':
                    if state.deny_jobs:
                        return self.reply(403, {'message': 'sensitive upstream detail'})
                    if self.command == 'GET':
                        return self.reply(200, {'items': state.jobs, 'metadata': {}})
                    if self.command == 'POST':
                        intent = json.loads(state.secret['metadata']['annotations'][replenish.ANNOTATION])
                        slot = body['metadata']['annotations']['railshot.io/runner-slot']
                        assert intent['slots'][slot]['pending'] == body['metadata']['name']
                        state.created.append(body['metadata']['name'])
                        if not state.drop_create:
                            state.jobs.append(copy.deepcopy(body))
                        if state.lose_create:
                            self.connection.shutdown(socket.SHUT_RDWR)
                            self.connection.close()
                            return
                        return self.reply(201, body)
                if path.path.endswith('/actions/runs'):
                    status = parse_qs(path.query).get('status', [''])[0]
                    return self.reply(200, {'workflow_runs': [{'id': state.queued_id}] if state.queued_id and status == 'queued' else []})
                if path.path.endswith('/jobs') and github:
                    return self.reply(200, {'jobs': [{'id': state.queued_id + i, 'status': 'queued', 'labels': state.labels} for i in range(state.demand)]})
                if path.path.endswith('/actions/runners/registration-token') and self.command == 'POST':
                    state.issued += 1
                    return self.reply(201, {'token': 'short-registration-' + str(state.issued), 'expires_at': '2099-01-01T00:00:00Z'})
                return self.reply(404, {'message': 'unexpected fake endpoint'})

            do_GET = do_POST = do_PATCH = handle_request

            def reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def controller(self, count=1):
        config = next(item for item in rendered(count) if item['kind'] == 'ConfigMap')['data']
        return replenish.Controller(replenish.Client(self.base, 'kube-token'), replenish.Client(self.base, 'long-github-token'),
                                    json.loads(config['job.json']), json.loads(config['policy.json']))

    def finish(self, kind='Complete'):
        self.jobs[-1]['status'] = {'conditions': [{'type': kind, 'status': 'True'}]}


class ReplenishTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.addCleanup(self.api.close)

    def test_two_queued_requests_create_distinct_runners_and_active_is_noop(self):
        first = self.api.controller().tick()
        self.assertEqual(first['state'], 'created')
        before = len(self.api.calls)
        self.assertEqual(self.api.controller().tick(), {'state': 'active'})
        self.assertTrue(all('/repos/' not in call[1] for call in self.api.calls[before:]))
        self.assertEqual(self.api.issued, 1)
        self.api.finish()
        self.api.queued_id = 2
        second = self.api.controller().tick()
        self.assertEqual(second['state'], 'created')
        self.assertNotEqual(first['job'], second['job'])
        self.assertEqual(self.api.created, [first['job'], second['job']])
        self.assertEqual(self.api.issued, 2)
        self.assertEqual(self.api.secret['metadata']['annotations']['other'], 'keep')
        self.assertEqual(self.api.secret['data']['unrelated'], 'a2VlcA==')
        for job in self.api.jobs:
            serialized = json.dumps(job)
            self.assertNotIn('long-github-token', serialized)
            self.assertNotIn('short-registration-', serialized)
            env = {item['name']: item.get('value') for item in job['spec']['template']['spec']['containers'][0]['env']}
            self.assertEqual(env['RAILSHOT_RUNNER_NAME'], job['metadata']['name'])
        self.api.finish()
        self.api.queued_id = None
        self.assertEqual(self.api.controller().tick(), {'state': 'idle'})

    def test_lost_create_response_is_observed_next_tick_without_second_dispatch(self):
        self.api.lose_create = True
        self.assertEqual(self.api.controller().tick(), {'state': 'unknown', 'code': 'JOB_CREATE_UNCERTAIN'})
        self.assertEqual(self.api.controller().tick(), {'state': 'active'})
        self.assertEqual(len(self.api.created), 1)
        self.api.jobs.clear()  # History loss cannot be interpreted as permission to create again.
        self.assertEqual(self.api.controller().tick(), {'state': 'unknown', 'code': 'PENDING_JOB_NOT_OBSERVED'})
        self.assertEqual(self.api.issued, 1)

    def test_three_failed_runner_jobs_persist_blocked_state_across_ticks(self):
        for _ in range(3):
            self.assertEqual(self.api.controller().tick()['state'], 'created')
            self.api.finish('Failed')
        for _ in range(2):
            self.assertEqual(self.api.controller().tick(), {'state': 'blocked', 'code': 'CONSECUTIVE_RUNNER_FAILURES'})
        self.api.jobs.clear()  # TTL cleanup does not reset the failure guard.
        self.assertEqual(self.api.controller().tick()['state'], 'blocked')
        self.assertEqual(self.api.issued, 3)

    def test_secret_conflict_or_api_permission_failure_never_creates_a_job(self):
        self.api.conflict_patch = True
        with self.assertRaisesRegex(replenish.Blocked, '^HTTP_409$'):
            self.api.controller().tick()
        self.assertEqual(self.api.created, [])
        self.api.conflict_patch = False
        self.api.deny_jobs = True
        with self.assertRaisesRegex(replenish.Blocked, '^HTTP_403$'):
            self.api.controller().tick()
        self.assertEqual(self.api.created, [])

    def test_nonmatching_or_extra_runner_labels_never_create_a_job(self):
        self.api.labels = ['ubuntu-latest']
        self.assertEqual(self.api.controller().tick(), {'state': 'idle'})
        self.api.labels = ['self-hosted', 'railshot-ci', 'unapproved-node']
        with self.assertRaisesRegex(replenish.Blocked, '^UNSUPPORTED_RUNNER_LABELS$'):
            self.api.controller().tick()
        self.assertEqual(self.api.issued, 0)

    def test_unreviewed_digest_registry_or_privileges_fail_before_network(self):
        config = next(item for item in rendered() if item['kind'] == 'ConfigMap')['data']
        template, policy = json.loads(config['job.json']), json.loads(config['policy.json'])
        for mutate, resign in [
            (lambda job: job['spec'].__setitem__('backoffLimit', 1), False),
            (lambda job: job['spec']['template']['spec']['containers'][0].__setitem__('image', 'attacker.invalid/runner:latest'), True),
            (lambda job: job['spec']['template']['spec']['containers'][0]['securityContext'].__setitem__('privileged', True), True),
            (lambda job: job['spec']['template']['spec'].__setitem__('serviceAccountName', 'cluster-admin'), True),
        ]:
            altered, changed_policy = copy.deepcopy(template), copy.deepcopy(policy)
            mutate(altered)
            if resign:
                changed_policy['template_sha256'] = hashlib.sha256(replenish.canonical(altered)).hexdigest()
            with self.assertRaisesRegex(replenish.Blocked, '^UNREVIEWED_RUNNER_TEMPLATE$'):
                replenish.Controller(None, None, altered, changed_policy)
        self.assertEqual(self.api.calls, [])

    def test_rendered_controller_reuses_api_and_has_only_named_secret_permissions(self):
        documents = rendered()
        self.assertFalse(any(item['kind'] == 'Job' for item in documents))
        role = next(item for item in documents if item['kind'] == 'Role' and item['metadata']['name'] == 'railshot-build-controller')
        self.assertEqual(role['rules'], [
            {'apiGroups': ['batch'], 'resources': ['jobs'], 'verbs': ['get', 'list', 'create']},
            {'apiGroups': [''], 'resources': ['secrets'], 'resourceNames': [replenish.SECRET], 'verbs': ['get', 'update', 'patch']}])
        cron = next(item for item in documents if item['kind'] == 'CronJob')
        self.assertEqual(cron['spec']['schedule'], '* * * * *')
        self.assertEqual(cron['spec']['concurrencyPolicy'], 'Forbid')
        pod = cron['spec']['jobTemplate']['spec']['template']['spec']
        self.assertEqual(pod['nodeSelector']['railshot.io/node-role'], 'platform')
        self.assertIn('railshot-api@sha256:', pod['containers'][0]['image'])
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertNotIn('hostNetwork', pod)
        self.assertFalse(any('hostPath' in volume for volume in pod['volumes']))
        self.assertEqual([v['secret']['secretName'] for v in pod['volumes'] if 'secret' in v], ['railshot-runner-controller-github'])

    def test_twelve_runner_slots_on_one_node_and_only_freed_slot_is_refilled(self):
        self.api.demand = 20
        result = self.api.controller(12).tick()
        self.assertEqual(result['capacity'], 12)
        self.assertEqual(len(result['jobs']), 12)
        assigned = [job['metadata']['annotations']['railshot.io/runner-slot'] for job in self.api.jobs]
        self.assertEqual(len(set(assigned)), 12)
        self.assertEqual({job['spec']['template']['spec']['nodeSelector']['kubernetes.io/hostname'] for job in self.api.jobs}, {'build-worker'})
        self.assertEqual(self.api.controller(12).tick(), {'state': 'active'})
        self.api.finish()
        refill = self.api.controller(12).tick()
        self.assertEqual(len(refill['jobs']), 1)
        self.assertEqual(self.api.jobs[-1]['metadata']['annotations']['railshot.io/runner-slot'], 'slot-11')
        self.assertEqual(len([job for job in self.api.jobs if not replenish.terminal(job)]), 12)

    def test_uncertain_create_reserves_one_slot_and_other_slots_can_progress(self):
        self.api.demand = 3
        self.api.drop_create = self.api.lose_create = True
        self.assertEqual(self.api.controller(3).tick()['code'], 'JOB_CREATE_UNCERTAIN')
        self.api.drop_create = self.api.lose_create = False
        result = self.api.controller(3).tick()
        self.assertEqual(len(result['jobs']), 2)
        self.assertEqual({job['metadata']['annotations']['railshot.io/runner-slot'] for job in self.api.jobs}, {'slot-1', 'slot-2'})
        self.assertEqual(self.api.controller(3).tick()['code'], 'PENDING_JOB_NOT_OBSERVED')
        self.assertEqual(len(self.api.created), 3)

    def test_legacy_intent_migrates_and_active_legacy_workspace_blocks_expansion(self):
        self.api.secret['metadata']['annotations'][replenish.ANNOTATION] = json.dumps({'version': 1, 'failures': 3, 'pending': None})
        self.api.demand = 2
        self.assertEqual(self.api.controller(2).tick()['state'], 'created')
        self.assertEqual(self.api.jobs[0]['metadata']['annotations']['railshot.io/runner-slot'], 'slot-1')
        state = json.loads(self.api.secret['metadata']['annotations'][replenish.ANNOTATION])
        self.assertEqual(state['slots']['slot-0']['failures'], 3)
        with self.assertRaisesRegex(replenish.Blocked, 'INVALID_CONTROLLER_STATE'):
            self.api.controller().tick()
        self.api.jobs[0]['metadata'].pop('annotations')
        self.assertEqual(self.api.controller(2).tick(), {'state': 'active'})

    def test_http_deadline_is_bounded_and_never_exposes_token(self):
        client = replenish.Client(self.api.base, 'must-not-be-printed', deadline=time.monotonic() - 1)
        with self.assertRaisesRegex(replenish.Blocked, '^HTTP_DEADLINE$'):
            client.request('GET', '/')
        self.assertEqual(self.api.calls, [])


if __name__ == '__main__':
    unittest.main()
