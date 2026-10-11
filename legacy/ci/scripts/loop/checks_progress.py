"""Bounded, best-effort GitHub Checks transport for safe supervisor observations.

This channel never decides gate outcomes. Only bounded, redacted change descriptions
from host-confirmed writes are forwarded; raw provider output and errors stay private.
The job token lives only in this process; callers remove it from the environment
before any child process starts.
"""
from datetime import datetime, timezone
import http.client
import json
import os
import re
import sys
import time
from urllib.parse import quote

NAME = 'Railshot agent events'
APP_ID = 15368  # github-actions on github.com; this transport has no configurable host.
MAX_BYTES, MAX_ITEMS, MAX_RESPONSE = 60000, 60, 2 * 1024 * 1024
OUTCOMES = {'RUNNING', 'PASS', 'FAIL', 'BLOCKED', 'UNKNOWN', 'NOT_RUN', 'INCOMPLETE'}
EVENTS = {'loop.started', 'loop.completed', 'agent.heartbeat', 'agent.observation',
          'gate.layer.started', 'gate.layer.completed', 'gate.layer.heartbeat', 'agent.repair'}


def integer(value):
    return type(value) is int and 0 <= value <= 2 ** 53 - 1


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value) is not None


def utc(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)', value):
        raise ValueError('invalid timestamp')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).isoformat().replace('+00:00', 'Z')


def row(event):
    """Project canonical events through an allowlist, including nested SDK counters."""
    name, native = event.get('event_name'), event.get('run_id')
    if name not in EVENTS or not identifier(native):
        raise ValueError('invalid event binding')
    result = {'occurred_at': utc(event['occurred_at']), 'event_name': name, 'native_run_id': native}
    attributes = event.get('attributes') or {}
    if name.startswith('loop.'):
        count = attributes.get('sdk_invocations')
        if event.get('phase') != 'loop' or event.get('outcome') not in OUTCOMES or not (count is None or integer(count)):
            raise ValueError('invalid loop observation')
        result.update(phase='loop', outcome=event['outcome'], sdk_invocations=count)
        if 'agent_budget' in attributes:
            budget = attributes['agent_budget']
            if (not isinstance(budget, dict) or set(budget) != {'enabled', 'max_invocations'}
                    or type(budget['enabled']) is not bool or type(budget['max_invocations']) is not int
                    or not 0 <= budget['max_invocations'] <= 16
                    or budget['enabled'] != (budget['max_invocations'] > 0)):
                raise ValueError('invalid agent budget')
            result['agent_budget'] = dict(budget)
    elif name == 'agent.repair':
        repair = attributes.get('repair')
        if (not identifier(event.get('attempt_id')) or not isinstance(repair, dict)
                or set(repair) != {'state', 'role', 'changes', 'omitted_changes', 'failure_layer'}
                or repair['state'] not in {'verifying', 'succeeded', 'failed', 'unknown'}
                or repair['role'] not in {'adapter', 'fixer'}
                or repair['failure_layer'] not in {None, 'L0', 'L1', 'Q', 'L2', 'L3', 'L4'}
                or not integer(repair['omitted_changes']) or not isinstance(repair['changes'], list)
                or len(repair['changes']) > 24):
            raise ValueError('invalid repair observation')
        for change in repair['changes']:
            if (not isinstance(change, dict) or set(change) != {'path', 'summary', 'status'}
                    or not isinstance(change['path'], str) or len(change['path']) > 240
                    or not re.fullmatch(r'[A-Za-z0-9_./@-]+', change['path'])
                    or change['path'].startswith('/') or any(p in ('', '.', '..') for p in change['path'].split('/'))
                    or not isinstance(change['summary'], str) or not 1 <= len(change['summary']) <= 300
                    or change['status'] != 'applied'):
                raise ValueError('invalid repair change')
        # Only these bounded, host-confirmed fields may cross the Checks boundary.
        from diagnostics import redact
        repair = {**repair, 'changes': [{**c, 'summary': redact(c['summary'])} for c in repair['changes']]}
        result.update(attempt_id=event['attempt_id'], repair=repair)
    elif name.startswith('gate.layer.'):

        if (not identifier(event.get('attempt_id')) or event.get('phase') not in {'L0', 'L1', 'Q', 'L2', 'L4', 'L3'}
                or event.get('outcome') not in OUTCOMES
                or any(not integer(attributes.get(key)) for key in ('completed_steps', 'total_steps'))
                or not 1 <= attributes['total_steps'] <= 6 or attributes['completed_steps'] > attributes['total_steps']):
            raise ValueError('invalid gate observation')
        duration = attributes.get('duration_s', 0)
        if type(duration) not in (int, float) or not 0 <= duration <= 86400:
            raise ValueError('invalid gate duration')
        result.update(attempt_id=event['attempt_id'], phase=event['phase'], outcome=event['outcome'],
                      completed_steps=attributes['completed_steps'], total_steps=attributes['total_steps'],
                      duration_s=duration)
    else:
        if (not identifier(event.get('attempt_id')) or attributes.get('role') not in {'adapter', 'fixer'}
                or attributes.get('provider') not in {'codex', 'claude'}
                or attributes.get('snapshot_state') not in {'current', 'unavailable'}
                or not integer(attributes.get('elapsed_ms'))
                or any(type(attributes.get(key)) is not bool for key in ('process_running', 'sdk_activity_since_previous'))
                or not (attributes.get('last_sdk_event_age_ms') is None or integer(attributes['last_sdk_event_age_ms']))):
            raise ValueError('invalid agent observation')
        result['attempt_id'] = event['attempt_id']
        for key in ('role', 'provider', 'elapsed_ms', 'process_running', 'snapshot_state',
                    'sdk_activity_since_previous', 'last_sdk_event_age_ms'):
            result[key] = attributes[key]
        if 'progress' in attributes:
            from runner.run_agent import validated_progress
            result['progress'] = validated_progress(attributes['progress'])
    return result


def restored_row(value):
    """Revalidate a prior same-attempt check before retaining its bounded history."""
    if not isinstance(value, dict) or not integer(value.get('sequence')) or value['sequence'] < 1:
        raise ValueError('invalid sequence')
    projected = row({'event_name': value.get('event_name'), 'run_id': value.get('native_run_id'),
                     'occurred_at': value.get('occurred_at'), 'attempt_id': value.get('attempt_id'),
                     'phase': value.get('phase'), 'outcome': value.get('outcome'), 'attributes': value})
    projected['sequence'] = value['sequence']
    if set(projected) != set(value):
        raise ValueError('unexpected public fields')
    return projected


def unavailable():
    try:
        print('{"event_name":"progress.unavailable","code":"PROGRESS_UNAVAILABLE"}', file=sys.stderr, flush=True)
    except OSError:
        pass


# Twelve active jobs produce at most 720 periodic updates/hour, leaving room
# in the repository GITHUB_TOKEN budget for registration/final publications.
PUBLISH_INTERVAL_SECONDS = 60


class ChecksProgress:
    def __init__(self, token, env, app_id):
        patterns = {'GITHUB_REPOSITORY': r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}',
                    'GITHUB_RUN_ID': r'[1-9][0-9]{0,19}', 'GITHUB_RUN_ATTEMPT': r'[1-9][0-9]{0,2}',
                    'GITHUB_SHA': r'[a-f0-9]{40}', 'APP': r'[a-z][a-z0-9-]{1,28}[a-z0-9]',
                    'TENANT': r'[a-z0-9]{1,20}', 'TARGET_ID': r'[a-z][a-z0-9-]{0,62}'}
        if (not isinstance(token, str) or not token or len(token) > 4096
                or any(not re.fullmatch(pattern, env.get(key, '')) for key, pattern in patterns.items())
                or env['APP'] != app_id or env['GITHUB_SHA'] != env.get('SOURCE_COMMIT')
                or int(env['GITHUB_RUN_ATTEMPT']) > 100):
            raise ValueError('invalid progress binding')
        self._token, self.repository = token, env['GITHUB_REPOSITORY']
        self.binding = {'version': 1, 'run_id': env['GITHUB_RUN_ID'], 'run_attempt': int(env['GITHUB_RUN_ATTEMPT']),
                        'source_commit': env['GITHUB_SHA'], 'app': env['APP'], 'tenant': env['TENANT'], 'target_id': env['TARGET_ID']}
        self.external_id = f"railshot-events:{self.binding['run_id']}:{self.binding['run_attempt']}"
        self.details_url = f"https://github.com/{self.repository}/actions/runs/{self.binding['run_id']}"
        self.check_id, self.native_run_id = None, None
        self.items, self.pending, self.sequence = [], [], 0
        self.next_at, self.backoff_until = 0, 0
        self.truncated, self.create_attempted, self.completed = False, False, False

    def request(self, method, path, body=None):
        connection = http.client.HTTPSConnection('api.github.com', timeout=2)
        try:
            connection.request(method, path, body=None if body is None else json.dumps(body).encode(),
                               headers={'Authorization': f'Bearer {self._token}', 'Accept': 'application/vnd.github+json',
                                        'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json',
                                        'User-Agent': 'railshot-progress'})
            response = connection.getresponse()
            if response.status in (403, 429):
                delay = response.getheader('Retry-After', '')
                delay = max(60, min(int(delay), 3600)) if delay.isdigit() else 60
                reset = response.getheader('X-RateLimit-Reset', '')
                if response.getheader('X-RateLimit-Remaining') == '0' and reset.isdigit():
                    delay = max(delay, int(reset) - time.time())
                self.backoff_until = time.monotonic() + delay
            # http.client never follows redirects. No response body or URL reaches logs.
            if response.status not in (200, 201):
                raise ValueError('progress transport unavailable')
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise ValueError('oversized response')
            return json.loads(data)
        finally:
            connection.close()

    def matches(self, check):
        return (type(check.get('id')) is int and check['id'] > 0 and check.get('name') == NAME
                and check.get('head_sha') == self.binding['source_commit'] and check.get('external_id') == self.external_id
                # GitHub Actions replaces details_url with this check's native URL.
                and check.get('details_url') in (self.details_url, f"https://github.com/{self.repository}/runs/{check['id']}")
                and check.get('app', {}).get('id') == APP_ID
                and check.get('app', {}).get('slug') == 'github-actions')

    def recover(self, check):
        if not self.matches(check):
            raise ValueError('check binding mismatch')
        text = check.get('output', {}).get('text')
        if not isinstance(text, str) or len(text.encode()) >= MAX_BYTES:
            raise ValueError('invalid check payload')
        value = json.loads(text)
        if (not isinstance(value, dict) or set(value) != set(self.binding) | {'updated_at', 'status', 'truncated', 'items'}
                or any(value.get(key) != expected for key, expected in self.binding.items())
                or type(value.get('version')) is not int or type(value.get('run_attempt')) is not int
                or type(value['truncated']) is not bool or value['status'] not in {'running', 'completed'}
                or not isinstance(value['items'], list) or len(value['items']) > MAX_ITEMS):
            raise ValueError('invalid check envelope')
        utc(value['updated_at'])
        items = [restored_row(item) for item in value['items']]
        if (any(item['native_run_id'] != self.native_run_id for item in items)
                or any(a['sequence'] >= b['sequence'] for a, b in zip(items, items[1:]))):
            raise ValueError('invalid check history')
        self.items, self.truncated = items, value['truncated']
        self.sequence = items[-1]['sequence'] if items else 0
        self.check_id, self.completed = check['id'], value['status'] == 'completed'

    def locate(self):
        found, observed, total = [], set(), None
        for page in range(1, 6):
            result = self.request('GET', f"/repos/{self.repository}/commits/{self.binding['source_commit']}/check-runs"
                                  f'?check_name={quote(NAME)}&filter=all&app_id={APP_ID}&per_page=20&page={page}')
            checks = result.get('check_runs')
            count = result.get('total_count')
            if (not integer(count) or count > 100 or total is not None and count != total
                    or not isinstance(checks, list) or len(checks) > 20):
                raise ValueError('invalid check list')
            total = count
            for check in checks:
                if not isinstance(check, dict) or not integer(check.get('id')) or check['id'] < 1 or check['id'] in observed:
                    raise ValueError('invalid check identity')
                observed.add(check['id'])
            found.extend(check for check in checks if check.get('external_id') == self.external_id)
            if len(observed) == total:
                break
            if len(checks) < 20 or len(observed) > total:
                raise ValueError('incomplete check list')
        else:
            raise ValueError('check lookup limit exceeded')
        if len(found) > 1:
            raise ValueError('ambiguous check')
        if found:
            self.recover(found[0])
        elif self.create_attempted:
            # A lost create response is uncertain: reconcile, never blindly POST again.
            raise ValueError('check creation remains uncertain')

    def payload(self):
        for item in self.pending:
            if item['event_name'] == 'loop.started' and self.items:
                continue
            self.sequence += 1
            self.items.append({**item, 'sequence': self.sequence})
        self.pending.clear()
        if len(self.items) > MAX_ITEMS:
            receipts = {r['attempt_id']: r for r in self.items if r['event_name'] == 'agent.repair'}
            keep = list(receipts.values())[-3:]
            self.items = sorted(keep + [r for r in self.items if r not in keep][-(MAX_ITEMS - len(keep)):], key=lambda r: r['sequence'])
            self.truncated = True
        while True:
            value = {**self.binding, 'updated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                     'status': 'completed' if self.completed else 'running', 'truncated': self.truncated, 'items': self.items}
            text = json.dumps(value, separators=(',', ':'), ensure_ascii=True)
            if len(text.encode()) < MAX_BYTES:
                return {'title': NAME, 'summary': 'Structured supervisor observations only; gate results remain authoritative.', 'text': text}
            if not self.items:
                raise ValueError('oversized progress binding')
            removable = next((i for i, r in enumerate(self.items) if r['event_name'] not in {'agent.repair', 'loop.completed'}), 0)
            self.items.pop(removable)
            self.truncated = True

    def emit(self, event, *, final=False):
        try:
            projected = row(event)
            if self.native_run_id is None:
                self.native_run_id = projected['native_run_id']
            if projected['native_run_id'] != self.native_run_id:
                raise ValueError('event binding mismatch')
            self.pending.append(projected)
            if len(self.pending) > MAX_ITEMS:
                self.pending = self.pending[-MAX_ITEMS:]
                self.truncated = True
            now = time.monotonic()
            if now < self.backoff_until:
                return
            if now < self.next_at and not final:
                return
            self.next_at = now + PUBLISH_INTERVAL_SECONDS
            if self.check_id is None:
                self.locate()
            self.completed = self.completed or final
            output = self.payload()
            payload = {'output': output, 'status': 'completed' if self.completed else 'in_progress'}
            if self.completed:
                payload.update(conclusion='neutral', completed_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
            if self.check_id is None:
                self.create_attempted = True
                result = self.request('POST', f'/repos/{self.repository}/check-runs',
                                      {**payload, 'name': NAME, 'head_sha': self.binding['source_commit'],
                                       'external_id': self.external_id, 'details_url': self.details_url})
                if not self.matches(result):
                    raise ValueError('created check binding mismatch')
                self.check_id = result['id']
            else:
                self.request('PATCH', f'/repos/{self.repository}/check-runs/{self.check_id}', payload)
        except Exception:
            self.next_at = max(time.monotonic() + PUBLISH_INTERVAL_SECONDS, self.backoff_until)
            unavailable()


def from_environment(token, app_id):
    if not token:
        return None
    try:
        return ChecksProgress(token, os.environ, app_id)
    except Exception:
        unavailable()
        return None
